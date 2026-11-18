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

- **USB ownership** — binds the dongle in userspace via libusb
  after detaching the kernel `btusb` driver, so no `hci0` is created on
  the agent host at all (zero interference with a host `bluetoothd`,
  which may keep serving the host's own devices). Production baseline
  (issue #89, `docs/adr/0004-hci-agent-runtime.md`): a **Rust
  implementation on top of libusb** — Python/pyusb and the USB
  transport of Google Bumble are the protocol reference and optional
  development tooling only, never the production data path (a GC
  pause on the isochronous endpoint is a lost SCO microframe, and
  the Python bindings do not expose the per-packet isoc control the
  SCO path requires — see §1a). **Dongle matching (round 30,
  review-19 B2b#1):** `usb_path` is the PRIMARY stable selector —
  USB descriptors carry no `BD_ADDR` (`iSerialNumber` is empty or a
  chip serial, and `bdaddr` in sysfs vanishes with `btusb`
  detached), so identifying a dongle by MAC requires an INVASIVE
  claim sequence on every candidate device. The selector order is
  therefore: pin `dongle.match.usb_path` in config (a stable
  physical port path — survives reboots, unique per socket);
  `adapter_mac` is then VERIFIED post-claim — after the agent owns
  the interface it issues `HCI_Read_BD_ADDR` and the container-side
  feeder re-verifies the returned address against the line's
  configured `adapter_mac` (§2 vhci feeder). An
  `adapter_mac`-ONLY scan (no `usb_path`) is a legal but OPT-IN
  invasive mode: the agent must detach `btusb`, claim Interface 0
  and send `HCI_Reset` + `HCI_Read_BD_ADDR` on EVERY candidate BT
  USB device — including the host's own adapter — resetting live
  controllers and possibly reading firmware stub MACs on
  Realtek/Broadcom/Intel chips that answer before their firmware
  loads. It exists for onboarding/discovery (`agent scan` command),
  never as the default runtime mode (§4).
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

### §1a. Implementation runtime — Rust + libusb (issue #89, ADR-0004)

The production agent is a **Rust process on top of libusb**
(`docs/adr/0004-hci-agent-runtime.md`). The runtime shape the
wire contract above assumes:

- a **dedicated USB isochronous pump thread** owns every isoc
  transfer — it never shares an executor or event loop with the
  tunnel tasks, because USB service windows and network pacing are
  different clocks with different deadlines;
- bounded queues and pooled buffers sit between the pump and the
  tunnel I/O — no unbounded allocation and no GC anywhere on the
  data path;
- the safe `rusb` API covers enumeration, descriptors,
  configuration/altsetting selection and the
  control/interrupt/bulk endpoints; a narrow audited `IsoPump`
  module is the ONLY place `rusb::ffi`/`libusb1-sys` (and `unsafe`)
  may appear — isochronous submission, per-packet descriptors,
  per-packet `status`/`actual_length`, completion callbacks and
  the transfer pool;
- malformed isoc input fails closed (counted + dropped, never
  forwarded), and shutdown cancels/drains in-flight transfers in a
  bounded window before releasing the claim and restoring `btusb`;
- acceptance is gated on real hardware (x86_64 AND ARM):
  `usb_path` enumeration + VID/PID/serial/MAC verification,
  dongle-scoped `btusb` detach, interfaces 0/1 claim, `HCI Reset` →
  Command Complete, SCO altsetting selection, continuous isoc RX,
  paced outbound SCO, complete-record preservation, safe
  cancel/drain, `btusb` restore, and measured CPU/jitter/lost
  frames — the full gate is enumerated in ADR-0004.

## §2. Wire protocol (V1)

Two channels per line, both always dialing *into* the container's
per-line listener (NAT-safe, consistent with the provider model):

### Control channel — TCP

Length-prefixed frames: `uint8 kind · uint16 BE len · uint32 BE
ctrl_seq · payload`, where `len` counts `ctrl_seq + payload` (all
bytes after the `len` field itself). `len >= 4` always (round 28):
even a payload-less frame carries the 4-byte `ctrl_seq`, so a
declared `len` below 4 is a framing violation exactly like an
overlong one — a frame with `len == 4` is the canonical
empty-payload shape: `{kind, no JSON body}` (round 30 — `0x13
RESET`/`0x15 SHUTDOWN`/any ack-only frame uses exactly this
encoding; a zero-length JSON document is never emitted). `ctrl_seq`
is a per-session
monotonic counter assigned by each sender independently
(agent→container and container→agent sequences are separate):
each direction starts at `0` on the first frame of the session
and wraps mod `2^32` (round 30) — the receiver detects gaps
under the wrap-aware serial comparison, and a brand-new session
legitimately re-uses sequence space because the transport is the
boundary. Structured payloads are JSON documents UTF-8-encoded
inside `payload` (round 30 — `0x01 HCI_PASSTHRU` is the only
binary payload kind; `server_time` fields are RFC 3339 UTC, and
`caps` inside `0x10 HELLO` is a bounded `{dial_out?:bool,
receive_inbound?:bool}` object, never a free-form list).

| kind | direction | payload |
|---|---|---|
| `0x01` HCI_PASSTHRU | both | HCI packet prefixed with its H4 type byte (`0x01` cmd / `0x02` ACL / `0x04` evt); the byte is synthesized from the USB endpoint class |
| `0x10` HELLO | agent→ | `{line_id, token, agent_version, dongle:{adapter_mac, usb_path}, caps}` — `token` is the **long-lived per-line pre-shared secret** from `agent_token_file` (round 23 fix — earlier drafts wrongly called it a single-use credential; it is re-presented on EVERY control session, sent inside the authenticated tunnel only). On WAN profiles the container validates `token` AND the tunnel peer identity together — WireGuard `/32` peer public key or mTLS client cert CN — both MUST map to the same `line_id`; either check failing rejects the session. On the co-located `loopback` profile the peer-identity factor is `SO_PEERCRED` (round 31, swe-2 item 32 — the earlier "token alone on `127.0.0.1`, UDS may substitute" wording is removed): `token` AND the connecting uid are BOTH REQUIRED and both MUST map to the same `line_id` — there is no token-only loopback variant (§3/§6) |
| `0x11` HELLO_ACK | container→ | `{accepted:true, line_id, heartbeat_interval_ms, server_time, session_nonce}` or reject+close — `session_nonce` is a fresh `uint32` minted per control session (round 20); the agent MUST echo it in every media-channel frame of this session (UDP datagram on tunnel profiles, a length-prefixed record on the connected SOCK_STREAM UDS of `loopback`). Rejection (round 30): `{accepted:false, reason}` (typed reason string; `session_nonce`/`server_time` ABSENT — no half-issued session state) followed by an immediate FIN — a rejected session is never left half-open. `heartbeat_interval_ms` in this frame is AUTHORITATIVE for the session (round 30 — `agent.yaml`'s `heartbeat.interval_ms` is the FALLBACK used only until the first `HELLO_ACK`; the kernel's per-line negotiated value wins every subsequent session, so `admin.configure_line` cadence changes propagate through the next re-handshake without editing every agent's file) |
| `0x12` HEARTBEAT | agent→ | `{seq, usb_ok, hci_ok}` every `heartbeat_interval_ms` — `hci_ok` reports the LOCAL health of the relay path (USB transfer engine alive, isoc state consistent), never a synthesized HCI command result: the dumb agent MUST NOT probe the controller with HCI commands of its own — every probe would consume the real controller's `ncmd` window and desync the kernel's command accounting (round 30) |
| `0x17` HEARTBEAT_ACK | container→ | `{seq}` — echoes the received `HEARTBEAT.seq` (round 30, review-19 B2b#7): the bidirectional half of liveness — the agent counts missing ACKs against the SAME `missed_limit` the feeder applies to missed heartbeats, so a dead tunnel is detected in ~3 intervals ON BOTH SIDES. Without it the agent could only write into a dead TCP send buffer until `tcp_retries2` (~15 min) noticed, while a read-deadline on an idle-but-healthy line would tear down a perfectly good session — the ACK is what makes `missed_limit` meaningful agent-side |
| `0x13` RESET | container→ | `{}` — agent power-cycles the dongle, then re-enumerates |
| `0x14` STATUS | agent→ | `{state: UP|LINK_DEGRADED|DONGLE_DEAD|TUNNEL_FLAP, detail}` — strictly agent→container (round 30): the container never emits STATUS toward the agent — container-side desync/signalling is expressed by CLOSING the control session (the agent re-handshakes), keeping one frame direction per responsibility; `state` vocabulary renamed `DEGRADED`→`LINK_DEGRADED` (round 30 — disambiguated from the kernel-side liveness rung of the same name) |
| `0x15` SHUTDOWN | both | graceful close |
| `0x16` SCO_STATE | container→ | `{open: bool, handle?, air_mode?, rx_pkt_len?, tx_pkt_len?}` — instructs the agent to enable/disable the USB isochronous path (rounds 19-21, see below); on `open: true` `handle` carries the 12-bit synchronous connection handle (`uint16`, meaningful bits `0x0FFF`) that the feeder parsed from `Synchronous Connection Complete 0x2c` — `0x2c` ONLY, never legacy `0x03` (round 30): the extended event carries `air_mode`/`rx_pkt_len`/`tx_pkt_len` the plain Connection Complete lacks, so deriving SCO geometry from `0x03` would be guesswork — REQUIRED on `open: true` (round 24) so the agent can form HCI SCO headers and underrun silence packets before the first UDP datagram ever arrives; `air_mode` is the negotiated encoding (`"cvsd"` \| `"msbc"`) DERIVED from the NUMERIC HCI `air_mode` field in the `0x2c` event — `0x02` = CVSD → `"cvsd"`, `0x03` = transparent (the encoding mSBC rides on eSCO/WBS) → `"msbc"`; the feeder MUST map the numeric code, never expect a string on the air interface (round 25); `rx_pkt_len`/`tx_pkt_len` are the negotiated air packet lengths; on `open: false` the trailing fields are OPTIONAL/absent (`null`) — no mixed-type `0` placeholders (round 21) |

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
  when neither exists; never
  altsetting 1 for mSBC. `air_mode == "cvsd"` (16-bit) →
  `altsetting 2` (`17`), `altsetting 1` (`9`) only for 8-bit CVSD
  links. **`sco_data_packet_len` follows the REAL altsetting
  (round 30, review-19 B2b + swe-2):** the geometry the feeder
  declares to the kernel — its LOCAL answer to EVERY kernel
  `HCI_Read_Buffer_Size` (MTU alignment below, round 32) — is not a fixed
  `60` — it is the HCI SCO payload length the selected
  altsetting actually produces on the wire: `48` B under
  `altsetting 2` (CVSD), `60` B under `altsetting 6` (mSBC),
  `60` B under `altsetting 3` (the mSBC fallback — the payload
  stays 60 B; the 63 B HCI SCO packet, 3-byte header included,
  is chopped across ~3 consecutive 25 B `wMaxPacketSize`
  microframes — round 31 fix, the earlier `72` B figure counted
  framing the payload never carries). Hard-coding a single `60`
  would let the kernel slice
  inbound 48 B packets at a wrong boundary and corrupt the
  mSBC frame alignment the H2 rewrite depends on; the feeder
  stamps the declared length per negotiated profile, and the
  agent chops outgoing HCI SCO packets into the REAL
  `wMaxPacketSize`-sized isoc frames of the selected
  altsetting. **`SCO_ALT_UNSUPPORTED` is a deliverable signal
  (round 30; ownership pinned round 33):** the FEEDER is the
  stateful owner of every sync-setup failure — it is the only
  component holding both the negotiated air profile and the
  descriptor geometry, so `SCO_ALT_UNSUPPORTED` resolves on ITS
  side as a sync-setup refusal (pre-`Synchronous Connection
  Complete` or failed geometry readback): the state transition is
  sync-setup → refused → Interface 1 back to `altsetting 0` with
  isoc transfers cancelled, and the fact reaches the LineProvider
  through the bundle IPC as a `provider.*` teardown/dial-refusal
  fact, never a silent reject. When the AGENT is the side that
  cannot select an altsetting its obligation is bounded: it
  reports `STATUS{LINK_DEGRADED,
  detail:"sco_alt_unsupported"}` within one heartbeat interval of
  giving up selection (never waits for the next periodic
  heartbeat), leaves Interface 1 at `altsetting 0`, and the
  feeder owns the teardown from there — a dead control session
  suppresses the STATUS delivery but the altsetting-0 default
  below still applies. **Timeout (round 33):** altsetting
  selection is not open-ended — a descriptor scan completing
  without a usable altsetting reports immediately, and a
  successful `open:true` followed by no valid SCO traffic is
  already bounded by the `SCO_RX_STARVATION` gate (~500 ms
  during an active call); no third "selecting forever" state
  exists. The signal joins the media-health event
  vocabulary below — an undeliverable signal would strand the
  sync connection half-open.
  `rx_pkt_len`/`tx_pkt_len`
  carry the ON-AIR eSCO packet length (e.g. `60` B for 2-EV3
  mSBC, `30` B for HV3) — the round-20 `48/64/128`-byte CVSD sizes
  were the HCI buffer MTU, not altsetting packet sizes. The agent
  then starts isoc IN+OUT transfers and chops outgoing HCI SCO
  packets into `wMaxPacketSize`-sized isoc frames.
- `open: false` (after disconnect): the agent stops isoc transfers
  and returns Interface 1 to `altsetting 0`, releasing the
  bandwidth. **Altsetting 0 is the failure default too (round
  30):** the agent MUST also return Interface 1 to `altsetting 0`
  and cancel isoc transfers on control-session death, watchdog
  teardown, `CTRL_DESYNC`, or a `STATUS{DONGLE_DEAD}` path — a
  tunnel drop never reaches a `SCO_STATE{open:false}` command, so
  without the default the isoc pipeline would keep replaying
  generated silence into the phone forever while holding host
  USB bandwidth hostage; release is unconditional on ANY path
  that ends the media pipeline.
- **SCO_STATE reissue on media reconnect (round 34, iteration-2
  audit item 19):** a clean media-channel reconnect is a NEW
  pipeline — the container MUST re-issue the CURRENT
  `SCO_STATE{open:true}` (same `handle`/`air_mode`/packet lengths
  the still-live SCO link negotiated) on every media
  (re)establishment; the agent MUST NOT assume its isoc
  transfers survived the channel drop. Symmetrically, if the
  SCO link itself closed while the channel was down, the
  re-issued state is `SCO_STATE{open:false}` — the reconnect
  handshake always re-declares the link truth, and the
  kernel-side geometry stays fixed per the `0x1005` rules above
  (a reopen reselects the same line profile, it never
  re-negotiates `sco_mtu`).

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
- **Session establishment rules (round 30, review-19 B2b#15):**
  the feeder applies a bounded HELLO read timeout (~5 s) after
  `accept()` — a socket that never presents `0x10` is closed; a
  NEW `0x10 HELLO` preempts a still-lingering half-open session
  for the same `line_id` (the old socket is closed in the same
  step — a restarted agent must never be locked out by its own
  zombie). `/dev/vhci` materialization happens ONLY after a
  validated HELLO (token + peer identity + `usb_path`/`adapter_mac`
  binding) — a bare TCP connect must never mint a kernel adapter.
  Write-side queues are bounded on BOTH directions: per-session
  outbound control queue ≤64 frames with a bounded drain deadline
  (~2 s); exceeding either is the same `CTRL_DESYNC` fatal class —
  and the `STATUS{CTRL_DESYNC}` notice is sent BEST-EFFORT on a
  still-writable socket only: when the failure is an unwritable or
  overflowing channel the notice is skipped (writing into the
  broken stream would corrupt the length framing a reconnect would
  mis-parse), close alone signals the state.
- Implementation note (CODING_HAZARDS rule 7): although `len` admits
  up to 65535 bytes, both ends MUST enforce
  `MAX_CTRL_FRAME_LEN = 4096` when parsing the TCP stream — the cap
  applies to the FULL received frame INCLUDING the 3-byte
  `kind`/`len` header (round 28), so the declared `len` must
  satisfy `4 <= len <= MAX_CTRL_FRAME_LEN - 3` — a
  desynchronized stream must fail fast instead of letting the
  allocator chase a garbage length field (HCI ACL/event payloads are
  ≲1 KiB; HELLO/STATUS JSON fits easily).

### Media channel — frames (UDP datagrams on tunnel profiles, connected SOCK_STREAM UDS on `loopback`)

The media channel carries one self-contained media frame per
record; the transport is profile-dependent (§3): UDP inside the
tunnel profiles, a CONNECTED `SOCK_STREAM` Unix socket at
`/run/phonebridge/media/<line_id>.sock` on `loopback` (round 32,
SA_swe2_review_2 item 5 — the earlier UDS-datagram design is
removed: a `SOCK_DGRAM` UDS has no `connect()`-time peer
authentication and its `EDESTADDRREQ`-driven rebind semantics
duplicated the nonce gate the control session already owns). On
`loopback` the feeder LISTENS and the agent CONNECTS — the
media peer is verified at `accept()` by the same `SO_PEERCRED`
uid ↔ `line_id` check the control socket applies, so the socket
holds an authenticated full-duplex stream, not a bound datagram
pair. The record layout below is IDENTICAL on every transport,
with ONE stream addition: a `SOCK_STREAM` has no packet
boundaries, so on `loopback` each record is prefixed with
`uint16 BE rec_len` counting the bytes that follow it
(`kind·nonce·seq·ts·payload`), and the parser bounds
`rec_len` before reading — a desynchronized stream must fail
fast exactly like the control channel's `MAX_CTRL_FRAME_LEN`
rule. **Hard bound and fail-closed recovery (round 33):** the
cap is `MAX_MEDIA_FRAME_LEN = 4096` — the same 4 KiB ceiling as
`MAX_CTRL_FRAME_LEN`, generous against a real record (≤ ~300 B:
11 B header + one HCI SCO packet ≤ 258 B) — and `rec_len`
outside `[11, MAX_MEDIA_FRAME_LEN]` declares the stream
DESYNCHRONIZED: there is no resync scan on a stream socket (a
misaligned byte offset can fake a valid header at any offset),
so the receiver counts the framing error, closes the media
socket, and lets the agent's reconnect path re-authenticate a
fresh stream — the malformed connection is NEVER kept alive on
the hope the next record parses. On reconnect BOTH sides reset
the media-channel state wholesale: `media_seq` freshness,
the jitter-buffer occupancy/watermark edges, and the isoc
plumbing restart from the new session's `HELLO_ACK` — no
buffered frames, sequence expectations, or watermark state may
cross a socket boundary. An EOF (orderly FIN) on the stream is
the same teardown path — a half-closed media socket is not a
recoverable record state.

`uint8 kind · uint32 BE session_nonce · uint16 BE media_seq ·
uint32 BE ts_ms · payload` — one record per media frame (round
20: `session_nonce` binds every frame to the current control
session — a changed NAT port does not prove knowledge of it, so
stale frames from a pre-reset session can't be accepted; the
feeder ignores and telemetry-counts any frame whose nonce does
not equal the nonce it minted in `HELLO_ACK`). **`ts_ms` clock
domain (round 30):** the stamp is `CLOCK_MONOTONIC` milliseconds
relative to the SESSION's `HELLO_ACK` instant on the sender's own
clock — never `CLOCK_REALTIME` and never cross-host wall time, so
NAT rebinding and the two machines' unsynchronized clocks can
never make a valid frame look stale (staleness comparisons are
same-session, same-clock only); the container-side `server_time`
in `HELLO_ACK` is informational, not the epoch. The agent applies
the SYMMETRIC check on container→agent frames: a valid
`session_nonce` AND the bound uplink source identity are both
required before a frame reaches the USB isoc path — the
nonce is the session credential in both directions (round 30;
on `loopback` the `accept()`-time `SO_PEERCRED` check is the
source identity — a connected stream has no per-record source
address to verify).
Kinds:

- `0x03` SCO — one **complete HCI SCO packet** per record
  (`2 B handle/flags · 1 B payload_len · payload`).
- `0x04` MEDIA_PING — empty payload endpoint-binding/keepalive frame
  (round 18, see below). It consumes the shared `media_seq` counter
  like any other record so the feeder's gap telemetry reflects the
  real transport, not phantom gaps around pings. The media `kind`
  namespace is the outer record discriminator and is disjoint from
  the H4 type byte carried *inside* an SCO payload — numeric overlap
  (e.g. `0x04`) is coincidence, not ambiguity.

**Uplink endpoint binding (round 18).** UDP is connectionless: the
feeder learns the agent's source `(IP, port)` only from received
datagrams. On `loopback` the media channel is a CONNECTED
`SOCK_STREAM` pair — the feeder accepts on
`/run/phonebridge/media/<line_id>.sock` and the endpoint is bound
and `SO_PEERCRED`-verified at `accept()`, so there is no address
learning, no rebind window and no `MEDIA_PING`-driven return-path
flip to guard (§3) — the `(IP, port)` learning path below
applies to the UDP profiles only. Until the first packet arrives — or after a NAT/firewall
mapping expires between calls — the container has no return path for
uplink audio. The agent therefore MUST send a `MEDIA_PING` frame
immediately after `HELLO_ACK` and then every ~15 s while the session
is up; the feeder binds its uplink destination to the last
authenticated source address of the session's media port — with a
freshness gate (round 30): a rebind to a NEW `(IP, port)` is
accepted only when the authenticating datagram carries a strictly
increasing `media_seq` (wrap-aware comparison) — a delayed
datagram from the OLD mapping must never flip the return path
back to a dead NAT entry and mute uplink for the ~15 s until the
next `MEDIA_PING`. A
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
- emit only complete packets, never split a packet across media
  records (a lost UDP datagram would permanently corrupt a
  reassembled stream; on `loopback` the `rec_len` record boundary
  is the equivalent atomic unit),
- verify `payload_len` equals the actual payload size; malformed
  frames are counted, not forwarded.

**Isochronous pacing is the agent's job too (round 30, swe-2).**
USB isochronous endpoints provide NO backpressure — submit URBs
faster than the negotiated air rate and the host controller
silently drops or errors them. The agent therefore paces URB
submission to the eSCO service interval implied by the selected
altsetting (one transfer window per `wMaxPacketSize` microframe
batch), keeps a small fixed number of URBs in flight, and
aggregates microframes ONLY up to the host's isochronous bus
budget for the transfer — never batching past what the selected
altsetting's microframe period can drain. On a lost/short isoc
microframe the agent re-syncs at the NEXT HCI SCO packet
boundary (a partial microframe tail is discarded, never padded
into a misaligned packet — the 3-byte-header reassembly above is
the resync point).

**Uplink playout jitter buffer (round 23, ADR-B; drift rule added
round 30).** UDP media and USB
isochronous transfers run on different clocks: datagrams arrive with
network jitter, while the dongle drains the isoc endpoint on a fixed
air schedule — feeding it straight from the socket produces
underruns (clicks) and bursts. The agent therefore keeps a small
bounded playout buffer for uplink SCO (agent→phone direction) of
**30–45 ms, i.e. 3–6 eSCO packets**, in front of the USB isoc OUT
endpoint — and because the kernel emits a CONTINUOUS SCO stream
(one packet every ~7.5 ms for the whole call, no silence
suppression between "bursts"), clock drift never resets: the
buffer MUST compensate rather than hoping a gap re-centres it
(round 30, swe-2):

- on buffer underrun the agent emits a codec-correct silence frame —
  a VALID 60 B mSBC silence frame (proper `0xAD` sync word and
  `CRC-8` — a zeroed buffer is a decoder error on the phone, not
  silence) for `air_mode == "msbc"`, or a `0x0000`-filled linear
  PCM payload when the HCI SCO stream carries linear-PCM-over-HCI
  CVSD (the common case: HCI SCO payloads are decoded PCM);
  `0x55`-fill is reserved for raw CVSD air passthrough where the
  payload IS undecoded CVSD data — so the air link never starves
  and the far side hears attenuated silence instead of garbage;
- drift compensation policy: sustained producer-faster drift
  (overflow pressure) drops WHOLE packets at the queue edge —
  never trims mid-frame; sustained producer-slower drift
  (underrun pressure) duplicates or inserts silence frames
  through the same egress path so the H2 sequence stays
  continuous; the drop/dup watermark sits at the buffer edges so
  ordinary jitter never engages it;
- **every packet the agent pushes to USB isoc OUT is a complete HCI
  SCO packet** with the 3-byte header `2 B handle/flags · 1 B
  payload_len` formed around the `handle` delivered in
  `0x16 SCO_STATE` (round 24) — transit UDP packets keep their
  header, generated silence packets get a fresh one built from the
  same handle;
- **continuous H2 sync counter (round 24; guarded round 30).** In
  `air_mode == "msbc"` each 60-byte eSCO payload starts with the
  2-byte H2 sync header (`0x01`, then a cyclic 2-bit sequence byte
  from the cycle `0x08 -> 0x38 -> 0xC8 -> 0xF8`). The agent runs
  ONE monotonic counter at the playout-buffer egress and REWRITES
  the sequence byte of every outbound USB mSBC packet — generated
  silence and UDP transit packets alike — so the phone's DSP never
  observes an
  H2 phase jump at a silence↔voice boundary (a raw passthrough of
  the tunnel's counter after inserted silence would click or mute
  the PLC). The rewrite is applied ONLY to packets whose
  `payload[0] == 0x01` (round 30): any other transparent-codec
  payload riding `air_mode 0x03` (LC3-SWB and friends) does NOT
  carry an H2 word, and an unconditional byte-2 rewrite would
  corrupt live audio — a non-matching packet is passed through
  verbatim and counted as `SCO_FRAMING_ERROR`-adjacent telemetry;
- on overflow the buffer drops the OLDEST packets (playout must stay
  near real time; holding stale audio only compounds the delay);
- the buffer depth and under/overflow counts feed the agent's
  `STATUS`/telemetry — a persistently underrunning buffer indicates
  tunnel quality problems, not an agent bug;
- start-of-call prefill is ~2 packets (round 25) so the jitter
  window opens ahead of the first frame; USB/dongle and tunnel
  clocks drift ±20..100 ppm, absorbed by the 30–45 ms buffer and
  corrected continuously by the drop/dup drift rule above — there
  is no burst-gap resync to rely on (the SCO stream is
  continuous); no adaptive DSP resampling is implemented or
  planned for V1.

**Feeder-side ingress playout — the downlink twin (issue #89).**
The uplink buffer above lives on the AGENT and paces frames into
the USB isoc OUT endpoint; the symmetric problem exists on the
CONTAINER side for the phone→agent direction: on the UDP
transport profiles, agent-originated SCO records arrive at the
`vhci-feeder` with network jitter and reordering, while
`/dev/vhci` expects a continuous SCO stream — feeding it straight
from the socket would turn WAN jitter directly into kernel-side
clicks and underruns. The feeder therefore keeps a bounded
**reorder/playout buffer on its media ingress, in front of
`/dev/vhci`**, that is independent of the agent-side buffer:

- depth is bounded at ~**30–45 ms** of SCO audio (the same order
  of magnitude as the agent's buffer — a different value is legal
  only with a documented justification, never an unbounded one);
- `media_seq` (wrap-aware) drives **dedup and gap accounting**:
  duplicates are dropped, small reorders are sorted inside the
  window, and a gap that is still open when the playout cursor
  reaches it is filled with **codec-correct PLC/silence** — a
  valid mSBC silence frame for `air_mode == "msbc"`, or the
  matching CVSD/PCM silence for CVSD links — so the kernel never
  sees a stall and the container-side decoder/downlink path never
  sees an H2 discontinuity (these are phone→agent downlink frames;
  the phone's DSP produced them, it does not consume the stream);
- **fail-closed on malformed records:** a record that fails the
  nonce/length/payload checks (bad `session_nonce`,
  `rec_len`/`payload_len` mismatch, truncated HCI SCO header)
  is counted and dropped — it must never reach
  `write("/dev/vhci")`, consistent with the frame-validation
  rules below;
- underflow, overflow and reorder events are counted and reported
  through the media-health telemetry vocabulary below (feeding
  the same `SCO_*` health surface as the handle gate);
- on `loopback` the SOCK_STREAM media channel cannot reorder or
  jitter at the transport layer, but the same buffer code path
  applies uniformly — the alternative (two ingress pipelines) is
  a drift source, not a simplification.

### vhci feeder — SCO handle gate and MTU alignment (round 25)

**Attach serialization (round 29, review-1 B4; hardened round
30).** `/dev/vhci` is a
single device node that mints `hciN` indices globally in `init_net`
— two feeders attaching concurrently (parallel line-bundle starts
or restarts) can cross their line↔hciN mapping before either reads
its assigned index back. The feeder MUST therefore hold a per-host
exclusive lock — `flock` on a lock file such as
`/run/phonebridge/vhci.lock` — across the ENTIRE attach sequence:
`open("/dev/vhci")` through hci-index discovery. The lock file
lives under the same `/run/phonebridge/` state dir the provider
state file uses (design §5.4) — and that dir MUST be a
host-shared mount (design §2.1, round 30): a container-private
`/run` cannot serialize a host-global `/dev/vhci` against a dying
previous container instance or a host-side consumer, so the lock
path is either a bind-mounted host dir or a host-side lock
service; `phonebridge doctor` fails fast otherwise. The critical
section is bounded every way it can stall (round 30): the
`flock` wait carries a DEADLINE (~10 s — a wedged predecessor
must not block every other line's attach forever; timeout is a
loud failure, not a retry loop), the lock fd is opened
`O_CLOEXEC`/`FD_CLOEXEC` (a `vhci-feeder` that `execve`s a helper
must never leak the lock to a child), it is held ONLY for the
`open()`→control-write→index-read window — never for the feeder's
lifetime — and the `vhci` group + state-dir permissions let an
unprivileged feeder take it (the feeder itself runs zero-caps).
**Index discovery is kernel-local, write-then-read (round 30;
order corrected round 31, swe-2 item 11):** `open("/dev/vhci")`
creates ONLY the transport endpoint — no `hciN` exists yet.
Inside the lock the feeder MUST first WRITE the one-time control
packet `[0xff, 0x00]` — H4 vendor type `0xff` plus opcode
`HCI_PRIMARY` (`0x00`); `hci_vhci` accepts ONLY a `0xff` write
while no adapter exists (anything else is `-EINVAL`) — which
makes the kernel create and register the adapter, and only then
READS back the vendor event `[0xff, 0x00, <index_lo>,
<index_hi>]` carrying the freshly minted `hciN` index. A bare
`read()` without the write blocks forever — nothing mints an
adapter on `open()` alone. The `0xff` type is INTERNAL to the
feeder↔kernel boundary: it is consumed at the feeder for index
assignment and MUST NEVER be forwarded onto the control tunnel
(it is not part of the `0x01/0x02/0x04` passthrough set, and the
agent would have nowhere meaningful to relay it). Discovery needs
no `bluetoothd` round-trip: the write/read pair is the whole
discovery — the lock is released as soon as the index lands.

**MTU alignment (round 32 rework — fixed kernel MTU, local first
answer; SA_swe2_review_2 item 6 — replaces the round-30 "proxy
verbatim, local intervention is only stamping" rule).**
`hci_vhci` carries no `btusb` USB quirks: the
SCO buffer geometry a real dongle negotiates over USB descriptors
does not exist on a virtual adapter — behind `/dev/vhci` the feeder
IS the controller, and `hci_vhci` fixes the kernel-visible
`sco_mtu` ONCE at adapter init from the profile the line targets.
The kernel's SCO MTU is therefore NOT a negotiated value and can
never be re-declared mid-session: the feeder answers EVERY kernel
`HCI_Read_Buffer_Size` (`0x1005`) LOCALLY with the line's
target-profile geometry — `sco_data_packet_len` `60` B for the
mSBC profile, `48` B for CVSD (the alt-3 mSBC fallback produces
the same 60 B payload as alt 6, only spread over more
microframes) — and `num_hci_command_packets` (`ncmd`) `= 1`
(round 34, iteration-2 audit item 19): the fixed ncmd serializes
the kernel's command window onto the one control path the tunnel
provides, and a value > 1 would invite concurrent HCI commands
the feeder cannot de-mux on return. The local answer is
IDENTICAL on every issue — a second `0x1005` after an hdev
reset or an SCO link close/reopen re-declares the SAME fixed
geometry, because the kernel-side `sco_mtu` was bound once at
adapter init and an SCO reopen NEVER re-negotiates it (the alt-
setting reopen reselects the SAME line profile; a profile flip
requires a new line configuration, not an in-session
renegotiation) — because a verbatim real-dongle answer would
declare a geometry the virtual adapter registered differently at
init, and the kernel's `hdev->cmd_cnt`/buffer bookkeeping must
agree with what `hci_vhci` announced. The REAL `0x1005` is STILL
proxied through the tunnel to the physical controller like any
other `HCI_PASSTHRU` — but for geometry LEARNING only: the
feeder OWNS the real dongle's `0x0e` response — it is consumed
for learning and DROPPED, the kernel's answer is always the
local one (round 34, same item: two answer paths can never race
a single command slot). The learned
`sco_data_packet_len`/ACL values drive the feeder's
pacing and buffer validation (its `/dev/vhci` read buffer is
sized ≥ the largest kernel frame, and ACL `data_len` is checked
against the learned geometry — see the frame-validation
paragraph), they are NEVER forwarded verbatim as the kernel's
`0x0e` answer. The strict mismatch check compares the REAL
dongle geometry against the line's REQUIRED profile, not the
kernel's answer: a real controller whose declared
`sco_data_packet_len` cannot carry the required payload refuses
sync setup with `SCO_ALT_UNSUPPORTED` (deliverable per the
altsetting section). The EXACT locally-rewritten command set is
enumerated, never implied by "etc." (round 30): in V1 the feeder
rewrites NOTHING else — every other command passes through
verbatim; the only LOCAL interventions are the locally answered
`0x1005` — answered locally on EVERY issue, the real dongle's
`0x0e` response is proxied for learning and consumed by the
feeder, never forwarded as the kernel's command response — and
the altsetting-derived SCO packet length the
kernel must slice at. If the kernel side ever
reports a different length (e.g.
a host bluetoothd SCO-buffer readback mismatch), the feeder refuses
sync setup and raises `SCO_ALT_UNSUPPORTED` — a mismatched length
would let the kernel slice SCO at a different boundary, so the
agent's byte-2 H2 sequence rewrite would corrupt mid-frame audio.

**BD_ADDR verification (round 30, review-19 B2b#5).** After the
virtual adapter materializes, the feeder forwards the kernel's
`HCI_Read_BD_ADDR` (`0x1009`) to the dongle and compares the
returned address against the line's configured `adapter_mac`
(the `lines` row pin — the same value `bluetoothd` will match
`Adapter1.Address` against): a MISMATCH is a teardown + alert —
the agent fed a different physical dongle than the line was
provisioned for (or a hostile agent is presenting a sibling
line's address), and letting it stand would cross two lines'
link-key stores inside the shared `bluetoothd`. The check is
mandatory on every session, not only first attach — a swapped
dongle on a re-handshake is the same violation.

**Frame validation before `/dev/vhci` (round 30; agent-side H4
allowlist tightened round 31, swe-2 item 32).** The feeder is
the last sanity gate between tunnel bytes and the kernel
transport: a `HCI_PASSTHRU` frame is checked before any
`write("/dev/vhci")` — the inner HCI header's declared length
(event `param_len` / ACL `data_len` / SCO `payload_len`) MUST
equal the actual inner payload (`len - 5 - header`), and the H4
type itself is allowlisted to the AGENT→FEEDER set
`0x02`/`0x04` (§"H4 type split"; round 32,
SA_swe2_review_2 item 7 — the earlier `0x01`-inclusive list was
wrong, and `0x03` rides the media channel as record kind
`0x03`, never a `HCI_PASSTHRU` write): HCI `0x01` commands flow
in ONE direction only —
kernel→agent, read off the `/dev/vhci` fd by the feeder and
proxied down the tunnel — the dongle never emits a command, so
an agent-originated `0x01` is a fabricated packet the kernel
never issued and is REJECTED before `write("/dev/vhci")`; an
agent-originated
`0xff` vendor type is REJECTED — `0xff` is reserved for the
feeder's OWN vhci index handshake at the kernel boundary
(above) and an agent has no business minting one; `0x03` SCO
is REJECTED on the control channel — SCO belongs on the media
channel as record kind `0x03`, and a control-carried SCO frame
would bypass the
handle gate entirely; H4 type
`0x05` (vendor-diag channel on real UARTs) is REJECTED — it is
not a legal H4 type on the `vhci` link — ACL `data_len` MUST NOT
exceed the negotiated buffer geometry learned from the real
`HCI_Read_Buffer_Size` answer, the feeder's `/dev/vhci` read
buffer is sized ≥ the largest kernel frame (no silent truncation
of an event the kernel will never repeat), and EVERY `write()` to
`/dev/vhci` carries exactly ONE complete HCI packet — never a
coalesced run, never a fragment: kernel `vhci` framing is
packet-per-write.

The split transport means SCO data can outrun the setup event that
created the synchronous connection. The container-side feeder
(vhci writer) therefore gates media on connection state learned
passively from the event stream it forwards:

- Open a gate for sync connection handle H when it forwards a
  `Synchronous Connection Complete` event `0x2c` — `0x2c` ONLY
  (round 30): the extended event is the only one carrying
  `air_mode`/`rx_pkt_len`/`tx_pkt_len` the feeder must stamp on
  `SCO_STATE`, and a legacy `0x03` cannot supply them —
  with
  `status == 0x00`. The full 16-bit handle field is preserved when
  writing to `/dev/vhci`; comparisons mask with `0x0FFF`.
- SCO frames for an unknown handle are either dropped or held in a
  tiny ring of **≤ 4 frames per handle (≈30 ms)** — never more; the
  prototype's 200-frame hold would inject ~1.5 s of audio latency.
  The hold TABLE is bounded globally (round 30): at most 8 pending
  handles (the 12-bit space is large enough to be a memory attack —
  4 frames × 4096 stale keys was the leak), each entry with a ~250 ms
  TTL flushed on gate close — a held handle that never sees its
  `0x2c` dies with the table, not with the line.
- Close the gate on a successful `Disconnect Complete` `0x05`
  **before** writing that event to the kernel — but ONLY when the
  event's `Connection_Handle & 0x0FFF` matches the open gate's
  handle (round 30): `0x05` fires for EVERY ACL/BLE disconnect the
  controller reports, and an unmatched close on a side channel
  would otherwise tear live SCO audio mid-call. An unrelated `0x05`
  is forwarded to the kernel untouched — the gate is keyed to its
  own handle, never to "any disconnect" — so trailing media can
  never reach a connection the kernel has already torn down.

### Media health events (feeder → line health)

| event | trigger |
|---|---|
| `SCO_UNEXPECTED_HANDLE` | frames for an unknown handle persist > ~250 ms or > 20 frames after connection setup — sync/event desync suspected |
| `SCO_RX_STARVATION` | gate open but zero valid SCO frames for > ~500 ms during an active call |
| `SCO_FRAMING_ERROR` | media record shorter than the HCI SCO header, `payload_len` mismatch, or handle 0 / out of range |
| `SCO_PEER_MISMATCH` | frame from a source address failing the tunnel peer check (WireGuard `/32` or mTLS-bound identity; on `loopback` the accept()-time `SO_PEERCRED` uid is the identity) — distinct from `SCO_SESSION_MISMATCH` (round 21) |
| `SCO_SESSION_MISMATCH` | frame with a `session_nonce` other than the current HELLO_ACK-issued value — stale pre-reset session traffic (round 20) |
| `SCO_ALT_UNSUPPORTED` | selected air profile cannot be carried: the descriptor offers no usable altsetting, or the dongle's declared `sco_data_packet_len` mismatches the negotiated profile — the feeder owns the refusal/teardown and the fact is reported to the LineProvider (round 30 — the deliverable form of the old "call rejected" note; owner/state/timeout pinned round 33) |

These are loud degradation signals feeding the line state machine —
the kernel-side symptom (`hci0: SCO packet for unknown connection
handle`, an hci-core printk emitted on the *drop* path, so adapter
`sco` RX counters alone cannot be trusted) must be turned into an
explicit health event.

### H4 type split

`0x01` command, `0x02` ACL, `0x04` event → control-TCP.
`0x03` SCO → the media channel (media-UDP on tunnel profiles, a
`rec_len` record on the `loopback` SOCK_STREAM). Direction is
part of the contract (round 32, SA_swe2_review_2 item 7):
`0x01` commands flow ONLY kernel→agent — the feeder reads them
off `/dev/vhci` and proxies them down the tunnel, the agent never
mints one — while `0x02`/`0x04` are the only H4 types an agent
may write back on the control channel (`0x03` SCO travels on the
media channel as record kind `0x03`, never inside a
`HCI_PASSTHRU` frame — see the allowlist in the
frame-validation paragraph). On the USB side the class is implied by the
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
| `wireguard` | system-managed `wg0` peer; auth = peer pubkey ↔ line_id | V1 default for remote lines (the contract's model default is `loopback` — co-located, below) |
| `loopback` | UDS control + UDS `SOCK_STREAM` media at `/run/phonebridge/media/<line_id>.sock`; auth = token + `SO_PEERCRED` uid ↔ line_id on BOTH sockets (media peer verified at `accept()`) | V1 (co-located agent) — round 30; stream media pinned round 32 |
| `direct_mtls` | plain TCP/UDP + mutual TLS; cert CN ↔ line_id | V1 (LAN/VPN-ready nets) |
| `sstp` | reserved profile — server pack shipped by system | future |
| `openconnect` | reserved profile | future |
| `vless_xray` | reserved profile | future |

Interface-creating profiles (wireguard, sstp, openconnect, vless_xray)
give the agent an L3 path; the two channels then run inside it
unmodified. `direct_mtls` wraps the TCP control channel in TLS and the
UDP media channel in DTLS. Line identity is always bound to the tunnel
credential — an agent cannot feed a line it was not provisioned for.

**`loopback` (round 30; media transport re-pinned to
`SOCK_STREAM` round 32, SA_swe2_review_2 item 5):** the canonical profile for a dongle
physically on the gateway host — control rides a per-line
`unix://` socket (the bind-mounted `0660` agent socket dir, group
`phonebridge`), media rides a CONNECTED `SOCK_STREAM` Unix socket
at `/run/phonebridge/media/<line_id>.sock` (same `0660` dir
discipline, one socket per line; the feeder listens, the agent
connects — the earlier `SOCK_DGRAM` pair is removed because a
datagram UDS cannot authenticate its peer at `connect()` and its
`EDESTADDRREQ` rebind semantics duplicated the nonce gate).
`SO_PEERCRED` is the second factor on BOTH
sockets: the feeder maps the connecting uid to the agent's
provisioned uid for that `line_id` — the `agent.token` in HELLO
is still REQUIRED (defence in depth for a same-host process
running under a shared uid), but the uid check is what a foreign
uid cannot forge. There is NO unauthenticated UDP path even on
loopback — the media peer is verified at `accept()` exactly
like the control peer, and the `session_nonce` still binds
frames to the live control session (§2). No wireguard/tls
stack is involved — loopback
is the one-host trust domain made explicit.

**`direct_mtls` and NAT rebind (round 30):** classic DTLS 1.2
binds its security context to the 4-tuple, so a NAT mapping
change mid-session would kill media silently. The profile
therefore REQUIRES one of: DTLS Connection ID (RFC 9146) on the
media channel, or a documented re-handshake procedure — a new
`MEDIA_PING` from an unbound `(IP, port)` under a still-live
control session triggers a fresh abbreviated DTLS handshake keyed
by the session_nonce, and the old association is retired only
after the new one authenticates. A plain `media_seq` rebind alone
is NOT sufficient on this profile (unlike wireguard, there is no
L3 cryptographic boundary to lean on).

**Weak-host caveat (round 30):** Linux accepts packets addressed
to a local IP on ANY interface (weak host model, `rp_filter=2`):
a `wg0`-addressed packet can physically arrive via `eth0`/`lo`.
Binding the feeder sockets to the `wg0` IP is therefore NOT a
perimeter on its own — the deployment MUST additionally pin the
traffic: host firewall `iifname`/`fwmark` rules admitting the
feeder ports only when they ingress on `wg0`, or the feeder
setting `SO_BINDTODEVICE wg0` on its sockets. Without one of
those, a same-LAN host can spoof the `/32` peer address and reach
the media port without holding the peer key.

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
    usb_path: "1-1.4"                  # PRIMARY stable selector
                                       # (round 30) — physical USB
                                       # port path, unique per socket,
                                       # survives reboots
    adapter_mac: "A8:9C:ED:C9:E0:FD"   # verified POST-CLAIM via
                                       # HCI_Read_BD_ADDR — pin, not
                                       # search key; adapter_mac-only
                                       # scan is opt-in invasive
                                       # (`scan_mode: invasive`, §1 —
                                       # resets every candidate dongle,
                                       # firmware-stub MAC caveat)
  detach_btusb: true                 # userspace ownership (recommended);
                                     # pair with a udev rule pinning
                                     # the port OFF btusb (round 30 —
                                     # a power-cycle re-enumerates the
                                     # device and the kernel rebinds
                                     # btusb in ~ms; the agent's
                                     # detach must survive the cycle:
                                     # `ATTR{authorized}="0"` or a
                                     # driver `unbind` rule keyed on
                                     # usb_path, else the host hciX
                                     # reappears mid-reset)
  reset:
    on_start: false
    on_watchdog: true                # power-cycle on DONGLE_DEAD —
                                     # bounded (round 30): max_attempts
                                     # per watchdog episode below,
                                     # runs OFF the heartbeat path
    max_attempts: 3                  # per episode — after that the
                                     # line stays DONGLE_DEAD + alert,
                                     # never a reset storm (round 30)
    backoff_ms: [1000, 30000]        # per-attempt exponential window
    exec_timeout_ms: 15000           # the cycle command runs detached
                                     # with a hard timeout — a hung
                                     # uhubctl (D-state in xhci_hcd)
                                     # can never stall heartbeat or
                                     # the control session (round 30)
    command: ["uhubctl", "-l", "1-1", "-p", "4", "-a", "cycle"]
                                             # host-specific, optional;
                                             # execve-style argv list —
                                             # NEVER a shell string
                                             # (round 28: no shell =
                                             # no injection, no quoting
                                             # surprises); the child
                                             # gets FD_CLOEXEC on every
                                             # inherited fd, and the
                                             # agent CANCELS libusb
                                             # transfers + closes the
                                             # handle BEFORE cycling
                                             # (a powered-off device
                                             # with open URBs leaks
                                             # descriptors and wedges
                                             # the hub driver's state
                                             # machine — round 30).
                                             # uhubctl needs WRITE on
                                             # the parent hub: grant via
                                             # a udev rule matching the
                                             # hub (MODE/GROUP), never
                                             # setuid-root the whole
                                             # agent (round 30)
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
                                      # (7300/7301 shown for line 1);
                                      # on `loopback` the pair is two
                                      # unix:// paths instead — the
                                      # 0660-dir control socket and
                                      # /run/phonebridge/media/<line_id>.sock
                                      # (SOCK_STREAM, agent connects);
                                      # the pair is stored in the
                                      # line's `lines` row at
                                      # provision time together with
                                      # the peer credential (design
                                      # §4.5 schema — wg pubkey /
                                      # cert CN / agent uid ↔ line_id),
                                      # so two lines can never claim
                                      # one port pair or one peer
                                      # identity (round 30)

heartbeat:
  interval_ms: 2000                # FALLBACK only — used until the
                                   # first HELLO_ACK; the frame's
                                   # `heartbeat_interval_ms` is
                                   # authoritative for the session
                                   # (round 30 — HELLO_ACK wins,
                                   # agent.yaml never overrides it)
  missed_limit: 3                  # → agent reports tunnel as dead;
                                   # counts BOTH missed HEARTBEAT_ACK
                                   # (0x17) and the local send-side
                                   # check (round 30)
reconnect:
  backoff_ms: [500, 30000]
  jitter_pct: 20

logging:
  level: "info"
  mask_pii: true                   # MANDATORY — non-disableable
                                   # (round 30): there is no `false`;
                                   # PII, Link Keys and the HELLO
                                   # `token` never reach a log line
```

Field rules:

- `line_id` + the credential in `agent_token_file` bind the agent to
  exactly one line at the container; mismatch → `HELLO` rejected,
  connection closed.
- `channels.control`/`channels.media` are **provisioned per line at
  line provisioning time** (issue #89): the endpoint pair is minted
  by the admin surface (`admin.provision_line`/`configure_line`),
  stored in the line's `lines` row, and shipped to the agent inside
  its config — the agent never learns its endpoints through a
  separate dynamic discovery API, and there is no protocol message
  that reallocates them. The pair above is only the canonical
  allocation example (one control+media pair per line).
- `dongle.match.usb_path` is REQUIRED for normal operation (hciN-style
  indices are unstable and USB descriptors carry no `BD_ADDR` —
  `usb_path` is the only non-invasive stable selector, §1);
  `adapter_mac` is REQUIRED as the post-claim PIN the feeder verifies
  via `HCI_Read_BD_ADDR`, and the `adapter_mac`-only invasive scan
  exists solely behind an explicit `scan_mode: invasive` opt-in for
  onboarding (§1 — it `HCI_Reset`s every candidate controller and can
  read firmware-stub MACs).
- Everything under `transport.<profile>` except file paths and the
  endpoint address is canonical — the system ships per-profile config
  packs; admins do not tune protocol internals.

## §5. Lifecycle

`PROVISIONING` (admin created line + issued token/keys) → `PAIRING`
(agent connected, dongle attached, adapter materialized, phone pairing
via container bluetoothd) → `READY` → `IN_CALL` → `LINK_DEGRADED`
(heartbeat/USB issues — round 30 rename: `DEGRADED` collided with the
kernel liveness rung) → `FAULTED` (manual or watchdog teardown) →
back to `READY` on recovery.

**USB power-cycle = full re-handshake (round 30).** A port cycle
(`0x13 RESET`, watchdog `uhubctl`, or an unplug) is a NEW PHYSICAL
DEVICE from the stack's point of view: the agent tears its control
session down, re-detects the re-enumerated dongle on `usb_path`,
re-claims it (the `detach_btusb` udev pin must keep the host's
`btusb` off the re-enumerated device — §4, else the kernel rebinds
it in ~ms and briefly materializes a rogue `hciX` in `init_net`),
and runs a FRESH HELLO. The feeder side mirrors this: a re-HELLO
after a USB-cycle notification mints a NEW `/dev/vhci` attach
(the old kernel adapter dies with the session — never recycled),
so `bluetoothd` sees a clean adapter, not a half-dead zombie.

**Headless pairing (round 19).** Pairing runs on `bluetoothd` INSIDE
the container, and modern phones use Secure Simple Pairing (numeric
comparison on both ends) — an operator cannot be expected to
`docker exec` into bluetoothctl per line. The container therefore
ships a managed BlueZ `org.bluez.Agent1` that REJECTS every pairing
attempt by default; `admin.pair_window {line_id, duration_sec}`
(required, `0 < duration_sec <= 300` — `PairWindowRequest` rejects
out-of-range values, no clamp and no default, round 28) sets
`Pairable` AND `Discoverable` on that line's adapter and arms
`PairableTimeout` to the requested window (round 30 — Pairable alone
still leaves the adapter invisible to phone-initiated discovery);
the managed `Agent1`
auto-confirms the SSP passkey comparison for the window's duration
solely for the line's `paired_phone_mac` (§6). On a successful pair
the agent marks `Device1.Trusted = true` (round 30 — otherwise
BlueZ re-prompts `AuthorizeService` on the phone's own reconnect,
killing headless recovery), and the supervisor force-clears
`Pairable`/`Discoverable` on ANY window close — timeout, restart,
or a daemon crash mid-window (a stale open window is a standing
MITM invitation).
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
  case round 27; `loopback` profile round 30): the container-side
  `vhci-feeder` binds its
  control-TCP listener and media-UDP socket ONLY to the tunnel
  interface IP (`wg0` for the `wireguard` profile) — never to
  `0.0.0.0` — AND the bind is enforced against the weak-host
  model (§3: `SO_BINDTODEVICE`/firewall `iifname` rules, or the
  `/32` peer address is spoofable off-link), so the line surface
  is unreachable except through the authenticated tunnel. For a
  dongle physically co-located with the container the
  `loopback` profile is canonical (§3): control rides a UDS
  socket in the bind-mounted `0660` dir, media rides a CONNECTED
  `SOCK_STREAM` UDS at `/run/phonebridge/media/<line_id>.sock`
  (round 32 — the feeder accepts, the agent connects,
  `SO_PEERCRED` verified at `accept()`) — the
  profile exposes NO UDP listener at all, so no unauthenticated
  datagram path exists even on loopback (round 31). The
  AUTHENTICATION model keeps two factors there too
  (round 30 — the earlier "token alone on loopback" rule is
  superseded): `agent.token` PLUS `SO_PEERCRED` — the connecting
  process's uid MUST match the uid provisioned for that
  `line_id`; the peer-key / mTLS-CN second factor applies to WAN
  profiles where a foreign host could hold a leaked token, and
  the uid factor is the loopback-domain equivalent a foreign
  host cannot forge.
- WireGuard peer isolation (round 18): the container-side server
  config MUST pin each peer to `AllowedIPs = <peer>/32` and MUST NOT
  forward between peers — a compromised agent host must not be able
  to inject UDP into a sibling line's media port. The `allowed_ips`
  in the agent-side example above is the agent's route scope; the
  server's per-peer `/32` is the isolation boundary.
- PII hygiene is non-negotiable (round 30): the agent must not log
  dialed numbers, caller IDs, or HCI payloads containing them —
  `mask_pii` has no `false` option (it is not a tunable but a
  hard rule): raw `HCI_PASSTHRU` frames carry Link Key exchange
  contents, the HELLO `token` and SDP/AT metadata, so diagnostic
  frame dumps are masked structurally, not by convention.
- SSP auto-confirm caveat (round 19; MAC-scoped since round 27):
  `admin.pair_window` trades the numeric-comparison MITM check for
  operability — but ONLY toward the configured target: during the
  open window the managed `Agent1` auto-confirms SSP solely for the
  line's `paired_phone_mac`, and every other Bluetooth address —
  including a nearby hostile device — hits the same unconditional
  rejection as outside the window. Mitigation for the target itself
  is scope, not crypto: the window is short (`duration_sec` is a
  MANDATORY bounded parameter — `0 < duration_sec <= 300`, no
  default, round 30 — the earlier "default 60 s" claim is removed;
  the admin always names the window), bound
  to ONE adapter, requires an admin-scope token, and the operator
  is expected to hold the target phone at the prompt.
- The agent binds to loopback/WAN only via the tunnel profile; it does
  not expose any local listener.
