# Changelog

## [0.0.8] - 2026-11-13

### User-facing

- `event.call_state_changed` now carries `answer_confidence`
  (`"provisional"|"confirmed"`) — the wire reflects how sure the
  bridge is that `active` means a human answered.

### Codebase

#### domain contract
- [contract] `CallStateChangedEvent.answer_confidence` field
  (additive). (issue #35)

#### docs
- [docs] §3.5 lists `provider.audio_ready`; §3.6 documents
  `event.fallback_triggered` + `answer_confidence`; §13 M4/M5
  rewritten for the hci-agent + vhci-feeder line-bundle topology.
  (issue #35)

## [0.0.7] - 2026-11-13

### User-facing

- `call.dial` requires `country` (ISO 3166-1 alpha-2) — breaking
  signature change on the domain kernel.

### Codebase

#### domain contract
- [contract] `LineManager.dial()` `country` param;
  `LineAssignment.allowed_number_types` (incl. mandatory
  `FIXED_LINE_OR_MOBILE` for NANP); `EventName` +
  `AudioReadyEvent`/`FallbackTriggeredEvent`/`LineStateChangedEvent`/
  `MissedCallEvent`; `FallbackDecision.fallback_call_token`;
  `LineManager.drop_session` + `ingest_audio_ready`;
  `RedactedString` explicit JSON serializer. (issue #33)

#### docs
- [docs] `MAX_CTRL_FRAME_LEN = 4096` in `docs/hci-agent.md` §2.
  (issue #33)

## [0.0.6] - 2026-11-13

### User-facing

- `provider.call_state` uses the canonical vocabulary
  `ringing|dialing|alerting|active|disconnected`; `call.acquire`
  serves `dial_out` only; new `provider.audio_ready` and
  `event.fallback_triggered` wire elements; `line.dial`/
  `line.answer` carry per-lease media credentials.

### Codebase

#### domain contract
- [contract] `ProviderCallState` canonical vocabulary (breaking);
  `ProviderCallStateUpdate.lease_id`. (issue #31)

#### docs
- [docs] Unified hci-agent model: `docs/hci-agent.md` agent spec +
  YAML config format, transport profiles, lossless `ctrl_seq`
  control channel, agent-side SCO isoc reassembly, feeder SCO handle
  gate, media health events — locked by the live vhci experiment.
  (issues #22, #29, #31)

## [0.0.2] - 2026-11-12

### User-facing

_No user-visible changes._

### Codebase

#### docs
- [docs] Consolidated `docs/phone-bridge-service-design.md` with all
  accumulated design decisions (partner addendum rounds 1-3,
  REQUIREMENT UPDATEs 1-2, UNIFIED ARCH, refined intent model,
  decision rounds 5-10): unified LineProvider registration model,
  Manager + APICallService surfaces, intent-based LineAssignment vs
  CallLease, wake-and-grant inbound, always-packetized server-issued
  media, mandatory FIFO queue, single-container prod topology,
  universal event fan-out, SQLite+MCP admin, SMS dropped.
  (issue #1)
- [docs] Added `docs/api-contract.md` (wire-level JSON-RPC contract)
  and English versions of `docs/architecture-unified.html` and
  `docs/interaction-flow.html`. (issue #1)

## [0.0.1] - 2026-11-12

### User-facing

_No user-visible changes._

### Codebase

#### tooling
- [chore] Initial canonical bootstrap: repository skeleton, .devin
  orchestration layer, CI workflows, and the founding design spec
  (docs/phone-bridge-service-design.md). (issue #0)
