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
dongle host (any machine)                 container (isolated netns)
┌───────────────────────────┐             ┌────────────────────────────┐
│ USB BT dongle             │             │ vhci-feeder → /dev/vhci    │
│  └ hci-agent              │  tunnel     │        └─ hciN             │
│     owns USB (libusb,     │─control-TCP─►            │              │
│     btusb detached)       │─media-UDP──►│   bluetoothd → PipeWire    │
│     relays HCI frames     │             │   → per-line worker        │
└───────────────────────────┘             └────────────────────────────┘
     paired phone nearby
```

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

## §2. Wire protocol (V1)

Two channels per line, both always dialing *into* the container's
per-line listener (NAT-safe, consistent with the provider model):

### Control channel — TCP

Length-prefixed frames: `uint8 kind · uint16 BE len · uint32 BE
ctrl_seq · payload`, where `len` counts `ctrl_seq + payload` (all
bytes after the `len` field itself). `ctrl_seq` is a per-session
monotonic counter assigned by each sender independently
(agent→container and container→agent sequences are separate).

| kind | direction | payload |
|---|---|---|
| `0x01` HCI_PASSTHRU | both | HCI packet prefixed with its H4 type byte (`0x01` cmd / `0x02` ACL / `0x04` evt); the byte is synthesized from the USB endpoint class |
| `0x10` HELLO | agent→ | `{line_id, token, agent_version, dongle:{adapter_mac, usb_path}, caps}` — `token` is the single-use enrollment credential from `agent_token_file`, sent inside the authenticated tunnel only |
| `0x11` HELLO_ACK | container→ | `{accepted, line_id, heartbeat_interval_ms, server_time}` or reject+close |
| `0x12` HEARTBEAT | agent→ | `{seq, usb_ok, hci_ok}` every `heartbeat_interval_ms` |
| `0x13` RESET | container→ | `{}` — agent power-cycles the dongle, then re-enumerates |
| `0x14` STATUS | agent→ | `{state: UP|DEGRADED|DONGLE_DEAD|TUNNEL_FLAP, detail}` |
| `0x15` SHUTDOWN | both | graceful close |
| `0x16` SCO_STATE | container→ | `{open: bool, packet_len}` — instructs the agent to enable/disable the USB isochronous path (round 19, see below) |

**USB SCO altsetting control (round 19).** The Bluetooth USB
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
  agent selects the working `altsetting` for the negotiated codec
  (mSBC / transparent 16-bit) on USB Interface 1, starts isoc IN+OUT
  transfers, and chops outgoing HCI SCO packets into
  `wMaxPacketSize`-sized isoc frames (`packet_len` mirrors the
  negotiated payload length).
- `open: false` (after disconnect): the agent stops isoc transfers
  and returns Interface 1 to `altsetting 0`, releasing the bandwidth.

This keeps the agent dumb about call state — it reacts to an
explicit command from the feeder that already knows the ground
truth.

Control traffic is small and latency-critical: it never shares a queue
with media. Missed N heartbeats → line `DEGRADED` → `DOWN`.

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
- No control frame is ever intentionally dropped; backpressure is the
  only flow control.
- Implementation note (CODING_HAZARDS rule 7): although `len` admits
  up to 65535 bytes, both ends MUST enforce
  `MAX_CTRL_FRAME_LEN = 4096` when parsing the TCP stream — a
  desynchronized stream must fail fast instead of letting the
  allocator chase a garbage length field (HCI ACL/event payloads are
  ≲1 KiB; HELLO/STATUS JSON fits easily).

### Media channel — UDP

`uint8 kind · uint16 BE media_seq · uint32 BE ts_ms · payload` —
one datagram per media frame. Kinds:

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
counted as `SCO_UNEXPECTED_HANDLE`-class telemetry.

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

### vhci feeder — SCO handle gate

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
                                   # per-line bootstrap credential,
                                   # sent once in HELLO (see §2)

dongle:
  match:
    adapter_mac: "A8:9C:ED:C9:E0:FD"   # primary selector — BDADDR
    usb_path: "1-1.4"                  # optional secondary selector —
                                       # physical USB port path
  detach_btusb: true                 # userspace ownership (recommended)
  reset:
    on_start: false
    on_watchdog: true                # power-cycle on DONGLE_DEAD
    command: "uhubctl -l 1-1 -p 4 -a cycle"   # host-specific, optional
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

**Headless pairing (round 19).** Pairing runs on `bluetoothd` INSIDE
the container, and modern phones use Secure Simple Pairing (numeric
comparison on both ends) — an operator cannot be expected to
`docker exec` into bluetoothctl per line. The container therefore
ships a managed BlueZ `org.bluez.Agent1` that REJECTS every pairing
attempt by default; `admin.pair_window {line_id, duration_sec}`
(default 60 s) sets `Pairable` on that line's adapter only and
auto-confirms the SSP passkey comparison for the window's duration.
The window, the adapter (by `adapter_mac`), and any discovered
devices are exposed through the admin surface (`phonebridgectl` /
admin MCP) — no shell access required.
(heartbeat/USB issues) → `FAULTED` (manual or watchdog teardown) →
back to `READY` on recovery. Container-side teardown of a line:
destroy the vhci adapter → bluetoothd emits `InterfacesRemoved` →
per-line worker FSM reaps cleanly — the same deterministic path for
every failure source, because a virtual adapter never wedges the kernel.

## §6. Security notes

- Bootstrap tokens are single-use enrollment credentials; after
  `HELLO_ACK` the tunnel credential (WG peer key or mTLS cert) is the
  line identity.
- WireGuard peer isolation (round 18): the container-side server
  config MUST pin each peer to `AllowedIPs = <peer>/32` and MUST NOT
  forward between peers — a compromised agent host must not be able
  to inject UDP into a sibling line's media port. The `allowed_ips`
  in the agent-side example above is the agent's route scope; the
  server's per-peer `/32` is the isolation boundary.
- PII hygiene: the agent must not log dialed numbers, caller IDs, or
  HCI payloads containing them (`mask_pii` default on).
- The agent binds to loopback/WAN only via the tunnel profile; it does
  not expose any local listener.
