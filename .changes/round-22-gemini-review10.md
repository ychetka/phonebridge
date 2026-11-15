---
type: refactor
scope: daemon
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-22 architect review (SA_carbon_review_10_gemini):
  LineManager.update_provider_capabilities + ProviderCapabilityUpdate
  ingress; CallLease.session_id ((session_id, lease_id) ownership);
  gateway-minted call_token on provider.call_state ringing +
  caller_id? on ring/state reports; event schema/wire alignment
  (caller_id, expires_at RFC 3339, WireCallState, ticket_id on
  event.line_acquired, required owner_agent_id on
  assignment_revoked); ephemeral CallLease auto-release vs durable
  call.release{assignment_id}; receive_inbound-scoped inbound events
  and accept; admin.assign_line + TimeoutConfig patch on
  configure_line; LineProvider owns DTMF synthesis/uplink muting;
  announcement_ref + fallback_worker acquire bypass +
  event.fallback_triggered as the sole delivery; bounded pending
  claims and ProviderRegistration.meta.
why: >-
  The review found the contract unsafe to freeze for M1-M5: no
  provider capability-update path, ambiguous multi-session lease
  ownership, an uncorrelatable early caller hangup on inbound rings,
  and event models drifted from the documented wire names/types;
  this PR closes the two critical, six high and six medium findings
  before implementation starts.
refs: issue #53
---

## Spec impact
- none
