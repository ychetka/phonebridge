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
wedges in DIALING.

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
`call.accept`, the kernel MUST burn the fallback token, transition
`RINGING_LOCKED -> FREE`, and emit
`MissedCallEvent(reason=ACCEPT_WINDOW_EXPIRED)`;
`event.fallback_triggered` carries `expires_at` bounded by
`fallback_accept_ttl_sec`.

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
`5000`, `> 0`). A `heartbeat_interval_sec` config change applies at
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
