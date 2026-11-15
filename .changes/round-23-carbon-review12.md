---
type: feat
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-23 architect review (SA_carbon_review_12), Phase-0 contract
  freeze: ProviderHangupCommand XOR target
  {lease_id?|call_token?}; ForwardViaLineFallback gains
  target_number/target_country with config-time self-loop ban;
  MediaAllocator lifecycle (allocate at dial/accept, idempotent
  revoke on lease ENDED); JSON-safe _mask_for_log
  (datetime/timedelta/Enum); outbox SOFT_CAP pre-flight semantics +
  teardown-never-raises; saga compensation on
  line.dial/line.answer; FallbackTriggeredEvent.expires_at +
  TimeoutConfig.fallback_accept_ttl_sec; event.idle_alert +
  AdminAudience (idle escalation alert-only); reissued_ticket_id on
  call_state_changed; bounded idempotency_key (1-128);
  phone_mac-to-agN resolution + Call Waiting auto-reject;
  long-lived hci-agent token + uplink playout jitter buffer;
  MediaRouter 20 ms pacing + asymmetric rings (ADR-A); M1/M2
  storage split; capability matrix; pinned PipeWire format;
  execve-only hooks + webhook retry deadline; informational
  profile; py.typed + docs/specs skeleton; HTML/doc drift fixes.
why: >-
  Freeze the domain contract and architecture spec before the
  M1-M5 implementation milestones: the review found one critical
  (unexecutable RejectFallback), six high and seven medium gaps in
  the contract and its docs; Phase 0 items only - M1-M5 sections
  are implementation notes, not runtime behavior.
refs: issue #55
---

## Spec impact
- adds: docs/specs/line-manager.md#provider-hangup-targets-exactly-one-identifier
- adds: docs/specs/line-manager.md#forward-via-line-carries-the-complete-dial-target
- adds: docs/specs/line-manager.md#outbox-soft-cap-pre-flight-and-drain-discipline
- adds: docs/specs/line-manager.md#saga-compensation-on-linedial-and-lineanswer
- adds: docs/specs/line-manager.md#idle-escalation-is-alert-only
- adds: docs/specs/line-manager.md#accept-window-always-elapses-before-fallback
- adds: docs/specs/line-manager.md#mediaallocator-lifecycle
- adds: docs/specs/line-manager.md#log-projection-is-json-safe
