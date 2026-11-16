# Spec — LineManager contract (Phase-0 freeze)

Normative requirements for `src/phonebridge/domain/contract.py`,
frozen at round 28 (architect reviews `SA_carbon_review_13`,
`SA_carbon_review_15`, `SA_carbon_review_16`, `SA_carbon_review_17`,
`SA_carbon_review_18`)
ahead of implementation milestones M1–M5. Prose sections carry the
requirements; each is cited by `.changes` fragments via its heading
anchor (see `docs/specs/README.md`).

### Requirement: Provider hangup targets exactly one identifier

`ProviderHangupCommand` (wire `line.hangup
{assignment_id, lease_id?, call_token?}`) MUST carry exactly one of
`lease_id` or `call_token`, enforced by a model validator: a
`lease_id` targets a live CallLease, a `call_token` rejects a
still-unanswered ring so the `RejectFallback` policy is executable
when no lease exists yet.

### Requirement: Forward-via-line carries the complete dial target

`ForwardViaLineFallback` MUST carry `forward.line_id`,
`target_number` (strict E.164, `RedactedString`) and
`target_country` (ISO alpha-2), pre-validated at `configure_line`
with the same checks `call.dial` applies, because the FallbackWorker
holds no admin scope to resolve missing pieces at runtime. The
`forward.line_id` target MUST resolve to an existing configured line
(`LINE_NOT_FOUND`, round 28) — a dead forward target is a config
defect, not a deferred discovery. A
self-loop (`forward.line_id` equal to the configured line) MUST be
rejected at `configure_line`.

### Requirement: Outbox soft-cap pre-flight and drain discipline

The outbox is a TWO-LEVEL bounded model (round 30). Only
agent-initiated resource-creating operations (`acquire`, `dial`,
`accept`, `claim_exclusive`) run the soft-cap pre-flight: they MUST
check remaining outbox capacity BEFORE mutating state and reject
with `RESOURCE_EXHAUSTED` only when the outbox ALREADY holds >=
`OUTBOX_SOFT_CAP` (256) entries — the check guards new ingress,
never a projected fanout. Provider facts, admin mutation cascades,
event fanout and teardown paths are EXEMPT from the soft cap:
`hangup`, `release`, `release_assignment`, `drop_session`,
`ingest_call_state` teardowns, provider-loss, `tick` expiries and
the admin mutations `revoke_assignment`/`remove_line` (plus their
lease/claim cascades) MUST never deadlock on the soft cap — they
are bounded by `OUTBOX_HARD_CAP`
(`OUTBOX_SOFT_CAP + MAX_SESSIONS_TOTAL + MAX_LINES_TOTAL +
MAX_SESSIONS_TOTAL * MAX_LEASES_PER_SESSION` = 5120, round 30), the
final bounded-capacity guard EVERY mutation checks BEFORE
committing partial state; overflow there is `RESOURCE_EXHAUSTED`.
`ingest_ring` event fanout is bounded by `MAX_SESSIONS_TOTAL`
sessions and admin cascades by `MAX_LINES_TOTAL` lines plus the
live-lease bound. The adapter MUST call
`drain_events()`/`drain_provider_commands()` after every kernel
call.

### Requirement: Saga compensation on line.dial and line.answer

The adapter MUST await the provider's `{accepted}` reply to
`line.dial`/`line.answer` with a ~2 s deadline. On
`{accepted:false}`, timeout or transport error the adapter MUST
compensate by calling `hangup()` FIRST (round 30): the
`ProviderHangupCommand` that call enqueues IS the best-effort
air-side teardown — a `dial`/`answer` that timed out on the wire
may still have executed on the modem, and an uncompensated air-side
call would outlive its lease as a ghost ring — and the lease ends
atomically with `network_error` so it never wedges in DIALING.
Synthesizing a `disconnected` `ingest_call_state` fact is
FORBIDDEN: the provider never reported it, and a fabricated fact
would confuse dedup; a late provider fact about the ended lease is
deduplicated through the `_closed_leases` ring afterwards (a
dedupe/no-op, never `STALE_TOKEN`).

### Requirement: Idle escalation is alert-only

`idle_revoke_after` expiry MUST emit `event.idle_alert`
(`IdleAlertEvent`, admin audience only, at most once per continuous
idle period) and MUST NOT emit `event.assignment_revoked` —
assignment revocation is produced ONLY by explicit revoke paths
(owner `call.release{assignment_id}` or admin
`revoke_assignment`/`line.revoke`).

### Requirement: Accept window always elapses before fallback

The kernel MUST let the full `accept_window_sec` elapse even when
zero live sessions exist for the line, so configured wake-and-grant
hooks can wake a sleeping agent. `tick(now)` is the ONLY timer
driver — the kernel itself MUST NEVER sleep to implement the wait:
when `now >= ring.expires_at` the kernel runs the fallback decision
inside `tick(now)`, and a direct `decide_fallback` call before
`expires_at` MUST raise `PhonebridgeError(INVALID_STATE)`. When the
second timer (`fallback_accept_ttl_sec`) expires without a worker
`call.accept`, the kernel MUST burn the fallback token and emit
`MissedCallEvent(reason=ACCEPT_WINDOW_EXPIRED)`;
`event.fallback_triggered` carries `expires_at` bounded by
`fallback_accept_ttl_sec`. The line then follows the ring-drain
rule below — it does NOT return to `FREE` while the physical call
still rings.

### Requirement: Ring drain after a fallback decision (round 25)

After the fallback decision (`no_pickup` chosen or the
`fallback_accept_ttl_sec` lapsed) the kernel emits
`event.missed_call` immediately but MUST keep the line
`RINGING_LOCKED` until the provider reports the ring's
`disconnected` — or a bounded `ring_drain_ttl_sec` (default ~60 s)
elapses — whichever comes first; a best-effort `line.hangup` MAY be
issued to shorten the drain ONLY on paths where the GATEWAY rejects
the call (`reject` fallback policy, round 28) — it is never queued
on a `no_pickup`/missed outcome, where the caller already left, and
never on the handset-answered `external_call` path. Rationale: handing a physically still-
ringing line to the next `call.dial` would collide with the live
air-side call, and a later `state_snapshot` would re-register the
same ring as a NEW `incoming_call`. The drain sub-phase itself is
KERNEL-INTERNAL bookkeeping (round 29): once `missed_call` is
emitted the interval awaiting `disconnected` or
`ring_drain_ttl_sec` carries no wire-visible marker —
`LineState.RINGING_LOCKED` remains the reported state throughout,
and implementations MUST track the sub-phase in private kernel
state; the contract deliberately adds no public enum rung or
field for it. Burned-token edge cases inside the drain (round 30):
(a) a handset answering during the drain sends `state:"active"`
correlated by the echoed BURNED `call_token` — the kernel MUST
transition the line to `BUSY`/`external_call`; the burned ring
token stays a CORRELATION key for that ring's lifecycle facts
until the line is FREE and is NEVER an action token; (b) a
genuinely NEW `ingest_ring` while the drain is pending MUST be
recorded as the line's `pending_ring` — dedupe is by ring
identity — and `ingest_ring` during the drain MUST NOT return the
burned token (it returns `None`); when the drain completes the
kernel MUST mint a fresh `call_token` and emit a new
`IncomingCallEvent` for the parked ring; (c) a `disconnected`
update correlated by the echoed burned ring token ends the
`external_call` — or the pending drain itself — and MUST return
the line to `FREE`, never `STALE_TOKEN`.

### Requirement: Driver methods are kernel-internal (round 29)

`tick`, `decide_fallback`, `drain_events` and
`drain_provider_commands` are KERNEL-DRIVER methods, not wire
methods — a transport adapter MUST NOT route any wire RPC to a
driver method, and the wire surface is exclusively the methods
mapped in `docs/api-contract.md` (`agent.*`/`call.*`/`provider.*`
on `LineManager`, `admin.*` on `LineAdminKernel`). The only legal
invocation of `decide_fallback` is the kernel's own deadline path
inside `tick`; `tick` is driven by the runtime's clock loop; the
drains are called by the adapter after kernel calls, never in
response to a wire request. A wire-facing route named like a
driver method — or a dispatch table built by iterating the ABC's
methods — is a contract violation. `tick(now)` additionally
MUST reject a backwards `now` with
`PhonebridgeError(INVALID_ARGUMENT)`: monotonicity is a hard
invariant, not an implementation option.

### Requirement: Fallback eligibility is evaluated at decision time (round 29)

The `answer_and_announce` eligibility predicate MUST be evaluated
inside `decide_fallback` on LIVE kernel state at decision time —
it cannot be model-enforced because the policy model cannot see
sessions. The policy is eligible when the line's
`assignment.mode == EXCLUSIVE` OR at least one live session holds
`Intent.RECEIVE_INBOUND` on it (the `system:fallback_worker`
session itself is excluded from that count); when neither holds,
`decide_fallback` MUST degrade to `NoPickupFallback` instead of
dispatching an announcement the worker could never execute.

### Requirement: Occupancy and liveness are orthogonal axes (round 25)

`LineState` is the OCCUPANCY axis only — `FREE`/`BUSY`/
`RINGING_LOCKED`; `RESERVED` has no transitions and is removed, and
`DEGRADED`/`DOWN` are removed because a missed heartbeat MUST never
overwrite a call fact (a `BUSY` line stays `BUSY` while its
provider degrades). Provider health lives on the separate
`LivenessState` axis (`reachable`/`degraded`/`ws_disconnected`/
`unreachable`, plus the listing-only `unbound` for a line with no
provider attached — legal as `event.line_health_changed`
`old_state` solely on the first-attach `unbound -> reachable`
transition, never as `new_state` or a reported fact, round 27;
`degraded` is the kernel-computed middle rung of the heartbeat
ladder `reachable -> degraded -> unreachable`, entered only from
`reachable` and resolving into `reachable`, `unreachable` or
`ws_disconnected` (a control-socket drop overrides the
elapsed-time rung, round-28 fix), never a provider-reported
fact, round 28),
surfaced via `event.line_health_changed`; liveness
transitions drive teardown policy (orphan grace vs immediate
hangup), never occupancy.

### Requirement: Claim and assignment results reach the agent (round 25)

`call.claim_exclusive` resolves ASYNCHRONOUSLY: the kernel MUST emit
`event.claim_resolved{claim_id, line_id, status, assignment_id?,
reason?}` to the claiming session on approval AND rejection, and on
approval additionally `event.assignment_granted{assignment_id,
line_id, mode}` to the new owner — the primary path delivering
the `assignment_id` an owner needs for
`call.release{assignment_id}` (an undelivered `assignment_id` makes
a granted exclusive line un-releasable). `admin.assign_line` grants
emit `event.assignment_granted` identically. Delivery MUST NOT
depend on the owner having a live session at grant time (round
27): a pending grant is replayed to an owner session on
`register_intent`, and `call.get_lines` fills
`LineSummary.assignment_id` on the owner's own EXCLUSIVE rows as
the durable read fallback. A still-PENDING claim MUST auto-resolve
`event.claim_resolved{status:"rejected", reason:"provider_lost"}`
when its line's provider binding dies (round 28) — a dead line
cannot honour an exclusive owner, so the claim MUST NOT sit pending
until a moderator notices; the same auto-rejection covers a
`claimable:false` flip and a line becoming owned.

### Requirement: Media credentials are split per role and reissuable (round 25)

`MediaCredentials` MUST carry SEPARATE `agent_media_endpoint` /
`provider_media_endpoint` pairs (local UDS vs WAN WSS coexist), and
the one-shot per-role tokens MUST be reissuable without tearing
down the call: `line.reattach_media` feeds fresh provider
credentials to a lease preserved across `provider.reregister`,
`provider.reattach_media` covers the same provider-side reissue
when the provider's media socket dropped while its control
channel stayed alive (round 27 — the reregister path cannot cover
it, it requires a control-socket drop first), and
`call.reattach_media` feeds fresh agent credentials when the
agent's media socket dropped while control stayed alive. A burned
one-shot token MUST never be required twice. Reissue is PER-ROLE
(`MediaAllocator.reissue`, round 28): the old token of the
addressed role MUST be invalidated at the issue instant, the other
role's token and socket MUST stay untouched — the two halves of a
pair have independent lifetimes. A media socket dropping therefore
MUST invalidate only its own role's credentials and close only its
own socket, never tear down the surviving role; an ESTABLISHED pair
that loses a half gets a `MEDIA_PAIR_TTL_SEC` (30 s) re-attach
deadline for the detached role to re-auth, and the lease MUST end
with `event.call_state_changed{reason:"network_error"}` when the
window lapses — a live call never silently loses its audio
channel. The media adapter reports attachment facts through
`ingest_media_state(lease_id, role, attached)` (round 30): it MUST
call it with `attached:true` after a successful `media.auth` for
that role and with `attached:false` when that role's media socket
closes — a detach arms a fresh `MEDIA_PAIR_TTL_SEC` window, an
attach disarms it, `MediaAllocator.reissue()` re-arms a fresh
window from the reissue instant, and `tick()` enforces the lapse
as the `network_error` teardown above.

### Requirement: Ring correlation is by line, not by token (round 25)

At most ONE open ring exists per line, so ring correlation MUST be
by `line_id`: a `disconnected` update carrying NEITHER `call_token`
nor `lease_id` on a `RINGING_LOCKED` line resolves to that line's
one open ring (covers snapshot-discovered rings and
`fallback_call_token` re-issue); the provider echoes the
gateway-minted `call_token` whenever it has one. A
`state_snapshot` call with `lease_id:null` binds to the line's
single orphaned lease under the 1:1 ambiguity rule (the provider
keeps a `/run` state file for exact re-binding after its own
restart) — the uncorrelated shape is legal ONLY when exactly ONE
orphaned lease or external call exists on the line: zero
candidates is meaningless and multiple candidates is ambiguous, so
the kernel MUST reject the fact `INVALID_ARGUMENT` rather than
guess (round 30). A snapshot element with `state:"ringing"` MUST
be processed through the `ingest_ring` path — the kernel mints a
`call_token` for it and returns it inside
`ProviderRegisterResult.pending_rings` so the adapter can relay it
to the provider (deduplicated by ring identity, never re-minted).

### Requirement: Queue sequence is minted at acquire ingress (round 25)

A single global monotonic `seq` is allocated to EVERY `call.acquire`
at ingress — including immediate grants, refusals, and deduplicated
retries (round 27; sequence gaps are expected and carry no
meaning) — so a preempted
grant-but-undialed lease re-enters the queue head with its ORIGINAL
sequence and FIFO fairness survives preemption; signed `seq` space
is not required because every participant already holds a value.
`position` reported on tickets/`event.queue_updated` is the GLOBAL
FIFO rank across all eligible tickets — never a per-line rank
(round 27).
Server-side ticket removal (exclusivity, provider loss, capability
removal, claim resolution) MUST emit `event.queue_cancelled` to the
ticket's session. Grant liveness (round 28): a head-of-line ticket
whose session already sits at `MAX_LEASES_PER_SESSION` MUST be
SKIPPED on each grant attempt — re-evaluated per attempt, never
parked — so one saturated session cannot block the queue; a
preemption reissue MUST NOT re-check queue caps; a POOL ticket (no
`line_id`) MUST be cancelled `event.queue_cancelled{reason:
"provider_lost"}` the moment the set of servable SHARED_POOL
`dial_out` lines becomes empty — re-checked on every liveness,
caps and assignment mutation — while a `claimable` flip alone
MUST NOT cancel queued tickets (claims auto-reject, tickets live
on). Acquire dedup MUST record the OUTCOME of a keyed request —
the granted `lease_id` or queued `ticket_id` — so a same-key retry
returns the recorded outcome while the object lives, and the same
key with different params MUST be rejected `INVALID_ARGUMENT`.
The per-session dedup table is BOUNDED (round 29): at most
`MAX_IDEMPOTENCY_KEYS_PER_SESSION` (256) recorded outcomes per
live session — a new key beyond the cap MUST be rejected
`RESOURCE_EXHAUSTED` fail-closed, and the table is never
LRU-trimmed (evicting a recorded outcome would let a same-key
retry re-execute an acquire).
An `acquire(profile?)` label MUST be recorded on the
`QueueTicket` for a queued acquire, and the `CallLease` minted
when the ticket is granted MUST inherit the ticket's `profile`
verbatim — the label survives queueing (round-28 fix).
`cancel_acquire` is OWNERSHIP-CHECKED like every ID-keyed
operation (round 30): it MUST look the ticket up and verify
`session_id` ownership BEFORE idempotent no-op handling — a
session that never held the ticket gets `OWNER_MISMATCH`
fail-closed, never a silent no-op — and a ticket id that was
never queued is an idempotent no-op: the method signature is
`-> None` and the wire answer is `{ok}`, never an error.

### Requirement: Exclusive-line privacy is filtered at the edge (round 25; routing fixed round 27)

`call.get_lines` MUST omit other agents' EXCLUSIVE lines entirely
(the claimable pool view is unaffected); `event.line_state_changed`
and `event.line_health_changed` for an agent-owned EXCLUSIVE line
MUST be routed ONLY to the owner's `agent` audience plus an
`admin` copy — broadcast is FORBIDDEN entirely, because even a
reason-redacted broadcast still leaks the private line's activity
(supersedes the round-25 broadcast-with-redacted-reason scheme);
SHARED_POOL lines keep broadcast routing. The agent-facing
`LineSummary` MUST NOT carry `provider_meta` (round 28) — provider
hardware metadata is privileged and belongs to the admin-scoped
`AdminLineView` only. The `intent:"exclusive_line"` filter MUST
exclude `unbound` rows (round 28 — a providerless line cannot
serve a new exclusive owner), and `free_lines` hints MUST list
only servable alternatives: SHARED_POOL lines advertising
`dial_out` whose binding liveness is neither `unbound` nor
`unreachable` (round 28).

### Requirement: Forwarding policies are loop-safe and bounded (round 25)

`configure_line` MUST reject a `forward_via_line` policy creating a
self-loop AND detect transitive cycles (`A → B → … → A`) — the
chain check MUST cover the full chain through the new edge in BOTH
directions (round 28): the stored predecessors forwarding INTO the
line plus the forward traversal of the target chain, so a cycle
cannot be closed from either end; the walk stays bounded at 8 hops
(contract constant
`MAX_FORWARD_HOPS = 8`, contract.py — round 26) — a forward loop would
otherwise spin fallback workers forever. Both failure cases are
`INVALID_ARGUMENT` but MUST carry distinguishing `data.detail`
text (round 29): "forward cycle" when the walk closes a loop vs
"forward chain exceeds MAX_FORWARD_HOPS" when it runs out of hops
on an open chain. On a SUCCESSFUL policy
mutation every line whose stored chain passes through the changed
one MUST be revalidated too (round 28 — a `fallback_policy` patch
or removal must not leave a dangling or over-deep downstream
chain), and `admin.remove_line` MUST refuse while another line's
`forward_via_line` still targets the deleted one (round 28).
`max_call_duration_sec`
(`None` = uncapped, ≤ 86400 s when set) bounds every call's
wall-clock length and the `record_and_telegram` recording length;
`clear_max_call_duration:true` removes a set cap and is
`INVALID_ARGUMENT` together with a value (same PATCH-tombstone
pattern as `clear_idle_revoke_after`).

### Requirement: Wire secrets are non-string containers (round 25)

`CallToken`/`MediaToken` MUST be `RedactedString`-class containers
(non-`str`, masked `repr`/`str`, explicit `.expose()`/`to_wire()`),
never `NewType(..., str)` — a raw `str` token serializes into
structured logs unmasked via C-level paths. `EventEnvelope` carries
a TYPED audience so a PII-bearing payload (`IncomingCallEvent`)
cannot be wrapped in `BroadcastAudience` by accident; external
`agent_id` values under the reserved `system:*` prefix MUST be
rejected with `UNAUTHORIZED` on external sockets.

### Requirement: MediaAllocator lifecycle

`MediaAllocator.allocate` is invoked at dial/accept time;
`reissue(lease_id, role)` (round 28) replaces the credential half
of ONE role of a live pair — invalidating that role's old token at
issue while the other role's token and socket survive — and is
idempotent by `lease_id` only while the pair is live (it MUST NOT
mint credentials on a revoked or never-allocated pair);
`revoke(lease_id)` is invoked on every `CallLease` → `ENDED`
transition, is idempotent, and releases the per-lease media
credentials so no socket outlives its `lease_id`. Allocator side
effects (`reissue`/`revoke`) MUST complete INSIDE the same kernel
mutation that ends or re-pairs the lease — before the mutating
method returns — so a drained event can never announce a teardown
whose media teardown has not run yet (round 29).

### Requirement: Log projection is JSON-safe

`to_log_dict()` MUST produce only `json.dumps`-serializable values:
`datetime` as ISO strings, `timedelta` as total seconds, `Enum` as
`.value`, containers recursively; `RedactedString` values MUST
never appear unmasked.

### Requirement: Effective timeouts hydrate the kernel DTOs

`LineAssignment.timeouts` MUST carry the fully-resolved
`LineTimeouts` snapshot (no `None` fields) so the admin adapter can
write it through to SQLite verbatim, and
`ProviderBinding.heartbeat_interval_ms` MUST stamp the heartbeat
interval negotiated for the current provider session (default
`5000`, `> 0`, `<= 86_400_000` ms — the exact counterpart of
`heartbeat_interval_sec <= 86400`, round 29), converted from the effective
`heartbeat_interval_sec` by `heartbeat_interval_ms_from_sec` —
nearest millisecond (halves up — `int(sec * 1000 + 0.5)`, round 27;
Python `round()` is banker's) clamped to ≥ 1 ms — which MUST
reject a non-real, non-finite or non-positive input (bool, `nan`,
`inf`, `<= 0`) and any value whose converted interval exceeds the
named bound `MAX_HEARTBEAT_INTERVAL_MS` (86_400_000, round 30);
`heartbeat_interval_sec`
itself is floored at `0.001` s (millisecond granularity, the wire
unit) on both `LineTimeouts` and `TimeoutConfig`, so a valid config
can never stamp a zero cadence. A `heartbeat_interval_sec` config
change applies at
the next `provider.reregister`, never retroactively to a live
session. `heartbeat_down_after` MUST strictly exceed
`heartbeat_degraded_after` on `LineTimeouts` and on `TimeoutConfig`
when both fields are set. A `TimeoutConfig` patch MUST be merged
into the stored `LineTimeouts` atomically via
`LineTimeouts.apply_patch` — a full `model_validate` of the merged
dict — so a single-field patch that is legal in isolation but
breaks the ladder against the stored values is rejected with
`PhonebridgeError(INVALID_ARGUMENT)` and leaves the effective
values untouched.

### Requirement: Dial-target and caps validation is model-level

`ForwardViaLineFallback.target_number`/`target_country` MUST be
validated by `validate_e164_country` at model construction (strict
E.164 shape `^\+[1-9]\d{6,14}$` plus a phonenumbers
parse/validity/region cross-check), and `LineAssignment` MUST
reject a `forward_via_line` policy whose `line_id` equals its own
`line_id` — a corrupt SQLite/config row can never hydrate an
invalid policy. Provider-declared `caps` (on
`ProviderRegistration`, `ProviderCapabilityUpdate`, and
`ProviderBinding`) MUST be non-empty and MUST NOT contain
`exclusive_line` — durable exclusivity is a `LineAssignment`
property, not a hardware capability.

### Requirement: Heartbeat liveness ladder is elapsed-time and kernel-computed (round 28)

Elapsed silence MUST be measured on KERNEL receipt time as
`now - binding.last_seen_at` — provider-side stamps
(`checked_at`, `observed_at`) never feed deadline arithmetic. The
binding walks `reachable -> degraded -> unreachable`: silence
`>= heartbeat_degraded_after * heartbeat_interval` (the stamped
millisecond interval converted to seconds) escalates to
`degraded`, and silence `>= heartbeat_down_after *
heartbeat_interval` escalates to `unreachable` with the full
teardown. The comparison is INCLUSIVE (`>=`): with
`heartbeat_degraded_after == 1` the first miss — counted once one
full interval has elapsed since the last fact — already degrades
the binding (a ping arriving exactly AT the interval boundary is
already late: the stamped interval is the expected arrival).
`degraded` is kernel-computed, never a provider-reported fact; a
REACHABLE fact on the live channel restores `degraded -> reachable`,
and `ws_disconnected`/`unreachable` can never transition INTO
`degraded`. From `degraded`, BOTH `-> unreachable` (the elapsed
threshold) and `-> ws_disconnected` (control-socket loss) are legal
exits (round-28 fix): the socket-loss axis overrides the
elapsed-time rung, so a DEGRADED binding that loses its control
socket still reaches the orphan-grace path; `ws_disconnected`
remains the ONLY liveness state that grants the orphan grace —
`degraded` and `unreachable` never arm it. `unreachable` is
TERMINAL on the event channel (round 30): an
`unreachable -> ws_disconnected` `event.line_health_changed` MUST
never be minted — a dead binding never re-enters the orphan-grace
state, and a socket-drop fact on an already-UNREACHABLE binding
maps to no rung change (recovery goes through reregister, not a
state flip). A stored `ProviderBinding` in `ws_disconnected` MUST
carry its `orphan_deadline_at` — the state exists precisely to arm
the grace window — and a `ProviderBinding` MUST never be stored
with `liveness=unbound` (round 30): `unbound` is a listing-only
projection for a line with no binding row, never a durable row
state.

### Requirement: Provider-side stamps are advisory only (round 28)

`ProviderLiveness.checked_at`,
`ProviderCallStateUpdate.observed_at` and
`IncomingRingIngress.observed_at` (the third provider-stamp path,
added to the rule in round 30) are ADVISORY dedup/ordering
keys on the provider's clock — a same-stamp repeat is a duplicate
— and MUST NOT be used for deadline arithmetic. ONLY a stamp
deeper than `MAX_CLOCK_SKEW_SEC` (30 s) into the FUTURE MUST be
normalized to kernel receipt time at ingestion, so a fast
provider clock can neither freeze nor fast-forward the heartbeat
ladder; a stamp in the PAST is retained VERBATIM as a
dedupe/ordering key and MUST NEVER feed elapsed-time or liveness
math (round-28 fix). The normalization MUST be applied through
the canonical `clamp_provider_stamp` helper (round 29) so every
provider-stamp ingress path shares one implementation of the
rule. Dedup keys are scoped to the LINE (round 30 — the
binding-scoped rule is superseded): the
`checked_at` liveness stamp dedupes on `(line_id,
accepted_stamp)` — a ping carries no call correlator — while the
`observed_at` call-state stamp dedupes on `(line_id,
lease_or_call, accepted_stamp)`; the per-line watermark survives
a reregister, and facts naming a superseded `assignment_id` are
already rejected `STALE_TOKEN` through the closed-assignment ring
before dedup is consulted. Each scope's watermark is
MONOTONE-MAX: a verbatim PAST stamp below the recorded maximum
stays a dedupe key for detecting repeats but MUST NEVER regress
the ordering — an out-of-order fact is dropped as a duplicate,
never applied backwards, and phase legality is governed solely by
`CALL_PHASE_FORWARD_TRANSITIONS`, not stamp comparisons.
`last_seen_at` is always kernel receipt
time and every timer stays kernel-clock.

### Requirement: Reregister supersedes a live binding, register does not (round 28)

The wire carries two method names and no `reregister` field: the
adapter MUST distill which method arrived into
`ProviderRegistration.reregister` (`false` for `provider.register`,
`true` for `provider.reregister`) — the flag is derived ONLY from
the RPC method name, and a `reregister` key in the wire params
MUST be ignored, never copied into the distilled fact (round-28
fix). A `provider.reregister` on a
live REACHABLE **or DEGRADED** binding is the LEGAL half-open
supersede (round-28 fix — DEGRADED is still a live-control
state; WS_DISCONNECTED keeps the orphan-grace ws-loss reattach
path and UNREACHABLE classifies as a fresh register) — the
provider's fresh control channel arrived before the old socket's
death was detected: the old binding MUST be marked superseded, a
FRESH `assignment_id` MUST be minted, and live leases MUST be
preserved — reconciled against `state_snapshot` when present,
preserved WITHOUT reconciliation when absent (the binding
hand-over alone does not kill calls). A `state_snapshot` is
authoritative ONLY for leases in a `dialing`-or-later phase —
formally, leases whose phase is a member of the contract constant
`SNAPSHOT_RECONCILED_PHASES` (`DIALING`/`ALERTING`/`ACTIVE`/
`TERMINATING`, round 29): an
undialed `ACQUIRED` grant is pre-dial, so an empty snapshot MUST
NOT kill it. `CallPhase.RINGING` is RESERVED for wire/schema
compatibility — its `CALL_PHASE_FORWARD_TRANSITIONS` row stays
for enum completeness but the phase is UNREACHABLE on a
`CallLease` (round 30 — `ACQUIRED -> RINGING` is removed;
inbound-ring state lives on `LineState.RINGING_LOCKED` and the
line's ring slot, never on a lease phase): a `CallLease` MUST
reject `phase="ringing"`, so no snapshot element can ever
reconcile against one.
Reregister is deterministic under a reconnect storm (round 29):
the supersede applies to the line's CURRENT live binding —
whatever it is at the instant the mutation runs, including a
binding minted moments earlier by a racing `provider.reregister`
— so the outcome is sequential last-writer-wins, never a
"reregister rejected on an already-reregistered binding" verdict.
Every `provider.*` fact carrying a SUPERSEDED `assignment_id`
(ping/liveness, call-state, ring, snapshot, capability update)
MUST resolve through the closed-assignment ring and be rejected
`STALE_TOKEN` (round 29), and the adapter MUST close the
superseded control socket after delivering the supersede reply.
A `provider.register` on an already-live binding MUST
be rejected `INVALID_STATE` — reconnect inside the grace window
uses `provider.reregister`. Registration facts MUST mint a
`channel_seq` — a per-channel strictly-positive monotone
ordinal assigned at register/reregister/`note_provider_liveness`/snapshot
ingestion (round 30): `ProviderRegisterResult` carries it on
`binding.channel_seq`, `ProviderLiveness` and
`ProviderRegistration` carry it as a field, and the kernel uses it
to demote a late-arriving stale-channel fact instead of letting a
pre-supersede ping overwrite the new binding's state: a REACHABLE
fact MUST clear `orphan_deadline_at` ONLY when its `channel_seq`
equals the binding's live `channel_seq` — a ping replayed on a
lingering pre-reregister socket can never disarm the new channel's
grace deadline.
`register_provider` MUST return a
`ProviderRegisterResult` (round 30 — never a bare
`ProviderBinding`): `binding` (the created/superseded
`ProviderBinding`) and `pending_rings` (a frozen `PendingRing`
tuple — snapshot-reported rings the kernel minted `call_token`s
for through the `ingest_ring` path, relayed to the provider so
each rediscovered ring keeps one kernel-minted token,
deduplicated by ring identity, never re-minted).

### Requirement: Dead-binding cleanup runs at the mutation (round 28)

Commands queued for a binding that dies or is superseded MUST be
handled INSIDE the mutation that killed or replaced the binding
(`provider.reregister` supersede, an UNREACHABLE liveness fact,
`admin.revoke_assignment`, heartbeat-down `tick`) — not at
`drain_provider_commands` time. Outbox entries whose leases
survive a re-bind (`line.hangup`, `line.send_dtmf`,
`line.reattach_media`) MUST be retargeted to the new
`assignment_id`; commands that can never land (the dead binding's
in-flight `line.dial`/`line.answer`) MUST be dropped fail-closed
with their lease teardown effects minted in the SAME transaction,
so teardown events reach the same cycle's `drain_events()`. The
command drain keeps ONLY a defensive drop for a command addressed
to a binding that vanished with no successor — a safety net,
never the mechanism. `line.revoke` still MUST deliver while the
binding's control channel is open — an UNREACHABLE-by-heartbeat
binding can hold a live socket.

### Requirement: Handset-answered rings surface as external_call (round 28)

A `provider.call_state` update with `state:"active"` that DOES
carry `call_token` on a still `RINGING_LOCKED` line means a human
answered the ringing call on the physical handset before any
`call.accept` burned the token. The kernel MUST close the ring
WITHOUT minting a lease and mark the line `BUSY` with
`reason:"external_call"` — exactly like a dialed external call:
no agent lease, no PipeWire bridge, NO `line.hangup` ever queued;
the state clears on the later `disconnected` report.

### Requirement: Line lifecycle admin operations (round 28)

`admin.provision_line` MUST create the durable `LineAssignment` in
its default pool shape (`mode=SHARED_POOL`, `owner_agent_id=None`,
`claimable` defaulting to `True`) and MUST reject an existing
`line_id` with `INVALID_ARGUMENT`; the new line is visible
immediately as `unbound` on the listings. The configured-line
count is bounded by `MAX_LINES_TOTAL` (256, round 29):
`admin.provision_line` beyond the cap MUST be rejected
`RESOURCE_EXHAUSTED`. `admin.configure_line`
MUST accept `profile?` (the administrative label, ≤128 chars,
`None` = unchanged, no clear op). `admin.remove_line` MUST refuse
while another configured line's `forward_via_line` targets it and
MUST emit `assignment_revoked` to the owner BEFORE deleting an
agent-OWNED quiescent line (a SHARED_POOL line emits no event). `admin.assign_line` and
`admin.approve_claim` MUST reject a `RINGING_LOCKED` line with
`INVALID_STATE` (round 30): an ownership mutation during a live
ring would strand the pending fallback decision and the parked
ring bookkeeping. `admin.remove_line` MUST mint the
`provider.revoke` command and every `assignment_revoked`/
`claim_resolved` cascade event BEFORE the line's
`ProviderBinding` is deleted, and MUST record every deleted
`assignment_id` — durable or live — in the bounded closed-
assignment ring so post-delete facts still resolve to `None`,
never `STALE_TOKEN` (round 30).
All three MUST follow kernel-first write-through ordering: the
kernel mutates its record first, the admin adapter writes/drops
the `lines` row second — a failed precondition never reaches the
store. When an adapter-side `lines`-row write fails AFTER the
kernel committed a mutation, the adapter MUST call
`restore_line(snapshot)` with the pre-mutation `LineAssignment` —
or `None` for a provision rollback — which is the ONLY legal
rollback path (round 30): it restores or deletes the line's
kernel record VERBATIM — the restored `LineAssignment` keeps its
stored `last_seen_at` and every durable field, so a persistence
retry never rotates an id or widens a deadline — mints the
compensating events
that keep the outbox truthful — an `assignment_revoked` for a
rolled-back grant, a grant-side event for a rolled-back revoke —
and MUST surface the divergence as a `config_stale` CONDITION
plus an admin alert — a flagged store/kernel-divergence state on
the admin console, not a `domain.*` event: the domain event
vocabulary has no such member.
`hydrate_lines(rows, bindings)` (round 30) is the batch variant:
the adapter MUST load ALL persisted `lines` rows and
`provider_bindings` rows in ONE hydration call — each call wipes
the previous hydration state wholesale so a retry after a partial
load cannot leave a half-hydrated kernel. Hydration restores each
record VERBATIM — `assignment_id`, owner, mode, `profile` and
`last_seen_at` survive a restart unmodified; it emits NO events
and rotates NO ids — and it validates the `forward_via_line` graph
only AFTER every row is loaded, rejecting the batch on a dangling
target or a cycle that line-by-line loading could never see.
Legacy-row compatibility (round 30): `channel_seq` is a REQUIRED
`ge=1` field on `ProviderBinding` — a `provider_bindings` row
persisted before the `channel_seq` era fails model validation, so
the STORE-layer migration MUST backfill a generation (the lowest
legal value is `1`) before `hydrate_lines` is called; the kernel
never normalizes a missing/zero generation itself — it hydrates
only fully-validated rows, verbatim.
`admin.revoke_assignment`
on a `SHARED_POOL`
`LineAssignment` id MUST be rejected `INVALID_STATE` (the pool
record is a configured line's default state, not a binding); a
repeated provider-binding revoke MUST be idempotent — the dead
id resolves through the closed-assignment ring and returns
`None`, never `STALE_TOKEN` for an id the kernel once owned —
and the same recorded-idempotency applies to a repeat
`revoke_assignment` on a `LineAssignment` id already revoked
(`EXCLUSIVE` or `SHARED_POOL`, round 30). The closed-assignment
and closed-lease rings are INSERTION-ORDER FIFO tombstone rings
(round 30): a hit marks an entry as duplicate — it MUST NOT
refresh the entry's eviction position — and new ids append at the
tail, so capacity evicts the oldest insertions and no ordering
field is needed. Every admin listing —
`list_assignments`, `list_pending_claims` and
`list_provider_bindings` — is PAGINATED (round 30):
`offset >= 0` and `1 <= limit <= MAX_LIST_PAGE` (default
`MAX_LIST_PAGE`) bound the returned slice so an unbounded dump is
impossible.

### Requirement: max_call_duration is enforced by tick (round 28)

When a line's effective `max_call_duration_sec` is set, a lease in
`DIALING`/`ALERTING`/`ACTIVE` crossing `now >= lease.created_at +
max_call_duration_sec` MUST be torn down by `tick` with
`reason:"max_call_duration"` (the `CallEndReason` member) — the
cap bounds one call's wall-clock length including a
`record_and_telegram` recording. An undialed `ACQUIRED` grant is
exempt — the grant TTL owns its deadline.

### Requirement: Idle window bound and activity stamping (round 28)

`idle_revoke_after` MUST be strictly positive when set and bounded
at 86400 s — a `timedelta.max`-scale value would overflow the
`last_seen_at + idle_revoke_after` hydration arithmetic.
`last_seen_at` — the idle clock — MUST refresh ONLY on owner line
operations (`acquire`, `dial`, `accept`, `release`, `hangup`,
`send_dtmf`, claim mutations) and on `drop_session` closing the
owner's last open session; provider facts (`ingest_ring`,
`ingest_call_state`, `note_provider_liveness`) and `configure_line` MUST
NEVER stamp it — the phone ringing, the modem reporting, and the
admin relabeling are not the owner using the line.

### Requirement: The 0x02 flush watermark rides the agent stream clock (round 28)

The media `0x02` flush's `timestamp_ms` is a watermark IN THE
AGENT'S UPLINK STREAM CLOCK — the `timestamp_ms` value of the last
agent `0x01` frame the LineProvider consumed before muting uplink
for DTMF synthesis, or `0` when no frame was consumed yet — never
a router-local wall-clock stamp. On receipt the router MUST drop
queued uplink `0x01` frames with `timestamp_ms <= watermark` for
that lease and keep post-watermark speech; the `0x02` itself is
consumed at the router and never forwarded to the provider. An
AGENT-originated `0x02` (barge-in, round 30) is the DISTINCT
discard-all case: it MUST carry the reserved marker
`timestamp_ms = 0xFFFFFFFF` — outside the serial-comparison range
a real stream stamp can reach — and on receipt the router MUST
drop ALL still-queued uplink frames for the lease regardless of
timestamp; it is consumed at the router and never bridged to the
provider.
