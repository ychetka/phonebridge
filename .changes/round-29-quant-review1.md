---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-29 Quant Partner audit fixes (SA_quant_review_1, 39 findings
  triaged to the verified set): kernel-driver surface hardened -
  tick/decide_fallback/drain_* are documented kernel-internal and
  adapters MUST NOT route wire calls to them; ring-drain sub-phase
  after missed_call declared kernel-internal bookkeeping
  (RINGING_LOCKED stays wire-visible); AnswerAndAnnounceFallback
  eligibility pinned as a decision-time normative requirement;
  single _clamp_provider_stamp helper unifies advisory-stamp
  normalization (only future skew > MAX_CLOCK_SKEW_SEC clamps);
  reregister determinism - always supersedes the current live
  binding (last-writer-wins, racing reregisters resolve
  sequentially), provider.* facts on superseded assignment_id
  resolve STALE_TOKEN and the adapter MUST close the superseded
  socket; SNAPSHOT_RECONCILED_PHASES exports the dialing-or-later
  set; ProviderBinding.heartbeat_interval_ms bounded le=86400000;
  drop_session ordering - allocator side-effects complete inside
  the mutation before drained events; bounded list pagination
  (offset/limit<=256, MAX_LIST_PAGE) on admin listings and
  get_lines; dedupe scope pinned to (assignment_id, call, stamp);
  MAX_IDEMPOTENCY_KEYS_PER_SESSION=256 fail-closed;
  tick(backwards now) MUST reject INVALID_ARGUMENT;
  MAX_LINES_TOTAL=256; media.auth token consume declared atomic
  (one auth_ok, concurrent losers rejected); /dev/vhci open+index
  discovery serialized under a per-host flock; 0x02 watermark
  discard scoped to undelivered buffered frames; worker-dies-
  mid-forward rides the ordinary dead-man path; feeder/LineProvider
  sibling liveness -> dongle_lost; webhook SSRF pins the
  resolved IP at connect; single control socket = one live
  binding; partial TCP frame = transport error, never written to
  vhci; state-file atomic write = tmp+fsync+rename; admin
  mutations serialized single-writer. Tests: per-role reissue
  isolation + never-allocated negative, RedactedString hash/eq
  consistency, explicit wire-None phase mapping, id-carrier
  boundary lengths, aggregate-limit consistency, reconciled-phases
  pin, clamp helper pin, heartbeat_ms bound, backwards-tick pin.
why: >-
  The Quant Partner paranoid audit (SA_quant_review_1,
  claude-sonnet-5 software-architect, three stateless slices) found
  39 defects: the dominant domain pattern was critical semantics
  living only in prose (driver-vs-wire surface, drain sub-phase,
  fallback eligibility, skew clamp) plus real wire/design gaps
  (unpaginated listings vs the 64 KiB frame cap, post-supersede
  fact acceptance, media.auth TOCTOU, /dev/vhci open races,
  unbounded per-session dedup, SSRF TOCTOU) and test-coverage
  blind spots on the per-role reissue invariant and wire mappings.
refs: issue #73, SA_quant_review_1
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-driver-methods-are-kernel-internal-round-29
- adds: docs/specs/line-manager.md#requirement-fallback-eligibility-is-evaluated-at-decision-time-round-29
- modifies: docs/specs/line-manager.md#requirement-ring-drain-after-a-fallback-decision-round-25
- modifies: docs/specs/line-manager.md#requirement-provider-side-stamps-are-advisory-only-round-28
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-line-lifecycle-admin-operations-round-28
- modifies: docs/specs/line-manager.md#requirement-forwarding-policies-are-loop-safe-and-bounded-round-25
- modifies: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- modifies: docs/specs/line-manager.md#requirement-mediaallocator-lifecycle
