---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-27 paranoid audit fixes (SA_carbon_review_17, 80 findings triaged to
  the verified set): kernel owns dead-binding command teardown inside
  drain_provider_commands (ProviderRevokeCommand exempt while the control
  channel is open); LineSummary.assignment_id + assignment_granted replay on
  owner session open fix pre-assignment delivery; LineAdminKernel gains
  list_lines/list_leases/list_tickets/remove_line + AdminLineView; new
  provider.reattach_media ingress covers media-socket death on a live control
  channel; tick(now) is the sole timer entry sourced from the injected Clock;
  ring-drain docstrings aligned to ring_drain_ttl_sec; exclusive-line
  state/health events no longer broadcast (owner-agent + admin only);
  UNBOUND legal only as old_state of line_health_changed (unbound->reachable
  on first attach); pool ring redelivery to new receive_inbound sessions;
  ProviderActiveCall call_token/lease_id XOR; ProviderCallStateUpdate rejects
  ringing + caller_id; send_dtmf requires audio_ready in ALERTING too;
  LineAssignment.owner_agent_id rejects system:*; strict _check_id charset
  [A-Za-z0-9._-]; ProviderRegistration.token removed (adapter authenticates
  before distilling the fact) and meta persists onto ProviderBinding;
  voicemail_ref dropped; exported bounds (OUTBOX_SOFT_CAP, MAX_CLOSED_*,
  MAX_SESSIONS_TOTAL, MAX_DETAIL_LEN, MAX_REASON_LEN); reason/detail/profile
  strings bounded; AssignmentGrantedEvent mode=EXCLUSIVE;
  ProviderLiveness.state_snapshot only on reachable; halves-up
  heartbeat_interval_ms; typed PhonebridgeError from validate_e164_country;
  CallLease.audio_ready_received; MediaTransport literal; PairWindowRequest
  (<=300 s); configured UNBOUND lines listed but not claimable;
  ProviderRegistration.meta mirrored on LineSummary.provider_meta;
  check_changes_entry.sh validates fragment shape and check_spec_anchors.sh
  gains --base mode proving the anchored spec file changed.
why: >-
  The post-v0.0.21 paranoid audit (SA_carbon_review_17) found five blockers
  that would have broken the M1 implementer outright — a dead-binding drop
  rule the adapter could not execute, an assignment_id that could be lost
  before the owner ever connected, wire-admin ops with no kernel surface,
  no provider-side media recovery on a live control channel, and a dual
  Clock/tick time authority contradicting the single-timer rule — plus a
  round-24 docstring contradicting the normative ring_drain_ttl_sec spec
  and a tail of verified Major/Minor gaps; all are closed here so the
  Phase-0 contract is unambiguous before implementation starts.
refs: issue #67
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-exclusive-line-privacy-is-filtered-at-the-edge-round-25-routing-fixed-round-27
- modifies: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- modifies: docs/specs/line-manager.md#requirement-occupancy-and-liveness-are-orthogonal-axes-round-25
- modifies: docs/specs/line-manager.md#requirement-claim-and-assignment-results-reach-the-agent-round-25
- modifies: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
