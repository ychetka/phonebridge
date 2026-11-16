# hci-agent — dongle-side line agent

Status: design spec (2026-11-13, unified agent model). Not yet implemented.

Every phone line in phonebridge terminates at a **hci-agent**: a small
process that runs on the machine physically owning a Bluetooth USB
dongle — whether that machine is a remote Raspberry Pi, a mini-PC in
another city, or the very host where the gateway container runs. The
model is uniform: **the container never touches USB or the host
Bluetooth subsystem; every adapter it sees is a kernel `vhci` device
fed by an agent.** Location is invisible to the telephony stack.

```
dongle host (any machine)            container (--network=host, /dev/vhci)
┌───────────────────────────┐          ┌────────────────────────────┐
│ USB BT dongle             │          │ vhci-feeder → /dev/vhci    │
│  └ hci-agent              │  tunnel  │        └─ hciN             │
│     owns USB (libusb,     │─ctrl-TCP─►            │              │
│     btusb detached)       │─mediaUDP─►│   bluetoothd → PipeWire    │
│     relays HCI frames     │          │   → per-line worker        │
└───────────────────────────┘          └────────────────────────────┘
     paired phone nearby
```

(The container shares the host's `init_net` — `AF_BLUETOOTH` does not
exist inside an isolated network namespace; see
`docs/phone-bridge-service-design.md` §2.1.)

## §1. Responsibilities

The agent is deliberately *dumb* — it owns hardware and moves frames:

- **USB ownership** — binds the dongle in userspace via libusb/pyusb
  after detaching the kernel `btusb` driver, so no `hci0` is created on
  the agent host at all (zero interference with a host `bluetoothd`,
  which may keep serving the host's own devices). Base implementation:
  the USB transport of Google Bumble.
- **HCI relay** — forwards HCI frames between the dongle and the
  container: commands/ACL/events on the **control channel** (TCP), SCO
  audio on the **media channel** (UDP). USB carries each class on its
  own endpoint (control EP = commands, interrupt EP = events, bulk EP =
  ACL, isochronous EP = SCO), so the agent tags each frame with the H4
  type byte implied by its endpoint — no protocol interpretation
  beyond that mapping.
- **Liveness** — heartbeat over the control channel; probes the health
  of its own USB/HCI path and reports `DONGLE_DEAD` explicitly.
- **Local reset** — on command (and/or by policy), power-cycles the
  dongle's USB port (`uhubctl`/`usbreset`), re-attaches, resumes.

It does **not** run bluetoothd, does not pair phones itself, and holds
no telephony state — the container-side `bluetoothd` performs pairing
through the virtual adapter exactly as it would for a local dongle.

**Bundle audio-stack note (round 28):** inside the container,
WirePlumber is configured WITHOUT registering an HFP AG
`Profile1` handler — PipeWire Telephony (the oFono-compatible
surface behind `org.pipewire.Telephony`, design §9) is the SOLE
owner of the HFP Audio-Gateway role. WirePlumber's bluez5
monitoring is scoped by an adapter filter, or its telephony support
is disabled outright, so it can never double-register the AG and
race the Telephony plugin — consistent with the static
`node.autoconnect=false` routing rule of design §6.4.

## §2. Wire protocol (V1)

Two channels per line, both always dialing *into* the container's
per-line listener (NAT-safe, consistent with the provider model):

### Control channel — TCP

Length-prefixed frames: `uint8 kind · uint16 BE len · uint32 BE
ctrl_seq · payload`, where `len` counts `ctrl_seq + payload` (all
bytes after the `len` field itself). `len >= 4` always (round 28):
even a payload-less frame carries the 4-byte `ctrl_seq`, so a
declared `len` below 4 is a framing violation exactly like an
overlong one. `ctrl_seq` is a per-session
monotonic counter assigned by each sender independently
(agent→container and container→agent sequences are separate).

| kind | direction | payload |
|---|---|---|
| `0x01` HCI_PASSTHRU | both | HCI packet prefixed with its H4 type byte (`0x01` cmd / `0x02` ACL / `0x04` evt); the byte is synthesized from the USB endpoint class |
| `0x10` HELLO | agent→ | `{line_id, token, agent_version, dongle:{adapter_mac, usb_path}, caps}` — `token` is the **long-lived per-line pre-shared secret** from `agent_token_file` (round 23 fix — earlier drafts wrongly called it a single-use credential; it is re-presented on EVERY control session, sent inside the authenticated tunnel only). On WAN profiles the container validates `token` AND the tunnel peer identity together — WireGuard `/32` peer public key or mTLS client cert CN — both MUST map to the same `line_id`; either check failing rejects the session. On a co-located `127.0.0.1` endpoint the peer-identity factor does not exist (loopback is a one-host trust domain, round 28) — the `token` alone authenticates, and a UDS variant may substitute `SO_PEERCRED` (§6) |
| `0x11` HELLO_ACK | container→ | `{accepted, line_id, heartbeat_interval_ms, server_time, session_nonce}` or reject+close — `session_nonce` is a fresh `uint32` minted per control session (round 20); the agent MUST echo it in every media-UDP datagram of this session |
| `0x12` HEARTBEAT | agent→ | `{seq, usb_ok, hci_ok}` every `heartbeat_interval_ms` |
| `0x13` RESET | container→ | `{}` — agent power-cycles the dongle, then re-enumerates |
| `0x14` STATUS | agent→ | `{state: UP|DEGRADED|DONGLE_DEAD|TUNNEL_FLAP, detail}` |
| `0x15` SHUTDOWN | both | graceful close |
| `0x16` SCO_STATE | container→ | `{open: bool, handle?, air_mode?, rx_pkt_len?, tx_pkt_len?}` — instructs the agent to enable/disable the USB isochronous path (rounds 19-21, see below); on `open: true` `handle` carries the 12-bit synchronous connection handle (`uint16`, meaningful bits `0x0FFF`) that the feeder parsed from `Synchronous Connection Complete 0x2c`/`0x03` — REQUIRED on `open: true` (round 24) so the agent can form HCI SCO headers and underrun silence packets before the first UDP datagram ever arrives; `air_mode` is the negotiated encoding (`"cvsd"` \| `"msbc"`) DERIVED from the NUMERIC HCI `air_mode` field in the `0x2c` event — `0x02` = CVSD → `"cvsd"`, `0x03` = transparent (the encoding mSBC rides on eSCO/WBS) → `"msbc"`; the feeder MUST map the numeric code, never expect a string on the air interface (round 25); `rx_pkt_len`/`tx_pkt_len` are the negotiated air packet lengths; on `open: false` the trailing fields are OPTIONAL/absent (`null`) — no mixed-type `0` placeholders (round 21) |

**USB SCO altsetting control (rounds 19-21).** The Bluetooth USB
Transport spec keeps Interface 1 (SCO audio) at `altsetting 0` —
zero bandwidth (`wMaxPacketSize = 0`) — while idle; isochronous
transfers cannot flow until a working altsetting is selected, and a
lingering active altsetting reserves isoc bandwidth on the host
controller forever, even between calls. The kernel `vhci` transport
(H4) knows nothing about USB altsettings, so the container-side
feeder — which already parses `Synchronous Connection Complete 0x2c`
and `Disconnect Complete 0x05` for the SCO handle gate — sends the
agent `0x16 SCO_STATE`:

- `open: true` (after a successful sync connection complete): the
  agent selects the Interface 1 `bAlternateSetting` by `air_mode`
  per Bluetooth Core Spec Vol 4 Part B §2.1.1 (round 21 fix — the
  round-20 text wrongly looked up `wMaxPacketSize == rx_pkt_len`,
  which matches nothing). The altsettings have FIXED
  `wMaxPacketSize` values: alt 0 = `0` (idle), alt 1 = `9`,
  alt 2 = `17` (standard for 16-bit CVSD), alt 3 = `25`,
  alt 4 = `33`, alt 5 = `49`, alt 6 = `63` (standard for
  WBS/mSBC). The mapping (round 25 fix — the earlier "fallback to
  altsetting 1" was wrong: `9` B/ms cannot carry a 60 B mSBC
  payload): `air_mode == "msbc"` → `altsetting 6`
  (`wMaxPacketSize = 63`), falling back to `altsetting 3` (`25`)
  when the descriptor lacks alt 6 — the 60 B packet then rides
  ~3 consecutive isoc microframes — and to `SCO_ALT_UNSUPPORTED`
  (call rejected, telemetry event) when neither exists; never
  altsetting 1 for mSBC. `air_mode == "cvsd"` (16-bit) →
  `altsetting 2` (`17`), `altsetting 1` (`9`) only for 8-bit CVSD
  links. `rx_pkt_len`/`tx_pkt_len`
  carry the ON-AIR eSCO packet length (e.g. `60` B for 2-EV3
  mSBC, `30` B for HV3) — the round-20 `48/64/128`-byte CVSD sizes
  were the HCI buffer MTU, not altsetting packet sizes. The agent
  then starts isoc IN+OUT transfers and chops outgoing HCI SCO
  packets into `wMaxPacketSize`-sized isoc frames.
- `open: false` (after disconnect): the agent stops isoc transfers
  and returns Interface 1 to `altsetting 0`, releasing the bandwidth.

This keeps the agent dumb about call state — it reacts to an
explicit command from the feeder that already knows the ground
truth.

Control traffic is small and latency-critical: it never shares a queue
with media. Missed N heartbeats → line health `degraded` → `down` on
the liveness axis (`event.line_health_changed`; occupancy `LineState`
is orthogonal — round 25).

**Feeder→kernel liveness mapping (round 27).** The vhci-feeder and
the LineProvider are sibling processes of the same line bundle; the
feeder owns the control-channel ground truth and MUST forward it to
the LineProvider over their bundle-local IPC so the provider can
report it on the `provider.*` surface: a control-channel heartbeat
miss (per `heartbeat.interval_ms`/`missed_limit`) or tunnel death
is converted into the absence/`unreachable` side of the
`provider.ping` cadence — the kernel's `heartbeat_degraded_after`/
`heartbeat_down_after` ladder escalates it into
`event.line_health_changed`; an explicit `STATUS{DONGLE_DEAD}` or a
fatal `CTRL_DESYNC` is converted into a provider-reported
`dongle_lost`-class teardown fact (immediate, no orphan grace —
same consequence as `provider.call_state{reason:"dongle_lost"}`).
The two ladders stay independent: the agent-side `missed_limit`
detects transport death faster than the kernel ladder escalates it,
and the kernel ladder remains authoritative for health events.

**Sibling liveness (round 29, review-1 B9).** The mapping above
depends on the feeder↔provider IPC being alive — a dead feeder
that cannot report leaves the provider pinging `reachable` over a
dead tunnel. The line-bundle supervisor therefore watches BOTH
children and the IPC channel itself: feeder process death, or
loss of the bundle-local IPC, is surfaced by the LineProvider as
a `dongle_lost` teardown fact (immediate, no orphan grace — the
same consequence class as tunnel/USB death it feeds into). The
provider owns no independent "feeder is healthy" timer; supervisor
observation is the liveness contract.

**The control channel is strictly lossless.** HCI assumes event and
ACL delivery with zero loss; a single dropped event (e.g.
`Synchronous Connection Complete 0x2c`, `Disconnect Complete 0x05`,
`Command Complete 0x0e`) permanently desynchronizes kernel/controller
state and produces unrecoverable silent failures — this was observed
live in the vhci experiment. Therefore:

- Every control frame carries `ctrl_seq`; the receiver MUST detect
  gaps.
- A sequence gap, an unwritable channel that cannot drain within the
  configured deadline, or a control queue overflow is a **fatal
  session error**: emit `STATUS state=DEGRADED detail=CTRL_DESYNC`,
  close the session, and reset/re-attach the line adapter. Silent
  continuation is forbidden.
- A connection drop MID-PAYLOAD of a length-valid frame is the same
  class of transport failure (round 29, review-1 B13): the partial
  frame is discarded wholesale — no partially received HCI command
  or event is ever written to `/dev/vhci` or forwarded to the
  dongle — and the session dies via the same
  `STATUS{CTRL_DESYNC}`/disconnect path. "Half a frame" never
  reaches either side: the reassembled-unit boundary is the full
  length-prefixed frame.
- No control frame is ever intentionally dropped; backpressure is the
  only flow control.
- Implementation note (CODING_HAZARDS rule 7): although `len` admits
  up to 65535 bytes, both ends MUST enforce
  `MAX_CTRL_FRAME_LEN = 4096` when parsing the TCP stream — the cap
  applies to the FULL received frame INCLUDING the 3-byte
  `kind`/`len` header (round 28), so the declared `len` must
  satisfy `4 <= len <= MAX_CTRL_FRAME_LEN - 3` — a
  desynchronized stream must fail fast instead of letting the
  allocator chase a garbage length field (HCI ACL/event payloads are
  ≲1 KiB; HELLO/STATUS JSON fits easily).

### Media channel — UDP

`uint8 kind · uint32 BE session_nonce · uint16 BE media_seq ·
uint32 BE ts_ms · payload` — one datagram per media frame (round
20: `session_nonce` binds every datagram to the current control
session — a changed NAT port does not prove knowledge of it, so
stale datagrams from a pre-reset session can't be accepted; the
feeder ignores and telemetry-counts any datagram whose nonce does
not equal the nonce it minted in `HELLO_ACK`). Kinds:

- `0x03` SCO — one **complete HCI SCO packet** per datagram
  (`2 B handle/flags · 1 B payload_len · payload`).
- `0x04` MEDIA_PING — empty payload endpoint-binding/keepalive frame
  (round 18, see below). It consumes the shared `media_seq` counter
  like any other datagram so the feeder's gap telemetry reflects the
  real transport, not phantom gaps around pings. The media `kind`
  namespace is the outer datagram discriminator and is disjoint from
  the H4 type byte carried *inside* an SCO payload — numeric overlap
  (e.g. `0x04`) is coincidence, not ambiguity.

**Uplink endpoint binding (round 18).** UDP is connectionless: the
feeder learns the agent's source `(IP, port)` only from received
datagrams. Until the first packet arrives — or after a NAT/firewall
mapping expires between calls — the container has no return path for
uplink audio. The agent therefore MUST send a `MEDIA_PING` frame
immediately after `HELLO_ACK` and then every ~15 s while the session
is up; the feeder binds its uplink destination to the last
authenticated source address of the session's media port. A
`MEDIA_PING` from an address that fails the tunnel peer check
(WireGuard `/32` source or mTLS-bound identity) is ignored and
counted under the dedicated `SCO_PEER_MISMATCH` counter (round 21) —
peer-identity failures and `session_nonce` failures
(`SCO_SESSION_MISMATCH`) are tracked separately.

Stale frames MAY be
dropped; late audio is worse than missing audio (isochronous semantics
preserved on purpose — this is why media is not TCP: head-of-line
blocking would stall real-time audio). `media_seq` wraps at 2^16 and
lets the feeder count gaps/reorders for telemetry only.

**SCO framing is the agent's job.** Raw USB isochronous buffers are
not aligned to HCI packet boundaries (observed: corrupt packets with
connection handle `0` reaching the kernel). The agent stays dumb about
Bluetooth/HFP state, but it MUST be framing-aware at the isochronous
endpoint boundary:

- skip zero-length isoc descriptors and padding,
- reassemble each HCI SCO packet via its 3-byte header + declared
  payload length,
- emit only complete packets, never split a packet across datagrams
  (a lost datagram would permanently corrupt a reassembled stream),
- verify `payload_len` equals the actual payload size; malformed
  frames are counted, not forwarded.

**Uplink playout jitter buffer (round 23, ADR-B).** UDP media and USB
isochronous transfers run on different clocks: datagrams arrive with
network jitter, while the dongle drains the isoc endpoint on a fixed
air schedule — feeding it straight from the socket produces
underruns (clicks) and bursts. The agent therefore keeps a small
bounded playout buffer for uplink SCO (agent→phone direction) of
**30–45 ms, i.e. 3–6 eSCO packets**, in front of the USB isoc OUT
endpoint:

- on buffer underrun the agent emits a codec-correct silence frame —
  the mSBC silence frame for `air_mode == "msbc"`, or `0x55`-filled
  payload for `air_mode == "cvsd"` — so the air link never starves
  and the far side hears attenuated silence instead of garbage;
- **every packet the agent pushes to USB isoc OUT is a complete HCI
  SCO packet** with the 3-byte header `2 B handle/flags · 1 B
  payload_len` formed around the `handle` delivered in
  `0x16 SCO_STATE` (round 24) — transit UDP packets keep their
  header, generated silence packets get a fresh one built from the
  same handle;
- **continuous H2 sync counter (round 24).** In `air_mode ==
  "msbc"` each 60-byte eSCO payload starts with the 2-byte H2 sync
  header (`0x01`, then a cyclic 2-bit sequence byte from the cycle
  `0x08 -> 0x38 -> 0xC8 -> 0xF8`). The agent runs ONE monotonic
  counter at the playout-buffer egress and REWRITES the sequence
  byte of every outbound USB mSBC packet — generated silence and
  UDP transit packets alike — so the phone's DSP never observes an
  H2 phase jump at a silence↔voice boundary (a raw passthrough of
  the tunnel's counter after inserted silence would click or mute
  the PLC);
- on overflow the buffer drops the OLDEST packets (playout must stay
  near real time; holding stale audio only compounds the delay);
- the buffer depth and under/overflow counts feed the agent's
  `STATUS`/telemetry — a persistently underrunning buffer indicates
  tunnel quality problems, not an agent bug;
- each speech burst starts with a ~2-packet prefill (round 25):
  USB/dongle and tunnel clocks drift ±20..100 ppm, which the 30–45 ms
  buffer absorbs and re-syncs between bursts; no adaptive DSP
  resampling is implemented or planned for V1.

### vhci feeder — SCO handle gate and MTU alignment (round 25)

**Attach serialization (round 29, review-1 B4).** `/dev/vhci` is a
single device node that mints `hciN` indices globally in `init_net`
— two feeders attaching concurrently (parallel line-bundle starts
or restarts) can cross their line↔hciN mapping before either reads
its assigned index back. The feeder MUST therefore hold a per-host
exclusive lock — `flock` on a lock file such as
`/run/phonebridge/vhci.lock` — across the ENTIRE attach sequence:
`open("/dev/vhci")` through hci-index discovery. The lock file
lives under the same `/run/phonebridge/` state dir the provider
state file uses (design §5.4); it is held only for the attach
critical section, never for the feeder's lifetime.

**MTU alignment.** `hci_vhci` carries no `btusb` USB quirks: the
SCO buffer geometry a real dongle negotiates over USB descriptors
does not exist on a virtual adapter — behind `/dev/vhci` the feeder
IS the controller, so it declares the synchronous geometry itself:
it answers `HCI_Read_Buffer_Size` (and the synchronous-flow-control
queries) with `sco_data_packet_len = 60` — the mSBC profile —
LOCALLY, consuming the command at the feeder and NEVER forwarding it
to the physical dongle over the tunnel (round 28 — forwarding it
would produce TWO `0x0e` Command Complete events for one command:
one from the real controller and one from the feeder's answer, and
the kernel's `ncmd` accounting would desynchronize the command
flow-control window) — and
emits SCO packets of exactly that negotiated length
(1 mSBC frame per HCI packet), before any sync connection is
accepted. If the kernel side ever reports a different length (e.g.
a host bluetoothd SCO-buffer readback mismatch), the feeder refuses
sync setup and raises `SCO_ALT_UNSUPPORTED` — a mismatched length
would let the kernel slice SCO at a different boundary, so the
agent's byte-2 H2 sequence rewrite would corrupt mid-frame audio.

The split transport means SCO data can outrun the setup event that
created the synchronous connection. The container-side feeder
(vhci writer) therefore gates media on connection state learned
passively from the event stream it forwards:

- Open a gate for sync connection handle H when it forwards a
  `Synchronous Connection Complete` event `0x2c` (or legacy
  `Connection Complete` `0x03` reporting a SCO/eSCO link) with
  `status == 0x00`. The full 16-bit handle field is preserved when
  writing to `/dev/vhci`; comparisons mask with `0x0FFF`.
- SCO frames for an unknown handle are either dropped or held in a
  tiny ring of **≤ 4 frames per handle (≈30 ms)** — never more; the
  prototype's 200-frame hold would inject ~1.5 s of audio latency.
- Close the gate on a successful `Disconnect Complete` `0x05`
  **before** writing that event to the kernel, so trailing media can
  never reach a connection the kernel has already torn down.

### Media health events (feeder → line health)

| event | trigger |
|---|---|
| `SCO_UNEXPECTED_HANDLE` | frames for an unknown handle persist > ~250 ms or > 20 frames after connection setup — sync/event desync suspected |
| `SCO_RX_STARVATION` | gate open but zero valid SCO frames for > ~500 ms during an active call |
| `SCO_FRAMING_ERROR` | datagram shorter than the HCI SCO header, `payload_len` mismatch, or handle 0 / out of range |
| `SCO_PEER_MISMATCH` | datagram from a source address failing the tunnel peer check (WireGuard `/32` or mTLS-bound identity) — distinct from `SCO_SESSION_MISMATCH` (round 21) |
| `SCO_SESSION_MISMATCH` | datagram with a `session_nonce` other than the current HELLO_ACK-issued value — stale pre-reset session traffic (round 20) |

These are loud degradation signals feeding the line state machine —
the kernel-side symptom (`hci0: SCO packet for unknown connection
handle`, an hci-core printk emitted on the *drop* path, so adapter
`sco` RX counters alone cannot be trusted) must be turned into an
explicit health event.

### H4 type split

`0x01` command, `0x02` ACL, `0x04` event → control-TCP.
`0x03` SCO → media-UDP. On the USB side the class is implied by the
endpoint (no literal H4 byte exists on USB); the agent adds/strips it
when translating to the wire framing, and the container's `vhci`
adapter consumes the same typed frames.

## §3. Tunnel profiles (`transport_profile`)

The wire protocol above is **invariant**; how it reaches the container
is a per-line administrative choice (geo-blocking resilience). The
admin picks *which* profile; the *parameters* of each profile are
canonical, shipped by the system, and not user-editable.

| profile | carriage | status |
|---|---|---|
| `wireguard` | system-managed `wg0` peer; auth = peer pubkey ↔ line_id | V1 default |
| `direct_mtls` | plain TCP/UDP + mutual TLS; cert CN ↔ line_id | V1 (LAN/VPN-ready nets) |
| `sstp` | reserved profile — server pack shipped by system | future |
| `openconnect` | reserved profile | future |
| `vless_xray` | reserved profile | future |

Interface-creating profiles (wireguard, sstp, openconnect, vless_xray)
give the agent an L3 path; the two channels then run inside it
unmodified. `direct_mtls` wraps the TCP control channel in TLS and the
UDP media channel in DTLS. Line identity is always bound to the tunnel
credential — an agent cannot feed a line it was not provisioned for.

## §4. Agent configuration file

Single YAML file (suggested path `/etc/phonebridge-agent/agent.yaml`,
or passed as `--config`). One file per dongle; N dongles on one host =
N agent instances.

```yaml
# phonebridge hci-agent configuration — example
line_id: "line-kitchen"            # assigned at line onboarding (admin)
agent_token_file: "/etc/phonebridge-agent/agent.token"
                                   # long-lived per-line pre-shared
                                   # secret, sent in EVERY HELLO and
                                   # validated together with the
                                   # tunnel peer identity (see §2/§6)

dongle:
  match:
    adapter_mac: "A8:9C:ED:C9:E0:FD"   # primary selector — BDADDR
    usb_path: "1-1.4"                  # optional secondary selector —
                                       # physical USB port path
  detach_btusb: true                 # userspace ownership (recommended)
  reset:
    on_start: false
    on_watchdog: true                # power-cycle on DONGLE_DEAD
    command: ["uhubctl", "-l", "1-1", "-p", "4", "-a", "cycle"]
                                             # host-specific, optional;
                                             # execve-style argv list —
                                             # NEVER a shell string
                                             # (round 28: no shell =
                                             # no injection, no quoting
                                             # surprises)
                                             # (port 4 of hub 1-1 = usb_path 1-1.4)

transport:
  profile: "wireguard"             # see §3 — admin chooses WHICH;
                                   # canonical params below are
                                   # system-shipped, not user-tuned
  endpoint: "phonebridge.example.net"  # gateway address
  wireguard:
    private_key_file: "/etc/phonebridge-agent/wg0.key"
    peer_public_key: "<gateway pubkey>"   # provisioned at onboarding
    allowed_ips: ["10.99.0.0/24"]
    endpoint_port: 51820
    persistent_keepalive_s: 25
  # direct_mtls:
  #   ca_file: /etc/phonebridge-agent/ca.pem
  #   cert_file: /etc/phonebridge-agent/agent.pem
  #   key_file: /etc/phonebridge-agent/agent.key

channels:                          # container-side endpoint pair for
  control: { proto: tcp, port: 7300 }   # THIS line, issued at
  media:   { proto: udp, port: 7301 }   # onboarding — canonical
                                      # allocation: one pair per line
                                      # (7300/7301 shown for line 1)

heartbeat:
  interval_ms: 2000                # canonical default
  missed_limit: 3                  # → agent reports tunnel as dead
reconnect:
  backoff_ms: [500, 30000]
  jitter_pct: 20

logging:
  level: "info"
  mask_pii: true                   # never log dialed numbers / IDs
```

Field rules:

- `line_id` + the credential in `agent_token_file` bind the agent to
  exactly one line at the container; mismatch → `HELLO` rejected,
  connection closed.
- `dongle.match.adapter_mac` is REQUIRED (hciN-style indices are
  unstable; `usb_path` may *additionally* pin the physical port so a
  second identical dongle cannot be claimed by mistake).
- Everything under `transport.<profile>` except file paths and the
  endpoint address is canonical — the system ships per-profile config
  packs; admins do not tune protocol internals.

## §5. Lifecycle

`PROVISIONING` (admin created line + issued token/keys) → `PAIRING`
(agent connected, dongle attached, adapter materialized, phone pairing
via container bluetoothd) → `READY` → `IN_CALL` → `DEGRADED`
(heartbeat/USB issues) → `FAULTED` (manual or watchdog teardown) →
back to `READY` on recovery.

**Headless pairing (round 19).** Pairing runs on `bluetoothd` INSIDE
the container, and modern phones use Secure Simple Pairing (numeric
comparison on both ends) — an operator cannot be expected to
`docker exec` into bluetoothctl per line. The container therefore
ships a managed BlueZ `org.bluez.Agent1` that REJECTS every pairing
attempt by default; `admin.pair_window {line_id, duration_sec}`
(required, `0 < duration_sec <= 300` — `PairWindowRequest` rejects
out-of-range values, no clamp and no default, round 28) sets
`Pairable` on that line's adapter only and
auto-confirms the SSP passkey comparison for the window's duration.
The window, the adapter (by `adapter_mac`), and any discovered
devices are exposed through the admin surface (`phonebridgectl` /
admin MCP) — no shell access required. Container-side teardown of a line:
destroy the vhci adapter → bluetoothd emits `InterfacesRemoved` →
per-line worker FSM reaps cleanly — the same deterministic path for
every failure source, because a virtual adapter never wedges the kernel.

## §6. Security notes

- `agent.token` is a **long-lived per-line pre-shared secret**
  (round 23 — NOT a single-use enrollment credential as earlier
  drafts claimed: the agent has no second credential to rotate to,
  and the token must survive reboots, tunnel flaps and control
  session resets). It is sent in every `HELLO` inside the
  authenticated tunnel, and the container validates it TOGETHER
  with the tunnel peer identity (WireGuard `/32` peer public key,
  or the mTLS client cert CN for `direct_mtls`) — identity is the
  conjunction of BOTH factors bound to the same `line_id`, so a
  leaked token alone cannot feed a line from a foreign tunnel
  peer. Rotation is an admin operation (`admin` scope) performed
  through the keys DB, not a protocol feature.
- Feeder socket binding (round 23, capability matrix; co-located
  case round 27): the container-side `vhci-feeder` binds its
  control-TCP listener and media-UDP socket ONLY to the tunnel
  interface IP (`wg0` for the `wireguard` profile) — never to
  `0.0.0.0` — so the line surface is unreachable except through the
  authenticated tunnel. For a dongle physically co-located with the
  container (design §2.1 requires it to be served by a host-side
  hci-agent "exactly like a remote Pi"), the canonical tunnel
  endpoint is the loopback address `127.0.0.1`: the feeder binds
  its listener there and the host-local agent dials it — loopback
  merely replaces the tunnel interface IP. The AUTHENTICATION model
  differs though (round 28): loopback is a single trust domain —
  only processes already on that host can reach `127.0.0.1`, so
  there is no tunnel peer to authenticate against and the two-factor
  rule relaxes to the mandatory `agent.token` alone (the
  HELLO-token → `line_id` binding is still enforced); the peer-key
  / mTLS-CN second factor applies only to WAN profiles where a
  foreign host could hold a leaked token. If a co-located variant
  ever carries the control channel on a Unix socket instead of
  loopback TCP, `SO_PEERCRED` supplies the equivalent peer check —
  the same one-host trust domain, expressed as a uid credential
  rather than an address.
- WireGuard peer isolation (round 18): the container-side server
  config MUST pin each peer to `AllowedIPs = <peer>/32` and MUST NOT
  forward between peers — a compromised agent host must not be able
  to inject UDP into a sibling line's media port. The `allowed_ips`
  in the agent-side example above is the agent's route scope; the
  server's per-peer `/32` is the isolation boundary.
- PII hygiene: the agent must not log dialed numbers, caller IDs, or
  HCI payloads containing them (`mask_pii` default on).
- SSP auto-confirm caveat (round 19; MAC-scoped since round 27):
  `admin.pair_window` trades the numeric-comparison MITM check for
  operability — but ONLY toward the configured target: during the
  open window the managed `Agent1` auto-confirms SSP solely for the
  line's `paired_phone_mac`, and every other Bluetooth address —
  including a nearby hostile device — hits the same unconditional
  rejection as outside the window. Mitigation for the target itself
  is scope, not crypto: the window is short (default 60 s), bound
  to ONE adapter, requires an admin-scope token, and the operator
  is expected to hold the target phone at the prompt.
- The agent binds to loopback/WAN only via the tunnel profile; it does
  not expose any local listener.
