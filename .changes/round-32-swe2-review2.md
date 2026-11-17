---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-32 audit fixes (SA_swe2_review_2, 27 findings triaged,
  five fix batches across contract/tests/api/design/hci/tools):
  hangup gains the TERMINATING leg - call.hangup on an in-flight
  phase parks the lease until the provider's disconnected or the
  hangup_hold_sec lapse (default 10 s, bound 1..120) ends it
  network_error kernel-side, only ACQUIRED grants end directly;
  the outbox fan-out mints ONE FanoutAudience envelope per
  multi-session delivery and tick deadline processing is chunked
  under MAX_TICK_DEADLINES with next-tick continuation (the
  256-deadline livelock fix); durable writes ride a pending-write
  marker with bounded admin waits and a guarded restore_line that
  degrades to config_stale + admin alert when the record was
  re-mutated instead of blindly restoring a stale snapshot;
  ensure_aware_utc bounds stamps absolutely (now +- window) so an
  RTC-less provider can never poison dedup keys or deadline
  arithmetic; line.answer failure rides the ring-scoped
  compensate_answer(line_id, assignment_id, lease_id, call_token)
  entry (hangup-on-call_token reject vehicle, ANSWERING lease
  ends network_error, line stays RINGING_LOCKED until drain) -
  distinct from lease-scoped hangup-first compensate_dial, which
  now dedupes against TERMINATING; external_call attribution is
  evidence-agreement based - a present-and-conflicting
  provider_call_id/caller_id/direction mints a NEW external_call
  record instead of guessing; provider.audio_lost is latch
  bookkeeping only (it never arms MEDIA_PAIR_TTL_SEC while the
  socket is attached and never produces event.media_overflow -
  that event is minted solely by ingest_media_overflow);
  provider-loss teardowns unify on provider_lost and a dead or
  tombstoned binding receives NO provider commands at all;
  listings carry has_more/cursor and admin.list_keys paginates
  like every other admin list; provider_bindings rows are a
  best-effort cache - a failed write is config_stale + admin
  alert with no kernel compensation (restore_line is
  LineAssignment-only), last_seen_at persists debounced at >=30 s,
  stamp watermarks persist on the row, channel_seq reseeds
  max(stored)+1 across restarts, and the hydrated-dead cascade
  suppresses external escalation hooks; the queue re-check
  trigger set is pinned (lease end, provider register,
  capability update, claim/assignment release, enqueue, tick
  expiry) and QueueTicket.intent is model-validated to
  DIAL_OUT; idempotency records expire at
  IDEMPOTENCY_TTL_SEC=3600 under a 256-entry per-session LRU and
  a same-key different-params retry is IDEMPOTENCY_CONFLICT,
  while register_intent returns the existing session only for an
  identical (agent_id, intents, profile) triple on the same
  socket; provider_call_id threads from line.dial's reply
  through DialResult to the ring-identity/evidence checks;
  announcement_ref rejects '..' traversal and leading '/' via
  the _CheckedAnnouncementRef grammar; all bounded
  strings/tokens reject surrogate and control characters and
  MediaCredentials mints two DISTINCT role tokens; a CallLease
  carries grant_expires_at only while ACQUIRED (the consumed
  deadline is None on every later phase); the api drift test is
  un-exempted onto the admin.* cells, ProviderRegistration.meta
  mirrors immutably onto the binding, and orphan_deadline_at
  reaches the admin projections; docs/hci-agent.md pins loopback
  media to a CONNECTED SOCK_STREAM UDS at
  /run/phonebridge/media/<line_id>.sock with SO_PEERCRED at
  accept() (feeder listens, agent connects, uint16 rec_len
  framing), rewrites MTU alignment - hci_vhci fixes sco_mtu at
  adapter init so the feeder answers the kernel's first
  HCI_Read_Buffer_Size LOCALLY with target-profile geometry
  (60 mSBC / 48 CVSD) while the real 0x1005 is still proxied for
  geometry learning only, and restricts the agent-originated H4
  allowlist to 0x02/0x03/0x04 (0x01 commands flow only
  kernel-to-agent); tools hardening - check_version_tag verifies
  the REMOTE tag marks the remote main tip and that main's
  pyproject carries the same version (a merge without version
  bump can no longer pass against the previous tag),
  check_spec_anchors --base requires per-FILE coverage (every
  changed docs/specs/*.md file needs at least one in-diff
  anchor), and release_notes unquotes multiline quoted scalars
  correctly and passes generated notes through a temp file
  instead of the GENERATED env var (MAX_ARG_STRLEN).
why: >-
  The swe-2 second-pass audit (SA_swe2_review_2, issue #79)
  found a hangup path that ended leases before modem teardown
  was confirmed, write-through compensation that could revert
  newer kernel state with a stale snapshot, provider-binding
  persistence treated as a second source of truth, provider-loss
  reasons split across two spellings, a kernel-side SCO MTU
  answer that proxied geometry the virtual adapter could never
  renegotiate, and tooling that verified tags against local
  ancestry only - plus a run of smaller contract/test drift;
  all are closed ahead of the M1 implementation milestone so the
  Phase-0 contract, its spec, the wire docs and the tool gates
  are unambiguous.
refs: issue #79, SA_swe2_review_2
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-answer-compensation-is-ring-scoped-round-32-item-9
- adds: docs/specs/line-manager.md#requirement-register_intent-idempotency-matches-the-recorded-identity-triple-round-32
- modifies: docs/specs/line-manager.md#requirement-call-lease-phases-and-the-grant-ttl-round-31
- modifies: docs/specs/line-manager.md#requirement-provider-hangup-targets-exactly-one-identifier
- modifies: docs/specs/line-manager.md#requirement-saga-compensation-on-linedial-and-lineanswer
- modifies: docs/specs/line-manager.md#requirement-outbox-soft-cap-pre-flight-and-drain-discipline
- modifies: docs/specs/line-manager.md#requirement-line-lifecycle-admin-operations-round-28
- modifies: docs/specs/line-manager.md#requirement-provider-side-stamps-are-advisory-only-round-28
- modifies: docs/specs/line-manager.md#requirement-ring-correlation-is-by-line-not-by-token-round-25
- modifies: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
- modifies: docs/specs/line-manager.md#requirement-the-audio_ready-latch-is-kernel-side-media-state-round-31
- modifies: docs/specs/line-manager.md#requirement-mediaallocator-lifecycle
- modifies: docs/specs/line-manager.md#requirement-tick-runs-the-orphan-cascade-atomically-round-31
- modifies: docs/specs/line-manager.md#requirement-dead-binding-cleanup-runs-at-the-mutation-round-28
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-forwarding-policies-are-loop-safe-and-bounded-round-25
- modifies: docs/specs/line-manager.md#requirement-wire-secrets-are-non-string-containers-round-25
- modifies: docs/specs/line-manager.md#requirement-dial-target-and-caps-validation-is-model-level
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-occupancy-and-liveness-are-orthogonal-axes-round-25
- modifies: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- modifies: docs/specs/line-manager.md#requirement-max_call_duration-is-enforced-by-tick-round-28
- modifies: docs/specs/line-manager.md#requirement-the-0x02-flush-watermark-rides-the-agent-stream-clock-round-28
- modifies: docs/specs/line-manager.md#requirement-idle-window-bound-and-activity-stamping-round-28
- modifies: docs/specs/line-manager.md#requirement-heartbeat-liveness-ladder-is-elapsed-time-and-kernel-computed-round-28
