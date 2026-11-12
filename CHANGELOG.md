# Changelog

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
