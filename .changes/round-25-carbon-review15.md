---
type: feat
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-25 architect audit (SA_carbon_review_15, 45 findings)
  consolidated ahead of M1: production deployment ratified as
  --network=host + /dev/vhci on a dedicated host (AF_BLUETOOTH is
  init_net-only; micro-VM kept as an optional post-MVP hardened
  profile) with a phonebridge doctor pre-flight; LineState split
  into occupancy (FREE/BUSY/RINGING_LOCKED) vs LivenessState +
  event.line_health_changed; claim_resolved / assignment_granted /
  queue_cancelled events deliver assignment_id and ticket fates;
  MediaCredentials split per role (agent/provider endpoints) with
  reattach_media token reissue on both sides; ring correlation by
  line_id (fallback tokens, snapshot reconcile, 1:1 ambiguity rule);
  global queue seq minted at acquire ingress; provider call_state
  echo/observed_at/checked_at closed vocabularies; system:*
  identities rejected externally; clear_idle_revoke_after /
  clear_max_call_duration tombstone flags; forward-cycle bound 8
  hops; ring_drain_ttl_sec + max_call_duration_sec timeouts;
  MEDIA_PAIR_TTL_SEC=30 with network_error teardown; DTMF
  timestamped 0x02 watermark flush + ALERTING early-media gate;
  exclusive-line privacy filtering in get_lines and redacted
  broadcast reasons; numeric JSON-RPC error codes + typed
  error_code (+UNAUTHORIZED/FORBIDDEN/SESSION_NOT_FOUND); tokens
  moved to non-string secret containers; tooling fixes
  (release_notes YAML block scalars, #requirement- spec anchors,
  version-tag cancel-in-progress, CI matrix 3.10-3.13,
  graceful-skip tooling tests, reserved example phone numbers).
why: >-
  Close the verified contract and deployment gaps the Carbon audit
  surfaced before M1-M5 implementation starts: eleven Blockers
  (undelivered assignment_id, token log leaks, non-JSON-RPC error
  codes, single media_endpoint, queue-seq underflow, restart/media
  re-attach races, missing admin drain) plus the major/minor set
  that would otherwise be re-litigated during implementation.
  Rejected on scope: DSP clock-drift resampling (30-45 ms jitter
  buffer absorbs it between bursts), DTLS connection-ID roaming
  (WireGuard covers V1), per-line bluetoothd SPOF split (YAGNI
  under thin-slice supervision).
refs: SA_carbon_review_15
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-accept-window-always-elapses-before-fallback
- modifies: docs/specs/line-manager.md#requirement-saga-compensation-on-linedial-and-lineanswer
- modifies: docs/specs/line-manager.md#requirement-effective-timeouts-hydrate-the-kernel-dtos
- adds: docs/specs/line-manager.md#requirement-ring-drain-after-a-fallback-decision-round-25
- adds: docs/specs/line-manager.md#requirement-occupancy-and-liveness-are-orthogonal-axes-round-25
- adds: docs/specs/line-manager.md#requirement-claim-and-assignment-results-reach-the-agent-round-25
- adds: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
- adds: docs/specs/line-manager.md#requirement-ring-correlation-is-by-line-not-by-token-round-25
- adds: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- adds: docs/specs/line-manager.md#requirement-exclusive-line-privacy-is-filtered-at-the-edge-round-25
- adds: docs/specs/line-manager.md#requirement-forwarding-policies-are-loop-safe-and-bounded-round-25
- adds: docs/specs/line-manager.md#requirement-wire-secrets-are-non-string-containers-round-25
