# Spec — LineManager contract (Phase-0 freeze)

Normative requirements for `src/phonebridge/domain/contract.py`,
frozen at round 24 (architect review `SA_carbon_review_13`) ahead of
implementation milestones M1–M5. Prose sections carry the
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
holds no admin scope to resolve missing pieces at runtime. A
self-loop (`forward.line_id` equal to the configured line) MUST be
rejected at `configure_line`.

### Requirement: Outbox soft-cap pre-flight and drain discipline

Load-creating operations (`acquire`, `dial`, `accept`, `claim_exclusive`)
MUST check remaining outbox capacity BEFORE mutating state and
reject with `RESOURCE_EXHAUSTED` when the soft cap would be
exceeded. Teardown paths (`hangup`, `ingest_call_state`
disconnected, `drop_session`, `tick`) MUST NEVER raise
`RESOURCE_EXHAUSTED` — their events append unconditionally, and the
adapter MUST call `drain_events()`/`drain_provider_commands()`
after every kernel call.

### Requirement: Saga compensation on line.dial and line.answer

The adapter MUST await the provider's `{accepted}` reply to
`line.dial`/`line.answer` with a ~2 s deadline. `{accepted:false}`,
timeout or transport error MUST be fed back as
`ingest_call_state(DISCONNECTED, NETWORK_ERROR)` so the lease never
wedges in DIALING. After either compensation the adapter MUST issue
a best-effort `line.hangup` against the call (round 25): a
`dial`/`answer` that timed out on the wire may still have executed
on the modem, and an uncompensated air-side call would outlive its
lease as a ghost ring.

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
issued to shorten the drain. Rationale: handing a physically still-
ringing line to the next `call.dial` would collide with the live
air-side call, and a later `state_snapshot` would re-register the
same ring as a NEW `incoming_call`.

### Requirement: Occupancy and liveness are orthogonal axes (round 25)

`LineState` is the OCCUPANCY axis only — `FREE`/`BUSY`/
`RINGING_LOCKED`; `RESERVED` has no transitions and is removed, and
`DEGRADED`/`DOWN` are removed because a missed heartbeat MUST never
overwrite a call fact (a `BUSY` line stays `BUSY` while its
provider degrades). Provider health lives on the separate
`LivenessState` axis (`reachable`/`ws_disconnected`/
`unreachable`), surfaced via `event.line_health_changed`; liveness
transitions drive teardown policy (orphan grace vs immediate
hangup), never occupancy.

### Requirement: Claim and assignment results reach the agent (round 25)

`call.claim_exclusive` resolves ASYNCHRONOUSLY: the kernel MUST emit
`event.claim_resolved{claim_id, line_id, status, assignment_id?,
reason?}` to the claiming session on approval AND rejection, and on
approval additionally `event.assignment_granted{assignment_id,
line_id, mode}` to the new owner — this is the ONLY path delivering
the `assignment_id` an owner needs for
`call.release{assignment_id}` (an undelivered `assignment_id` makes
a granted exclusive line un-releasable). `admin.assign_line` grants
emit `event.assignment_granted` identically.

### Requirement: Media credentials are split per role and reissuable (round 25)

`MediaCredentials` MUST carry SEPARATE `agent_media_endpoint` /
`provider_media_endpoint` pairs (local UDS vs WAN WSS coexist), and
the one-shot per-role tokens MUST be reissuable without tearing
down the call: `line.reattach_media` feeds fresh provider
credentials to a lease preserved across `provider.reregister`, and
`call.reattach_media` feeds fresh agent credentials when the
agent's media socket dropped while control stayed alive. A burned
one-shot token MUST never be required twice.

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
restart).

### Requirement: Queue sequence is minted at acquire ingress (round 25)

A single global monotonic `seq` is allocated to EVERY `call.acquire`
at ingress — including immediate grants — so a preempted
grant-but-undialed lease re-enters the queue head with its ORIGINAL
sequence and FIFO fairness survives preemption; signed `seq` space
is not required because every participant already holds a value.
Server-side ticket removal (exclusivity, provider loss, capability
removal, claim resolution) MUST emit `event.queue_cancelled` to the
ticket's session.

### Requirement: Exclusive-line privacy is filtered at the edge (round 25)

`call.get_lines` MUST omit other agents' EXCLUSIVE lines entirely
(the claimable pool view is unaffected); `event.line_state_changed`
for an exclusive line MUST broadcast a redacted `reason` to
non-owners and deliver the real reason ONLY to the owner's
session(s) — a shared event bus MUST NOT leak whose private line
did what.

### Requirement: Forwarding policies are loop-safe and bounded (round 25)

`configure_line` MUST reject a `forward_via_line` policy creating a
self-loop AND detect transitive cycles (`A → B → … → A`) by walking
the stored policy chain bounded at 8 hops — a forward loop would
otherwise spin fallback workers forever. `max_call_duration_sec`
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
`revoke(lease_id)` is invoked on every `CallLease` → `ENDED`
transition, is idempotent, and releases the per-lease media
credentials so no socket outlives its `lease_id`.

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
`5000`, `> 0`), converted from the effective
`heartbeat_interval_sec` by `heartbeat_interval_ms_from_sec` —
`round(sec * 1000)` clamped to ≥ 1 ms; `heartbeat_interval_sec`
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
