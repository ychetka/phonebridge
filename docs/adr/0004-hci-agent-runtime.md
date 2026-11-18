# ADR-0004: hci-agent production runtime is Rust + libusb

Status: accepted (issue #89, architecture diagram review).
Date: 2026-11-18. Supersedes the "Bumble USB transport as base
implementation" wording in `docs/hci-agent.md` §1.

## Context

The hci-agent owns a Bluetooth USB dongle in userspace (`btusb`
detached) and relays HCI frames between the dongle and the
container-side `vhci-feeder`: commands/ACL/events on a lossless
control channel and SCO audio on a real-time media channel
(`docs/hci-agent.md` §2).

SCO/eSCO audio rides USB **isochronous** transfers. The host
controller services them on a fixed microframe schedule and offers
no backpressure or retransmission: a userspace process that misses a
service window silently loses audio frames, and a stalled userspace
process lets underrun silence reach the phone. Earlier drafts named
a Python implementation — libusb/pyusb with the USB transport of
Google Bumble — as the "base implementation". That is the wrong
baseline for production:

- Python's GC pauses and GIL contention are unbounded relative to
  the ~1-7.5 ms isochronous service interval; any pause on the data
  path produces clicks, underruns and lost HCI SCO records.
- The Python libusb bindings expose isochronous transfer control
  (per-packet descriptors, per-packet status/`actual_length`,
  transfer callback pools) poorly or not at all, while the protocol
  requires keeping several RX transfers in flight and reassembling
  complete HCI SCO packets at the isoc endpoint boundary.

## Decision

**The production hci-agent is implemented in Rust on top of
libusb.** Python/Bumble is kept only as the protocol reference and
as optional development tooling (scanning, experiments,
golden-transcript generation). We do NOT split production into a
Python control plane plus a native data-path helper — one language,
one process, one audited unsafe surface.

### Implementation boundary

- `rusb`'s safe Rust API may cover enumeration, descriptor reads,
  configuration/altsetting selection, detach/claim and the
  control/interrupt/bulk endpoints.
- A narrow, separately audited `IsoPump` module may use
  `rusb::ffi`/`libusb1-sys` directly for what the safe API does not
  expose: isochronous transfer submission, per-packet descriptor
  setup, per-packet `status`/`actual_length` readback, completion
  callbacks, and the transfer pool. `IsoPump` is the only module
  allowed to contain `unsafe` USB code.
- `nusb` is NOT the baseline until its isochronous API is validated
  on the points this service depends on: per-packet
  `status`/`actual_length` reporting, transfer pooling, pacing
  control, and parity with the libusb path on ARM targets;
  `rusb-async` is NOT required baseline while it is
  alpha/incomplete for per-packet status reporting.
- C (raw libusb) is a fallback only if the Rust/libusb path fails
  the hardware acceptance gate below.

### Runtime shape

- A dedicated **USB isochronous pump thread** owns all isoc
  transfers; it never shares an executor or event loop with the
  network tunnel tasks (USB timing and network pacing are different
  clocks and different deadlines).
- Bounded queues and pooled buffers between the pump and the tunnel
  I/O — no unbounded allocation on the data path, no GC anywhere on
  it.
- A bounded **outbound playout buffer** sits between the tunnel
  media reader and `IsoPump` TX, drained by the isochronous
  completion callback clock (the eSCO service interval, delivered
  in `SCO_SETUP.service_interval_ms`) — drift compensation is
  whole-packet drop/dup only, NO ASRC, because HCI SCO payloads
  are transparent codec frames (issue #91, iteration-4 audit
  item 14; `docs/hci-agent.md` §2 "Uplink playout jitter buffer").
- Malformed isoc input fails closed: incomplete or length-mismatched
  HCI SCO reassembly is counted and dropped, never forwarded
  (consistent with the wire contract's framing rules).
- Shutdown cancels and drains in-flight transfers inside a bounded
  window, releases the claim, and — on remote/dedicated agent
  hosts only — restores `btusb` (issue #93, iteration-5 audit
  item 4: `btusb` re-attach is REMOTE-ONLY. On the co-located
  `loopback` profile the dongle is administratively assigned to
  the container, which shares `init_net` — re-binding `btusb`
  would register a PHYSICAL `hciN` in `init_net` carrying the same
  `BD_ADDR` as the line's `/dev/vhci` adapter, and the container's
  own `bluetoothd` could then claim the physical dongle. A
  loopback agent therefore MUST NOT re-bind `btusb` on shutdown
  or wedge recovery: the device stays userspace-claimed or
  unbound by policy, and the deployment keeps the physical MAC
  unmanaged by the container's `bluetoothd`). Startup runs
  the symmetric **wedge recovery** (issue #91, iteration-4 audit
  item 15): the agent detects a dongle left detached from `btusb`
  or on a stale non-zero altsetting by a previous unclean exit —
  on a remote host it restores `btusb` BEFORE re-attempting the
  claim sequence, on the `loopback` profile it detects the same
  wedge but resets the device WITHOUT re-binding `btusb`
  (unbound is the loopback steady state) — so a
  crashed predecessor never leaves the device half-owned.

### Hardware acceptance gate

Before the runtime is accepted for production the implementation
must demonstrate, on both x86_64 and an ARM target:

1. enumerate the dongle by `usb_path` and verify VID/PID/serial;
2. detach `btusb` for the configured dongle only and claim
   interfaces 0/1;
3. send `HCI Reset`, read `Command Complete`, then issue
   `HCI_Read_BD_ADDR` and verify the returned adapter MAC against
   the configured `adapter_mac` — the MAC check is POST-CLAIM by
   construction: `BD_ADDR` is not carried in USB descriptors and
   only the live controller answers `0x1009` (issue #91,
   iteration-4 audit item 15);
4. select the SCO alternate setting per the negotiated `air_mode`
   on the explicit `SCO_SETUP`/`SCO_TEARDOWN` control messages —
   the actuation pair is the altsetting trigger mechanism, so the
   agent needs no HCI-event parsing to time Interface-1 switches
   (issue #91, iteration-4 audit item 5; sniffing
   `Synchronous Connection Complete 0x2c` is a consistency check
   only, never a dependency);
5. keep isochronous RX transfers continuously in flight and pace
   outbound SCO to the eSCO service interval;
6. preserve complete HCI SCO records end to end and expose
   per-packet `status`/`actual_length`;
7. cancel/drain callbacks safely on stop and — verified on the
   remote profile — restore `btusb`; the `loopback` profile is
   verified to leave `btusb` UNBOUND on stop and wedge recovery
   (issue #93, iteration-5 audit item 4);
8. report CPU/jitter/lost-frame measurements within the documented
   media budget.

## Consequences

- `docs/hci-agent.md` §1's "Base implementation: the USB transport
  of Google Bumble" is replaced by this ADR; Bumble remains useful
  for protocol conformance experiments only.
- The `IsoPump` module gets a dedicated audit entry in
  `docs/CODING_HAZARDS.md`-style review: it is the single `unsafe`
  surface on the data path.
- The agent host keeps the "dumb relay" property — the Rust process
  still owns no bluetoothd, no pairing and no telephony state.
