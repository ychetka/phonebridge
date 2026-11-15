# Spec — LineManager contract (Phase-0 freeze)

Normative requirements for `src/phonebridge/domain/contract.py`,
frozen at round 23 (architect review `SA_carbon_review_12`) ahead of
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
wedges in DIALING.

### Requirement: Idle escalation is alert-only

`idle_revoke_after` expiry MUST emit `event.idle_alert`
(`IdleAlertEvent`, admin audience only, at most once per continuous
idle period) and MUST NOT emit `event.assignment_revoked` —
assignment revocation is produced ONLY by explicit revoke paths
(owner `call.release{assignment_id}` or admin
`revoke_assignment`/`line.revoke`).

### Requirement: Accept window always elapses before fallback

`decide_fallback` MUST wait the full `accept_window_sec` even when
zero live sessions exist for the line, so configured wake-and-grant
hooks can wake a sleeping agent; `event.fallback_triggered`
carries `expires_at` bounded by `fallback_accept_ttl_sec`.

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
