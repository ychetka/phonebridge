---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-26 post-release audit fixes (SA_carbon_review_16): EventEnvelope now
  validates the (name -> payload type) leg via EVENT_PAYLOAD_TYPES so a
  broadcast-scoped envelope can never carry a PII payload; MAX_FORWARD_HOPS=8
  contract constant pins the transitive forward_via_line cycle bound the docs
  already required; ProviderCallStateUpdate rejects reason on non-disconnected
  states; profile strings bounded (MAX_PROFILE_LEN=128) on
  ProviderRegistration/ProviderCapabilityUpdate/LineSummary; free_lines goes
  through _check_line_id; new LivenessState.UNBOUND for provider-less lines
  (LineSummary caps==() iff liveness==unbound; never a reported fact or event
  endpoint); media-token reissue documented as superseding the prior pair;
  dead-binding provider commands documented fail-closed with network_error
  teardown; admin.assign_line documents the system:* owner ban;
  design/api-contract stale DEGRADED-DOWN and idle_revoke_after:null
  vocabulary corrected; .changes scope list fixed to actual repo scopes.
why: >-
  The independent post-v0.0.20 audit (SA_carbon_review_16) found two Major
  gaps — an envelope could stamp a PII payload under a broadcast-legal event
  name because only (name, audience.scope) was validated, and the documented
  8-hop forwarding bound had no contract-level anchor an M1 implementer would
  see — plus a set of Minor hygiene drift items; all are closed here ahead
  of the M1 implementation milestone.
refs: issue #65
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-forwarding-policies-are-loop-safe-and-bounded-round-25
