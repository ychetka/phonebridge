---
type: feat
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-24 architect review (SA_carbon_review_13), final Phase-0
  contract/doc freeze v0.0.19: MediaRouter pacing direction
  corrected - the agent-socket reader drains continuously into the
  1500-frame uplink ring (0x02 flushes it immediately) while a
  monotonic pacer writes ONE 640 B frame per 20 ms tick to the
  LineProvider; new frozen LineTimeouts effective model (defaults
  5.0/10.0/5.0/2/3/10.0, down>degraded validator) exposed via
  LineAssignment.timeouts for SQLite write-through;
  ProviderBinding.heartbeat_interval_ms stamps the per-session
  cadence (default 5000, gt>0); TimeoutConfig gains the same
  ordering validator; LineTimeouts.apply_patch merges a
  TimeoutConfig patch atomically via full re-validation of the
  merged dict (round-24b residual: a lone-field patch that breaks
  the heartbeat ladder maps to INVALID_ARGUMENT, no partial
  state); validate_e164_country + ForwardViaLineFallback
  model validator + LineAssignment self-loop ban move dial-target
  validation into constructors; provider caps are non-empty
  telecom-only (no exclusive_line); docstrings freeze the
  ingest_ring glare matrix, tick-driven decide_fallback and
  fallback TTL expiry (MissedCallEvent ACCEPT_WINDOW_EXPIRED),
  error-code mapping (CLAIM_NOT_FOUND/STALE_TOKEN/
  INVALID_ARGUMENT) and AssignmentId rotation with a bounded
  _closed_assignments ring; hci-agent 0x16 SCO_STATE gains
  handle?:uint16 (required on open) + continuous 2-bit H2 sync
  counter rewritten on every outbound mSBC packet including
  silence; spec-impact noop literal unified to bare "-";
  interaction-flow/architecture-unified HTML updated (expires_at,
  ticket_id, event.idle_alert, admin audience). Round 24c
  (post-review nits): the heartbeat_interval_sec ->
  heartbeat_interval_ms conversion is pinned -
  heartbeat_interval_ms_from_sec (round(sec*1000), min 1 ms) +
  ge=0.001 granularity floor on LineTimeouts and TimeoutConfig -
  so a valid config can never stamp a zero cadence;
  LineTimeouts.apply_patch ErrorData.detail now joins ALL
  validation messages; design.md aligns to the 0x16 SCO_STATE
  wire field name "handle".
why: >-
  Close the last contract/documentation drift before the M1-M5
  implementation milestones: the review found two critical defects
  (inverted MediaRouter pacing that would strand the 0x02 barge-in
  flush inside SO_RCVBUF; missing effective-timeout fields on the
  kernel DTOs so M2 could not answer heartbeat_interval_ms or
  persist timeouts to SQLite), four high (hci-agent silence/H2
  phase defect, tick vs decide_fallback lifecycle, ingest_ring
  glare matrix, idle_alert delivery channel), five medium and two
  low findings; Phase-0 items only - implementation notes for
  M1-M5 are deferred to the milestone acceptance notes.
refs: issue #59
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-accept-window-always-elapses-before-fallback
- adds: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- adds: docs/specs/line-manager.md#requirement-dial-target-and-caps-validation-is-model-level
