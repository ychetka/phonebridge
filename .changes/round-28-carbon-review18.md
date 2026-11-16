---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-28 blind paranoid audit fixes (SA_carbon_review_18, 33 findings):
  LivenessState gains the kernel-computed DEGRADED rung
  (reachable -> degraded -> unreachable, elapsed-time ladder;
  degraded -> ws_disconnected legal on control-socket loss);
  CALL_PHASE_FORWARD_TRANSITIONS pins the forward-only CallPhase
  matrix including the alerting-less DIALING -> ACTIVE answer;
  MediaAllocator.reissue(lease_id, role) supersedes one role's media
  credentials without touching the other half; ProviderRegistration
  carries the adapter-distilled reregister flag (derived from the
  RPC method name only - a `reregister` wire-params key is ignored)
  so a reregister on a live REACHABLE or DEGRADED binding is a
  legal half-open supersede (fresh
  assignment_id, leases preserved, snapshot reconciles only
  >=DIALING leases - ACQUIRED grants exempt); dead-binding outbox
  cleanup moved from drain time to the killing/superseding mutation
  (surviving-lease commands retargeted to the new assignment_id,
  drain keeps only a defensive drop); configure_line(claimable=False)
  no longer cancels shared-pool dial-out tickets (only pending
  claims auto-reject); LineAdminKernel.provision_line creates
  quiescent UNBOUND lines and configure_line patches profile;
  CallEndReason.MAX_CALL_DURATION enforced by tick on DIALING+
  leases (created_at + effective cap, inadmissible on missed_call);
  provider checked_at/observed_at stamps are advisory dedupe keys
  only - only future skew beyond MAX_CLOCK_SKEW_SEC is normalized
  to kernel now, past stamps stay verbatim; handset-answered
  rings (call_state active + call_token on RINGING_LOCKED) surface
  as external_call BUSY with no lease/hangup/bridge, and ring-drain
  best-effort hangup only rides reject-path outcomes; 0x02 flush
  watermark defined in the agent uplink stream's own timestamp
  domain; forward-chain validation walks both directions at
  MAX_FORWARD_HOPS and revalidates dependents + hydration parity;
  idle_revoke_after bounded at 86400 s and last_seen_at stamped by
  owner ops + last-session drop_session only; queue HoL ticket at
  MAX_LEASES_PER_SESSION is skipped, preempted reissue exempt from
  ticket caps, pool tickets cancel when their eligibility set
  empties, idempotency dedup records outcomes; CallLease.profile +
  QueueTicket.profile (granted lease inherits the ticket label) +
  AdminLineView.profile/provider_meta added, provider_meta removed
  from LineSummary; get_lines(EXCLUSIVE_LINE) excludes UNBOUND;
  pending claims auto-reject on provider loss; forward target must
  resolve an existing line; remove_line refuses inbound forward
  refs and emits assignment_revoked for owned quiescent lines;
  SHARED_POOL revoke is INVALID_STATE and binding revoke idempotent;
  token/caller_id/media_endpoint fields bounded; acquire requires
  session dial_out intent; ring fanout is receive_inbound-scoped;
  free_lines predicate + BUSY->BUSY no-event rule; confirmed
  answer_confidence gated to active/disconnected; vhci-feeder
  answers HCI_Read_Buffer_Size locally (no double 0x0e); WirePlumber
  scoped off the oFono-owned HFP AG profile; loopback hci-agent auth
  model documented; webhook SSRF re-validates resolved IP at
  connect; docs/audits archives SA_carbon_review_18.
why: >-
  The blind post-freeze audit (SA_carbon_review_18, Gemini 4 Carbon
  High) found six kernel-contract blockers that the M1 implementer
  could not satisfy — an unexpressible degraded heartbeat rung, a
  media port that can only reissue both roles at once, an
  indistinguishable register/reregister ingress with a half-open
  supersede race, drain-time dead-binding cleanup whose teardown
  events missed the same-cycle drain_events, a claimable flag that
  destroyed live pool queues, and a missing line-provisioning
  surface — plus two media-protocol blockers (per-role socket loss
  vs whole-pair teardown, an incomparable 0x02 watermark clock) and
  a verified Major/Minor set including stale M1/M2 acceptance notes
  in .devin/roadmap.md that contradicted the frozen contract.
refs: issue #71, SA_carbon_review_18
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-heartbeat-liveness-ladder-is-elapsed-time-and-kernel-computed-round-28
- adds: docs/specs/line-manager.md#requirement-provider-side-stamps-are-advisory-only-round-28
- adds: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- adds: docs/specs/line-manager.md#requirement-dead-binding-cleanup-runs-at-the-mutation-round-28
- adds: docs/specs/line-manager.md#requirement-handset-answered-rings-surface-as-external_call-round-28
- adds: docs/specs/line-manager.md#requirement-line-lifecycle-admin-operations-round-28
- adds: docs/specs/line-manager.md#requirement-max_call_duration-is-enforced-by-tick-round-28
- adds: docs/specs/line-manager.md#requirement-idle-window-bound-and-activity-stamping-round-28
- adds: docs/specs/line-manager.md#requirement-the-0x02-flush-watermark-rides-the-agent-stream-clock-round-28
- modifies: docs/specs/line-manager.md#requirement-occupancy-and-liveness-are-orthogonal-axes-round-25
- modifies: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
- modifies: docs/specs/line-manager.md#requirement-ring-correlation-is-by-line-not-by-token-round-25
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-exclusive-line-privacy-is-filtered-at-the-edge-round-25-routing-fixed-round-27
- modifies: docs/specs/line-manager.md#requirement-forwarding-policies-are-loop-safe-and-bounded-round-25
- modifies: docs/specs/line-manager.md#requirement-ring-drain-after-a-fallback-decision-round-25
- modifies: docs/specs/line-manager.md#requirement-forward-via-line-carries-the-complete-dial-target
- modifies: docs/specs/line-manager.md#requirement-mediaallocator-lifecycle
