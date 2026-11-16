# phonebridge API contract (v0 draft)

Status: design draft (consolidated with design rounds 1-24,
2026-11-16; see docs/phone-bridge-service-design.md decision log).
Transport: **JSON-RPC 2.0 bidirectional over one connection per peer**.
- Local peers: Unix-domain socket (bind-mounted out of the container).
- Remote peers: **WSS** — providers/agents always dial *into* the gateway (NAT-safe).
- **Control-channel framing (round 25):** the local Unix-domain control
  socket uses the SAME WebSocket-over-UDS framing as the media path —
  a bare `SOCK_STREAM` has no message boundaries, so ad-hoc NDJSON or
  length-prefix framing is forbidden. Every JSON-RPC message is exactly
  one WS TEXT frame, hard-capped at **64 KiB**; an oversized frame is
  closed `1009 Message Too Big`, a non-TEXT control frame
  `1002 Protocol Error`. Server→client `event.*` deliveries are JSON-RPC
  **Notifications** (no `id`, no response expected); a client MUST NOT
  send `event.*` methods upstream. No request is ever parked
  server-side — an unanswered request is retried by the CLIENT within
  its own deadline.
- The agent surface is also reachable through an **MCP transport
  adapter** (round 12): the same `agent.*`/`call.*` namespaces and
  the same token scopes, exposed as MCP tools for AI agents that
  natively speak MCP (e.g. the developer's secretary agent). The
  admin UI is just one consumer of the same API — nothing in the
  contract is UI-specific.
- **MCP adapter boundary (round 19):** the MCP surface is the
  CONTROL plane only. A standard MCP client cannot stream binary
  PCM, so a `call.dial`/`call.accept` issued through MCP returns the
  same `{lease_id, media_endpoint, media_token}` — the media socket
  is held either by a dedicated audio worker of the calling agent
  (which receives the endpoint+token out of band) or by a local
  `MediaSidecar` exposing higher-level tools (`speak_text`,
  `listen`). The MCP adapter never owns a media socket itself.

Two endpoint surfaces, one gateway daemon, shared in-process `LineManager` kernel:

| Surface | Listener (example) | Peer | Namespace |
|---|---|---|---|
| **Manager** | `wss://gw:7001/provider` | Line providers (LocalLine, RemoteLine) | `provider.*` (up), `line.*` (down) |
| **APICallService** | `wss://gw:7002/agent` | AI agents | `agent.*`, `call.*` (up), `event.*` (down) |

SMS: OUT OF SCOPE (spike-0, 2026-11-12: no org.ofono.MessageManager
on bus; Android exposes SMS via MAP not HFP). Backlog note only -
design doc §8 lists the hard paths.

Token scopes are separate: provider tokens never grant `agent.*`/`call.*`, agent tokens never grant `provider.*`/`line.*`; `admin` scope is a third class.

`line_id:"default"` is accepted **only in inbound RPC requests
from agents that take a `line_id` parameter** (`call.acquire`) as
an alias for "any suitable pool line" (`call.get_lines` has no
`line_id` parameter - it returns the pool view filtered by
`intent?`, round 23 drift fix). Every server response and every
`event.*` payload carries the canonical `line_id` (e.g.
`"line-main"`) - the alias never leaks into wire output (round 11).

## Common types

```ts
type Intent   = "dial_out" | "receive_inbound" | "exclusive_line";
                // earlier drafts named these outbound|inbound|exclusive
type LineState = "FREE"|"BUSY"|"RINGING_LOCKED";
                // call-occupancy ONLY (round 25): provider/transport
                // health moved to `liveness` — a flat enum let a
                // missed ping overwrite BUSY, and RESERVED had no
                // transitions. DEGRADED/DOWN now surface via
                // `event.line_health_changed`, never as a LineState.
type LivenessState = "reachable"|"ws_disconnected"|"unreachable"|"unbound";
// "unbound" = no provider attached — primarily a LineSummary
// listing value (round 26); legal on event.line_health_changed
// ONLY as old_state of the first-attach unbound→reachable
// transition (round 27), never as new_state.
type LineSummary = {
  line_id: string,
  caps: Intent[],
  state: LineState,
  liveness: LivenessState,   // provider health as last reported;
                             // "unbound" iff no provider attached
                             // (then caps == [] — a bound provider
                             // always contributes non-empty caps)
  profile?: string,
  assignment_id?: string,    // round 27: set ONLY on rows for lines
                             // the calling agent owns EXCLUSIVELY
  provider_meta?: object     // round 27: live binding's meta mirror
};

// PII masking (CODING_HAZARDS rule 10): number / caller_id values
// use a RedactedString type at the serializer/logger boundary -
// the wire value is complete, the log/error-dump rendering is
// masked (e.g. "+373****56").
type RedactedString = string;

// Wire time convention (round 21): every `*_at` field and
// `server_time` is an RFC 3339 / ISO-8601 string in UTC
// ("2026-11-15T12:00:00.000Z"); every `*_ms` field is a
// non-negative integer count of milliseconds.
```

## Identifier taxonomy (round 11)

| id | issued by | lifetime | purpose |
|---|---|---|---|
| `assignment_id` | LineManager | durable | line<->owner/provider binding; survives reconnects. Two DISTINCT object spaces share this tag (round 20): a `LineAssignment` id identifies agent ownership of a line, a `ProviderBinding` id identifies a registered provider attachment — admin operations disambiguate by which object the id resolves to; `provider.register` returns a ProviderBinding id |
| `call_token` | LineManager | TTL = `accept_window_sec` (default 30 s) | one-shot token for an inbound ring (`RINGING_LOCKED`); burns on `call.accept` or timeout. Minted inside `ingest_ring` and handed to the provider in the `provider.call_state{state:"ringing"}` response `{ok, call_token}` (round 22) — the provider echoes it on every later update for that ring so a pre-answer caller hangup correlates |
| `lease_id` | LineManager | ephemeral (one call / one hold) | single id of the active line hold AND the call - replaces any `call_id` everywhere (methods and events) |
| `media_token` | MediaRouter | ephemeral (bound to `lease_id`) | authorizes the binary media socket handshake. TWO independent one-time tokens are issued per `lease_id` (round 21): `agent_media_token` bound to `(lease_id, "agent")` (delivered in `call.dial`/`call.accept` results) and `provider_media_token` bound to `(lease_id, "provider")` (delivered in `line.dial`/`line.answer`). Both travel on the wire as `media_token`; the MediaRouter validates `(token -> lease_id, expected_role)`, so neither side can spoof the other's role in `media.auth` |

## Error codes

| code | meaning |
|---|---|
| `OWNER_MISMATCH` | caller does not own the lease/assignment |
| `LINE_EXCLUSIVE_NOT_OWNER` | line held exclusive by another agent |
| `LINE_BUSY_INCOMING` | line locked by an inbound ring (RINGING_LOCKED) |
| `LINE_NOT_FOUND` | unknown line_id |
| `INVALID_INTENT` | intent not in line's caps |
| `STALE_TOKEN` | `call_token` consumed/expired, unknown durable `assignment_id`, or a dead-lease media reattach — token-class staleness only (grant-TTL expiry is `RESOURCE_EXHAUSTED`/`GRANT_TTL_EXPIRED`, never `STALE_TOKEN`) |
| `PROVIDER_UNREACHABLE` | line's provider binding degraded/unreachable (was `ws_disconnected` past orphan grace, or heartbeat-down) |
| `DIAL_REJECTED` | dial string failed pre-line validation — `data.reason` carries a typed sub-code (see Dial validation) |
| `INVALID_STATE` | operation invalid in the current call/line phase — repeated `dial`, `send_dtmf` outside ACTIVE, approving a closed claim (round 20) |
| `INVALID_ARGUMENT` | invalid input parameters — DTMF alphabet/length violations (`data.reason: "INVALID_DTMF"`), empty intent sets (round 20) |
| `RESOURCE_EXHAUSTED` | queue capacity or per-session ticket/lease limits exceeded (`data.reason`: `QUEUE_FULL`, `SESSION_LIMIT_EXCEEDED`, `GRANT_TTL_EXPIRED` — round 20; these were previously folded into `STALE_TOKEN`) |
| `UNAUTHORIZED` | missing/invalid/revoked API key or token; a reserved `system:*` identity presented on an external socket (round 25) |
| `FORBIDDEN` | authenticated identity lacks the method's scope (agent token on `provider.*`, provider token on `call.*`/`line.*`, non-admin on `admin.*`) or `agent_id` does not match the key's bound identity (round 25) |
| `SESSION_NOT_FOUND` | `session_id` unknown to the gateway — never registered or already closed by `drop_session` (round 25) |

The JSON-RPC error object on the wire (round 25): `error.code` is an
INTEGER in the application range `-32099..-32000` (the
`ErrorCode`→integer mapping lives in `ERROR_CODE_WIRE`,
`src/phonebridge/domain/contract.py`); `error.message` carries the
`ErrorCode` string from the table above; `error.data` carries the
`ErrorData` JSON `{reason?, detail?, free_lines?}`.

`SESSION_NOT_FOUND` ownership (round 27): the KERNEL emits it —
every `LineManager` method that takes a `session_id` resolves an
unknown or already-dropped session as `SESSION_NOT_FOUND` (never
`OWNER_MISMATCH`), so an adapter surfaces the kernel error verbatim;
the lone exception is `drop_session` itself, which stays idempotent
on an already-gone session.

`NO_FREE_LINE` was REMOVED (round 9): a busy pool is a normal
result, not an exception - see `call.acquire`.

## Dial validation (round 12, issue #25)

`call.dial` takes TWO number-related fields and validates them
BEFORE the call can reach the line — malformed input must never
become `Dial` on the bus:

- `number` — STRICT E.164 only: `^\+[1-9]\d{6,14}$`, checked before
  parsing. Local/national forms, missing `+`, bare-`+` hybrids
  (e.g. `+77828221` — parses as calling code `+7`), spaces,
  punctuation, extension markers → rejected.
- `country` — MANDATORY ISO 3166-1 alpha-2 destination country.
  It is a cross-check ASSERTION, never a fallback parse region and
  never a source of prefixes: the system adds nothing to a number,
  ever.
- Validation (libphonenumber / `phonenumbers`): `parse(number, None)`
  → `is_valid_number()` → `is_valid_number_for_region(parsed,
  country)`. Region check uses `is_valid_number_for_region`, NOT
  `region_code_for_number() == country` — shared calling codes
  (NANP +1, +44 Crown Dependencies, +7) must not false-reject.
- **Country vocabulary (round 25):** `country` MUST be a known ISO
  3166-1 alpha-2 region — an alpha-2-shaped but unassigned code
  (`"ZZ"`, `"AA"`) is `UNKNOWN_COUNTRY_CODE`, not `COUNTRY_MISMATCH`.
  The special region `"001"` is accepted ONLY for non-geographic
  numbering plans (`+800`/`+808`/`+870`/`+878`/`+881`/`+882`/`+883`/
  `+888`/`+979` — phonenumbers reports `region_code_for_number ==
  "001"`); a geographic number paired with `"001"`, or a
  non-geographic number paired with a real country code, is
  `COUNTRY_MISMATCH`.
- **Per-line number-type allowlist** (admin config):
  default `{FIXED_LINE, MOBILE, FIXED_LINE_OR_MOBILE, VOIP, TOLL_FREE}` —
  `FIXED_LINE_OR_MOBILE` is mandatory because `phonenumbers.number_type()`
  returns this composite type for NANP numbers, so a plain US/CA mobile
  would otherwise be rejected (round 15); an admin may
  additionally enable `{PREMIUM_RATE, SHARED_COST, PAGER,
  PERSONAL_NUMBER}`. Short codes / emergency numbers are NOT
  representable in E.164 — always rejected on this path (a separate
  explicitly-authorized pathway would be required if ever needed).
- On success the validated E.164 string is passed to `line.dial`
  UNCHANGED — verified live on the current stack (full E.164
  terminates correctly; no national-format conversion is needed on
  this line). Any future per-line dial-format serialization would be
  an explicit declared adapter capability, never implicit mutation.

`DIAL_REJECTED.data`:

```ts
{ reason: "INVALID_FORMAT_NOT_E164"
       | "UNKNOWN_COUNTRY_CODE"      // country input is not ISO alpha-2
       | "COUNTRY_MISMATCH"          // valid E.164, not valid for declared region
       | "INVALID_NUMBER"            // parses, but not a valid subscriber number
       | "UNSUPPORTED_NUMBER_TYPE"   // type not in the line's allowlist
       | "LINE_POLICY_REJECT" }      // other per-line policy
```

Provider/setup failures after a valid dial are a SEPARATE error class
from these input rejections and from ordinary no-answer termination.

## Manager namespace — `provider.*` (Line → Manager)

| method | params | returns |
|---|---|---|
| `provider.register` | `{line_id, token, caps, profile?, meta?}` | `{assignment_id, heartbeat_interval_ms, provider_media_endpoint?, server_time}` — `provider_media_endpoint` is the provider-side static socket address (agent-side endpoints come from `MediaCredentials.agent_media_endpoint` inside each grant — round 25 endpoint split); the per-lease `media_token` arrives later inside `line.dial`/`line.answer` (round 14). `heartbeat_interval_ms` is stamped from the line's effective `heartbeat_interval_sec` as nearest-millisecond (halves up — `int(sec * 1000 + 0.5)`, round 27; Python `round()` is banker's) clamped to ≥1 ms — the interval is floored at 0.001 s (millisecond granularity, the wire unit) so a valid config can never yield a zero cadence (round 24c). `meta` is bounded (round 22): ≤16 keys, keys ≤64 chars, values ≤256 chars — `INVALID_ARGUMENT` beyond. `caps` MUST be non-empty and telecom-only (`dial_out`/`receive_inbound`): an empty set or any `exclusive_line` entry → `INVALID_ARGUMENT` (round 24) — durable exclusivity is a `LineAssignment.claimable`/`mode` property granted by the claim flow, never a hardware capability |
| `provider.reregister` | same as register + `state_snapshot?` | idempotent; preserves lease state during the 10 s orphan-grace window — `state_snapshot` lists the calls the provider still sees on the D-Bus (`{lease_id?, call_token?, state, caller_id?}[]`, `lease_id`/`call_token` absent for a call the gateway never issued) so the kernel can re-bind surviving leases (round 21, design §5.4); every preserved lease receives FRESH provider media credentials via `line.reattach_media` — media tokens are one-shot and died with the dropped socket (round 25); grace covers the WS-drop level only — provider-reported tunnel/USB death or heartbeat-down unreachable = immediate teardown, no grace (round 20). A `provider.register` on an already-live binding is `INVALID_STATE` — reconnect inside the grace window uses `provider.reregister` (round 25) |
| `provider.ping` | `{line_id, assignment_id, state_snapshot?, checked_at?}` | `{ok, server_time}` — missed pings escalate `LivenessState` along the `heartbeat_degraded_after`/`heartbeat_down_after` ladder (`reachable` → `unreachable`, surfaced as `event.line_health_changed`); the comparison is elapsed-time based — `now - last_seen_at` vs the stamped thresholds, no separate missed-count counter (round 27). `checked_at` is an optional provider-side RFC 3339 UTC stamp of the liveness observation (gateway stamps receipt time when absent; stale-fact dedupe orders on the accepted stamp, round 25). `state_snapshot` is legal only on `reachable` pings (round 27 — a dead/dying channel's call list is not reconciled). A REACHABLE fact clears the orphan-grace deadline only on the binding's own live channel — inside the grace window the authoritative recovery is `provider.reregister` (round 27) |
| `provider.capability_update` | `{line_id, assignment_id, caps, profile?}` | `{ok}` — replaces caps/profile on the ACTIVE binding; carries NO `token` — the binding id authenticates the peer ONLY together with arrival on that binding's own live control channel (round 27: the adapter verifies request `assignment_id` == the socket's binding id; bare id knowledge is not a credential). `profile` absent/`null` = leave unchanged; there is no clear-profile op in v1 (round 27). An identical update is an idempotent no-op (round 22); the same `caps` invariant as `provider.register` applies — non-empty and telecom-only, `exclusive_line` → `INVALID_ARGUMENT` (round 24) |
| `provider.call_state` | `{line_id, assignment_id, call_token?, lease_id?, caller_id?, state, reason?, observed_at?}` | `{ok, call_token?}` — `state` is the canonical set `ringing` \| `dialing` \| `alerting` \| `active` \| `disconnected`. `caller_id` carries the inbound caller's E.164 on `ringing` (round 22 — previously the wire had no field for WHO rings). **`call_token` minting (round 22):** on `state:"ringing"` the gateway mints the one-shot `call_token` inside `ingest_ring` and returns it in `{ok, call_token}`; the provider MUST echo that `call_token` on every subsequent update for the same ring — including a pre-answer `disconnected` (early caller hangup) — until `line.answer` hands the call a `lease_id`. For rings the gateway discovered via `state_snapshot` (the provider never received a token), a `disconnected` update with NEITHER id on a `RINGING_LOCKED` line resolves to that line's one open ring. On every other state the response is `{ok}`. `lease_id` identifies an established call. Recognized `observed_at` is an optional provider-side RFC 3339 UTC stamp of the transition observation (gateway stamps receipt time when absent; ordering/dedupe is per (line, call) on the accepted stamp — a same-stamp repeat is a duplicate, not a regression, round 25). `reason` is REJECTED unless `state:"disconnected"` (round 26 — a reason on a still-live call state is a contradiction). Recognized `reason` values on `disconnected` — the provider-reported vocabulary is CLOSED (round 21): `normal`, `busy`, `no_answer`, `rejected`, `network_error`, `dongle_lost` (tunnel/USB death — immediate teardown, no orphan grace), `stack_restart` (vanished call found by `GetCalls()` reconciliation after a bus/name-owner restart). Gateway-minted reasons such as `provider_lost` (orphan-grace expiry or heartbeat timeout) are emitted on the agent event channel as `event.call_state_changed{reason:"provider_lost"}`, never reported here. An update carrying NEITHER `call_token` nor `lease_id` on a line NOT in `RINGING_LOCKED` reports a human-initiated call on the physical phone (round 21): the line goes `BUSY` with reason `external_call`, no lease is minted, and no `line.hangup` is ever sent for it (design §9) |
| `provider.audio_ready` | `{line_id, assignment_id, lease_id}` | `{ok}` — one-shot notification: the provider's local BT SCO path is up (early media flows, uplink accepted). Gateway emits `event.audio_ready` to the agent from this signal |
| `provider.reattach_media` | `{line_id, assignment_id, lease_id}` | `{media_endpoint, media_token}` / `STALE_TOKEN` (dead binding or lease) / `OWNER_MISMATCH` — fresh PROVIDER media credentials for a live lease when the provider's media socket dropped while its control channel stayed up (round 27): `line.reattach_media` covers only the `provider.reregister` reissue path, which needs the control socket to drop first. The reissued pair SUPERSEDES the prior provider-side pair at issue time and obeys a fresh `MEDIA_PAIR_TTL_SEC` handshake window counted from reissue — the provider MUST re-run `media.auth{role:"provider"}` inside it |

## Manager → Line — `line.*` (down-channel commands)

| method | params | returns |
|---|---|---|
| `line.dial` | `{assignment_id, lease_id, number, media_endpoint, media_token}` | `{accepted}` — `number` is the already-validated E.164 string, passed through unchanged (see Dial validation); `media_endpoint`/`media_token` authorize the provider's `media.auth` for this `lease_id`. **Saga compensation (round 23; hangup leg added round 25):** the gateway answers the agent's `call.dial` without waiting for this reply; the adapter MUST await `{accepted}` with a ~2 s deadline and, on `{accepted:false}`/timeout/transport error, feed `provider.call_state{state:"disconnected", reason:"network_error"}` so the lease never wedges in DIALING — AND issue a best-effort `line.hangup{lease_id}` afterwards, because a dial that physically reached the phone (WS dropped after delivery, or a slow radio wakeup past the deadline) would otherwise stay ringing uncontrolled while the gateway already freed the line |
| `line.answer` | `{assignment_id, lease_id, call_token, media_endpoint, media_token}` | `{accepted}` — same per-lease media credentials as `line.dial`; same ~2 s `{accepted}` deadline + `disconnected`/`network_error` + best-effort hangup compensation as `line.dial` (rounds 23/25). **Ring correlation (round 25):** a line has AT MOST one open ring — the provider answers whichever ring is open on its line regardless of which `call_token` value arrives (original ring token or re-issued `fallback_call_token`); the field is informational for the provider's own bookkeeping |
| `line.hangup` | `{assignment_id, lease_id?, call_token?}` | `{accepted}` — exactly ONE target (round 23): `lease_id` tears down an established call; `call_token` rejects a still-unanswered inbound ring (the `reject` fallback path - no `lease_id` exists before `call.accept`). Same by-line ring correlation as `line.answer` — the provider targets its one open ring (round 25) |
| `line.reattach_media` | `{assignment_id, lease_id, media_endpoint, media_token}` | `{accepted}` — fresh provider media credentials for a lease PRESERVED across `provider.reregister` (round 25): media tokens are one-shot, so the dropped socket's pair is dead and the kernel reissues the provider role's half; the provider MUST re-run `media.auth` with the new token inside a FRESH `MEDIA_PAIR_TTL_SEC` (30 s) window counted from the reissue instant (round 27 — the dead socket's old deadline is meaningless). A reissued pair SUPERSEDES the prior one — the old `media_token` becomes invalid at issue time (round 26: single active pair per lease; the replay window closes at the reissue instant, not the pair TTL) |
| `line.send_dtmf` | `{assignment_id, lease_id, digits}` | `{accepted}` |
| `line.revoke` | `{assignment_id, reason?}` | `{accepted}` — **admin scope only** |

Sent as server-initiated JSON-RPC requests over the provider's own inbound connection — no reverse dial needed. **Dead-binding drain (round 26; kernel-owned since round 27):** a queued `line.*` command drained against a binding that has since died or been replaced by a `provider.reregister` is DROPPED by the kernel inside `drain_provider_commands` — never returned for delivery; the kernel itself performs the teardown for a dropped in-flight `line.dial`/`line.answer` (same `disconnected`/`network_error` effect — the lease never wedges in DIALING) so the adapter owes no compensation for commands it never saw. Exception: `line.revoke` still delivers while the binding's control channel is open — an UNREACHABLE-by-heartbeat binding can hold a live socket (round 27).

## APICallService — `agent.*` / `call.*` (Agent → gateway)

| method | params | returns / errors |
|---|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` | `{session_id}` / `INVALID_ARGUMENT` on an empty `intents` set; `INVALID_INTENT` when `intents` contains `exclusive_line` — durable exclusivity is requested only via `call.claim_exclusive` (round 21). `agent_id` MUST match the API key's bound identity (`FORBIDDEN` on mismatch); `system:*` agent ids are RESERVED for kernel-internal service identities provisioned out-of-band and are `UNAUTHORIZED` on external sockets (round 25). At most 32 concurrent sessions per `agent_id` AND 512 sessions globally (`RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED`, round 27). Every call mints a NEW `session_id` — there is no update-intents op; a reconnect does not resume the old session (round 27). `profile?` is an optional session label ≤128 chars for observability (round 27). A reconnecting session re-receives any LIVE ring as a fresh `event.incoming_call` (same `call_token`, original `expires_at`, round 25) — on an EXCLUSIVE line only for its owner; on a SHARED_POOL line for ANY new session holding `receive_inbound` (round 27). An owner registering later also gets a replayed `event.assignment_granted` with its current `assignment_id` (round 27) |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?, idempotency_key?}` | `{granted: true, line_id, lease_id, expires_at}` or `{granted: false, free_lines[]}` or `{granted: false, queued: true, ticket_id, position}` — `intent` must be `dial_out` (round 14; other intents → `INVALID_INTENT`); `expires_at` is the grant TTL deadline (round 20; missing it → `event.call_state_changed{reason: "grant_ttl_expired"}`); retry dedup is scoped to a non-empty `(session_id, idempotency_key)` whose entries live only inside the LIVE session and die with it via `drop_session` — a key never dedups across a reconnect (round 27); calls without a key create independent tickets so multi-call agents are not merged; `position` is the GLOBAL FIFO rank across all eligible tickets (round 27); `profile?` is an optional per-call label ≤128 chars (round 27); `LINE_EXCLUSIVE_NOT_OWNER` (+ `data.free_lines[]`) — except a `system:fallback_worker` session, which bypasses this check to execute admin-configured `forward_via_line` policy (round 22); `RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED` on per-session limits — at most 16 queued tickets per session and 1024 globally (`QUEUE_FULL`, round 25) |
| `call.cancel_acquire` | `{session_id, ticket_id}` | `{ok}` — withdraw a queued acquire |
| `call.release` | `{session_id, lease_id?, assignment_id?}` | `{ok}` / `OWNER_MISMATCH` / `STALE_TOKEN` (unknown durable `assignment_id`) — exactly ONE target (round 22): `lease_id` ends the EPHEMERAL lease (an undialed grant frees the line; an in-flight call is hung up first — the lease never outlives the call); `assignment_id` is the durable-binding path — the exclusive owner voluntarily releases its `LineAssignment` (line reverts to the claimable pool, `event.assignment_revoked` to the owner; the `assignment_id` itself arrives via `event.assignment_granted`, round 25); zero or BOTH targets passed → `INVALID_ARGUMENT` (round 24 — the XOR is a hard wire rule, not a preference) |
| `call.dial` | `{session_id, lease_id, number, country}` | `{lease_id, media_endpoint, media_token}` / `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE`, `DIAL_REJECTED` |
| `call.accept` | `{session_id, call_token}` | `{lease_id, line_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH` — atomic: issues the lease, answers the ring, attaches media; the accepting session MUST hold `receive_inbound` intent (`INVALID_INTENT` otherwise, round 22 — `system:fallback_worker` exempt with its minted `fallback_call_token`) |
| `call.hangup` | `{session_id, lease_id}` | `{ok}` — ends the call AND the ephemeral lease atomically (auto-release, round 22); a trailing `call.release` is an idempotent no-op |
| `call.send_dtmf` | `{session_id, lease_id, digits}` | `{ok}` — `digits` must match `^[0-9A-D*#,wW]{1,32}$` (`,`/`w`/`W` = ~500 ms pauses) else `INVALID_ARGUMENT`/`INVALID_DTMF`; `INVALID_STATE` unless the lease is ACTIVE or ALERTING AND `audio_ready` was received (round 27 — early-media IVR means SCO is up while alerting; a tone sent before `audio_ready` has no uplink to ride and the `0.3–0.6 s` `State=active`→SCO-up gap would swallow it anyway); in-band tones are synthesized by the LineProvider (oFono `SendTones` is broken on PipeWire Telephony, design §5.5/§6.4) which mutes agent uplink PCM for their duration and emits a timestamped `0x02` flush afterwards (round 22; watermark semantics round 25 — see Binary media protocol) |
| `call.reattach_media` | `{session_id, lease_id}` | `{lease_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH` — fresh AGENT media credentials for a live lease after the agent's media socket dropped while the control channel lived (round 25); the new pair obeys the same `MEDIA_PAIR_TTL_SEC` handshake deadline and SUPERSEDES the prior agent token, which is invalidated at issue time (round 26) |
| `call.claim_exclusive` | `{session_id, line_id}` | `{claim_id, status: "pending_approval"}` — moderator approval required in admin, never self-granted; pending claims are bounded (≤8 per agent, ≤256 total — `RESOURCE_EXHAUSTED` beyond) and socket-bound (`drop_session` cancels them, round 22). Resolution arrives asynchronously: `event.claim_resolved` to the claiming session and, on approval, `event.assignment_granted` carrying the `assignment_id` the owner needs for `call.release{assignment_id}` (round 25). A line with NO live provider binding (`UNBOUND`) is `LINE_NOT_CLAIMABLE`, not `LINE_NOT_FOUND` (round 27 — it exists and is claimable, just cannot serve calls) |
| `call.get_lines` | `{session_id, intent?}` | `{lines: LineSummary[]}` — `intent?` filters by the line's declared caps; `intent:"exclusive_line"` has no provider cap to match — it returns the claimable-exclusive view: lines whose `assignment.claimable && assignment.mode == SHARED_POOL` (round 24), i.e. the pool lines an agent may still file `call.claim_exclusive` against. Provider-less configured lines appear as `{state:"unbound", provider_id:null}` rows (round 27 — no `UNBOUND` `LineState` member). Owned EXCLUSIVE rows carry `assignment_id` (round 27 — the durable fallback to `event.assignment_granted` for `call.release{assignment_id}`); provider-bound SHARED_POOL rows carry `provider_meta` (round 27). Privacy (round 25): a session sees SHARED_POOL lines plus EXCLUSIVE lines it owns — other agents' exclusive lines are omitted entirely |

## APICallService → Agent — `event.*`

| event | payload |
|---|---|
| `event.incoming_call` | `{line_id, call_token, caller_id?, expires_at}` — first valid `call.accept` wins; `expires_at` is the absolute accept-window deadline (RFC 3339 UTC — the earlier relative `expires_in_ms` would skew by queue delay at outbox drain, round 22) |
| `event.line_acquired` | `{session_id, ticket_id, line_id, lease_id, intent, expires_at}` — the grant for a queued ticket; `ticket_id` stays referenced until its lease ends (a preempted queue-origin grant reissues the SAME `ticket_id` at position 1 via `event.queue_updated`, round 22); `expires_at` is the grant TTL deadline — missing it ends the lease with `event.call_state_changed{reason: "grant_ttl_expired"}` (round 20) |
| `event.queue_updated` | `{session_id, ticket_id, position}` — `position` is the ticket's GLOBAL FIFO rank among ALL eligible tickets (per-line + pool compete in one order, round 27), never a per-line rank |
| `event.missed_call` | `{line_id, caller_id?, reason}` — `voicemail_ref` removed (round 27): no producer ever minted it |
| `event.fallback_triggered` | `{line_id, call_token, caller_id?, policy, expires_at}` — THE fallback delivery channel (round 22): delivered to the `system:fallback_worker` session only, when `accept_window_sec` expires on a WORKER-policy line; `call_token` is the internal `fallback_call_token` (round 14); `expires_at` is the worker's absolute accept deadline bounded by `fallback_accept_ttl_sec` (~10 s, round 23 — a dead worker can never hold a ring open) |
| `event.line_state_changed` | `{line_id, old_state, new_state, reason?}` — broadcast ONLY for SHARED_POOL lines (round 27): on an agent-owned EXCLUSIVE line the event routes ONLY to the owner's `agent` audience plus an `admin` copy — broadcast is forbidden entirely, because even a `reason:null` broadcast copy still leaks the private line's activity (the round-25 broadcast+redaction scheme is superseded) |
| `event.line_health_changed` | `{line_id, old_state, new_state}` — provider liveness transition on the `LivenessState` vocabulary (`reachable`\|`ws_disconnected`\|`unreachable`); replaces the removed DEGRADED/DOWN `LineState` members (round 25). `unbound` is legal ONLY as `old_state` (round 27): the first provider attach on a configured line emits `unbound -> reachable`; it is never a `new_state` — a dying binding reports `unreachable` instead. Same exclusive-line routing as `line_state_changed`: broadcast for SHARED_POOL lines only; EXCLUSIVE-owned lines route owner-agent + admin (round 27) |
| `event.claim_resolved` | `{claim_id, line_id, status, assignment_id?, reason?}` — `status`: `"approved"` \| `"rejected"`; delivered to the claiming session; `assignment_id` present iff approved (round 25) |
| `event.assignment_granted` | `{assignment_id, line_id, mode}` — a durable `LineAssignment` was granted to the agent (approved claim or `admin.assign_line`); carries the `assignment_id` needed for `call.release{assignment_id}` (round 25). `mode` is always `"exclusive"` (round 27). Re-delivered to an owner session registering later — the original emit may predate every live session (round 27); the durable fallback is `LineSummary.assignment_id` on the owner's own `call.get_lines` rows |
| `event.queue_cancelled` | `{session_id, ticket_id, reason}` — a pending ticket was dropped server-side; `reason`: `line_exclusive` \| `provider_lost` \| `capability_removed` \| `claim_resolved` (round 25). Reason split on a claim approval (round 27): the CLAIMING session's own queued tickets on the line die as `claim_resolved` (moot now that the claimant owns it), FOREIGN sessions' tickets die as `line_exclusive` |
| `event.audio_ready` | `{lease_id, line_id}` — BT SCO path is up: early media (ringback) audible, uplink accepted without first-word clipping |
| `event.call_state_changed` | `{lease_id, line_id, state, reason?, answer_confidence?, reissued_ticket_id?}` — `state`: `dialing` \| `alerting` \| `active` \| `disconnected` (the provider's `ringing` maps to `event.incoming_call`, not to a lease event); `answer_confidence`: `"provisional"` \| `"confirmed"` — how sure the bridge is that `active` means a human answered (oFono `State=active` observed firing on remote answer, but the guarantee is provisional — design §5.5); agent may start its greeting only on `state == "active"` AND `audio_ready` received; `reissued_ticket_id` is present only on a `preempted_by_incoming` teardown of an immediate grant taken with `enqueue:true` — it names the NEW head-of-queue ticket that replaced the dead lease (round 23, so an agent with parallel acquires can correlate lease→ticket; the matching `event.queue_updated{position: 1}` names the same id) |
| `event.assignment_revoked` | `{assignment_id, line_id, owner_agent_id, reason?}` (round 19b; `owner_agent_id` REQUIRED since round 22) — a durable AGENT-owned `LineAssignment` ended: owner `call.release{assignment_id}` or admin `revoke_assignment` — ONLY explicit revoke paths (round 23: `idle_revoke_after` expiry is alert-only and emits `event.idle_alert`, never a revoke). Delivered ONLY to the revoked owner's session(s). Emitted ONLY for agent assignments — revoking a `ProviderBinding` has no agent audience and instead produces `line.revoke` + `event.line_health_changed{new_state:"unreachable"}` (round 22; DOWN is a liveness fact, not a LineState, since round 25). Without this event an exclusive owner would learn about the revoke only as an `OWNER_MISMATCH` on its next `call.dial` |
| `event.idle_alert` | `{assignment_id, line_id, owner_agent_id, idle_since}` (round 23) — an EXCLUSIVE line's `idle_revoke_after` elapsed. STRICTLY alert-only: routed to admin-scope subscribers and configured escalation channels, never to ordinary agent sessions and never implying a revoke; emitted at most once per continuous idle period — the alerted latch re-arms on the FIRST line activity (any op refreshing `last_seen_at`) or on the owner opening a session, so a later idle period produces a fresh single alert (round 27). Delivery in v1 (round 24): the EventBus pushes `admin`-audience events to the configured `escalation_hooks` (Webhook / Telegram / Exec targets from admin config) and the admin MCP stream (`c_admin`); `agent.register_intent` has no admin subscription path — an idle alert never requires an agent socket |

**Event audience routing (round 21; `admin` scope added round 23).**
The kernel stamps a routing
audience on every event at emission time (`broadcast` \| `agent` \|
`session` \| `admin`); the EventBus is a stateless router over it and never
re-enters kernel tables after the fact — a torn-down lease or
session must not strand its queued event, and payloads that carry
PII or a one-shot `call_token` must never reach uninvolved agents:

- `event.incoming_call` / `event.missed_call` — the line owner's
  agent on an exclusive line; on a shared-pool line, ONE envelope
  per live session that registered `receive_inbound` intent (each
  stamped `session`, round 22 — a `dial_out`-only session can
  neither see caller PII nor hijack the one-shot `call_token`).
- `event.fallback_triggered` — the `system:fallback_worker` agent
  only.
- `event.assignment_revoked` — the revoked owner only; a pool/
  provider attachment has no agent audience and emits NO
  `assignment_revoked` at all (it produces `line.revoke` +
  `event.line_health_changed{new_state:"unreachable"}`, round 22 —
  DOWN is a liveness fact, not a `LineState`, since round 25).
- `event.line_state_changed` — broadcast for SHARED_POOL lines;
  on an agent-owned EXCLUSIVE line it routes ONLY to the owner's
  `agent` audience plus an `admin` copy — broadcast forbidden
  (round 27).
- `event.line_health_changed` — same rule: broadcast for
  SHARED_POOL lines; EXCLUSIVE-owned lines route owner-agent +
  admin only (round 27).
- `event.claim_resolved` — the claiming session only; a claim whose
  session died resolves as rejected (`event.queue_cancelled`-class
  drop, `CLAIM_NOT_FOUND` on any later approve).
- `event.assignment_granted` — the newly owning agent only;
  replayed to a later-registering owner session (round 27).
- `event.queue_cancelled` — the ticket-owning session only.
- `event.line_acquired`, `event.queue_updated`, `event.audio_ready`,
  `event.call_state_changed` — the owning session only.
- `event.idle_alert` — `admin` scope (round 23): admin subscribers
  and configured escalation channels only, never an ordinary agent
  session. In v1 the delivery legs are the `escalation_hooks`
  (Webhook / Telegram / Exec) plus the admin MCP stream `c_admin`
  (round 24) — there is no `agent.register_intent` path into the
  `admin` audience.

## Queue semantics (mandatory FIFO)

- `call.acquire` with `enqueue:true` that cannot be served joins the
  FIFO queue (per-line + global pool) and returns
  `{granted:false, queued:true, ticket_id, position}` — `position`
  is the ticket's GLOBAL FIFO rank among ALL eligible tickets
  (round 27), not a per-line rank; per-line and pool tickets compete
  in one order, so `position` is always the honest global distance
  from the head.
- Position advances via `event.queue_updated`; on grant the agent
  gets `event.line_acquired` and must act within an admin-configured
  grant TTL (~5 s illustrative) or the next ticket is served.
- **Fairness across scopes (round 19; seq minting fixed round 25;
  refusals clarified round 27):** one monotonic `seq` counter
  covers ALL queue scopes AND is minted at `call.acquire` INGRESS
  for EVERY attempt — an immediate grant consumes a `seq`, and so
  do a refusal (`granted:false`, not queued) and a deduplicated
  retry (same `seq`, no new ticket). Sequence gaps in the queue are
  therefore NORMAL and carry no meaning. When line `L` frees, the
  grant goes to the lowest-`seq` eligible ticket — per-line tickets
  (`line_id == L`) and pool tickets (`line_id` unspecified /
  `None`) compete in one global order; no starvation of either
  scope. Because a preempted immediate grant re-materializes a
  ticket reusing its ORIGINAL ingress `seq` (always lower than any
  later-queued ticket), head insertion never needs a negative or
  fractional sequence (round 25).
- **Server-side ticket drops (round 25; claim-split round 27):** a
  pending ticket is cancelled — `event.queue_cancelled`, never
  silently parked — when its target line becomes EXCLUSIVE
  (`admin.assign_line` / approved claim), when `claimable` flips
  false, when the binding loses the capability the ticket asked
  for, or when the provider is revoked. Reason split on a claim
  approval (round 27): the CLAIMING session's own queued tickets on
  the line die as `claim_resolved` (moot — the claimant owns the
  line now), FOREIGN sessions' tickets die as `line_exclusive`.
  Pending `call.claim_exclusive` entries on a line that became
  owned or `claimable:false` auto-resolve rejected. Queue shifts
  emit `O(queue depth)` `event.queue_updated` envelopes — the
  total is bounded by `MAX_QUEUE_TICKETS_TOTAL`.
- **Dedup scope (round 27):** the `(session_id, idempotency_key)`
  dedup table lives inside the LIVE session only — `drop_session`
  discards it entirely, so a key never dedups across a reconnect.
  Ticket caps are per-session (16) and global
  (`MAX_QUEUE_TICKETS_TOTAL` = 1024, `QUEUE_FULL` beyond).
- **Grant vs incoming-ring race (round 19; ticket lifecycle
  clarified round 22):** a physical `ingest_ring` on a line in the
  granted-but-undialed window PREEMPTS the grant (if the line
  profile admits inbound): the unused lease ends with
  `event.call_state_changed{state: "disconnected",
  reason: "preempted_by_incoming"}`; a queue-origin ticket is
  reissued at the head (`position=1`, same `seq` retained). A
  granted ticket STAYS REFERENCED until its lease ends, so the
  agent learns the reissue as
  `event.queue_updated{session_id, ticket_id, position: 1}` on the
  SAME `ticket_id` it already holds. An IMMEDIATE grant acquired
  with `enqueue:true` materializes a NEW ticket at the head; its
  `ticket_id` is reported in the teardown itself as
  `event.call_state_changed{reason:"preempted_by_incoming",
  reissued_ticket_id}` (round 23 — so a session holding several
  parallel acquires can correlate which lease became which ticket)
  and confirmed by the `event.queue_updated` — the caller's
  enqueue wish is honored; `enqueue:false` grants are torn down
  without reissue.
- **Ephemeral-lease auto-release (round 22):** a `CallLease` never
  outlives its call — `call.hangup` ends call AND lease atomically,
  `provider.call_state{state:"disconnected"}` ends the lease, and
  `call.release{lease_id}` during an ACTIVE call is hangup-intent
  (hangup first, then the lease ends). No line stays `BUSY`
  because an agent forgot to `call.release`. The durable
  `LineAssignment` has a separate release path:
  `call.release{assignment_id}` by its owner (or admin
  `revoke_assignment`).
- Tickets are **socket-bound**: disconnect kills them.
- `call.standby` / `line.standby` REMOVED (round 8): no server-side
  parked requests - client retry is the freshness guarantee.

## Intent / allocation semantics

- `call.acquire` serves **only `dial_out`** (round 14): inbound rings are consumed atomically by `call.accept(call_token)` without a prior grant, and durable exclusive ownership goes through `call.claim_exclusive` — so `acquire` never needs the other intents, which halves the LineManager grant state machine. `call.get_lines` still accepts any `intent?` for capability filtering.
- `dial_out` — line granted from pool for dialing; the ephemeral lease ends on `call.hangup` (auto-release, round 22) or `call.release{lease_id}` (undialed grant frees the line; in-flight call is hung up first).
- `receive_inbound` — a registered inbound intent does NOT hold the line `BUSY` (round 11): the line stays `FREE` (available to the pool unless `exclusive_line`) and transitions to `RINGING_LOCKED` only while a physical call rings; `call.accept` consumes the ring and issues the lease; the lease ends with the call (auto-release, round 22). Only `receive_inbound` sessions see `event.incoming_call` on a shared pool and may `call.accept` (round 22).
- `exclusive_line` — durable `LineAssignment` (survives disconnects, no heartbeat); granted via `call.claim_exclusive` + moderator approval or `admin.assign_line` (round 22); released voluntarily by the owner via `call.release{assignment_id}`; conflict → `LINE_EXCLUSIVE_NOT_OWNER` + `data.free_lines[]`.
- All lines busy → `call.acquire` returns `{granted: false, free_lines: []}` — routine result, not an error; client retries itself. No server-side parked requests (a stale parked request = zombie grant risk; client retry = freshness).
- Inbound: ring → `RINGING_LOCKED` → hooks + `event.incoming_call{call_token}` → first `call.accept` = atomic lease+answer. Accept window default **30 s**; ALL timeouts/windows admin-configurable. Timeout → per-line `fallback_policy`.
- Per-line inbound `fallback_policy` (admin config). CORE policies (executed inside LineManager, atomic telecom ops): `no_pickup` (carrier voicemail - default) | `reject`. WORKER policies (executed by the internal `FallbackWorker` - a system agent that connects via the standard agent API, ADR-01): `record_and_telegram:<channel>` | `answer_and_announce:<announcement_ref>` (only if an inbound agent is registered; `announcement_ref` is the admin-provisioned WAV/text selector the worker plays — carried inside the policy so the FallbackWorker needs no admin scope, round 22) | `forward_via_line:<line_id>:<e164_number>:<country>` (round 23 — the policy must carry the full dial target: `<e164_number>` is strict E.164 and `<country>` the ISO alpha-2 cross-check, both validated at `configure_line` AND again at model construction so a hydrated row cannot bypass them, round 24; the `system:fallback_worker` session bypasses `LINE_EXCLUSIVE_NOT_OWNER` when acquiring the target line, round 22; a self-loop `forward_via_line` whose `line_id` equals the configured line is rejected at `configure_line`). NOTE: no `spill_to_pool` - a live-but-slow owner = missed call.
- **Fallback token mechanics (round 14; delivery channel fixed round 22):** when `accept_window_sec` expires on a WORKER-policy line, the original `call_token` is already stale and an exclusive-owner check would reject the worker's `call.accept`. LineManager therefore re-issues the ring as an internal `fallback_call_token` bound to `agent_id: "system:fallback_worker"` (bypasses `OWNER_MISMATCH` on accept and `LINE_EXCLUSIVE_NOT_OWNER` on a `forward_via_line` acquire, still single-use and TTL-bound) and delivers `event.fallback_triggered{line_id, call_token: fallback_call_token, caller_id?, policy, expires_at}` to the FallbackWorker session only — `event.fallback_triggered` is THE delivery channel (round 22; the earlier "or `event.incoming_call`" hedge is removed because the worker needs the `policy` field).

## LineProvider contract (one codebase)

```ts
interface LineProviderConfig {
  manager_endpoint: string;      // wss://127.0.0.1:7001/provider  |  wss://<wan>:7001/provider
  line_id: string;
  adapter_mac: string;           // BDADDR of the BT dongle - hciX names are unstable across reboots (ADR-03)
  token: string;                 // per-line, provisioned out-of-band
  caps: Intent[];
  profile?: string;
  reconnect: { backoff_ms: [number, number]; jitter_pct: number };
  heartbeat_interval_ms: number;
  media_transport: "ws_pcm";  // UNIFIED: framed PCM over WebSocket (LocalLine -> 127.0.0.1)
}
```

LocalLine and RemoteLine run identical logic end-to-end, including media: audio is ALWAYS packetized and sent over the dedicated media channel (loopback for LocalLine). No local PipeWire fast-path - one MediaSink implementation.

**Unified agent model (round 12, 2026-11-13):** under the vhci-agent
architecture EVERY line provider process runs inside the container —
all adapters are kernel `vhci` devices fed by per-line `hci-agent`s
on the dongle hosts (see `docs/hci-agent.md`). "Remote line" no
longer means a provider running the BT stack remotely; remoteness
lives one layer below, in the agent's `transport_profile` tunnel
(control-TCP + media-UDP HCI relay). The `provider.*`/`line.*`
contract above is unchanged — it now binds worker processes that are
always container-local; a smart-remote LineProvider remains a
documented alternative for links too hostile for HCI-over-IP.

## Binary media protocol (ADR-02, round 11)

Both agents and line providers attach to a server-issued
`media_endpoint` (`unix:///run/phonebridge/media.sock` local or
`wss://gw:7003/media` remote) and a per-lease `media_token`, returned
by `call.dial` / `call.accept` (agent) or `line.dial` / `line.answer`
(provider, round 14). Both endpoint schemes carry the WebSocket
protocol — `unix://` means a WS handshake over AF_UNIX,
`wss://` over TCP/TLS; the frames below are identical on either.

Each lease carries **two independent one-time `media_token`s**
(round 21): the agent's token (issued in `call.dial`/`call.accept`
results) is bound to `(lease_id, "agent")` and the provider's token
(issued in `line.dial`/`line.answer`) is bound to
`(lease_id, "provider")`. `media.auth` succeeds only when the
presented token matches BOTH the lease and the self-declared `role`
— a stolen or misused `"agent"` token cannot open a second socket
as `"provider"` and hijack the phone-side stream. The `role` field
is client-declared, so the `(token → lease_id, expected_role)`
validation is the ONLY line of defense (round 27); implementations
MUST never write the raw `media_token` into access logs — the
auth TEXT frame is logged through the model's masked serialization
only.

The local endpoint is **WebSocket over the Unix socket**, not a raw
`SOCK_STREAM` (round 18): a bare UDS byte stream has no message
boundaries, so an 8-byte `flush`/`keepalive` frame (`0x02`/`0x03`)
would desynchronize a fixed-648-byte parser. WS-over-UDS preserves
the TEXT/BINARY distinction and gives local and remote peers one
identical framing code path (`websockets.unix_serve` —
`websockets>=14` is already a dependency).

1. **Auth handshake** — first frame is TEXT JSON:
   - client: `{"type":"media.auth","role":"agent"|"provider","lease_id":"...","media_token":"..."}`
   - server: `{"type":"media.auth_ok"}` or socket close `4001 Unauthorized`.
2. **Streaming** — all subsequent frames are BINARY with a fixed
   layout: `byte 0` `uint8` flags, `bytes 1..3` `uint24` BE `seq`
   (monotonic, loss detection), `bytes 4..7` `uint32` BE
   `timestamp_ms` (stream-relative), then the payload. The total
   frame length depends on the flag (round 21):
   - `0x01` audio — exactly **648 B**: 8 B header + 640 B payload
     (raw PCM 16 kHz S16_LE mono = 20 ms);
   - `0x02` flush/clear-uplink-buffer, `0x03` keepalive — exactly
     **8 B**: header only, NO payload;
   - any other length is a framing violation — the receiving side
     closes the socket with `1002 Protocol Error`.
   Flush is in-band — `clear_uplink_buffer`/`flush_audio_out` map to flag `0x02`, no control/media race. A `0x02` frame carries NO audio payload; it has PRIORITY over queued audio frames on the same lease — on receipt the bridge discards every still-queued uplink audio frame for that lease whose `timestamp_ms` is ≤ the flush's `timestamp_ms` (round 25 watermark semantics: agent speech submitted AFTER the DTMF `{accepted}` survives, while audio buffered during tone synthesis — which would otherwise play back stale by up to ~16 s — is dropped; the earlier "drop everything queued" rule could erase brand-new post-`{ok}` TTS).

3. **Resource bounds (round 14, CODING_HAZARDS rule 7; revised round 25):**
   - **Pair TTL** — a `media_token` expires **30 s**
     (`MEDIA_PAIR_TTL_SEC`) after issue if no peer completes
     `media.auth`; a half-connected pair (one peer authed, the other
     never arrives) is torn down on the same timer. The earlier ~5 s
     value was incompatible with MCP LLM agents, whose next tool call
     routinely lands >5 s after `call.dial`/`call.accept` returns
     (round 25). A pair-timeout teardown is reported to the kernel,
     which ends the lease with
     `event.call_state_changed{reason:"network_error"}` — a live call
     never silently loses its audio channel. No socket may outlive
     its `lease_id`.
   - **Auth handshake deadline** — a freshly accepted media socket
     MUST send its `media.auth` TEXT frame within **5 s**
     (`MEDIA_AUTH_TIMEOUT_SEC`) or it is closed (round 25). After
     auth, ANY TEXT frame or any flag/length outside the table above
     is a protocol violation → `1002 Protocol Error`.
   - **Socket-loss recovery (rounds 25/27):** the agent's media socket
     dropping while its control channel lives tears down that lease's
     media pair — the agent recovers via `call.reattach_media`. The
     PROVIDER's media socket dropping while its CONTROL channel lives
     is recovered by `provider.reattach_media` (round 27) — fresh
     provider credentials on the same lease; the
     `provider.reregister` → `line.reattach_media` reissue inside the
     orphan-grace window covers only the case where the control
     socket dropped too. An established pair whose half disconnects
     is torn down the same way (no socket outlives its `lease_id`).
   - **Bounded queues + uplink pacing (round 23, ADR-A; direction
     made explicit round 24)** — the two directions are clocked
     DIFFERENTLY (design §6.6). Agent→provider INGRESS is
     CONTINUOUS: the MediaRouter drains the agent's media socket at
     socket speed (non-blocking read, never on a timer — a paced
     read would strand an agent-originated `0x02` barge-in behind
     queued audio inside `SO_RCVBUF` for seconds while the router
     ring idles near-empty); each `0x01` frame appends to the uplink
     ring and a `0x02` frame drains it IMMEDIATELY on receipt.
     Pacing applies strictly on EGRESS: a monotonic pacer writes
     **ONE 640 B frame per 20 ms tick** from the ring into the
     LineProvider socket, so the whole TTS preload buffer lives
     inside the router where the `0x02` flush can actually clear it
     (at socket speed the buffer would migrate into the provider's
     PipeWire queue, out of flush reach). Downlink (provider→agent)
     is forwarded immediately on arrival — it is already clocked by
     PipeWire/SCO. Ring sizes are
     ASYMMETRIC: uplink **1500 frames (30 s, drop-NEWEST + the
     `media.uplink_overflow` counter)** — for long TTS bursts,
     keeping the start of the phrase matters more than the tail;
     downlink **100 frames (2 s, drop-oldest)**. During in-band DTMF
     the LineProvider substitutes tone frames into uplink locally —
     no reverse `0x02` toward the agent is needed; a `0x02` the
     provider does emit is consumed by the MediaRouter (uplink ring
     drain) and is NEVER forwarded to the agent. Symmetrically
     (round 27): an agent-emitted `0x02` drains ONLY the kernel's
     uplink ring — it is consumed at the router and NEVER bridged to
     the provider, so it cannot recall downlink audio already sent
     toward the agent (no barge-in on already-transmitted audio).
     The router never
     buffers unboundedly between mismatched-rate peers.

## Admin surface — kernel operations (round 19, wire contract in c_admin/item_13)

The agent-facing surface has no privileged mutations. Admin-scope
tokens reach a separate kernel interface (`LineAdminKernel`) —
implemented by the same `InMemoryLineManager` class but bound only
to the admin adapter:

- `admin.assign_line {line_id, owner_agent_id}` → grants a durable
  `exclusive_line` assignment WITHOUT a pending claim (round 22 —
  the §4.2 "admin assign" override path; pre-provisions an owner
  before the agent connects and files a claim).
- `admin.approve_claim {claim_id}` → grants `exclusive_line`
  assignment (moderator approval of a pending `call.claim_exclusive`).
- `admin.reject_claim {claim_id, reason?}`.
- `admin.revoke_assignment {assignment_id, reason?}` → ends a durable
  binding (exclusive owner OR provider attachment); an agent-owned
  assignment emits `assignment_revoked` to the owner, a
  `ProviderBinding` emits `line.revoke` +
  `event.line_health_changed{new_state:"unreachable"}` instead
  (round 22; DOWN is a liveness fact, not a `LineState`, since
  round 25).
- `admin.configure_line {line_id, fallback_policy?, accept_window_sec?,
  allowed_number_types?, claimable?, idle_revoke_after?,
  clear_idle_revoke_after?, timeouts?}` →
  runtime patch, no restart; `timeouts` carries the remaining
  admin-configurable knobs `{grant_ttl_sec?, orphan_grace_sec?,
  heartbeat_interval_sec?, heartbeat_degraded_after?,
  heartbeat_down_after?, fallback_accept_ttl_sec?,
  ring_drain_ttl_sec?, max_call_duration_sec?,
  clear_max_call_duration?}` (rounds 22-23; ring-drain and
  max-duration knobs added round 25 — `ring_drain_ttl_sec` bounds
  the RINGING_LOCKED drain wait of design §5.2 step 6,
  `max_call_duration_sec` caps a single call's wall-clock length
  and bounds `record_and_telegram` recording, `None` effective
  value = no cap, `clear_max_call_duration:true` removes a set cap
  and passing it together with `max_call_duration_sec` is
  `INVALID_ARGUMENT`). `heartbeat_interval_sec` is floored at
  0.001 s — millisecond granularity, the wire unit (round 24c). A
  patched
  `heartbeat_interval_sec` takes effect on
  the provider's NEXT `provider.reregister` — the live provider
  learns its interval only from the register response, so the new
  cadence is never enforced against the current session (round 23).
  `idle_revoke_after` patches the alert-only idle deadline;
  `clear_idle_revoke_after:true` disables it (`idle_revoke_after` +
  the flag together is `INVALID_ARGUMENT`, round 25).
  `forward_via_line` policies are validated HERE: `target_number`
  (strict E.164) + `target_country` (ISO alpha-2) go through the
  same dial-time checks, a self-loop (`forward.line_id ==
  line_id`) is `INVALID_ARGUMENT` (round 23), and a transitively
  cyclic forward chain (`A → B → … → A`) is detected by walking the
  stored policies — bounded at 8 hops — and rejected `INVALID_ARGUMENT`
  (round 25); the same checks are
  ALSO enforced at model construction inside
  `ForwardViaLineFallback`/`LineAssignment`, so a corrupt hydrated
  row can never carry an invalid policy past the model boundary
  (round 24). The `timeouts` patch merges ATOMICALLY into the
  line's effective `LineTimeouts` via full model re-validation —
  the heartbeat ladder invariant `heartbeat_down_after >
  heartbeat_degraded_after` is checked on the MERGED values (a lone
  `heartbeat_degraded_after` patch that is legal in isolation can
  still fail against the stored `heartbeat_down_after`); a failed
  merge → `INVALID_ARGUMENT` and the stored values are left
  untouched (round 24b).
- `admin.list_lines` → `AdminLineView[]` (round 27): the admin-scope
  superset of `LineSummary` — every configured line with its durable
  `assignment_id`, `owner_agent_id`, live `lease_id` and the global
  FIFO rank of its head queue ticket. A kernel operation now:
  `CallLease`/`QueueTicket` state lives only inside the kernel, so
  the admin listings route to `LineAdminKernel` (round 27 — the
  earlier "non-kernel admin op" placement had no surface capable of
  answering them).
- `admin.list_leases` → `CallLease[]` (round 27): all live kernel
  leases, bounded by live sessions × `MAX_LEASES_PER_SESSION`.
- `admin.list_tickets` → `QueueTicket[]` (round 27): all pending
  global-FIFO tickets, bounded by `MAX_QUEUE_TICKETS_TOTAL`.
- `admin.remove_line {line_id}` (round 27): deletes a QUIESCENT
  configured line — it must exist and carry no provider binding, no
  live lease, no pending ticket and no pending claim
  (`INVALID_STATE` otherwise). Removes the durable `LineAssignment`
  WITHOUT emitting `assignment_revoked` — the line itself ceases to
  exist, so there is nothing left for a revoked event to describe.

Non-kernel admin operations route to their owning subsystem instead
(they are NOT `LineAdminKernel` methods — the kernel owns call/line
state only, not provisioning or bus plumbing):

- `admin.pair_window {line_id, duration_sec}` → routed by the admin
  adapter to the line-bundle supervisor, which drives the container's
  BlueZ `Agent1` (see hci-agent.md §5); opens the pairing window on
  that line's adapter only. `duration_sec` is clamped to ≤ 300 s
  (round 25; typed `PairWindowRequest` since round 27) and during
  the window SSP confirmation is auto-accepted
  ONLY for the line's configured `paired_phone_mac` — never for an
  arbitrary neighboring Bluetooth device.
- Additional non-kernel admin operations (design §10, round 25):
  API-key lifecycle
  (`admin.issue_key`/`admin.revoke_key` — revocation MUST close the
  revoked key's live sessions). Bind-mounted sockets are created
  `0660` under a dedicated `phonebridge` group; the internal WSS
  listener's TLS material lives under the secrets directory (`0700`)
  and is hot-reloadable without a daemon restart.

## Decisions (developer, 2026-11-12)

1. Media path: SEPARATE sockets — one control WSS + one dedicated media
   channel, used by ALL providers (LocalLine streams to 127.0.0.1).
   Media endpoint is SERVER-ISSUED: gateway allocates the media
   address/port (per line or per call) and returns it in the
   register-ack / call grant; media frames carry stream identity +
   auth so the gateway routes them to the right call (muxing/auth
   inside the media protocol, or per-call ephemeral port).
   Media transport: evaluate RTP+Opus vs framed PCM.
2. Orphaned-lease grace on provider reconnect: **10 s**.
3. Auth = dedicated workstream + admin surface. START: SQLite DB for
   keys (agents + lines) + config files for client configs; web UI
   deferred. Key lifecycle: issue/revoke, scopes provider|agent|admin,
   line config (caps, profile, claimable, accept_window_sec,
   fallback_policy). ADMIN additionally: all timeouts/windows
   configurable; per-line inbound fallback as listed above;
   escalation channels (e.g. Telegram) for idle/broken-line alerts;
   exclusive assignment = CLAIM -> moderator approval; private MCP
   server interface so the developer's trusted AI agent can admin.
4. Remote line (e.g. Raspberry Pi) — UPDATED round 12: the remote
   site runs a dumb `hci-agent` (USB↔HCI relay, no bluetoothd, no
   telephony state) that feeds a kernel `vhci` adapter inside the
   container over a `transport_profile` tunnel (wireguard default;
   direct_mtls; sstp/openconnect/vless_xray reserved). Two channels:
   control-TCP (HCI cmd/ACL/evt + agent mgmt) and media-UDP (SCO).
   See `docs/hci-agent.md` for the agent config format and wire
   protocol. The earlier "full provider on the Pi" shape remains an
   alternative for links too lossy for HCI-over-IP.
