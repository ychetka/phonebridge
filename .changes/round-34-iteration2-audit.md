---
type: fix
scope: domain
user_visible: false
breaking: true
user_summary: "-"
dev_summary: >-
  Round-34 contract/specification hardening (issue #83, iteration-2
  architecture audit — 16 accepted, 9 partial findings; contract
  surface only, no M1 runtime implementation): pending_command now
  survives forward phase advances (dial on DIALING/ALERTING/ACTIVE,
  answer on ANSWERING/ACTIVE) so a late ack still records
  provider_call_id; the withheld call.dial/call.accept reply is
  terminally PROVIDER_UNREACHABLE when teardown wins the ack race —
  dead-lease credentials are never delivered; the servable
  predicate (SERVABLE_LIVENESS_STATES = reachable/degraded) splits
  from claimable — ws_disconnected stays claim-visible but never
  servable, no line.dial/line.answer until a newer
  provider.reregister — AdminLineView gains
  orphan_deadline_at mirrored from the live binding, and
  ws_disconnected recovery requires a strictly newer channel_seq;
  provider death unifies on provider_lost and dead bindings accept
  no queued commands; media pair TTL arms at {accepted:true} with
  media_epoch carried on MediaCredentials; live call-state dedup
  scopes per (line_id, call_identity, accepted_stamp) under a
  bounded 64-entry watermark table; external_call mints only on
  call-present states with the flat UNMATCHED_CALL_SAFETY_SEC
  safety net;
  pending_ring tokens return through pending_rings incl.
  provider.ping; WakeAudience gains kernel-visible wake_hooks,
  masked-str caller_id and exclusive-line zero-session routing;
  event.queue_updated coalesces per ticket per mutation, bounded
  by affected tickets; call.release rides
  the terminating leg; forward_via_line's second leg takes one
  bounded queued acquire on BUSY/RINGING_LOCKED and
  answer_and_announce supplies the terminal announcement;
  register_provider's
  auto-created assignment counts MAX_LINES_TOTAL with write-through
  persistence; _FrozenMeta replaces MappingProxyType for provider
  metadata (JSON/deepcopy/pickle/log-mask safe); provider.ping
  returns promoted pending_ring tokens; second-socket register
  wording resolved; FanoutAudience rejects duplicate members and
  fallback_triggered binds to system:fallback_worker; idempotent
  replay limited to granted/queued outcomes; expired grants answer
  RESOURCE_EXHAUSTED/GRANT_TTL_EXPIRED; the canonical has_more
  formula (rows remain after the returned slice) and
  context-bound opaque cursors; split /run/phonebridge-private
  mount; masked hook payload + HMAC fields; the DTMF pacer pause
  bounded at DTMF_PAUSE_BUDGET_MS = 30 000 ms
  (MAX_DTMF_STREAM_PAUSE_SEC = 30 s, covering the 32-digit
  send_dtmf maximum); hci-agent 0x1005 answered locally on every
  issue with ncmd=1, real response owned by the feeder, fixed
  geometry across SCO reopen; typed DIAL_REJECTED sub-codes, queue
  edge pins, last_seen_at assign_line stamping and
  paired_phone_mac uniqueness; false-positive tests, fake-kernel
  shapes, spec-anchor/release-note tooling and stale CHANGELOG
  fixed. Breaking pre-M1 contract deltas vs the pre-review shape:
  CallEndReason.ADMIN_REVOKED is removed (provider-binding
  revocation surfaces through line.revoke plus the
  line_state_changed/provider_lost paths, never as a lease
  teardown reason), IncomingCallWakeEvent.caller_id is retyped to
  the masked plain-str projection (the pinned
  .{0,4}****.{0,2} shape - raw caller strings no longer
  validate), MediaCredentials.media_epoch is a new REQUIRED >=1
  field on every minted pair, and
  LineManager.note_provider_liveness now returns
  tuple[PendingRing, ...] for the provider.ping reply instead of
  None.
why: >-
  The iteration-2 external audit returned STOP for M1 until these
  contract contradictions land; this round hardens the executable
  contract and the normative docs without touching runtime code
  outside the contract surface.
refs: issue #83, iteration-2 findings registry
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-provider-claimability-and-servability-are-distinct-predicates-round-34
- adds: docs/specs/line-manager.md#requirement-provider-death-paths-use-provider_lost-and-never-queue-to-dead-bindings-round-34
- adds: docs/specs/line-manager.md#requirement-media-pair-ttl-arms-after-command-acknowledgement-round-34
- adds: docs/specs/line-manager.md#requirement-queue-update-fanout-is-mutation-coalesced-round-34
- modifies: docs/specs/line-manager.md#requirement-provider-command-acknowledgements-are-kernel-facts-round-33
- modifies: docs/specs/line-manager.md#requirement-answer-pending-rings-suppress-the-fallback-decision-round-33
- modifies: docs/specs/line-manager.md#requirement-unqualified-provider-state-attribution-needs-positive-evidence-round-33
- modifies: docs/specs/line-manager.md#requirement-provider-stamp-watermarks-are-durable-and-line-scoped-round-33
- modifies: docs/specs/line-manager.md#requirement-zero-subscriber-inbound-routing-uses-the-wake-audience-round-33
- modifies: docs/specs/line-manager.md#requirement-listing-cursors-are-adapter-owned-and-fail-closed-round-33
- modifies: docs/specs/line-manager.md#requirement-forward-via-line-carries-the-complete-dial-target
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-the-0x02-flush-watermark-rides-the-agent-stream-clock-round-28
- modifies: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
