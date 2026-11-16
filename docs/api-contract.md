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
- **Pre-auth handshake (round 30, review-19 B1):** every control
  listener enforces an `AUTH_HANDSHAKE_TIMEOUT_SEC` deadline —
  a freshly accepted socket MUST complete its identity handshake
  (a `provider.register`/`provider.reregister`, an
  `agent.register_intent`, or an `admin.auth` call) inside the
  deadline or it is closed. Before the handshake completes the
  socket accepts ONLY its own handshake call: any other RPC is
  answered with a JSON-RPC `UNAUTHORIZED` error and the socket is
  closed — an unauthenticated socket carries no session/binding
  context to answer against. Unauthenticated connections are
  BOUNDED by a listener-wide cap (`RESOURCE_EXHAUSTED` + close
  beyond it) so a connection flood cannot starve handshake slots,
  and the one-connection-per-peer rule holds from the first
  byte: a peer's second live control connection either is a
  `provider.reregister` supersede (which closes the predecessor
  socket after the reply) or is rejected — `agent.register_intent`
  multiplexing over one socket is forbidden, each call mints its
  own session on its own socket.
- **MCP adapter boundary (round 19):** the MCP surface is the
  CONTROL plane only. A standard MCP client cannot stream binary
  PCM, so a `call.dial`/`call.accept` issued through MCP returns the
  same `{lease_id, media_endpoint, media_token}` — the media socket
  is held either by a dedicated audio worker of the calling agent
  (which receives the endpoint+token out of band) or by a local
  `MediaSidecar` exposing higher-level tools (`speak_text`,
  `listen`). The MCP adapter never owns a media socket itself.

Four listener surfaces, one gateway daemon, shared in-process `LineManager` kernel:

| Surface | Listener (example) | Peer | Namespace |
|---|---|---|---|
| **Manager** | `wss://gw:7001/provider` | Line providers (LocalLine, RemoteLine) | `provider.*` (up), `line.*` (down) |
| **APICallService** | `wss://gw:7002/agent` | AI agents | `agent.*`, `call.*` (up), `event.*` (down) |
| **AdminService** | `unix:///run/phonebridge/admin.sock` (mode 0600) or `wss://gw:7004/admin` | Admin clients (phonebridgectl, admin MCP) | `admin.*` |
| **InternalService** | `unix:///run/phonebridge/internal.sock` (mode 0600, never bind-mounted or network-exposed) | In-container service identities (`system:*`) | `agent.*`, `call.*` (up), `event.*` (down) |

The internal listener is the ONLY socket family where a `system:*`
`agent_id` may register (round 30, review-19 B1): it is bound
inside the container filesystem, never bind-mounted out, and the
adapter additionally verifies the peer's uid via `SO_PEERCRED`
(root or the dedicated `phonebridge` service uid — anything else
is `UNAUTHORIZED` + close). A `system:*` identity presented on
the agent, provider, admin, or any bind-mounted socket is
`UNAUTHORIZED` — the reserved namespace exists to authenticate
kernel-internal workers (the `system:fallback_worker`), never
external callers (round 25).

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
type LivenessState = "reachable"|"degraded"|"ws_disconnected"|"unreachable"|"unbound";
// "degraded" (round 28) is a KERNEL-COMPUTED middle rung of the
// heartbeat ladder reachable → degraded → unreachable — never a
// provider-reported fact. `provider.ping` reports an explicit
// `state` of "reachable"|"unreachable" ONLY (round 30):
// "ws_disconnected" is a kernel-observed socket-loss fact, never a
// provider-reported state, and "unreachable" is TERMINAL on the
// event channel — a later socket-close fact can never move
// unreachable → ws_disconnected and re-arm the orphan grace.
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
  // provider_meta REMOVED (round 28): provider hardware metadata is a
  // privileged fact — it lives on the admin-scoped AdminLineView
  // (admin.list_lines) only, never on the agent-facing listing.
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
| `PROVIDER_UNREACHABLE` | line's provider binding `unreachable` or `ws_disconnected` past orphan grace (heartbeat-down — the kernel-computed `degraded` rung is a warning, not a failure, round 28) |
| `DIAL_REJECTED` | dial string failed pre-line validation — `data.reason` carries a typed sub-code (see Dial validation) |
| `INVALID_STATE` | operation invalid in the current call/line phase — repeated `dial`, `send_dtmf` outside ACTIVE, approving a closed claim (round 20), an `admin` owner change (`assign_line`/`approve_claim`) while the line is `RINGING_LOCKED` (round 30). Typed `data.reason` sub-codes refine it (round 30): `LINE_NOT_CLAIMABLE` — `call.claim_exclusive` (or `admin.approve_claim`) targets a line whose `assignment.claimable` is false or that has no live provider binding — it exists, it just cannot serve an exclusive owner, so this is distinct from `LINE_NOT_FOUND`, which is reserved for a `line_id` absent from the configured set entirely; `CLAIM_NOT_FOUND` — `admin.approve_claim`/`admin.reject_claim` names an unknown, already-resolved, or socket-cancelled `claim_id` — a pending claim dies with its claiming session, so a moderator acting on it afterwards is a typed error, not a no-op, and a claim is pending STATE, never a token (`STALE_TOKEN` is inadmissible) |
| `INVALID_ARGUMENT` | invalid input parameters — DTMF alphabet/length violations (`data.reason: "INVALID_DTMF"`), empty intent sets (round 20) |
| `RESOURCE_EXHAUSTED` | queue capacity or per-session ticket/lease limits exceeded (`data.reason`: `QUEUE_FULL`, `SESSION_LIMIT_EXCEEDED`, `GRANT_TTL_EXPIRED` — round 20; these were previously folded into `STALE_TOKEN`) |
| `UNAUTHORIZED` | missing/invalid/revoked API key or token; a reserved `system:*` identity presented on any non-internal socket (rounds 25/30 — `system:*` registers only on the `internal.sock` listener with `SO_PEERCRED` verification); any non-handshake RPC on a pre-auth connection (round 30) |
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
| `provider.register` | `{line_id, token, caps, profile?, meta?}` | `{assignment_id, heartbeat_interval_ms, provider_media_endpoint?, server_time, pending_rings?}` — `pending_rings` (round 30) carries every ring a `state_snapshot` re-minted through the `ingest_ring` path as `{call_token, caller_id?, observed_at}` — deduplicated by ring identity, never double-minted; absent/empty when the register carried no ringing snapshot entries; the provider MUST echo each minted `call_token` on the ring's later `provider.call_state` updates exactly like a live-call token. `provider_media_endpoint` is the provider-side static socket address (agent-side endpoints come from `MediaCredentials.agent_media_endpoint` inside each grant — round 25 endpoint split); the per-lease `media_token` arrives later inside `line.dial`/`line.answer` (round 14). `heartbeat_interval_ms` is stamped from the line's effective `heartbeat_interval_sec` as nearest-millisecond (halves up — `int(sec * 1000 + 0.5)`, round 27; Python `round()` is banker's) clamped to ≥1 ms and bounded above by `MAX_HEARTBEAT_INTERVAL_MS` = 86 400 000 (24 h — the fail-closed converter rejects non-finite, non-positive, or overflowing intervals, round 30) — the interval is floored at 0.001 s (millisecond granularity, the wire unit) so a valid config can never yield a zero cadence (round 24c). `meta` is bounded (round 22): ≤16 keys, keys ≤64 chars, values ≤256 chars — `INVALID_ARGUMENT` beyond. `caps` MUST be non-empty and telecom-only (`dial_out`/`receive_inbound`): an empty set or any `exclusive_line` entry → `INVALID_ARGUMENT` (round 24) — durable exclusivity is a `LineAssignment.claimable`/`mode` property granted by the claim flow, never a hardware capability |
| `provider.reregister` | same as register + `state_snapshot?` | idempotent; preserves lease state during the 10 s orphan-grace window — `state_snapshot` lists the calls the provider still sees on the D-Bus (`{lease_id?, call_token?, state, caller_id?}[]`, `lease_id`/`call_token` absent for a call the gateway never issued) so the kernel can re-bind surviving leases (round 21, design §5.4) — bounded at **64 entries** on the wire, `INVALID_ARGUMENT` beyond (round 30 — a line's call set can never legitimately approach that: at most one established call plus a handful of pre-dial grants); an element with `state:"ringing"` rides the `ingest_ring` path and returns its minted token in `pending_rings` (round 30), an element with NEITHER `lease_id` nor `call_token` is legal only when the line holds exactly ONE orphaned lease or external call to attribute it to — zero or multiple candidates is `INVALID_ARGUMENT` (round 30); every preserved lease receives FRESH provider media credentials via `line.reattach_media` — media tokens are one-shot and died with the dropped socket (round 25); grace covers the WS-drop level only — provider-reported tunnel/USB death or heartbeat-down unreachable = immediate teardown, no grace (round 20). **Method-name distillation (round 28; hardened in the round-28 fix):** the wire carries the two method names — the adapter distills WHICH one arrived into `ProviderRegistration.reregister` (`false` for `provider.register`, `true` for `provider.reregister`); the flag is derived ONLY from the RPC method name, a `reregister` key in the wire params is ignored and never distilled (a provider cannot self-promote a register into a supersede). A `provider.reregister` on a live REACHABLE **or DEGRADED** binding is a LEGAL half-open supersede (round 28; DEGRADED added in the fix — still a live-control state; WS_DISCONNECTED keeps the orphan-grace reattach path, UNREACHABLE classifies as fresh register — the provider's fresh control channel arrived before the old socket's death was detected): the old binding is marked superseded, a FRESH `assignment_id` is minted, and live leases are preserved; with `state_snapshot` present they are reconciled against it, with `state_snapshot` absent they are preserved WITHOUT reconciliation (the binding hand-over alone does not kill calls). The snapshot is authoritative ONLY for leases in a `dialing`-or-later phase — an undialed `ACQUIRED` grant is pre-dial, so an empty snapshot never kills it (round 28). A `provider.register` on an already-live binding is `INVALID_STATE` — reconnect inside the grace window uses `provider.reregister` (round 25) |
| `provider.ping` | `{line_id, assignment_id, state, state_snapshot?, checked_at?}` | `{ok, server_time}` — `state` is the provider-REPORTED rung `"reachable"` \| `"unreachable"` (round 30 — the field is now explicit: `"degraded"` is kernel-computed from elapsed silence, `"ws_disconnected"` is a kernel-observed socket-loss fact, `"unbound"` is a listing projection — none is ever a legal `state` value here). Missed pings escalate `LivenessState` along the `heartbeat_degraded_after`/`heartbeat_down_after` ladder `reachable` → `degraded` → `unreachable`, surfaced as `event.line_health_changed`; a `reachable` ping on the live channel restores `degraded` → `reachable`, and a control-socket drop on a DEGRADED binding resolves `degraded` → `ws_disconnected` — the socket-loss axis overrides the elapsed-time rung so the orphan grace stays reachable (round-28 fix); `unreachable` is TERMINAL on the event channel — a later socket-close fact can never move `unreachable` → `ws_disconnected` and re-arm the grace (round 30). The comparison is elapsed-time based — `now - last_seen_at` vs the stamped thresholds, no separate missed-count counter (round 27). `checked_at` is an optional provider-side RFC 3339 UTC stamp of the liveness observation — ADVISORY ONLY (round 28): a dedupe/ordering key scoped to `(line_id, accepted_stamp)` (round 30 re-scope — the per-LINE monotonic watermark survives a reregister, so a stamp replayed by a NEW binding still dedupes against the line's recorded watermark; facts naming a superseded `assignment_id` are already `STALE_TOKEN` through the closed-assignment ring before dedup is consulted), never a deadline input — `last_seen_at` is always KERNEL receipt time, ONLY a stamp deeper than `MAX_CLOCK_SKEW_SEC` (30 s) into the FUTURE is normalized to receipt time at ingestion, and a PAST stamp is retained verbatim as the dedupe key, never feeding elapsed-time or liveness math (round-28 fix). The watermark is MONOTONE-MAX per scope (round 30): a verbatim PAST stamp below the recorded maximum stays a dedupe key for detecting repeats but NEVER regresses the ordering — an out-of-order fact is dropped as a duplicate, never applied backwards. `state_snapshot` is legal only on `state:"reachable"` pings (round 27 — a dead/dying channel's call list is not reconciled; the domain model rejects it on any other state). A `reachable` ping clears the orphan-grace deadline only when it arrives on the binding's OWN live channel generation — the adapter stamps every accepted provider control socket with a monotonically increasing `channel_seq` the kernel records on the binding, so a delayed ping on a still-lingering OLDER socket is recorded but never disarms the deadline; inside the grace window the authoritative recovery is `provider.reregister` (rounds 27/30) |
| `provider.capability_update` | `{line_id, assignment_id, caps, profile?}` | `{ok}` — replaces caps/profile on the ACTIVE binding; carries NO `token` — the binding id authenticates the peer ONLY together with arrival on that binding's own live control channel (round 27: the adapter verifies request `assignment_id` == the socket's binding id; bare id knowledge is not a credential). `profile` absent/`null` = leave unchanged; there is no clear-profile op in v1 (round 27). An identical update is an idempotent no-op (round 22); the same `caps` invariant as `provider.register` applies — non-empty and telecom-only, `exclusive_line` → `INVALID_ARGUMENT` (round 24) |
| `provider.call_state` | `{line_id, assignment_id, call_token?, lease_id?, caller_id?, state, reason?, observed_at?}` | `{ok, call_token?}` — `state` is the canonical set `ringing` \| `dialing` \| `alerting` \| `active` \| `disconnected`. `caller_id` carries the inbound caller's identity on `ringing` — BEST-EFFORT E.164 (round 30): the field is a bounded non-empty string (≤ `MAX_CALLER_ID_LEN` = 64 chars) that normally holds an E.164 number, but CLIR/withheld markers (`"anonymous"`, `"restricted"`) and alphanumeric sender strings are legal values because the air side cannot always produce digits; a withheld identity may also be expressed by omitting the field entirely (never by `""`). **`call_token` minting (round 22):** on `state:"ringing"` the gateway mints the one-shot `call_token` inside `ingest_ring` and returns it in `{ok, call_token}`; the provider MUST echo that `call_token` on every subsequent update for the same ring — including a pre-answer `disconnected` (early caller hangup) — until `line.answer` hands the call a `lease_id`. **Burned-token correlation (round 30):** the echo obligation survives the token's own expiry — once a ring is decided (`call.accept`, a fallback dispatch, `missed_call`) its `call_token` is BURNED as an action token but stays the ring's CORRELATION key until the line returns to `FREE`: a `state:"active"` echo of the burned token during ring-drain is the handset-answered `external_call` case below, and a `disconnected` echo ends the drain or the external call — never `STALE_TOKEN`. For rings the gateway discovered via `state_snapshot` (the provider never received a token), a `disconnected` update with NEITHER id on a `RINGING_LOCKED` line resolves to that line's one open ring; the minted token for a snapshot ring arrives in the register response's `pending_rings` (round 30). On every other state the response is `{ok}`. `lease_id` identifies an established call. Recognized `observed_at` is an optional provider-side RFC 3339 UTC stamp of the transition observation (gateway stamps receipt time when absent; ordering/dedupe is scoped to `(line_id, lease_or_call, accepted_stamp)` — round 30 re-scope: the per-LINE monotonic watermark survives a reregister, so a stamp replayed by a NEW binding still dedupes against the line's recorded watermark, and facts naming a superseded `assignment_id` are already `STALE_TOKEN` through the closed-assignment ring before dedup is consulted; the watermark is MONOTONE-MAX per scope — a verbatim PAST stamp below the recorded maximum stays a dedupe key for detecting repeats but NEVER regresses the ordering, an out-of-order fact is dropped as a duplicate, and phase legality is governed by the forward transition matrix, never by stamp comparisons; a same-stamp repeat is a duplicate, not a regression, round 25; ADVISORY ONLY, round 28 — ONLY a stamp deeper than `MAX_CLOCK_SKEW_SEC` (30 s) into the FUTURE is normalized to receipt time at ingestion, a PAST stamp is retained verbatim as the dedupe key, and deadline arithmetic never reads the provider clock (round-28 fix)). `reason` is REJECTED unless `state:"disconnected"` (round 26 — a reason on a still-live call state is a contradiction). Recognized `reason` values on `disconnected` — the provider-reported vocabulary is CLOSED (round 21): `normal`, `busy`, `no_answer`, `rejected`, `network_error`, `dongle_lost` (tunnel/USB death — immediate teardown, no orphan grace), `stack_restart` (vanished call found by `GetCalls()` reconciliation after a bus/name-owner restart). Gateway-minted reasons such as `provider_lost` (orphan-grace expiry or heartbeat timeout) are emitted on the agent event channel as `event.call_state_changed{reason:"provider_lost"}`, never reported here. An update carrying NEITHER `call_token` nor `lease_id` on a line NOT in `RINGING_LOCKED` reports a human-initiated call on the physical phone (round 21): the line goes `BUSY` with reason `external_call`, no lease is minted, and no `line.hangup` is ever sent for it (design §9). Symmetrically (round 28), an `active` update that DOES carry `call_token` on a still `RINGING_LOCKED` line means a human answered the ringing call on the physical handset before any `call.accept` burned the token — the ring closes WITHOUT minting a lease, the line goes `BUSY` with reason `external_call` exactly like a dialed external call (no lease, no PipeWire bridge, no `line.hangup`), and the state clears on the later `disconnected` report (design §9). **Late facts on an ended lease (round 30):** a `call_state` update naming a `lease_id` (or burned `call_token`) whose lease already ended resolves through the kernel's closed-lease tombstone ring as a dedupe/no-op — `{ok}`, never `STALE_TOKEN`: a provider legitimately reports a teardown the gateway already performed (saga compensation, admin revoke, TTL teardown), and erroring it would fabricate a desync that never happened |
| `provider.audio_ready` | `{line_id, assignment_id, lease_id}` | `{ok}` — one-shot notification: the provider's local BT SCO path is up (early media flows, uplink accepted). Gateway emits `event.audio_ready` to the agent from this signal |
| `provider.audio_lost` | `{line_id, assignment_id, lease_id?}` | `{ok}` — the provider's BT SCO path dropped while the control channel and the call lived (round 30): the lease's `audio_ready` latch is RESET (a later `call.send_dtmf` is `INVALID_STATE` again until a fresh `provider.audio_ready`) and the uplink/downlink bridge drains its buffers for the lease. `lease_id` is omitted only for a line-scoped loss that resets every live lease on the line. The latch ALSO resets implicitly on `provider.reregister` (the preserved lease's SCO path cannot be assumed across a socket supersede) and on the provider's media socket dropping — in all three cases the provider MUST re-report `provider.audio_ready` before tones or uplink are accepted again |
| `provider.reattach_media` | `{line_id, assignment_id, lease_id}` | `{media_endpoint, media_token}` / `STALE_TOKEN` (dead binding or lease) / `OWNER_MISMATCH` — fresh PROVIDER media credentials for a live lease when the provider's media socket dropped while its control channel stayed up (round 27): `line.reattach_media` covers only the `provider.reregister` reissue path, which needs the control socket to drop first. The reissued pair SUPERSEDES the prior provider-side pair at issue time and obeys a fresh `MEDIA_PAIR_TTL_SEC` handshake window counted from reissue — the provider MUST re-run `media.auth{role:"provider"}` inside it |

**Superseded-binding facts and socket discipline (round 29,
review-1 B2/B12):** one control socket carries AT MOST ONE live
`assignment_id` for the socket's entire lifetime — a
`provider.reregister` always arrives on a NEW connection, and an
adapter MUST NOT multiplex several bindings on one socket (the
request `assignment_id` MUST equal the binding minted on THAT
socket, as `provider.capability_update` already requires). A
`provider.reregister` supersedes the current live binding
deterministically — REACHABLE, DEGRADED, or a binding minted
moments earlier by a racing reregister; sequential last-writer-wins
applies. When a supersede lands, the adapter MUST close the
superseded control socket AFTER answering the superseding
reregister on the new connection — and any `provider.*` fact
(`ping`, `call_state`, `capability_update`, `audio_ready`,
`reattach_media`) still in flight on the OLD socket carries a
superseded `assignment_id` that resolves through the closed
`_closed_assignments` ring and is rejected `STALE_TOKEN`, never
applied to the new binding: a racing TCP FIN in flight must not
let stale facts reach the successor. The same `STALE_TOKEN`
classification covers any dead or unknown `assignment_id` — the
closed ring is one bounded space shared by `LineAssignment` and
`ProviderBinding` ids (`MAX_CLOSED_ASSIGNMENTS`, round 28/29).
**Channel generations (round 30):** the adapter stamps every
ACCEPTED provider control socket with a monotonically increasing
`channel_seq` and records the highest generation on the binding —
it is a gateway-internal stamp, never a wire param: the provider
does not send it. The generation is what makes "a REACHABLE fact
clears the orphan deadline only on the binding's own live
channel" enforceable: a delayed `provider.ping` or `call_state`
riding a still-lingering OLDER socket is recorded but never
disarms `orphan_deadline_at`, and after a supersede only facts
stamped with the new socket's generation can affect the new
binding.

## Manager → Line — `line.*` (down-channel commands)

| method | params | returns |
|---|---|---|
| `line.dial` | `{assignment_id, lease_id, number, media_endpoint, media_token}` | `{accepted}` — `number` is the already-validated E.164 string, passed through unchanged (see Dial validation); `media_endpoint`/`media_token` authorize the provider's `media.auth` for this `lease_id`. **Saga compensation (round 23; hangup leg added round 25, ordering fixed round 30):** the gateway answers the agent's `call.dial` without waiting for this reply; the adapter MUST await `{accepted}` with a ~2 s deadline and, on `{accepted:false}`/timeout/transport error, issue a best-effort `line.hangup{lease_id}` FIRST — the hangup is the modem teardown for a dial that may have physically reached the phone (WS dropped after delivery, or a slow radio wakeup past the deadline) — and only THEN end the lease with `network_error` through the kernel. The adapter MUST NOT synthesize a `provider.call_state{state:"disconnected"}` fact on this path (round 30 — a fabricated fact would lie about an observation the provider never reported); a genuine provider `call_state` arriving later for the already-ended lease dedupes through the closed-lease ring as `{ok}` |
| `line.answer` | `{assignment_id, lease_id, call_token, media_endpoint, media_token}` | `{accepted}` — same per-lease media credentials as `line.dial`; same ~2 s `{accepted}` deadline + hangup-first `network_error` compensation as `line.dial` (rounds 23/25/30 — never a fabricated `provider.call_state`). **Ring correlation (round 25):** a line has AT MOST one open ring — the provider answers whichever ring is open on its line regardless of which `call_token` value arrives (original ring token or re-issued `fallback_call_token`); the field is informational for the provider's own bookkeeping |
| `line.hangup` | `{assignment_id, lease_id?, call_token?}` | `{accepted}` — exactly ONE target (round 23): `lease_id` tears down an established call; `call_token` rejects a still-unanswered inbound ring (the `reject` fallback path - no `lease_id` exists before `call.accept`; round 28 — inside ring-drain the gateway queues this best-effort hangup ONLY on paths where the GATEWAY rejects the call (`reject` fallback policy), never on `no_pickup`/missed outcomes where the caller already left, and never on the handset-answered `external_call` path). Same by-line ring correlation as `line.answer` — the provider targets its one open ring (round 25). **`{accepted:false}`/timeout (round 30):** the hangup is best-effort MODEM teardown — the lease already ended kernel-side before the command was queued, so a refused or unanswered reply NEVER reopens it and MUST NOT be synthesized into a `provider.call_state` fact; the adapter MAY retry the hangup once while the binding stays live (a physically ringing phone must not be abandoned), and drops it when the binding dies |
| `line.reattach_media` | `{assignment_id, lease_id, media_endpoint, media_token}` | `{accepted}` — fresh provider media credentials for a lease PRESERVED across `provider.reregister` (round 25): media tokens are one-shot, so the dropped socket's pair is dead and the kernel reissues the provider role's half; the provider MUST re-run `media.auth` with the new token inside a FRESH `MEDIA_PAIR_TTL_SEC` (30 s) window counted from the reissue instant (round 27 — the dead socket's old deadline is meaningless). A reissued pair SUPERSEDES the prior one — the old `media_token` becomes invalid at issue time (round 26: single active pair per lease; the replay window closes at the reissue instant, not the pair TTL) and the gateway FORCE-CLOSES the prior role socket if it is still live (close `4001` reason `superseded`, round 30 — a half-open predecessor socket must never block the new `media.auth`). **`{accepted:false}`/timeout (round 30):** the reissued pair stays armed — the provider retries `media.auth` inside the same 30 s window; if the window expires without a completed auth, the lease ends `network_error` as on every pair-deadline breach |
| `line.send_dtmf` | `{assignment_id, lease_id, digits}` | `{accepted}` — **`{accepted:false}`/timeout (round 30):** the tone is LOST and the lease continues — in-band DTMF is a cosmetic side-channel, so the adapter logs the refusal and moves on; no compensation, no fabricated `provider.call_state` (a transport error is a channel-health fact that feeds the ping ladder, never a call teardown reason by itself) |
| `line.revoke` | `{assignment_id, reason?}` | `{accepted}` — **admin scope only**. **`{accepted:false}`/timeout (round 30):** the binding is already dead kernel-side (the revoke mutation committed before the command was queued), so the adapter treats any refusal as terminal and closes the provider's control socket — the `STALE_TOKEN` discipline covers facts the dead socket may still emit |

Sent as server-initiated JSON-RPC requests over the provider's own inbound connection — no reverse dial needed. **Dead-binding cleanup (round 28 — moved from drain time to mutation time):** commands queued for a binding that dies or is superseded are handled INSIDE the mutation that killed or replaced the binding (`provider.reregister` supersede, an UNREACHABLE liveness fact, `admin.revoke_assignment`, heartbeat-down `tick`) — outbox entries whose leases survive a re-bind (`line.hangup`, `line.send_dtmf`, `line.reattach_media`) are RETARGETED to the new `assignment_id`, while commands that can never land (the superseded binding's in-flight `line.dial`/`line.answer` whose lease did not re-bind) are dropped fail-closed and their leases end with the same `disconnected`/`network_error` effect IN THE SAME TRANSACTION, so teardown events reach the same cycle's `drain_events()` and the adapter owes no compensation for commands it never saw. The drain (`drain_provider_commands`) keeps only a DEFENSIVE drop for a command addressed to a binding that vanished with no successor — a safety net, never the mechanism (round 28). Exception: `line.revoke` still delivers while the binding's control channel is open — an UNREACHABLE-by-heartbeat binding can hold a live socket (round 27). The command/event outbox itself is BOUNDED (round 30): agent-initiated resource-creating calls (`call.acquire`, `call.dial`, `call.accept`, `call.claim_exclusive`) pre-flight against `OUTBOX_SOFT_CAP` = 256 and refuse `RESOURCE_EXHAUSTED` when the outbox already stands at the cap, while teardown, provider-fact, fanout and admin cascades bypass that soft pre-flight but remain bounded by `OUTBOX_HARD_CAP` — every mutation checks the hard capacity BEFORE committing partial state, so a burst can never wedge the command channel halfway through a cascade.

## APICallService — `agent.*` / `call.*` (Agent → gateway)

| method | params | returns / errors |
|---|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` | `{session_id, server_time}` / `INVALID_ARGUMENT` on an empty `intents` set; `server_time` is the gateway's UTC clock at registration (RFC 3339 — round 29: the reconciliation reference for absolute `expires_at` deadlines on `event.incoming_call`/`event.line_acquired`, so a re-delivered ring's remaining accept window can be computed against server time rather than local clock guesswork); `INVALID_INTENT` when `intents` contains `exclusive_line` — durable exclusivity is requested only via `call.claim_exclusive` (round 21). `agent_id` MUST match the API key's bound identity (`FORBIDDEN` on mismatch); `system:*` agent ids are RESERVED for kernel-internal service identities provisioned out-of-band — they register ONLY on the private `internal.sock` listener (which additionally verifies `SO_PEERCRED`); on every other socket a `system:*` identity is `UNAUTHORIZED` (rounds 25/30). At most 32 concurrent sessions per `agent_id` AND 512 sessions globally (`RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED`, round 27). Every call mints a NEW `session_id` — there is no update-intents op; a reconnect does not resume the old session (round 27). `profile?` is an optional session label ≤128 chars for observability (round 27). A reconnecting session re-receives any LIVE ring as a fresh `event.incoming_call` (same `call_token`, original `expires_at`, round 25) — on an EXCLUSIVE line only for its owner; on a SHARED_POOL line for ANY new session holding `receive_inbound` (round 27). The `system:fallback_worker` identity is excluded from pool ring fan-out and re-delivery alike (round 28) — it is dispatched via `event.fallback_triggered`, never as an `incoming_call` subscriber. An owner registering later also gets a replayed `event.assignment_granted` with its current `assignment_id` (round 27) |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?, idempotency_key?}` | `{granted: true, line_id, lease_id, expires_at}` or `{granted: false, free_lines[]}` or `{granted: false, queued: true, ticket_id, position}` — `intent` must be `dial_out` AND must sit inside the SESSION's registered intent set (round 14/28; other intents or an intent the session never registered → `INVALID_INTENT`); `expires_at` is the grant TTL deadline (round 20; missing it → `event.call_state_changed{reason: "grant_ttl_expired"}`); retry dedup is scoped to a non-empty `(session_id, idempotency_key)` whose entries live only inside the LIVE session and die with it via `drop_session` — a key never dedups across a reconnect (round 27); the dedup entry records the OUTCOME (granted `lease_id` or queued `ticket_id`) so a same-key retry returns the recorded outcome while the object lives, and the same key with different params → `INVALID_ARGUMENT` (round 28); the dedup table itself is bounded — at most `MAX_IDEMPOTENCY_KEYS_PER_SESSION` = 256 distinct keys per live session, a NEW key beyond the cap is `RESOURCE_EXHAUSTED` fail-closed (never LRU-trimmed — evicting a recorded outcome would let a same-key retry re-execute, round 29); calls without a key create independent tickets so multi-call agents are not merged; `position` is the GLOBAL FIFO rank across all eligible tickets (round 27); `profile?` is an optional per-call label ≤128 chars (round 27) — it is recorded on the queued `QueueTicket` and the granted `CallLease` inherits it verbatim, so the label survives queueing (round-28 fix); `free_lines` carries only servable alternatives — SHARED_POOL lines advertising `dial_out` whose binding liveness is neither `unbound` nor `unreachable` (round 28 — dead hardware is never suggested as an alternative); `LINE_EXCLUSIVE_NOT_OWNER` (+ `data.free_lines[]`) — except a `system:fallback_worker` session, which bypasses this check to execute admin-configured `forward_via_line` policy (round 22); `RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED` on per-session limits — at most 16 queued tickets per session and 1024 globally (`QUEUE_FULL`, round 25). Queue liveness (round 28): a head-of-line ticket whose session already sits at `MAX_LEASES_PER_SESSION` is SKIPPED on each grant attempt — no head-of-line blocking, re-evaluated per attempt, not parked; a preemption reissue never re-checks queue caps; a POOL ticket (no `line_id`) is cancelled `event.queue_cancelled{reason:"provider_lost"}` when the set of servable SHARED_POOL `dial_out` lines becomes EMPTY — checked on every liveness/caps/assignment mutation; a `claimable` flip alone never cancels queued tickets |
| `call.cancel_acquire` | `{session_id, ticket_id}` | `{ok}` — withdraw a queued acquire; `OWNER_MISMATCH` when the ticket belongs to a different session (round 30 — a ticket is socket-bound, so a foreign session can never cancel it); an already-granted or already-cancelled `ticket_id` is an idempotent `{ok}` no-op through the tombstone table |
| `call.release` | `{session_id, lease_id?, assignment_id?}` | `{ok}` / `OWNER_MISMATCH` / `STALE_TOKEN` (unknown durable `assignment_id`) — exactly ONE target (round 22): `lease_id` ends the EPHEMERAL lease (an undialed grant frees the line; an in-flight call is hung up first — the lease never outlives the call); `assignment_id` is the durable-binding path — the exclusive owner voluntarily releases its `LineAssignment` (line reverts to the claimable pool, `event.assignment_revoked` to the owner; the `assignment_id` itself arrives via `event.assignment_granted`, round 25); zero or BOTH targets passed → `INVALID_ARGUMENT` (round 24 — the XOR is a hard wire rule, not a preference) |
| `call.dial` | `{session_id, lease_id, number, country, idempotency_key?}` | `{lease_id, media_endpoint, media_token}` / `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE`, `DIAL_REJECTED` — `idempotency_key?` (round 30; ≤ `MAX_IDEMPOTENCY_KEY_LEN` = 128 chars) rides the same per-session dedup table as `call.acquire`: a same-key retry inside the `MAX_IDEMPOTENCY_KEYS_PER_SESSION` window returns the STORED result — including the minted media credentials — instead of re-dialing |
| `call.accept` | `{session_id, call_token, idempotency_key?}` | `{lease_id, line_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH` — atomic: issues the lease, answers the ring, attaches media; the accepting session MUST hold `receive_inbound` intent (`INVALID_INTENT` otherwise, round 22 — `system:fallback_worker` exempt with its minted `fallback_call_token`). `idempotency_key?` (round 30) records the STORED accept result so a same-key retry returns the minted `lease_id` and media credentials instead of racing a second `line.answer` at the modem |
| `call.hangup` | `{session_id, lease_id}` | `{ok}` — ends the call AND the ephemeral lease atomically (auto-release, round 22); a trailing `call.release` is an idempotent no-op |
| `call.send_dtmf` | `{session_id, lease_id, digits}` | `{ok}` — `digits` must match `^[0-9A-D*#,wW]{1,32}$` (`,`/`w`/`W` = ~500 ms pauses) else `INVALID_ARGUMENT`/`INVALID_DTMF`; `INVALID_STATE` unless the lease is (ACTIVE or ALERTING) AND `audio_ready` was received (round 27 — early-media IVR means SCO is up while alerting; a tone sent before `audio_ready` has no uplink to ride and the `0.3–0.6 s` `State=active`→SCO-up gap would swallow it anyway) — the `audio_ready` latch RESETS on `provider.audio_lost`, on `provider.reregister`, and on the provider media socket dropping, so a post-loss `send_dtmf` is `INVALID_STATE` again until a fresh `provider.audio_ready` (round 30); in-band tones are synthesized by the LineProvider (oFono `SendTones` is broken on PipeWire Telephony, design §5.5/§6.4) which mutes agent uplink PCM for their duration and emits a timestamped `0x02` flush afterwards (round 22; watermark semantics round 25 — see Binary media protocol) |
| `call.reattach_media` | `{session_id, lease_id}` | `{lease_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH` — fresh AGENT media credentials for a live lease after the agent's media socket dropped while the control channel lived (round 25); the new pair obeys the same `MEDIA_PAIR_TTL_SEC` handshake deadline and SUPERSEDES the prior agent token, which is invalidated at issue time (round 26) — and the gateway FORCE-CLOSES the prior agent media socket if it is still live (close `4001` reason `superseded`, round 30 — a half-open predecessor must never block the new `media.auth`) |
| `call.claim_exclusive` | `{session_id, line_id, idempotency_key?}` | `{claim_id, status: "pending_approval"}` — `idempotency_key?` (round 30) dedups retries the same way: a same-key retry returns the stored `claim_id` instead of minting a second pending claim toward the per-agent cap; moderator approval required in admin, never self-granted; pending claims are bounded (≤8 per agent, ≤256 total — `RESOURCE_EXHAUSTED` beyond) and socket-bound (`drop_session` cancels them, round 22). Resolution arrives asynchronously: `event.claim_resolved` to the claiming session and, on approval, `event.assignment_granted` carrying the `assignment_id` the owner needs for `call.release{assignment_id}` (round 25). A line with NO live provider binding (`unbound`, or `unreachable` past its orphan grace — the uniform "live binding" definition, round 30) is `LINE_NOT_CLAIMABLE`, not `LINE_NOT_FOUND` (round 27 — it exists and is claimable, just cannot serve calls). A still-PENDING claim resolves automatically when its line's provider binding dies: `event.claim_resolved{status:"rejected", reason:"provider_lost"}` (round 28 — a dead line cannot honour an exclusive owner, so the claim must not sit pending until a moderator notices) |
| `call.get_lines` | `{session_id, intent?, offset?, limit?}` | `{lines: LineSummary[]}` — `intent?` filters by the line's declared caps; `intent:"exclusive_line"` has no provider cap to match — it returns the claimable-exclusive view: lines whose `assignment.claimable && assignment.mode == SHARED_POOL` (round 24) AND whose binding is live — a "live provider binding" means `liveness` is NEITHER `unbound` NOR `unreachable` everywhere this contract uses the phrase (rounds 28/30 — `unbound`/`unreachable` rows are EXCLUDED from this filter: a providerless or dead-hardware line cannot serve a new exclusive owner, so offering it in the claim view is a lie), i.e. the pool lines an agent may still file `call.claim_exclusive` against. Provider-less configured lines appear as `{liveness:"unbound", caps:[]}` rows in the unfiltered/telecom views (round 27 — `unbound` is a `LivenessState` listing value, never a `LineState`; inside the teardown window such a row may still carry `state:"busy"|"ringing_locked"` while the leases drain). Owned EXCLUSIVE rows carry `assignment_id` (round 27 — the durable fallback to `event.assignment_granted` for `call.release{assignment_id}`). `LineSummary` carries NO `provider_meta` since round 28 — provider hardware metadata is privileged and lives only on the admin-scoped `AdminLineView` (`admin.get_line`; the `admin.list_lines` compact projection omits it too, round 30). Privacy (round 25): a session sees SHARED_POOL lines plus EXCLUSIVE lines it owns — other agents' exclusive lines are omitted entirely. Paged via the shared `{offset?, limit?}` window (round 29 — see Bounded list pagination below) |

**Bounded list pagination (round 29, review-1 B1; bound named
`MAX_LIST_PAGE`, PR #74 review; extended to the full admin surface
round 30):** every listing
method — `call.get_lines`, `admin.list_lines`, `admin.list_leases`,
`admin.list_tickets`, `admin.list_pending_claims`,
`admin.list_assignments`, `admin.list_provider_bindings` — accepts
an optional `{offset?: int >= 0,
limit?: int 1..MAX_LIST_PAGE}` window (`limit` defaults to
**`MAX_LIST_PAGE` = 256**,
`offset` to `0`; a negative `offset` or a `limit` outside
`1..MAX_LIST_PAGE`
is `INVALID_ARGUMENT`). The result is a STABLE-ORDERED slice of the
method's deterministic enumeration — `line_id` for line listings,
`(created_at, lease_id)` for `CallLease` rows (the lease carries no
`seq`; the mint time plus the unique lease id is deterministic),
`seq` for `QueueTicket` rows (the mandatory FIFO order),
`(created_at, claim_id)` for `PendingClaim` rows and
`assignment_id` for the two binding listings — so paging a
quiescent listing never skips or repeats a row. A full
`limit`-sized page means "more rows may exist — advance `offset`
by `limit`"; a short page (including an empty one) ends the list.
The window is MANDATORY, not optional-polish: every JSON-RPC
message is exactly one WS TEXT frame hard-capped at **64 KiB** (see
Control-channel framing), so an unbounded `AdminLineView[]` or
`QueueTicket[]` could physically exceed one frame at scale — the
caller pages until a short page instead. **Compact projections
(round 30, review-19 B1):** `admin.list_lines` additionally returns
the COMPACT `AdminLineSummary` row — `AdminLineView` WITHOUT the
`provider_meta` mirror — so a max-size 256-row page stays well
inside the 64 KiB frame cap even at maximum field lengths; the
full row including `provider_meta` is fetched per line via
`admin.get_line`.

## APICallService → Agent — `event.*`

| event | payload |
|---|---|
| `event.incoming_call` | `{line_id, call_token, caller_id?, expires_at}` — first valid `call.accept` wins; `expires_at` is the absolute accept-window deadline (RFC 3339 UTC — the earlier relative `expires_in_ms` would skew by queue delay at outbox drain, round 22). **Re-delivery deadline semantics (round 29, review-1 B11):** `expires_at` is stamped on the SERVER clock and is the ONLY authoritative deadline — agents reconcile it against the `server_time` returned by `agent.register_intent`, never their local wall clock. A re-delivered ring (round 25/27) carries the ORIGINAL `expires_at` — the accept window is NEVER extended by re-delivery, so a session reconnecting late legitimately receives a near-expired deadline: that is intentional, the ring's lifetime belongs to the physical call, not to the delivering session |
| `event.line_acquired` | `{session_id, ticket_id, line_id, lease_id, intent, expires_at}` — the grant for a queued ticket; `ticket_id` stays referenced until its lease ends (a preempted queue-origin grant reissues the SAME `ticket_id` at position 1 via `event.queue_updated`, round 22); `expires_at` is the grant TTL deadline — missing it ends the lease with `event.call_state_changed{reason: "grant_ttl_expired"}` (round 20) |
| `event.queue_updated` | `{session_id, ticket_id, position}` — `position` is the ticket's GLOBAL FIFO rank among ALL eligible tickets (per-line + pool compete in one order, round 27), never a per-line rank |
| `event.missed_call` | `{line_id, caller_id?, reason}` — `voicemail_ref` removed (round 27): no producer ever minted it; `reason` rides the closed `CallEndReason` vocabulary, which gained `max_call_duration` in round 28 (see the `call_state_changed` row below) — BUT `max_call_duration` is INADMISSIBLE on `missed_call` (round-28 fix): a max-duration end is a lease teardown (`call_state_changed{disconnected}` + ended lease), never an unanswered ring |
| `event.fallback_triggered` | `{line_id, call_token, caller_id?, policy, expires_at}` — THE fallback delivery channel (round 22): delivered to the `system:fallback_worker` session only, when `accept_window_sec` expires on a WORKER-policy line; `call_token` is the internal `fallback_call_token` (round 14); `expires_at` is the worker's absolute accept deadline bounded by `fallback_accept_ttl_sec` (~10 s, round 23 — a dead worker can never hold a ring open) |
| `event.line_state_changed` | `{line_id, old_state, new_state, reason?}` — broadcast ONLY for SHARED_POOL lines (round 27): on an agent-owned EXCLUSIVE line the event routes ONLY to the owner's `agent` audience plus an `admin` copy — broadcast is forbidden entirely, because even a `reason:null` broadcast copy still leaks the private line's activity (the round-25 broadcast+redaction scheme is superseded) |
| `event.line_health_changed` | `{line_id, old_state, new_state}` — provider liveness transition on the `LivenessState` vocabulary (`reachable`\|`degraded`\|`ws_disconnected`\|`unreachable`); replaces the removed DEGRADED/DOWN `LineState` members (round 25). `unbound` is legal ONLY as `old_state` (round 27): the first provider attach on a configured line emits `unbound -> reachable`; it is never a `new_state` — a dying binding reports `unreachable` instead. `degraded` (round 28) is the kernel-computed middle rung of the heartbeat ladder: it enters ONLY as `reachable -> degraded` and resolves into `degraded -> reachable` (a live ping restored health), `degraded -> unreachable` (elapsed-time escalation) or `degraded -> ws_disconnected` (control-socket loss — the socket-loss axis overrides the elapsed-time rung so the 10 s orphan grace stays reachable, round-28 fix; `ws_disconnected` remains the only state that arms that grace) — `ws_disconnected`/`unreachable` can never transition INTO `degraded`. Same exclusive-line routing as `line_state_changed`: broadcast for SHARED_POOL lines only; EXCLUSIVE-owned lines route owner-agent + admin (round 27) |
| `event.claim_resolved` | `{claim_id, line_id, status, assignment_id?, reason?}` — `status`: `"approved"` \| `"rejected"`; delivered to the claiming session; `assignment_id` present iff approved (round 25) |
| `event.assignment_granted` | `{assignment_id, line_id, mode}` — a durable `LineAssignment` was granted to the agent (approved claim or `admin.assign_line`); carries the `assignment_id` needed for `call.release{assignment_id}` (round 25). `mode` is always `"exclusive"` (round 27). Re-delivered to an owner session registering later — the original emit may predate every live session (round 27); the durable fallback is `LineSummary.assignment_id` on the owner's own `call.get_lines` rows |
| `event.queue_cancelled` | `{session_id, ticket_id, reason}` — a pending ticket was dropped server-side; `reason`: `line_exclusive` \| `provider_lost` \| `capability_removed` \| `claim_resolved` (round 25). Reason split on a claim approval (round 27): the CLAIMING session's own queued tickets on the line die as `claim_resolved` (moot now that the claimant owns it), FOREIGN sessions' tickets die as `line_exclusive` |
| `event.audio_ready` | `{lease_id, line_id}` — BT SCO path is up: early media (ringback) audible, uplink accepted without first-word clipping. The lease's `audio_ready` latch behind `call.send_dtmf` resets on `provider.audio_lost`, `provider.reregister`, or a provider media socket drop (round 30) |
| `event.call_state_changed` | `{lease_id, line_id, state, reason?, answer_confidence?, reissued_ticket_id?}` — `state`: `dialing` \| `alerting` \| `active` \| `disconnected` (the provider's `ringing` maps to `event.incoming_call`, not to a lease event); `answer_confidence`: `"provisional"` \| `"confirmed"` — how sure the bridge is that `active` means a human answered (oFono `State=active` observed firing on remote answer, but the guarantee is provisional — design §5.5); agent may start its greeting only on `state == "active"` AND `audio_ready` received; `reissued_ticket_id` is present only on a `preempted_by_incoming` teardown of an immediate grant taken with `enqueue:true` — it names the NEW head-of-queue ticket that replaced the dead lease (round 23, so an agent with parallel acquires can correlate lease→ticket; the matching `event.queue_updated{position: 1}` names the same id); `reason` rides the closed `CallEndReason` vocabulary, which gained `max_call_duration` in round 28 — the wall-clock lease cap `lease.created_at + line.max_call_duration_sec` enforced by `tick` on DIALING/ALERTING/ACTIVE leases (an undialed `ACQUIRED` grant is exempt — the grant TTL owns its deadline); the same cap bounds `record_and_telegram` recording |
| `event.assignment_revoked` | `{assignment_id, line_id, owner_agent_id, reason?}` (round 19b; `owner_agent_id` REQUIRED since round 22) — a durable AGENT-owned `LineAssignment` ended: owner `call.release{assignment_id}` or admin `revoke_assignment` — ONLY explicit revoke paths (round 23: `idle_revoke_after` expiry is alert-only and emits `event.idle_alert`, never a revoke). Delivered ONLY to the revoked owner's session(s). Emitted ONLY for agent assignments — revoking a `ProviderBinding` has no agent audience and instead produces `line.revoke` + `event.line_health_changed{new_state:"unreachable"}` (round 22; DOWN is a liveness fact, not a LineState, since round 25). Without this event an exclusive owner would learn about the revoke only as an `OWNER_MISMATCH` on its next `call.dial` |
| `event.idle_alert` | `{assignment_id, line_id, owner_agent_id, idle_since}` (round 23) — an EXCLUSIVE line's `idle_revoke_after` elapsed. STRICTLY alert-only: routed to admin-scope subscribers and configured escalation channels, never to ordinary agent sessions and never implying a revoke; emitted at most once per continuous idle period — the alerted latch re-arms on the FIRST line activity or on the owner opening a session, so a later idle period produces a fresh single alert (round 27). `last_seen_at` — the idle clock — refreshes ONLY on owner line operations and on `drop_session` closing the owner's last session (round 28 — a bare provider-side `ingest_ring` never counts as owner activity: the phone ringing is not the owner using the line). Delivery in v1 (round 24): the EventBus pushes `admin`-audience events to the configured `escalation_hooks` (Webhook / Telegram / Exec targets from admin config) and the admin MCP stream (`c_admin`); `agent.register_intent` has no admin subscription path — an idle alert never requires an agent socket |

**Reserved wire name (round 30):** `event.media_overflow
{lease_id, line_id}` is minted once per contiguous overflow burst
when agent-uplink backpressure engages — the agent's uplink
produced frames faster than the paced 20 ms egress could drain
the router ring, the router stopped reading the agent's media
socket and emitted a `0x04 OVERFLOW` frame back to the producer
(see Binary media protocol), so the agent MUST pause/throttle TTS
preload until socket reads resume. Its domain payload lands with
the media-overflow contract slice, so the name is deliberately
absent from the normative table above until then (the
doc↔contract drift tests assert 1:1 parity).

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
- **Server-side ticket drops (round 25; claim-split round 27; claimable
  fix round 28):** a pending ticket is cancelled —
  `event.queue_cancelled`, never silently parked — when its target
  line becomes EXCLUSIVE (`admin.assign_line` / approved claim),
  when the binding loses the capability the ticket asked for, or
  when the provider is revoked; a POOL ticket additionally dies as
  `provider_lost` when the set of servable SHARED_POOL `dial_out`
  lines becomes empty (round 28 — checked on every liveness, caps
  and assignment mutation). A `claimable` flip to false is NOT a
  ticket fate (round 28): the line keeps serving the pool, only the
  pending CLAIMS on it auto-resolve rejected. Reason split on a claim
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
   - **Token consumption is ATOMIC (round 29, review-1 B3):** a
     one-shot `media_token` burns under a single
     compare-and-consume, never a check-then-invalidate pair — so
     two concurrent `media.auth` attempts carrying the same token
     produce exactly ONE `auth_ok`; every loser is closed
     `4001 Unauthorized`. The same atomicity covers the role slot:
     a `media.auth` on a role that already owns a LIVE media socket
     for that `(lease_id, role)` pair is rejected — the pair's slot
     is occupied until that socket dies (socket-drop recovery
     re-issues credentials via `call.reattach_media`/
     `provider.reattach_media`, item 3 below).
2. **Streaming** — all subsequent frames are BINARY with a fixed
   layout: `byte 0` `uint8` flags, `bytes 1..3` `uint24` BE `seq`
   (monotonic, loss detection), `bytes 4..7` `uint32` BE
   `timestamp_ms` (stream-relative), then the payload. **Sequence
   and clock bases (round 30):** `seq` is ONE shared `uint24`
   counter per direction covering ALL frame kinds (`0x01` audio and
   the `0x02`/`0x03`/`0x04` control frames alike) — a control frame
   consumes a sequence value like any other frame so gap detection
   cannot be fooled by an interleaved control burst; it wraps
   modulo 2^24. `timestamp_ms` is `uint32` milliseconds counted
   from the moment `media.auth_ok` completed on that socket
   (auth-time is `0`), wraps modulo 2^32, and every comparison —
   including the `0x02` watermark test below — uses RFC-1982-style
   serial comparison: `a` is "before or at" `b` iff
   `0 <= (b - a) mod 2^32 < 2^31`, so a wrap can never reorder the
   ring. The total
   frame length depends on the flag (round 21):
   - `0x01` audio — exactly **648 B**: 8 B header + 640 B payload
     (raw PCM 16 kHz S16_LE mono = 20 ms);
   - `0x02` flush/clear-uplink-buffer, `0x03` keepalive — exactly
     **8 B**: header only, NO payload;
   - `0x04` OVERFLOW — exactly **8 B**, router→producer only
     (round 30): emitted once per contiguous uplink-overflow burst
     when the router stops reading the agent socket (see Resource
     bounds); `timestamp_ms`/`seq` ride the router's own stream;
   - any other length is a framing violation — the receiving side
     closes the socket with `1002 Protocol Error`.
   Flush is in-band — `clear_uplink_buffer`/`flush_audio_out` map to flag `0x02`, no control/media race. A `0x02` frame carries NO audio payload; it has PRIORITY over queued audio frames on the same lease — on receipt the bridge discards every still-queued uplink audio frame for that lease whose `timestamp_ms` is ≤ the flush's `timestamp_ms` (round 25 watermark semantics; round 28 clock-domain fix — the `0x02` `timestamp_ms` is a watermark IN THE AGENT'S UPLINK STREAM CLOCK: the `timestamp_ms` value of the last agent `0x01` frame the LineProvider consumed before muting uplink for DTMF synthesis, or `0` when no frame was consumed yet — never a router-local wall-clock stamp). **Discard scope (round 29, review-1 B7):** "still-queued" means frames buffered inside the MediaRouter's uplink ring that have NOT yet been written to the LineProvider socket by the egress pacer — a frame already delivered to the provider is out of the router's reach and cannot be recalled, while a frame still in the ring with `timestamp_ms` > the watermark SURVIVES the flush untouched. The watermark is a provider-consumed stamp in the agent-stream clock domain, so it may lag behind the freshest frame sitting in the ring — that gap is exactly the audio the provider never heard and the flush is meant to drop. Agent speech submitted AFTER the DTMF `{accepted}` — frames stamped above the watermark — survives, while audio buffered during tone synthesis — which would otherwise play back stale by up to ~16 s — is dropped; the earlier "drop everything queued" rule could erase brand-new post-`{ok}` TTS. **Agent-originated `0x02` — barge-in (round 30):** the AGENT's own `0x02` (barge-in — the user/agent wants its queued TTS abandoned NOW, e.g. the callee started speaking) uses the RESERVED marker `timestamp_ms = 0xFFFFFFFF` and means DISCARD ALL still-queued uplink frames for the lease regardless of timestamp — the whole preloaded ring is flushed, not just the pre-watermark prefix. The marker value is deliberately outside the legal stream-clock range the serial-comparison rule could ever match (a real `0x01` stamp never reaches 2^32−1 inside a call), so a barge-in can never be misread as a provider DTMF watermark and vice versa: the provider's `0x02` always carries a real watermark in the agent's stream clock, the agent's `0x02` always carries the discard-all marker. Both directions are still consumed at the router and never forwarded onward.

3. **Resource bounds (round 14, CODING_HAZARDS rule 7; revised round 25):**
   - **Pair TTL** — a `media_token` expires **30 s**
     (`MEDIA_PAIR_TTL_SEC`) after issue if no peer completes
     `media.auth` — the deadline always counts from the FRESH
     issue/reissue instant (round 28: a rotated credential gets a
     fresh window, the dead credential's old deadline never carries
     over); media authentication happens AFTER the endpoint/token
     pair was delivered inside the grant or command, so the clock
     starts at minting. A half-connected pair (one peer authed, the other
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
   - **Socket-loss recovery is PER-ROLE (rounds 25/27; per-role rule
     round 28):** a media socket dropping invalidates only ITS OWN
     role's credentials and closes its own socket — the surviving
     role's token and socket are untouched: the two halves of a pair
     have independent lifetimes, so losing one half never breaks the
     pair wholesale. An ESTABLISHED pair that loses a half gets a
     re-attach deadline of `MEDIA_PAIR_TTL_SEC` (30 s) counted from
     the drop: if the detached role does not complete a fresh
     `media.auth` inside that window (agent side via
     `call.reattach_media`, provider side via
     `provider.reattach_media`), the lease ends with
     `event.call_state_changed{reason:"network_error"}` — a live call
     never silently loses its audio channel. Attach/detach is a
     KERNEL fact (round 30): the media adapter feeds every
     successful `media.auth` and every role-socket close into the
     kernel's `ingest_media_state(lease_id, role, attached)` — an
     attach disarms the re-attach deadline, a detach (or a
     `*.reattach_media` reissue) arms a FRESH `MEDIA_PAIR_TTL_SEC`
     window per role, and `tick` ends the lease on expiry; an
     attach/detach fact on an unknown or already-ended lease is a
     dedupe/no-op or `STALE_TOKEN`, never a minted external call. The agent's media socket
     dropping while its control channel lives is recovered via
     `call.reattach_media`. The PROVIDER's media socket dropping while
     its CONTROL channel lives is recovered by
     `provider.reattach_media` (round 27) — fresh provider
     credentials on the same lease; the `provider.reregister` →
     `line.reattach_media` reissue inside the orphan-grace window
     covers only the case where the control socket dropped too. A
     never-authenticated (half-connected) pair stays under the same
     `MEDIA_PAIR_TTL_SEC` pairing deadline from issue — no socket may
     outlive its `lease_id`.
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
     ASYMMETRIC: uplink **1500 frames (30 s)** and downlink
     **100 frames (2 s, drop-oldest)**. **Uplink overflow =
     backpressure, not silent drop (round 30):** when the uplink
     ring is full the MediaRouter STOPS READING the agent's media
     socket — the kernel socket buffer and TCP/UDS flow control
     apply real backpressure to the producer — and emits ONE
     `0x04 OVERFLOW` control frame plus ONE
     `event.media_overflow{lease_id, line_id}` per contiguous
     overflow burst (the `media.uplink_overflow` counter still
     ticks). The producer contract: an agent MUST NOT stream audio
     faster than real-time minus jitter headroom — a TTS preload
     burst paces itself to ≤1 `0x01` frame per 20 ms once it sees
     `0x04`, resuming only when socket reads drain (its own writes
     start blocking-free again). Frames already in the ring are
     never dropped by overflow — the paced egress still delivers
     them in order. Downlink keeps the drop-oldest bound. During in-band DTMF
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
   - **Keepalive timing (round 30):** `0x03` keepalive frames ride
     the same shared `seq`/`timestamp_ms` space as audio — a sender
     MUST emit `0x03` at least once per **5 s** of stream silence
     (no `0x01`/`0x02` frames in flight), and a receiver MAY treat
     **15 s** without ANY frame — audio, control, or keepalive — as
     a dead media socket and close it, which lands as a detach fact
     on `ingest_media_state` and arms the role's re-attach window.
     WS-level ping/pong remains the transport liveness floor; the
     `0x03` cadence is the application-level proof the media peer
     is still processing, not merely connected.

## Admin surface — `admin.*` wire namespace (round 19; wire surface formalized round 30, review-19 B1)

The agent-facing surface has no privileged mutations. Admin calls
arrive on the **dedicated admin listener** —
`unix:///run/phonebridge/admin.sock` (mode `0600`, group
`phonebridge`) or the `wss://<host>:7004/admin` path for remote
tooling — never multiplexed onto the agent or provider sockets.
**Admin auth (round 30):** the connection's first call MUST be
`admin.auth {api_key}` → `{ok, server_time}`; the key's `admin`
scope is checked at handshake time (inside
`AUTH_HANDSHAKE_TIMEOUT_SEC`, like every listener), and every
later call on the socket inherits the authenticated admin
identity — an expired/revoked key is `UNAUTHORIZED`, a
non-`admin`-scope key is `FORBIDDEN`. Admin-scope
tokens reach a separate kernel interface (`LineAdminKernel`) —
implemented by the same `InMemoryLineManager` class but bound only
to the admin adapter. Two admin faces are kernel-INTERNAL and
never wire methods: boot-time **hydration** (`hydrate_lines`
restores durable line/assignment rows verbatim — ids, owner,
mode, profile, `last_seen_at`, timeouts, claimability — emits no
events and rotates no ids; the whole batch is loaded before
forward targets and cycles are validated, so a dangling target or
cycle rejects the ENTIRE batch; hydration replaces prior state
wholesale) and the write-through **rollback hook**
(`restore_line(snapshot)` — the only legal compensation after a
failed durable write: `snapshot=None` removes a just-created
record, a previous snapshot is restored verbatim, compensating
events keep the outbox truthful, orphan deadlines on a restored
disconnected binding are preserved, and the failure surfaces as
`domain.config_stale` plus an admin alert).

**Wire methods (round 30 — every admin RPC and its response schema):**

| method | params | returns |
|---|---|---|
| `admin.auth` | `{api_key}` | `{ok, server_time}` — the connection handshake; the ONLY call a fresh admin socket accepts (see Pre-auth handshake) |
| `admin.provision_line` | `{line_id, profile?, fallback_policy?, accept_window_sec?, allowed_number_types?, claimable?, idle_revoke_after?, timeouts?}` | `{assignment}` — the created `LineAssignment` / `INVALID_ARGUMENT`, `RESOURCE_EXHAUSTED` |
| `admin.configure_line` | `{line_id, fallback_policy?, profile?, accept_window_sec?, allowed_number_types?, claimable?, idle_revoke_after?, clear_idle_revoke_after?, timeouts?}` | `{assignment}` — the patched `LineAssignment` / `INVALID_ARGUMENT`, `LINE_NOT_FOUND` |
| `admin.remove_line` | `{line_id}` | `{ok}` / `LINE_NOT_FOUND`, `INVALID_STATE` |
| `admin.assign_line` | `{line_id, owner_agent_id}` | `{assignment_id, line_id, mode}` — the granted durable `LineAssignment` (`mode:"exclusive"`) / `LINE_NOT_FOUND`, `INVALID_STATE`, `INVALID_ARGUMENT` |
| `admin.approve_claim` | `{claim_id}` | `{assignment}` — the granted `LineAssignment` / `CLAIM_NOT_FOUND`, `LINE_NOT_CLAIMABLE`, `INVALID_STATE` |
| `admin.reject_claim` | `{claim_id, reason?}` | `{ok}` / `CLAIM_NOT_FOUND` |
| `admin.revoke_assignment` | `{assignment_id, reason?}` | `{ok}` — `{revoked:false}` on an already-tombstoned id (idempotent) / `INVALID_STATE` |
| `admin.get_line` | `{line_id}` | `{line}` — the FULL `AdminLineView` row including `provider_meta` / `LINE_NOT_FOUND` |
| `admin.list_lines` | `{offset?, limit?}` | `{lines: AdminLineSummary[]}` — the COMPACT projection (no `provider_meta`) |
| `admin.list_leases` | `{offset?, limit?}` | `{leases: CallLease[]}` |
| `admin.list_tickets` | `{offset?, limit?}` | `{tickets: QueueTicket[]}` |
| `admin.list_pending_claims` | `{offset?, limit?}` | `{claims: PendingClaim[]}` — `{claim_id, line_id, requester_agent_id, session_id, created_at}` rows |
| `admin.list_assignments` | `{offset?, limit?}` | `{assignments: LineAssignment[]}` |
| `admin.list_provider_bindings` | `{offset?, limit?}` | `{bindings: ProviderBinding[]}` |
| `admin.pair_window` | `{line_id, duration_sec}` | `{ok}` / `INVALID_ARGUMENT`, `LINE_NOT_FOUND` |
| `admin.issue_key` | `{agent_id, scopes[], line_id?, expires_at?}` | `{key_id, api_key}` — the raw key material is returned ONCE here and never re-listed |
| `admin.revoke_key` | `{key_id}` | `{ok}` — revocation also closes the revoked key's live sessions |

Semantics, bounds and error detail for each method:

- `admin.provision_line {line_id, profile?, fallback_policy?,
  accept_window_sec?, allowed_number_types?, claimable?,
  idle_revoke_after?, timeouts?}` → `LineAssignment` (round 28):
  creates a new configured line in the default pool shape —
  `SHARED_POOL` mode, `owner_agent_id == None`, `claimable`
  defaulting to `True` — visible immediately to agents as an
  `unbound` row and attachable by a provider. `line_id` must not
  already exist (`INVALID_ARGUMENT`); the configured-line set is
  capped at `MAX_LINES_TOTAL` = 256 — a provisioning attempt
  beyond the cap is `RESOURCE_EXHAUSTED` (round 29, keeps the
  forward-chain walk, admin listings and liveness sweeps finite); `profile?` (≤128 chars) is
  the administrative label; every other field follows the
  `configure_line` validation rules. Kernel-first write-through
  ordering applies (same as `configure_line`): the kernel object is
  created, then the durable row is written.
- `admin.assign_line {line_id, owner_agent_id}` → grants a durable
  `exclusive_line` assignment WITHOUT a pending claim (round 22 —
  the §4.2 "admin assign" override path; pre-provisions an owner
  before the agent connects and files a claim). `INVALID_STATE`
  while the line is `RINGING_LOCKED` (round 30 — an owner change
  mid-ring would break the ring's audience routing and the
  exclusive-owner checks the pending `call.accept` was evaluated
  against; the operator waits for the ring to resolve or tears it
  down first). `owner_agent_id` is a checked agent id — a
  `system:*` identity is `INVALID_ARGUMENT` (round 30: durable
  ownership can never belong to a service identity; the fallback
  worker executes admin policy through its own session, it does
  not own lines).
- `admin.approve_claim {claim_id}` → grants `exclusive_line`
  assignment (moderator approval of a pending
  `call.claim_exclusive`); `CLAIM_NOT_FOUND` on an unknown,
  already-resolved, or socket-cancelled `claim_id` (round 30 —
  see the error table); `LINE_NOT_CLAIMABLE` when the line's
  binding died since the claim was filed; the same
  `RINGING_LOCKED` → `INVALID_STATE` owner-change guard as
  `assign_line` applies (round 30).
- `admin.reject_claim {claim_id, reason?}` → `CLAIM_NOT_FOUND` on
  an unknown or already-resolved `claim_id` (round 30).
- `admin.revoke_assignment {assignment_id, reason?}` → ends a durable
  binding (exclusive owner OR provider attachment); an agent-owned
  assignment emits `assignment_revoked` to the owner, a
  `ProviderBinding` emits `line.revoke` +
  `event.line_health_changed{new_state:"unreachable"}` instead
  (round 22; DOWN is a liveness fact, not a `LineState`, since
  round 25). A `SHARED_POOL` `LineAssignment` id is `INVALID_STATE`
  (round 28 — a pool line has no owner to revoke and the line itself
  is retired via `admin.remove_line`); repeating a provider-binding
  revoke on an already-dead binding is an idempotent no-op, not an
  error (round 28).
- `admin.configure_line {line_id, fallback_policy?, profile?,
  accept_window_sec?,
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
  `idle_revoke_after` patches the alert-only idle deadline —
  strictly positive and bounded at 86400 s (round 28 — a
  `timedelta.max`-scale value would overflow the
  `last_seen_at + idle_revoke_after` hydration arithmetic);
  `clear_idle_revoke_after:true` disables it (`idle_revoke_after` +
  the flag together is `INVALID_ARGUMENT`, round 25). `profile?`
  patches the administrative label (round 28 — `None` = unchanged,
  a non-empty value ≤128 chars; there is no clear-profile op).
  `forward_via_line` policies are validated HERE: `target_number`
  (strict E.164) + `target_country` (ISO alpha-2) go through the
  same dial-time checks, `forward.line_id` MUST resolve to an
  existing configured line (`LINE_NOT_FOUND`, round 28 — a dead
  forward target is a config defect, not a deferred discovery),
  a self-loop (`forward.line_id ==
  line_id`) is `INVALID_ARGUMENT` (round 23), and the cycle check
  covers the FULL chain through the new edge in BOTH directions
  (round 28): stored PREDECESSORS — policies that already forward
  INTO this line — plus the forward traversal of the target chain,
  so a new edge closing a cycle (`A → B → … → A`) from either end
  or pushing the combined depth past 8 hops (`MAX_FORWARD_HOPS`) is
  rejected `INVALID_ARGUMENT`; on a SUCCESSFUL policy mutation every
  line whose stored chain passes through the changed one is
  revalidated too (round 28 — a `fallback_policy` patch or removal
  must not leave a dangling or over-deep downstream chain); the same checks are
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
- `admin.list_lines {offset?, limit?}` → `AdminLineSummary[]`
  (round 27; paged via the shared Bounded list pagination window,
  round 29; COMPACT projection round 30): the admin-scope
  superset of `LineSummary` — every configured line with its durable
  `assignment_id`, `owner_agent_id`, live `lease_id` and the global
  FIFO rank of its head queue ticket — PLUS the stored `profile`
  label, but WITHOUT the `provider_meta` mirror: `AdminLineSummary`
  is `AdminLineView` minus `provider_meta`, so a full
  `MAX_LIST_PAGE` = 256-row page stays inside the 64 KiB frame cap
  even at maximum field lengths (round 30, review-19 B1 — a
  `provider_meta`-carrying page could reach ~1.28 MiB and die on
  the transport's `1009 Message Too Big`). The complete row —
  including the live binding's bounded `provider_meta` — is
  fetched per line via `admin.get_line {line_id}` → `AdminLineView`
  (`LINE_NOT_FOUND` on a configured line that does not exist).
  A kernel
  operation now:
  `CallLease`/`QueueTicket` state lives only inside the kernel, so
  the admin listings route to `LineAdminKernel` (round 27 — the
  earlier "non-kernel admin op" placement had no surface capable of
  answering them).
- `admin.list_pending_claims {offset?, limit?}` → `PendingClaim[]`
  (round 30 — surfaced on the wire, paged): every claim awaiting
  moderator decision as `{claim_id, line_id, requester_agent_id,
  session_id, created_at}`, ordered by `(created_at, claim_id)`;
  bounded by `MAX_PENDING_CLAIMS_TOTAL` = 256.
- `admin.list_assignments {offset?, limit?}` → `LineAssignment[]`
  (round 30 — surfaced on the wire, paged): every durable
  agent-ownership record verbatim (`assignment_id`, `line_id`,
  `owner_agent_id`, `mode`, `claimable`, `profile`, `last_seen_at`,
  `accept_window_sec`, `idle_revoke_after`, `fallback_policy`,
  `allowed_number_types`), ordered by `assignment_id` — the same
  row shape `hydrate_lines` restores.
- `admin.list_provider_bindings {offset?, limit?}` →
  `ProviderBinding[]` (round 30 — surfaced on the wire, paged):
  every live provider attachment (`assignment_id`, `line_id`,
  `caps`, `profile`, `meta`, heartbeat thresholds, `connected_at`,
  `last_seen_at`, `liveness`, `channel_seq`), ordered by
  `assignment_id`; the `liveness` column is the kernel-computed
  rung — `unbound` can never appear here (a missing binding is the
  absent row, `AdminLineView` projects `unbound` for it).
- `admin.list_leases {offset?, limit?}` → `CallLease[]` (round 27;
  paged, round 29): all live kernel
  leases, bounded by live sessions × `MAX_LEASES_PER_SESSION`.
- `admin.list_tickets {offset?, limit?}` → `QueueTicket[]` (round 27;
  paged, round 29): all pending
  global-FIFO tickets, bounded by `MAX_QUEUE_TICKETS_TOTAL`.
- `admin.remove_line {line_id}` (round 27): deletes a QUIESCENT
  configured line — it must exist and carry no provider binding, no
  live lease, no pending ticket and no pending claim
  (`INVALID_STATE` otherwise), AND no OTHER configured line's
  `forward_via_line` policy may still target it (round 28 —
  deleting a forward target would leave a dangling chain the
  FallbackWorker discovers only at ring time; the operator retargets
  or clears the referencing policy first). Removes the durable
  `LineAssignment` — emitting `assignment_revoked` to the owner
  BEFORE deleting when the line was agent-OWNED (round 28 — the
  owner's `assignment_id` must not silently stop resolving); a
  SHARED_POOL line emits no event, the line itself ceases to exist.
  Kernel-first write-through ordering: the kernel deletes its record
  first, the admin adapter drops the `lines` row second (round 28).

Non-kernel admin operations route to their owning subsystem instead
(they are NOT `LineAdminKernel` methods — the kernel owns call/line
state only, not key provisioning or bus plumbing):

- `admin.pair_window {line_id, duration_sec}` → routed by the admin
  adapter to the line-bundle supervisor, which drives the container's
  BlueZ `Agent1` (see hci-agent.md §5); opens the pairing window on
  that line's adapter only. `duration_sec` is REJECTED when > 300 s
  — the `PairWindowRequest` model bound is `0 < duration_sec <= 300`,
  there is no clamp and no default (round 28; typed
  `PairWindowRequest` since round 27) — and during
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

## Decisions (developer, 2026-11-12; refreshed round 30)

1. Media path: SEPARATE sockets — one control WSS + one dedicated media
   channel, used by ALL providers (LocalLine streams to 127.0.0.1).
   Media endpoint is SERVER-ISSUED: gateway allocates the media
   address/port (per line or per call) and returns it in the
   register-ack / call grant; media frames carry stream identity +
   auth so the gateway routes them to the right call (muxing/auth
   inside the media protocol, or per-call ephemeral port).
   Media transport: **DECIDED (ADR-02, round 11)** — framed raw PCM
   16 kHz S16_LE over WebSocket (`media_transport: "ws_pcm"`), the
   Binary media protocol section above is the normative wire shape;
   RTP+Opus was evaluated and dropped (codec loss on the BT SCO
   boundary + per-frame watermark semantics Opus could not give).
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
   **Wire shape DECIDED (round 30):** admin scope rides a dedicated
   listener (`admin.sock` UDS 0600 or `wss://…/admin`) with an
   `admin.auth {api_key}` handshake — see the `admin.*` namespace
   above for the full method table; `system:*` service identities
   are confined to the private `internal.sock` listener with
   `SO_PEERCRED` verification.
4. Remote line (e.g. Raspberry Pi) — UPDATED round 12: the remote
   site runs a dumb `hci-agent` (USB↔HCI relay, no bluetoothd, no
   telephony state) that feeds a kernel `vhci` adapter inside the
   container over a `transport_profile` tunnel (wireguard default;
   direct_mtls; sstp/openconnect/vless_xray reserved). Two channels:
   control-TCP (HCI cmd/ACL/evt + agent mgmt) and media-UDP (SCO).
   See `docs/hci-agent.md` for the agent config format and wire
   protocol. The earlier "full provider on the Pi" shape remains an
   alternative for links too lossy for HCI-over-IP.
