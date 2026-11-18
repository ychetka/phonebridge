---
type: fix
scope: domain
user_visible: false
breaking: true
user_summary: "-"
dev_summary: >-
  Round-35 contract/specification hardening (issue #85, iteration-3
  architecture audit — all ACCEPTED findings plus confirmed PARTIAL
  residuals; contract surface only, no M1 runtime implementation):
  CallLease.media_epoch splits into per-role
  agent_media_epoch/provider_media_epoch on both the lease and
  MediaCredentials so a single-role reissue burns only the
  addressed role's token while the sibling socket keeps streaming
  under its own epoch; MEDIA_PAIR_TTL_SEC arming is per-role —
  {accepted:true} arms only unattached roles and a role-specific
  detach (socket close, DTMF pause-budget lapse) arms that role's
  re-attach window alone; a pending answer handshake surviving
  ANSWERING -> ACTIVE splits teardown scope at the ACTIVE
  boundary (ring-scoped reject before, lease-scoped
  line.hangup{lease_id} + network_error after, never a retconned
  missed_call), and a pre-dispatch PROVIDER_UNREACHABLE leaves
  call_token unconsumed on call.accept and the ACQUIRED grant
  unspent and reusable on call.dial; no line.* command is queued to a
  ws_disconnected/dead binding — TERMINATING preserves the
  teardown intent and provider.reregister re-mints the hangup on
  the new channel inside the supersede mutation; the ban reaches
  the saga path too: compensate_dial mints no hangup command on
  a dead binding while still ending the lease network_error, and
  compensate_answer/an {accepted:false} answer refusal mints no
  ring-scoped reject — the open ring carries the reject intent
  and provider.reregister re-mints it while the ring remains
  undrained;
  ProviderRegisterResult.auto_created_assignment exposes the
  register-minted LineAssignment for restore_line(line_id, None)
  compensation; plain provider.register on a new socket is a
  legal fresh supersede against orphaned or hydrated-dead
  bindings — never a same-binding recovery — and its snapshotless
  attach ends the orphan's live leases provider_lost atomically;
  drop_session and the max_call_duration tick path ride
  TERMINATING with the pending end reason preserved;
  provider_lost unifies every dead-binding end including
  dongle_lost while network_error stays
  compensation/deadline-only; MissedCallWakeEvent is the
  tokenless masked missed_call wake projection (the wake
  envelope validator rejects the crossed payload); stale,
  equal-generation and older-generation provider facts are
  record-only — they may raise the dedup watermark but mutate
  neither liveness nor last_seen_at and disarm no deadline, so a
  replayed reachable off a dead channel can never clear orphan
  grace; pool and preferred tickets survive while any eligible
  binding's orphan grace is open and cancel only after the last
  window lapses; a line_id-naming acquire defines preferred-line
  semantics (enqueue on RINGING_LOCKED/busy, LINE_BUSY_INCOMING
  without enqueue, PROVIDER_UNREACHABLE on unbound/dead
  bindings) and the preempted-grant reissue revalidates that
  eligibility before falling back to the pool or cancelling
  provider_lost; tokenless active/disconnected facts on a
  RINGING_LOCKED line resolve the open ring or mint the
  handset-answer external_call; a forward_via_line terminal
  second-leg failure is ordinary answered-lease teardown —
  never missed_call/no-pickup semantics — and owes no terminal
  announcement on the forwarding hop;
  exclusive-owner inbound delivery requires an owner session
  that declared receive_inbound; idempotent replay revalidates
  the recorded lease/ticket liveness before serving a stored
  outcome; has_more is computed by the adapter's locked two-call
  probe while the kernel returns bare lists; admin.issue_key
  gains subject_id? — required on admin-scope keys, forbidden
  elsewhere — so admin.list_keys always reports a bound subject;
  the DTMF pause-budget exhaustion semantic is unified as
  provider-role detach (resumed pauses never accumulate).
  Breaking pre-M1 contract deltas vs 0.0.29:
  CallLease.media_epoch and MediaCredentials.media_epoch are
  replaced by the REQUIRED per-role agent_media_epoch/
  provider_media_epoch fields; ProviderRegisterResult gains the
  auto_created_assignment field; wake-audience missed_call
  envelopes require MissedCallWakeEvent instead of
  MissedCallEvent; admin.issue_key accepts the new subject_id
  parameter.
why: >-
  The iteration-3 external audit returned a third fix round for
  pre-M1 contract contradictions; this round re-freezes the
  contract and normative docs at 0.0.30 with executable GWT
  coverage of the newly pinned edges.
refs: issue #85, iteration-3 findings registry
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-iteration-3-audit-hardening-pins-round-35
- modifies: docs/specs/line-manager.md#requirement-forward-via-line-carries-the-complete-dial-target
- modifies: docs/specs/line-manager.md#requirement-zero-subscriber-inbound-routing-uses-the-wake-audience-round-33
- modifies: docs/specs/line-manager.md#requirement-media-pair-ttl-arms-after-command-acknowledgement-round-34
- modifies: docs/specs/line-manager.md#requirement-saga-compensation-on-linedial-and-lineanswer
- modifies: docs/specs/line-manager.md#requirement-answer-compensation-is-ring-scoped-round-32-item-9
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-the-0x02-flush-watermark-rides-the-agent-stream-clock-round-28
- modifies: docs/specs/line-manager.md#requirement-provider-claimability-and-servability-are-distinct-predicates-round-34
- modifies: docs/specs/line-manager.md#requirement-provider-death-paths-use-provider_lost-and-never-queue-to-dead-bindings-round-34
