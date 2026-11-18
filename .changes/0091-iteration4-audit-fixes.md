---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Issue #91 iteration-4 external architecture audit fixes
  implemented (docs/diagram only, 23 findings: 20 accepted, 2
  documentation clarifications, 1 disputed/deferred): the Mermaid
  block diagram now draws /dev/vhci as a bidirectional reader/writer,
  terminates the Bluetooth RF edge at the remote DONGLE, routes all
  post-mutation dispatch through the ordered writer task
  (write-through first, then drained envelopes/commands to
  EventBus/Manager/bundle supervisor/Agent1), adds the container
  init / stack supervisor with a reconciliation edge to the kernel,
  draws the oFono HandsfreeAudioAgent SCO-fd path to PipeWire,
  makes InternalService-FallbackWorker and FallbackWorker-
  MediaRouter edges bidirectional, adds the LineProvider->MediaRouter
  0x05/0x06 DTMF control edge, scopes pair-window force-clear to
  the kernel's authoritative line_id-to-hciN map with
  PairableTimeout/DiscoverableTimeout backstop, and labels the
  tunnel edge with SCO_SETUP/SCO_TEARDOWN plus
  control-TCP(mTLS)/media-DTLS and SOCK_SEQPACKET loopback
  transport; hci-agent.md gains the SCO_SETUP/SCO_TEARDOWN
  altsetting actuation pair, SOCK_SEQPACKET loopback media, the
  mandatory DTLS-on-UDP note for direct_mtls, HELLO-inside-
  authenticated-tunnel + session_nonce binding, the IsoPump-paced
  outbound playout buffer with whole-packet drop/dup drift
  compensation, codec-correct feeder PLC (H2 seq continuation +
  Packet_Status_Flag=0b10), feeder->supervisor->kernel hciN
  reporting, fixed hardware gate ordering (post-claim
  HCI_Read_BD_ADDR MAC verify) and startup wedge recovery;
  ADR-0004 mirrors the gate fix, wedge recovery, playout buffer
  and actuation pair; the design doc pins the in-memory-
  authoritative/SQLite-write-through invariant with restore_line
  compensation, writer-owned ordered dispatch, the stack
  supervisor's full kernel reconciliation, kernel-timer-driven
  fallback activation with bounded ack-retry
  (event.fallback_triggered informational), hydrated-binding
  quarantine revalidation, per-uid container separation with
  group-scoped admin.sock + pid-to-role internal.sock, and
  credentials-at-rest hashing/0600 rules; api-contract.md adds
  the 0x06 DTMF-END/resume opcode, the per-session-probe
  semantics of SendTones/AT+VTS and the session_nonce media
  binding; line-manager.md gains a round-36 requirement for the
  authoritative hciN map and hydrated-binding quarantine.
why: >-
  The iteration-4 audit found bidirectional/ownership errors and
  missing supervision/dispatch edges on the block diagram plus
  underspecified protocol surfaces (altsetting trigger, UDP media
  auth, hciN mapping, PLC codec state, drift compensation, wedge
  recovery, credential storage); the accepted resolutions correct
  the topology and pin the mechanisms in the normative docs without
  touching implementation code.
refs: issue #91
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-iteration-4-audit-domain-state-pins-round-36
