# phonebridge API contract (v0 draft)

Status: design draft (consolidated with design rounds 1-24,
2026-11-16; see docs/phone-bridge-service-design.md decision log).
Transport: **JSON-RPC 2.0 bidirectional over one control
connection per session/binding** (round 31 — a session, not a
peer identity, is the connection unit; an agent may hold several
sessions up to `MAX_SESSIONS_PER_AGENT`).
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
  listener enforces an `AUTH_HANDSHAKE_TIMEOUT_SEC` = 10 s
  deadline —
  a freshly accepted socket MUST complete its identity handshake
  (a `provider.register`/`provider.reregister`, an
  `agent.register_intent`, or an `admin.auth` call) inside the
  deadline or it is closed. Before the handshake completes the
  socket accepts ONLY its own handshake call: any other RPC is
  answered with a JSON-RPC `UNAUTHORIZED` error and the socket is
  closed — an unauthenticated socket carries no session/binding
  context to answer against. Unauthenticated connections are
  BOUNDED by a listener-wide cap (`RESOURCE_EXHAUSTED` + close
  beyond it) so a connection flood cannot starve handshake slots.
  The connection unit is the SESSION, not the peer (round 31,
  SA_swe2_review_1 item 14): each session/binding owns exactly one
  live control connection — a `provider.reregister` supersede
  closes the predecessor socket after the reply, and a second
  socket carrying the same `line_id` binding does the same —
  while an agent is free to open additional sockets for
  additional sessions up to `MAX_SESSIONS_PER_AGENT` = 32 (the
  MCP-control/audio-worker split and multi-tool agents need more
  than one session per identity). `agent.register_intent` is
  IDEMPOTENT within its session: a repeat call on an
  already-registered socket returns the same `session_id`, never
  a fresh session and never an error. The transport cannot
  identify a peer "from the first byte" — a UDS peer or a NATed
  TCP peer carries no wire identity before the handshake, so the
  session/binding resolution happens only inside the handshake
  call itself.
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
| **AdminService** | `unix:///run/phonebridge-private/admin.sock` (mode 0600) or `wss://gw:7004/admin` | Admin clients (phonebridgectl, admin MCP) | `admin.*` |
| **InternalService** | `unix:///run/phonebridge-private/internal.sock` (mode 0600, never bind-mounted or network-exposed) | In-container service identities (`system:*`) | `agent.*`, `call.*` (up), `event.*` (down) |

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

Token scopes are separate: provider tokens never grant `agent.*`/`call.*`, agent tokens never grant `provider.*`/`line.*`; `admin` scope is a third class. An issued key carries EXACTLY ONE scope class — combined-scope keys are forbidden (round 31, SA_swe2_review_1 item 29; see `admin.issue_key`).

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
| `PROVIDER_UNREACHABLE` | line's provider binding `unreachable`, or `ws_disconnected` at ANY point inside orphan grace — the socketless binding keeps its leases claim-visible but can never serve a new grant/`line.dial`/`line.answer` until a live channel is restored: either `provider.reregister` on a newer `channel_seq` (the lease-preserving reattach of the SAME binding) or a plain `provider.register` on a new socket — a LEGAL fresh supersede, never recovery of the same binding: it mints a fresh `assignment_id`/`channel_seq` and ends the orphaned leases `provider_lost` in the same mutation (round 35, iteration-3 audit item 5) (heartbeat-down — the kernel-computed `degraded` rung is a warning, not a failure, round 28) |
| `DIAL_REJECTED` | dial string failed pre-line validation — `data.reason` carries a typed sub-code (see Dial validation) |
| `INVALID_STATE` | operation invalid in the current call/line phase — repeated `dial`, `send_dtmf` outside ACTIVE, approving a closed claim (round 20), an `admin` owner change (`assign_line`/`approve_claim`) while the line is `RINGING_LOCKED` (round 30). Typed `data.reason` sub-codes refine it (round 30): `LINE_NOT_CLAIMABLE` — `call.claim_exclusive` (or `admin.approve_claim`) targets a line whose `assignment.claimable` is false or that has no live provider binding — it exists, it just cannot serve an exclusive owner, so this is distinct from `LINE_NOT_FOUND`, which is reserved for a `line_id` absent from the configured set entirely; `CLAIM_NOT_FOUND` — `admin.approve_claim`/`admin.reject_claim` names an unknown, already-resolved, or socket-cancelled `claim_id` — a pending claim dies with its claiming session, so a moderator acting on it afterwards is a typed error, not a no-op, and a claim is pending STATE, never a token (`STALE_TOKEN` is inadmissible) |
| `INVALID_ARGUMENT` | invalid input parameters — DTMF alphabet/length violations (`data.reason: "INVALID_DTMF"`), empty intent sets (round 20) |
| `RESOURCE_EXHAUSTED` | queue capacity or per-session ticket/lease limits exceeded (`data.reason`: `QUEUE_FULL`, `SESSION_LIMIT_EXCEEDED`, `GRANT_TTL_EXPIRED` — round 20; these were previously folded into `STALE_TOKEN`). `GRANT_TTL_EXPIRED` applies uniformly to `call.dial`, `call.hangup`, `call.release{lease_id}` and `call.send_dtmf` on an expired-but-known grant (round 34, iteration-2 audit item 20 — the lease identity resolves, only its grant window lapsed) |
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
- `country` — MANDATORY uppercase alpha-2 destination telephony
  region (the phonenumbers-supported region vocabulary — PR #82
  review round).
  It is a cross-check ASSERTION, never a fallback parse region and
  never a source of prefixes: the system adds nothing to a number,
  ever.
- Validation (libphonenumber / `phonenumbers`): `parse(number, None)`
  → `is_valid_number()` → `is_valid_number_for_region(parsed,
  country)`. Region check uses `is_valid_number_for_region`, NOT
  `region_code_for_number() == country` — shared calling codes
  (NANP +1, +44 Crown Dependencies, +7) must not false-reject.
- **Country vocabulary (round 25; wording corrected in the PR #82
  review round):** `country` MUST be a known
  phonenumbers-supported uppercase alpha-2 region — the ISO
  3166-1-style region set PLUS phonenumbers' telephony extensions
  such as `"XK"` (Kosovo), `"AC"` (Ascension) and `"TA"` (Tristan
  da Cunha), NOT strict ISO 3166-1 only — an alpha-2-shaped but
  unassigned code
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
       | "UNKNOWN_COUNTRY_CODE"      // country input is not a known
                                  // phonenumbers-supported alpha-2 region
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
| `provider.register` | `{line_id, token, caps, profile?, meta?}` | `{assignment_id, heartbeat_interval_ms, provider_media_endpoint?, server_time, pending_rings?, auto_created_assignment?}` — `auto_created_assignment` (round 35, iteration-3 audit item 4) carries the `LineAssignment` record the register mutation auto-created for a previously unconfigured `line_id`, absent/`null` on an already-configured line; it exists so a failed adapter write-through can compensate with `restore_line(line_id, None)` — removing the auto-created record and tombstoning the same-mutation binding in one rollback — `pending_rings` (round 30) carries every ring a `state_snapshot` re-minted through the `ingest_ring` path as `{call_token, caller_id?, provider_call_id?, observed_at}` — `provider_call_id?` (round 32) echoes the snapshot element's own handle so the provider can bind its call object to the minted `call_token` on the rediscovery direction too; rows are deduplicated by ring identity (exact when the snapshot element carried `provider_call_id`, best-effort by `RING_DEDUP_MIN_GAP_SEC` otherwise), never double-minted; absent/empty when the register carried no ringing snapshot entries; the provider MUST echo each minted `call_token` on the ring's later `provider.call_state` updates exactly like a live-call token. `provider_media_endpoint` is the provider-side static socket address (agent-side endpoints come from `MediaCredentials.agent_media_endpoint` inside each grant — round 25 endpoint split); the per-lease `media_token` arrives later inside `line.dial`/`line.answer` (round 14). `heartbeat_interval_ms` is stamped from the line's effective `heartbeat_interval_sec` as nearest-millisecond (halves up — `int(sec * 1000 + 0.5)`, round 27; Python `round()` is banker's) clamped to ≥1 ms and bounded above by `MAX_HEARTBEAT_INTERVAL_MS` = 86 400 000 (24 h — the fail-closed converter rejects non-finite, non-positive, or overflowing intervals, round 30) — the interval is floored at 0.001 s (millisecond granularity, the wire unit) so a valid config can never yield a zero cadence (round 24c). `meta` is bounded (round 22): ≤16 keys, keys ≤64 chars, values ≤256 chars — `INVALID_ARGUMENT` beyond. `caps` MUST be non-empty and telecom-only (`dial_out`/`receive_inbound`): an empty set or any `exclusive_line` entry → `INVALID_ARGUMENT` (round 24) — durable exclusivity is a `LineAssignment.claimable`/`mode` property granted by the claim flow, never a hardware capability. On a first-ever attach the kernel auto-creates the line's default `LineAssignment` (SHARED_POOL, `owner_agent_id=None`) — and that record rides the FULL provisioning contract (round 34, iteration-2 audit item 15): it counts against `MAX_LINES_TOTAL` (`RESOURCE_EXHAUSTED` past 256) and the adapter persists its `lines` row under the same kernel-first write-through ordering as `admin.provision_line`, compensating a failed write through `restore_line(line_id, None)` — durable config, never the best-effort `provider_bindings` cache path |
| `provider.reregister` | `{line_id, token, caps, profile?, meta?, state_snapshot?}` — the `provider.register` param set PLUS `state_snapshot?` (spelled out in full, round 32, SA_swe2_review_2 item 23 — the "same as register" prose cell was never parseable by the drift test) | idempotent; preserves lease state during the 10 s orphan-grace window — `state_snapshot` lists the calls the provider still sees on the D-Bus (`{lease_id?, call_token?, provider_call_id?, state, caller_id?}[]`, `lease_id`/`call_token` absent for a call the gateway never issued; `provider_call_id` is the provider's OWN call identifier — round 31, SA_swe2_review_1 item 20 — the exact dedup/correlation handle on the ring/call INGRESS facts; subsequent updates correlate by `call_token`/`lease_id` with the handle recorded from ingress) so the kernel can re-bind surviving leases (round 21, design §5.4) — bounded at **64 entries** on the wire, `INVALID_ARGUMENT` beyond (round 30 — a line's call set can never legitimately approach that: at most one established call plus a handful of pre-dial grants); an element with `state:"ringing"` rides the `ingest_ring` path and returns its minted token in `pending_rings` (round 30), an element with NEITHER `lease_id` nor `call_token` is legal when the line holds exactly ONE orphaned lease or external-call record to attribute it to — and even that single-candidate bind is conditioned on EVIDENCE AGREEMENT (round 32, SA_swe2_review_2 item 10 — the same rule the `provider.call_state` row pins for unqualified updates): the attribution lands only when every present evidence agrees — `provider_call_id` vs the lease's recorded handle, `caller_id` vs the dialed/ringing peer, call direction implied by the lease's phase — while a PRESENT-but-DISAGREEING field is conflicting evidence: the kernel mints a fresh `external_call` for the call the element truly describes and leaves the unmatched lease to its own deadline/orphan grace rather than binding the wrong call to it — MORE than one candidate is ambiguous and rejected `INVALID_ARGUMENT`, while ZERO candidates is NOT a defect (round 31, SA_swe2_review_1 item 7): the entry reports a call the kernel never tracked (a handset-dialed call the gateway missed across a restart), so the kernel mints a fresh `external_call` tracking record for it instead of rejecting or dropping the desync; every preserved lease receives FRESH provider media credentials via `line.reattach_media` — media tokens are one-shot and died with the dropped socket (round 25); grace covers the WS-drop level only — provider-reported tunnel/USB death or heartbeat-down unreachable = immediate teardown, no grace (round 20). **Method-name distillation (round 28; hardened in the round-28 fix):** the wire carries the two method names — the adapter distills WHICH one arrived into `ProviderRegistration.reregister` (`false` for `provider.register`, `true` for `provider.reregister`); the flag is derived ONLY from the RPC method name, a `reregister` key in the wire params is ignored and never distilled (a provider cannot self-promote a register into a supersede). A `provider.reregister` on a live REACHABLE **or DEGRADED** binding is a LEGAL half-open supersede (round 28; DEGRADED added in the fix — still a live-control state; WS_DISCONNECTED keeps the orphan-grace reattach path, UNREACHABLE classifies as fresh register — the provider's fresh control channel arrived before the old socket's death was detected): the old binding is marked superseded, a FRESH `assignment_id` is minted, and live leases are preserved; with `state_snapshot` present they are reconciled against it, with `state_snapshot` absent they are preserved WITHOUT reconciliation (the binding hand-over alone does not kill calls). The snapshot is authoritative ONLY for leases in a `dialing`-or-later phase — including the internal `answering` answer-in-flight phase the `call.accept` minted lease occupies until the network confirms (round 31; the provider CAN observe a call whose `line.answer` was already sent) — an undialed `ACQUIRED` grant is pre-dial, so an empty snapshot never kills it (round 28). A `provider.register` on an already-live binding is `INVALID_STATE` — reconnect inside the grace window uses `provider.reregister` (round 25). **Second-socket `provider.register` (round 34, iteration-2 audit item 24 — resolves the wording/replacement conflict):** the `INVALID_STATE` guard covers ONLY bindings with a live control channel (`reachable`/`degraded`); against a `ws_disconnected` binding still inside orphan grace a plain `register` arriving on a NEW socket is a LEGAL fresh supersede, not a violation — the old socket is gone, so the kernel supersedes the orphaned row, mints a fresh `assignment_id`/`channel_seq`, and reconciles by the dead-binding fresh-register rules — and because `provider.register` carries NO `state_snapshot`, the orphan's live leases end `provider_lost` ATOMICALLY inside that same supersede mutation (round 35, iteration-3 audit item 5 — a preserved-but-unverified lease would be a zombie the new binding could never serve, since the provider side never learned its `lease_id`; the air-side calls the restarted provider still holds re-surface as `external_call` facts on the NEW binding's own `provider.call_state` reports) — supersede is always legal, it is never a same-binding recovery; `provider.reregister` inside the same window stays the lease-PRESERVING idempotent reattach — its `state_snapshot` is what makes preservation verifiable. **Hydrated-dead bindings (round 31, SA_swe2_review_1 item 18):** a binding restored from the durable store by `hydrate_lines` boots DEAD (`ws_disconnected`, orphan deadline already expired) — no `CallLease` survives a restart, so the row's recorded call surface is the only thing the first `tick` cascade can tear down — unless a fresh `provider.register` on a NEW socket supersedes it first: the dead row is legally replaced (never `INVALID_STATE` — the "already-live" guard covers only live-channel bindings), the register mints a FRESH `assignment_id` and channel generation, and the dead row's recorded call surface still tears down through its pending `provider_lost` tombstone cascade (the `tick` cascade hits the dead row, never the fresh binding) — a `CallLease` never persists (see Persistence below: only `provider_bindings` rows survive a restart, no lease does), so the fresh binding owns no live leases to re-bind and `provider.register` carries no `state_snapshot` to reconcile against: the restarted provider's own `provider.call_state` reports re-describe whatever the air side still holds — `state:"ringing"` reports mint fresh `call_token`s, and unattributed call reports mint `external_call` records per the ZERO-candidates rule above |
| `provider.ping` | `{line_id, assignment_id, state, state_snapshot?, checked_at?}` | `{ok, server_time, pending_rings?}` — `pending_rings` (round 34, iteration-2 audit items 12/24) reuses the `provider.register` tuple shape `{call_token, caller_id?, provider_call_id?, observed_at}` and is the ONLY ping-side token channel: it carries (a) the tokens of `pending_ring` entries promoted after a ring-drain completes — the parked ring's fresh `call_token` has no other delivery path — and (b) the minted token of any `state_snapshot` element with `state:"ringing"` fed through `ingest_ring` on this ping, so a snapshot ring can never be minted silently; absent/empty when nothing was minted or promoted — `state` is the provider-REPORTED rung `"reachable"` \| `"unreachable"` (round 30 — the field is now explicit: `"degraded"` is kernel-computed from elapsed silence, `"ws_disconnected"` is a kernel-observed socket-loss fact, `"unbound"` is a listing projection — none is ever a legal `state` value here). Missed pings escalate `LivenessState` along the `heartbeat_degraded_after`/`heartbeat_down_after` ladder `reachable` → `degraded` → `unreachable`, surfaced as `event.line_health_changed`; a `reachable` ping on the live channel restores `degraded` → `reachable`, and a control-socket drop on a DEGRADED binding resolves `degraded` → `ws_disconnected` — the socket-loss axis overrides the elapsed-time rung so the orphan grace stays reachable (round-28 fix); `unreachable` is TERMINAL on the event channel — a later socket-close fact can never move `unreachable` → `ws_disconnected` and re-arm the grace (round 30). The comparison is elapsed-time based — `now - last_seen_at` vs the stamped thresholds, no separate missed-count counter (round 27). `checked_at` is an optional provider-side RFC 3339 UTC stamp of the liveness observation — ADVISORY ONLY (round 28): a dedupe/ordering key scoped to `(line_id, accepted_stamp)` (round 30 re-scope — the per-LINE monotonic watermark survives a reregister, so a stamp replayed by a NEW binding still dedupes against the line's recorded watermark; facts naming a superseded `assignment_id` are already `STALE_TOKEN` through the closed-assignment ring before dedup is consulted), never a deadline input — `last_seen_at` is always KERNEL receipt time, ONLY a stamp deeper than `MAX_CLOCK_SKEW_SEC` (30 s) into the FUTURE is normalized to receipt time at ingestion, and a PAST stamp is retained verbatim as the dedupe key, never feeding elapsed-time or liveness math (round-28 fix). The watermark is MONOTONE-MAX per scope (round 30): a verbatim PAST stamp below the recorded maximum stays a dedupe key for detecting repeats but NEVER regresses the ordering. The out-of-order disposition is split by fact kind (round 31, SA_swe2_review_1 item 19): a ping carrying a sub-watermark stamp is still a live-channel heartbeat — it is APPLIED CLAMPED to the recorded watermark stamp (the liveness refresh lands, `last_seen_at` updates to kernel receipt time, the stored ordering key is never rewound), while a state-carrying fact (`provider.call_state`, a `state_snapshot` element) keeps the strict rule — it is dropped as a duplicate and never applied backwards. `state_snapshot` is legal only on `state:"reachable"` pings (round 27 — a dead/dying channel's call list is not reconciled; the domain model rejects it on any other state). A `reachable` ping clears the orphan-grace deadline only when stamped with a `channel_seq` STRICTLY NEWER than the binding's recorded generation (round 34, iteration-2 audit item 8) — in practice only `provider.reregister` satisfies that, since it mints the fresh socket generation: an EQUAL-generation ping is a delayed fact replayed off the dead socket — recorded but never disarming — and an older-generation fact is stale outright. Record-only is total (round 35, iteration-3 audit items 8/17): a stale or equal-generation fact may still RAISE the recorded dedup watermark (a monotone-max never rewinds) but it mutates NEITHER `liveness` NOR `last_seen_at`, disarms NO deadline, and can never clear the orphan grace — a replayed `reachable` off a dead generation is bookkeeping, not health; inside the grace window the authoritative recovery is `provider.reregister` (rounds 27/30) |
| `provider.capability_update` | `{line_id, assignment_id, caps, profile?}` | `{ok}` — replaces caps/profile on the ACTIVE binding; carries NO `token` — the binding id authenticates the peer ONLY together with arrival on that binding's own live control channel (round 27: the adapter verifies request `assignment_id` == the socket's binding id; bare id knowledge is not a credential). `profile` absent/`null` = leave unchanged; there is no clear-profile op in v1 (round 27). An identical update is an idempotent no-op (round 22); the same `caps` invariant as `provider.register` applies — non-empty and telecom-only, `exclusive_line` → `INVALID_ARGUMENT` (round 24) |
| `provider.call_state` | `{line_id, assignment_id, call_token?, lease_id?, caller_id?, provider_call_id?, state, reason?, observed_at?}` | `{ok, call_token?}` — `state` is the canonical set `ringing` \| `dialing` \| `alerting` \| `active` \| `disconnected` — a provider-REPORTED fact vocabulary, distinct from the kernel-computed `LivenessState` rungs it never spells (round 30). `provider_call_id` (round 31, SA_swe2_review_1 item 20) is the provider's own call handle — it rides the `state:"ringing"` report and `state_snapshot` elements (the ring/call INGRESS facts) as the exact dedup/correlation key; post-ring updates correlate by `call_token`/`lease_id` (below), with the provider-side handle recorded from the ingress fact. `caller_id` carries the inbound caller's identity on `ringing` — BEST-EFFORT E.164 (round 30): the field is a bounded non-empty string (≤ `MAX_CALLER_ID_LEN` = 64 chars) that normally holds an E.164 number, but CLIR/withheld markers (`"anonymous"`, `"restricted"`) and alphanumeric sender strings are legal values because the air side cannot always produce digits; a withheld identity may also be expressed by omitting the field entirely (never by `""`). Since round 33 `caller_id` is ALSO legal on post-ring updates as bounded correlation EVIDENCE - one of the two positive-evidence fields the sole-live-lease rule below requires (the other is `provider_call_id`): it is normalized E.164 before comparison and is never used to re-report or re-mint caller identity, only to corroborate or block an attribution. **`call_token` minting (round 22):** on `state:"ringing"` the gateway mints the one-shot `call_token` inside `ingest_ring` and returns it in `{ok, call_token}`; the provider MUST echo that `call_token` on every subsequent update for the same ring — including a pre-answer `disconnected` (early caller hangup) — until `line.answer` hands the call a `lease_id`. A `state:"ringing"` report dedupes by ring identity — EXACTLY via `provider_call_id` when the provider supplies it, best-effort via the `RING_DEDUP_MIN_GAP_SEC` (30 s) time window otherwise — so a re-reported ring never mints a second token (round 31). **Burned-token correlation (round 30):** the echo obligation survives the token's own expiry — once a ring is decided (`call.accept`, a fallback dispatch, `missed_call`) its `call_token` is BURNED as an action token but stays the ring's CORRELATION key until the line returns to `FREE`: a `state:"active"` echo of the burned token during ring-drain is the handset-answered `external_call` case below, and a `disconnected` echo ends the drain or the external call — never `STALE_TOKEN`. **Drain-parked ring-ingress race (round 30):** a `state:"ringing"` report arriving while the drain is parked — the same physical ring re-reported, or a genuinely new ring — is CORRELATION-ACCEPTED and returns `{ok:true}` with no `call_token` (the burned token is never re-minted: a same-ring re-report is ignored, a new ring parks as the line's `pending_ring` and gets a FRESH token only when the drain completes); a fact carrying a `call_token`/`assignment_id` that matches nothing — a superseded binding id or an unknown token — is rejected with the typed `STALE_TOKEN` error instead (a JSON-RPC error response, not an `{ok:false}` payload — the `call_state` result shape only ever carries `{ok[, call_token?]}`). For rings the gateway discovered via `state_snapshot` (the provider never received a token), a `disconnected` update with NEITHER id on a `RINGING_LOCKED` line resolves to that line's one open ring; the minted token for a snapshot ring arrives in the register response's `pending_rings` (round 30). On every other state the response is `{ok}`. `lease_id` identifies an established call. Recognized `observed_at` is an optional provider-side RFC 3339 UTC stamp of the transition observation (gateway stamps receipt time when absent; ordering/dedupe is scoped to `(line_id, lease_or_call, accepted_stamp)` — round 30 re-scope: the per-LINE monotonic watermark survives a reregister, so a stamp replayed by a NEW binding still dedupes against the line's recorded watermark, and facts naming a superseded `assignment_id` are already `STALE_TOKEN` through the closed-assignment ring before dedup is consulted; the watermark is MONOTONE-MAX per scope — a verbatim PAST stamp below the recorded maximum stays a dedupe key for detecting repeats but NEVER regresses the ordering, an out-of-order fact is dropped as a duplicate, and phase legality is governed by the forward transition matrix, never by stamp comparisons; a same-stamp repeat is a duplicate, not a regression, round 25; ADVISORY ONLY, round 28 — ONLY a stamp deeper than `MAX_CLOCK_SKEW_SEC` (30 s) into the FUTURE is normalized to receipt time at ingestion, a PAST stamp is retained verbatim as the dedupe key, and deadline arithmetic never reads the provider clock (round-28 fix)). `reason` is REJECTED unless `state:"disconnected"` (round 26 — a reason on a still-live call state is a contradiction). Recognized `reason` values on `disconnected` — the provider-reported vocabulary is CLOSED (round 21): `normal`, `busy`, `no_answer`, `rejected`, `network_error`, `dongle_lost` (tunnel/USB death — immediate teardown, no orphan grace), `stack_restart` (vanished call found by `GetCalls()` reconciliation after a bus/name-owner restart). Gateway-minted reasons such as `provider_lost` (orphan-grace expiry or heartbeat timeout) are emitted on the agent event channel as `event.call_state_changed{reason:"provider_lost"}`, never reported here. An update carrying NEITHER `call_token` nor `lease_id` on a line NOT in `RINGING_LOCKED` is correlated against the line's live leases BEFORE the external mint (round 32, SA_swe2_review_2 item 10 — evidence agreement): with ZERO live candidates it reports a human-initiated call on the physical phone (round 21) — the line goes `BUSY` with reason `external_call`, no lease is minted, and no `line.hangup` is ever sent for it (design §9); with EXACTLY ONE live candidate the bind is allowed ONLY when every evidence the update carries AGREES with the lease — `provider_call_id` vs the recorded handle, `caller_id` vs the dialed/ringing peer, call direction implied by the lease's phase (a field absent on either side is unverifiable and never blocks); a PRESENT-but-DISAGREEING field is conflicting evidence — the kernel mints a NEW `external_call` record for the call the update truly describes and leaves the unmatched lease to its own deadline/orphan grace (it is never ended early by foreign evidence). POSITIVE-EVIDENCE FLOOR (round 33): agreement alone is not enough - AT LEAST ONE corroborating field (`provider_call_id` or normalized `caller_id`) must be present on BOTH sides and agree before an unqualified update may bind; an update carrying neither evidence field - or only fields the lease cannot answer for - can never attach an external provider call to an agent lease: the kernel mints the `external_call` hold and leaves the sole live lease untouched (a `dialing` lease whose `line.dial` ack is still pending is protected TWICE - once by this floor, once by the command-ack gate that excludes it from unqualified attribution while `pending_command` is armed); two-or-more live candidates make the update unattributable and it is minted `external_call` rather than guessed. **Call-present minting only + bounded safety net (round 34, iteration-2 audit item 13):** the `external_call` mint exists for call-PRESENT states (`dialing`/`alerting`/`active`) — an unqualified `disconnected` with ZERO live candidates mints NOTHING (a teardown report with no call behind it is a dedupe/`{ok}` no-op, not a phantom call record), and an unqualified `disconnected` against a `pending_ring` still parked in ring-drain clears the parked ring without minting a token or an external record. The minted `external_call` hold itself is BOUNDED: when the line's `max_call_duration_sec` is unset the kernel still tears the hold down at `UNMATCHED_CALL_SAFETY_SEC` = 3600 s (a handset call can legitimately outlive the configured call cap, but no unmatched record may live forever), and a live lease left unmatched by conflicting evidence keeps its own grant/call deadlines — foreign evidence can never strip a lease's safety net. Symmetrically (round 28), an `active` update that DOES carry `call_token` on a still `RINGING_LOCKED` line means a human answered the ringing call on the physical handset before any `call.accept` burned the token — the ring closes WITHOUT minting a lease, the line goes `BUSY` with reason `external_call` exactly like a dialed external call (no lease, no PipeWire bridge, no `line.hangup`), and the state clears on the later `disconnected` report (design §9). **Late facts on an ended lease (round 30):** a `call_state` update naming a `lease_id` (or burned `call_token`) whose lease already ended resolves through the kernel's closed-lease tombstone ring as a dedupe/no-op — `{ok}`, never `STALE_TOKEN`: a provider legitimately reports a teardown the gateway already performed (saga compensation, admin revoke, TTL teardown), and erroring it would fabricate a desync that never happened |
| `provider.audio_ready` | `{line_id, assignment_id, lease_id}` | `{ok}` — one-shot notification: the provider's local BT SCO path is up (early media flows, uplink accepted). Gateway emits `event.audio_ready` to the agent from this signal. The readiness latch is armed ONCE per media-attachment epoch (round 31): it is re-armable — a fresh report is REQUIRED again after `provider.audio_lost`, after `provider.reregister`, and after the provider's media socket detaches, before tones or uplink are accepted again |
| `provider.audio_lost` | `{line_id, assignment_id, lease_id?}` | `{ok}` — the provider's BT SCO path dropped while the control channel and the call lived (round 30; mapped to the kernel's `ingest_audio_lost(line_id, assignment_id, lease_id?)`, round 31): the lease's `audio_ready` latch is RESET (a later `call.send_dtmf` is `INVALID_STATE` again until a fresh `provider.audio_ready`) and the uplink/downlink bridge drains its buffers for the lease. `lease_id` is omitted only for a line-scoped loss that resets every live lease on the line. The latch is scoped per media-attachment epoch and is RE-ARMABLE: it ALSO resets implicitly on `provider.reregister` (the preserved lease's SCO path cannot be assumed across a socket supersede) and on the provider's media socket dropping — in all three cases the provider MUST re-report `provider.audio_ready` before tones or uplink are accepted again. The fact is latch bookkeeping ONLY (round 32, SA_swe2_review_2 item 11): while the media socket stays attached it arms NO `MEDIA_PAIR_TTL_SEC` re-attach window — detach facts alone own that deadline — and it NEVER produces `event.media_overflow`, which is minted only by the MediaRouter's `ingest_media_overflow` PCM-drop ingress |
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
| `line.dial` | `{assignment_id, lease_id, number, media_endpoint, media_token}` | `{accepted, provider_call_id?, reason?}` — `number` is the already-validated E.164 string, passed through unchanged (see Dial validation); `reason?` (round 33) is the provider's refusal detail and is legal ONLY on `{accepted:false}` — an accepted dial never carries one; `provider_call_id?` (round 32, SA_swe2_review_2 item 18) is the provider's own call handle for the dialed leg when its stack minted one — the gateway records it as the lease's external-handle evidence and echoes it in the agent's `call.dial` reply so both sides' call logs name the same call; `media_endpoint`/`media_token` authorize the provider's `media.auth` for this `lease_id`. **Command acknowledgement as a kernel fact (round 33):** the reply is distilled into `ProviderCommandAck{line_id, assignment_id, command:"dial", lease_id, accepted, provider_call_id?, reason?}` and reported through the kernel's `ingest_command_ack` entry — the acknowledgement is a first-class kernel fact, not a mere transport outcome: the lease minted by `call.dial` carries `pending_command:"dial"` plus a `provider_command_ack_sec` (default 2 s, line-configurable ≤ 60 s) deadline from the moment the command is queued, is EXCLUDED from snapshot reconciliation while the handshake is armed, and the agent's `call.dial` wire answer is withheld until the handshake resolves — it waits ONLY for the command ack, never for cellular alerting or answer. On `{accepted:true}` the kernel clears `pending_command` and records `provider_call_id` on the lease (the value mirrored into the agent's `call.dial` reply); `{accepted:false}` applies the `compensate_dial` effects inside the same mutation — the refused command needs no separate entry call. A timeout or transport error carries NO reply to distill: the adapter calls the binding-scoped `compensate_dial(line_id, assignment_id, lease_id)` entry (round 31, SA_swe2_review_1 item 12 — adapter/kernel-internal, never a wire method), which enqueues a best-effort `line.hangup{lease_id}` FIRST — the hangup is the modem teardown for a dial that may have physically reached the phone (WS dropped after delivery, or a slow radio wakeup past the deadline) — and only THEN ends the lease with `network_error`, both legs inside the same mutation. The lease's stamped `provider_command_ack_sec` deadline is the kernel-side BACKSTOP (round 33): `tick` enforces the identical terminal effect when no report ever arrives, so an adapter that goes silent can never wedge an ack-pending lease; the deadline counts against `MAX_TICK_DEADLINES` like every other deadline class. A late rejected-dial report racing an already-queued hangup is a dedupe/no-op (round 32, SA_swe2_review_2 item 26): a lease already in `terminating` — or already ended — mints NO second `line.hangup` and never ends twice; the parked provider fact or the `hangup_hold_sec` lapse closes it. The adapter MUST NOT synthesize a `provider.call_state{state:"disconnected"}` fact on this path (round 30 — a fabricated fact would lie about an observation the provider never reported); a genuine provider `call_state` arriving later for the already-ended lease dedupes through the closed-lease ring as `{ok}`. **Pending-dial teardown semantics (round 33, PR #82 review — normative text under Pending-command teardown below):** whenever the `dialing` lease leaves `DIALING` through ANY path — agent `call.hangup`/`call.release`, `drop_session`, provider/binding death, `compensate_dial`, or a `tick` lapse — `pending_command` and `provider_command_ack_deadline_at` clear ATOMICALLY with the teardown and the lease-scoped `line.hangup{lease_id}` stays queued; a command acknowledgement arriving afterwards is STALE — a dedupe/no-op through the closed-lease ring that can never resurrect or mutate the torn-down lease. While the caller session is alive the withheld `call.dial` answer stays handshake-bound while the lease is: `{accepted:true}` yields the normal `DialResult` ONLY when the lease is still live at ack resolution — when a teardown already ended it the late ack is stale and the pending call answers `PROVIDER_UNREACHABLE` terminally, never a `DialResult` carrying the dead lease's credentials (round 34, iteration-2 audit item 10 — the normative paragraph below); `{accepted:false}`/timeout/unreachable yields `PROVIDER_UNREACHABLE`; a dead session receives no answer at all |
| `line.answer` | `{assignment_id, lease_id, call_token, media_endpoint, media_token}` | `{accepted, reason?}` — **NO `provider_call_id` ever (round 33, PR #82 review):** the answer acknowledgement shape is `{accepted, reason?}` only — `ProviderCommandAck{command:"answer"}` rejects any provider call handle, accepted or refused, because ring correlation stays `call_token`/`line_id` (the provider's own handle would be meaningless on the ring-scoped command); `reason?` is the same refusal-only detail as `line.dial`; same per-lease media credentials as `line.dial`; same `provider_command_ack_sec` (default 2 s) bounded handshake as `line.dial` (round 33 — the distilled reply rides `ProviderCommandAck{command:"answer", call_token}` through `ingest_command_ack`, the agent's `call.accept` answer is withheld until the handshake resolves, the lease carries `pending_command:"answer"` + the same deadline-class backstop, and a `tick` lapse applies the identical effects): on `{accepted:true}` the kernel clears `pending_command` and commits the consumed ring — the `answering` lease stays `answering` until provider `active`/`disconnected` facts drive the phase (the ack itself NEVER advances a phase: "the provider took the command" is not "the call progressed"); `{accepted:false}` applies the same failure effects as `compensate_answer` inside the same mutation — phase-conditional at the `ACTIVE` boundary (round 35, iteration-3 audit item 2): while the lease still sits `answering` it is the RING-scoped reject + `answering`-lease end below, but once a provider `active` fact has moved the lease `answering -> active` with the handshake still armed, the refusal is LEASE-scoped — `line.hangup{lease_id}`, the lease ends `network_error`, `call_state_changed` emits, the line goes `busy -> free`, and NO `missed_call` is ever minted for the confirmed call. Its compensation is the `compensate_answer(line_id, assignment_id, lease_id, call_token)` entry (round 32, SA_swe2_review_2 item 9 — adapter/kernel-internal, never a wire method): on `{accepted:false}`/timeout/transport error while the lease is still `answering` it enqueues a best-effort `line.hangup{call_token}` — a RING-scoped REJECT of the still-ringing call, not the lease-scoped teardown `compensate_dial` issues (`line.hangup{call_token}` IS the frozen reject vehicle: there is deliberately no separate reject command in the provider command vocabulary) — ends the `answering` lease `network_error` in the same mutation, and keeps the line `RINGING_LOCKED` under the ring-drain rule until the provider's `disconnected` or `ring_drain_ttl_sec` resolves it (the physical inbound ring can outlive the failed answer and the reject itself can be lost, so the line is never freed early); on a post-`ACTIVE` lease the same entry takes the LEASE-scoped arm above — the provider's own `active` fact already committed the call, so the ring's fate is settled and the line never returns to `RINGING_LOCKED`; a second `compensate_answer` on the already-ended lease — or on a lease already parked in `terminating` (round 32, SA_swe2_review_2 item 26, the same dedupe rule `compensate_dial` carries: a late rejected-answer report mints no second ring-scoped reject and never re-ends the lease) — is a dedupe/no-op; never a fabricated `provider.call_state` (rounds 30/32). **Ring correlation (round 25):** a line has AT MOST one open ring — the provider answers whichever ring is open on its line regardless of which `call_token` value arrives (original ring token or re-issued `fallback_call_token`); the field is informational for the provider's own bookkeeping. **Pending-answer teardown semantics (round 33, PR #82 review):** whenever the `answering` lease leaves `ANSWERING` through ANY path — agent `call.hangup`/`call.release`, `drop_session`, provider/binding death, `compensate_answer`, or a `tick` lapse — `pending_command` and `provider_command_ack_deadline_at` clear ATOMICALLY with the teardown and the RING-scoped `line.hangup{call_token}` (the reject vehicle) stays queued; a command acknowledgement arriving afterwards is STALE — a dedupe/no-op through the closed-lease ring that can never resurrect or mutate the torn-down lease. While the caller session is alive the withheld `call.accept` answer stays handshake-bound while the lease is: `{accepted:true}` yields the normal `AcceptResult` ONLY when the lease is still live at ack resolution — when a teardown already ended it the late ack is stale and the pending call answers `PROVIDER_UNREACHABLE` terminally, never an `AcceptResult` carrying the dead lease's credentials (round 34, iteration-2 audit item 10 — the normative paragraph below); `{accepted:false}`/timeout/unreachable yields `PROVIDER_UNREACHABLE`; a dead session receives no answer at all |
| `line.hangup` | `{assignment_id, lease_id?, call_token?}` | `{accepted}` — exactly ONE target (round 23): `lease_id` tears down an established call (this is the INTERNAL modem-teardown path — agent-driven hangups arrive through the owner-scoped `call.hangup`/`call.release` semantics and are enqueued here, while adapter saga compensation reaches it through the kernel's `compensate_dial` entry, never through a fabricated provider fact, round 31); `call_token` rejects a still-unanswered inbound ring (the `reject` fallback path - no `lease_id` exists before `call.accept`; round 28 — inside ring-drain the gateway queues this best-effort hangup ONLY on paths where the GATEWAY rejects the call (the `reject` fallback policy, or the failed-answer `compensate_answer` ring-reject above), never on `no_pickup`/missed outcomes where the caller already left, and never on the handset-answered `external_call` path). Same by-line ring correlation as `line.answer` — the provider targets its one open ring (round 25). **`{accepted:false}`/timeout (round 30; `terminating` leg pinned round 32, SA_swe2_review_2 item 1):** the hangup is best-effort MODEM teardown — a refused or unanswered reply NEVER changes the kernel-side fact and MUST NOT be synthesized into a `provider.call_state` fact. On an in-flight phase the lease is NOT pre-ended: it parks in `terminating` when the command is queued and stays live until the provider's `disconnected` confirms air-side teardown (`terminating -> ended`) or the line's `hangup_hold_sec` (default 10 s, bound 1–120) lapses — at which `tick` ends it `network_error` as a KERNEL-side timeout, so a lost or wedged hangup can never hold the line `busy` forever; only the saga-compensation paths (`compensate_dial`/`compensate_answer`) end the lease in the same mutation that queued the command. The adapter MAY retry the hangup once while the binding stays live (a physically ringing phone must not be abandoned), and drops it when the binding dies |
| `line.reattach_media` | `{assignment_id, lease_id, media_endpoint, media_token}` | `{accepted}` — fresh provider media credentials for a lease PRESERVED across `provider.reregister` (round 25): media tokens are one-shot, so the dropped socket's pair is dead and the kernel reissues the provider role's half; the provider MUST re-run `media.auth` with the new token inside a FRESH `MEDIA_PAIR_TTL_SEC` (30 s) window counted from the reissue instant (round 27 — the dead socket's old deadline is meaningless). A reissued pair SUPERSEDES the prior one — the old `media_token` becomes invalid at issue time (round 26: single active pair per lease; the replay window closes at the reissue instant, not the pair TTL) and the gateway FORCE-CLOSES the prior role socket if it is still live (close `4001` reason `superseded`, round 30 — a half-open predecessor socket must never block the new `media.auth`). **`{accepted:false}`/timeout (round 30):** the reissued pair stays armed — the provider retries `media.auth` inside the same 30 s window; if the window expires without a completed auth, the lease ends `network_error` as on every pair-deadline breach |
| `line.send_dtmf` | `{assignment_id, lease_id, digits}` | `{accepted}` — **`{accepted:false}`/timeout (round 30):** the tone is LOST and the lease continues — DTMF is a cosmetic side-channel, so the adapter logs the refusal and moves on; no compensation, no fabricated `provider.call_state` (a transport error is a channel-health fact that feeds the ping ladder, never a call teardown reason by itself) |
| `line.revoke` | `{assignment_id, reason?}` | `{accepted}` — **admin scope only**. **`{accepted:false}`/timeout (round 30):** the binding is already dead kernel-side (the revoke mutation committed before the command was queued), so the adapter treats any refusal as terminal and closes the provider's control socket — the `STALE_TOKEN` discipline covers facts the dead socket may still emit |

Sent as server-initiated JSON-RPC requests over the provider's own inbound connection — no reverse dial needed. **Dead-binding cleanup (round 28 — moved from drain time to mutation time):** commands queued for a binding that dies or is superseded are handled INSIDE the mutation that killed or replaced the binding (`provider.reregister` supersede, an UNREACHABLE liveness fact, `admin.revoke_assignment`, heartbeat-down `tick`) — outbox entries whose leases survive a re-bind (`line.hangup`, `line.send_dtmf`, `line.reattach_media`) are RETARGETED to the new `assignment_id`, while commands that can never land (the superseded binding's in-flight `line.dial`/`line.answer` whose lease did not re-bind) are dropped fail-closed and their leases end with the same `provider_lost` effect IN THE SAME TRANSACTION, so teardown events reach the same cycle's `drain_events()` and the adapter owes no compensation for commands it never saw. The drain (`drain_provider_commands`) keeps only a DEFENSIVE drop for a command addressed to a binding that vanished with no successor — a safety net, never the mechanism (round 28). **Dead-binding command ban + unified end reason (round 32, SA_swe2_review_2 item 12):** NO `line.hangup` — and no other `line.*` command — is ever queued FOR a dead or tombstoned binding: the dead-binding outbox purge wins, the modem-side call is left to the provider's own reconnect/caller-side cleanup, and every lease the dead binding held ends with the ONE unified gateway-minted reason `event.call_state_changed{reason:"provider_lost"}` — identical across orphan-grace expiry, heartbeat-down `unreachable`, `admin.revoke_assignment` and `dongle_lost` teardowns (the earlier `network_error` spelling for the same fact is gone — `network_error` names only the saga-compensation and deadline-lapse paths above). Exception: `line.revoke` still delivers while the binding's control channel is open — an UNREACHABLE-by-heartbeat binding can hold a live socket (round 27). The command/event outbox itself is BOUNDED (round 30): agent-initiated resource-creating calls (`call.acquire`, `call.dial`, `call.accept`, `call.claim_exclusive`) pre-flight against `OUTBOX_SOFT_CAP` = 256 and refuse `RESOURCE_EXHAUSTED` when the outbox already stands at the cap, while teardown, provider-fact, fanout and admin cascades bypass that soft pre-flight but remain bounded by `OUTBOX_HARD_CAP` — every mutation checks the hard capacity BEFORE committing partial state, so a burst can never wedge the command channel halfway through a cascade.

**Pending-command teardown semantics (round 33, PR #82 review):**
whenever a lease leaves `DIALING` or `ANSWERING` through ANY path —
agent `call.hangup`/`call.release`, `drop_session`, provider/binding
death, adapter compensation (`compensate_dial`/`compensate_answer`),
or a `tick` deadline lapse — the mutation clears `pending_command`
and `provider_command_ack_deadline_at` ATOMICALLY with the teardown:
a lease can never carry a live command-pending marker past its own
teardown, and a command acknowledgement arriving afterwards is
STALE — a dedupe/no-op through the closed-lease ring that can never
resurrect or mutate the torn-down lease. The teardown's hangup
vehicle keeps its command scope: a pending `line.dial` teardown
queues the LEASE-scoped `line.hangup{lease_id}` (the modem teardown
for a dial that may have reached the phone), while a pending
`line.answer` teardown queues the RING-scoped
`line.hangup{call_token}` (the frozen reject vehicle for the
still-ringing call). The withheld `call.dial`/`call.accept` wire
answer stays handshake-bound while the caller session is alive —
but only while the lease is (round 34, iteration-2 audit item 10 —
supersedes the round-33 "yields the normal result even when a
teardown raced" clause): `{accepted:true}` yields the normal
`DialResult`/`AcceptResult` ONLY when the lease is still live at
ack resolution; when a teardown already ended it (the race the
audit flagged), the late ack is STALE — the pending wire call
answers `PROVIDER_UNREACHABLE` TERMINALLY and the dead lease's
`media_token`/`provider_call_id` are NEVER delivered (a successful
reply carrying revoked credentials would hand the agent a token
that can never authenticate). `{accepted:false}`, a handshake
timeout, or an unreachable provider yields `PROVIDER_UNREACHABLE`
the same way; a session that died while the handshake was open
receives no answer at all. Event ownership on the answer-pending
path is the RING lifecycle's, never the failed reply's (same
item): the failed `call.accept` exposes no `lease_id` — the
consumed ring reports its missed outcome through
`event.missed_call`/the ring-drain path instead.

## APICallService — `agent.*` / `call.*` (Agent → gateway)

| method | params | returns / errors |
|---|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` | `{session_id, server_time}` / `INVALID_ARGUMENT` on an empty `intents` set; `server_time` is the gateway's UTC clock at registration (RFC 3339 — round 29: the reconciliation reference for absolute `expires_at` deadlines on `event.incoming_call`/`event.line_acquired`, so a re-delivered ring's remaining accept window can be computed against server time rather than local clock guesswork); `INVALID_INTENT` when `intents` contains `exclusive_line` — durable exclusivity is requested only via `call.claim_exclusive` (round 21). `agent_id` MUST match the API key's bound identity (`FORBIDDEN` on mismatch); `system:*` agent ids are RESERVED for kernel-internal service identities provisioned out-of-band — they register ONLY on the private `internal.sock` listener (which additionally verifies `SO_PEERCRED`); on every other socket a `system:*` identity is `UNAUTHORIZED` (rounds 25/30). At most 32 concurrent sessions per `agent_id` AND 512 sessions globally (`RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED`, round 27). The call is IDEMPOTENT within its session (round 31, SA_swe2_review_1 item 14; identity match pinned round 32, SA_swe2_review_2 item 17): a repeat `agent.register_intent` on an already-registered socket returns the SAME `session_id` verbatim — it mints nothing — but ONLY when the presented `(agent_id, intents, profile)` triple is IDENTICAL to the live session's recorded one; the same socket presenting a different intent set or label is `INVALID_ARGUMENT` with `reason: "IDEMPOTENCY_CONFLICT"` — a conflicting registration is never silently merged or swapped. On a NEW socket every call mints a NEW `session_id` — there is no update-intents op; a reconnect does not resume the old session (round 27). `profile?` is an optional session label ≤128 chars for observability (round 27). A reconnecting session re-receives any LIVE ring as a fresh `event.incoming_call` (same `call_token`, original `expires_at`, round 25) — on an EXCLUSIVE line only for its owner sessions that ALSO declared `receive_inbound` (round 35, iteration-3 audit item 15 — ownership alone is not an inbound channel: an owner session registered `dial_out`-only is skipped by live ring delivery AND by this re-delivery alike); on a SHARED_POOL line for ANY new session holding `receive_inbound` (round 27). The `system:fallback_worker` identity is excluded from pool ring fan-out and re-delivery alike (round 28) — it is dispatched via `event.fallback_triggered`, never as an `incoming_call` subscriber. An owner registering later also gets a replayed `event.assignment_granted` with its current `assignment_id` (round 27) |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?, idempotency_key?}` | `{granted: true, line_id, lease_id, expires_at}` or `{granted: false, free_lines[]}` or `{granted: false, queued: true, ticket_id, position}` — `intent` must be `dial_out` AND must sit inside the SESSION's registered intent set (round 14/28; other intents or an intent the session never registered → `INVALID_INTENT`); `expires_at` is the grant TTL deadline (round 20; missing it → `event.call_state_changed{reason: "grant_ttl_expired"}`); retry dedup is scoped to a non-empty `(method, session_id, idempotency_key)` triple (round 31, SA_swe2_review_1 item 6 — the keyspace is partitioned by METHOD, so the same key under `call.acquire`/`call.dial`/`call.accept`/`call.claim_exclusive` names independent operations, never a shared entry) whose entries live only inside the LIVE session and die with it via `drop_session` — a key never dedups across a reconnect (round 27); the dedup entry records the OUTCOME (granted `lease_id` or queued `ticket_id`) so a same-key retry returns the recorded outcome VERBATIM (replay-after-completion — the stored result is served, never re-derived) — but ONLY while the recorded object is still LIVE (round 35, iteration-3 audit item 20): a stored `lease_id` whose lease already ENDED (an elapsed grant TTL included) or a stored `ticket_id` whose ticket is gone is NOT replayed — the retry re-executes fresh instead of resurrecting a dead id — and ONLY those two outcomes are replayable under the full TTL (round 34, iteration-2 audit item 20): a REFUSED (`granted:false`/`free_lines`) or TRANSIENT verdict (`PROVIDER_UNREACHABLE`, `LINE_BUSY_INCOMING`, `RESOURCE_EXHAUSTED`, an error thrown before recording) is NEVER stored for `IDEMPOTENCY_TTL_SEC` — pinning a transient refusal for 3600 s would turn a recoverable retry into a permanent dead key, so a same-key retry after such an outcome simply re-executes; the same key with DIFFERENT params inside the live window → `INVALID_ARGUMENT` with `reason: "IDEMPOTENCY_CONFLICT"` (round 32 — the typed detail, replacing the bare INVALID_ARGUMENT of round 28), and a retry AFTER the window re-executes fresh: every entry expires `IDEMPOTENCY_TTL_SEC` = 3600 s after recording — the contract is a bounded retry window, not a permanent alias; the dedup table itself is bounded twice over — at most `MAX_IDEMPOTENCY_KEYS_PER_SESSION` = 256 distinct keys per live session, with bounded-LRU overflow: the OLDEST entry is evicted to admit a new key (round 32, SA_swe2_review_2 item 17 — REPLACES the round-29 fail-closed `RESOURCE_EXHAUSTED` rule: a new key is never rejected for table fullness; an evicted entry's key replaying later simply re-executes inside its still-live TTL), and every entry dies with its session via `drop_session`; calls without a key create independent tickets so multi-call agents are not merged; `position` is the GLOBAL FIFO rank across all eligible tickets (round 27); `profile?` is an optional per-call label ≤128 chars (round 27) — it is recorded on the queued `QueueTicket` and the granted `CallLease` inherits it verbatim, so the label survives queueing (round-28 fix); `free_lines` carries only servable alternatives — SHARED_POOL lines advertising `dial_out` whose binding liveness is `reachable` or `degraded` (round 34, iteration-2 audit item 4 — the claimable/servable split is now explicit: **claimable** (what `call.get_lines` offers and `call.claim_exclusive` accepts) is "liveness neither `unbound` nor `unreachable`" — a `ws_disconnected` orphan inside its grace window stays claim-VISIBLE so a reconnect does not flap claims — while **servable** (what may receive a new grant, dial, or provider command) requires a live control channel, `reachable` or `degraded` — restored either by a `provider.reregister` preserving the orphaned binding or by a plain `provider.register` superseding it fresh — an orphan has no socket a command could ever ride, so offering it as a dial alternative was a lie — dead or socketless hardware is never suggested as an alternative); a `line_id`-naming acquire (round 35, iteration-3 audit item 11) targets a PREFERRED line: with `enqueue:true` a `RINGING_LOCKED` or busy preferred line queues like any other, while `enqueue:false`/`absent` answers `LINE_BUSY_INCOMING` on `RINGING_LOCKED` (the ring's accept window owns the line until a fallback decides it) and the ordinary `granted:false`/`free_lines` shape on busy; a preferred line whose binding sits `ws_disconnected` inside orphan grace may still queue — the ticket is claim-visible, not servable — while `unbound`, expired-grace and `unreachable` bindings answer `PROVIDER_UNREACHABLE`; the preemption-reissue path RE-VALIDATES the same eligibility (round 35, iteration-3 audit item 13): a preempted queue-origin grant whose recorded preferred line is still eligible reissues on it, falls back to the servable SHARED_POOL otherwise, and cancels with `event.queue_cancelled{reason:"provider_lost"}` when nothing servable remains; `LINE_EXCLUSIVE_NOT_OWNER` (+ `data.free_lines[]`) — except a `system:fallback_worker` session, which bypasses this check to execute admin-configured `forward_via_line` policy (round 22); `RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED` on per-session limits — at most 16 queued tickets per session and 1024 globally (`QUEUE_FULL`, round 25). Queue liveness (round 28): a head-of-line ticket whose session already sits at `MAX_LEASES_PER_SESSION` is SKIPPED on each grant attempt — no head-of-line blocking, re-evaluated per attempt, not parked; a preemption reissue never re-checks queue caps; a POOL ticket (no `line_id`) is cancelled `event.queue_cancelled{reason:"provider_lost"}` when the set of servable SHARED_POOL `dial_out` lines becomes EMPTY — checked on every liveness/caps/assignment mutation — BUT the set counts a `ws_disconnected` binding still inside its orphan grace as surviving (round 35, iteration-3 audit item 10): pool and preferred tickets are PRESERVED while any eligible binding's grace window is open, and the cancellation fires only after the LAST such window has lapsed — a socket flap that heals by `provider.reregister` never loses a queued caller's place; a `claimable` flip alone never cancels queued tickets |
| `call.cancel_acquire` | `{session_id, ticket_id}` | `{ok}` — withdraw a queued acquire; `OWNER_MISMATCH` when the ticket belongs to a different session (round 30 — a ticket is socket-bound, so a foreign session can never cancel it); EVERY non-live `ticket_id` — already-granted, already-cancelled, OR never-issued/unknown to this session — resolves through the tombstone table as an idempotent `{ok}` no-op (round 31, SA_swe2_review_1 item 9 — a cancellation intent is already satisfied when the ticket is not live; non-live and unknown ids are indistinguishable on purpose so a stale ticket reference can never probe the queue). |
| `call.release` | `{session_id, lease_id?, assignment_id?}` | `{ok}` / `OWNER_MISMATCH` / `STALE_TOKEN` (unknown durable `assignment_id`) — exactly ONE target (round 22): `lease_id` ends the EPHEMERAL lease (an undialed grant frees the line; an in-flight lease releases through the SAME `terminating` leg `call.hangup` parks — the queued `line.hangup` runs, the lease lives until the provider's `disconnected` or `hangup_hold_sec` resolves it, so a release is never a faster kill than a hangup — round 34, iteration-2 audit item 18, the unified terminating semantics); `assignment_id` is the durable-binding path — the exclusive owner voluntarily releases its `LineAssignment` (line reverts to the claimable pool, `event.assignment_revoked` to the owner; the `assignment_id` itself arrives via `event.assignment_granted`, round 25); zero or BOTH targets passed → `INVALID_ARGUMENT` (round 24 — the XOR is a hard wire rule, not a preference) |
| `call.dial` | `{session_id, lease_id, number, country, idempotency_key?}` | `{lease_id, provider_call_id?, media_endpoint, media_token}` / `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE`, `DIAL_REJECTED` — **bounded command handshake (round 33):** the wire answer is withheld until the provider's `{accepted}` reply to `line.dial` resolves inside the line's `provider_command_ack_sec` window (default 2 s) — it waits ONLY for the command ack, never for cellular alerting or answer; a refused, timed-out or unreachable handshake answers `PROVIDER_UNREACHABLE` (all three command-handshake failures collapse to the one existing code — no new error code is minted). **Grant survival at the dispatch edge (round 35, iteration-3 audit item 10 — mirrors the `call.accept` token-survival pin):** a `PROVIDER_UNREACHABLE` verdict rendered BEFORE `line.dial` was ever dispatched — the binding already `ws_disconnected`/unservable when `call.dial` ran — leaves the `ACQUIRED` grant UNSPENT: the lease stays live under its `grant_expires_at` deadline and can be dialed again once a live channel is restored (a `provider.reregister` preserving the binding, or a plain `provider.register` superseding it fresh), so an agent holding a just-granted line is never burned by a socket that died between acquire and dial; a POST-dispatch handshake failure — refused, timed out or lost after the command went out — consumes the grant through the compensation path: the lease ends `network_error`, terminal, and the agent re-acquires. `provider_call_id?` (round 32; handshake-locked round 33) mirrors the provider's own call handle echoed on the `line.dial` reply — an advisory handle for call-log correlation, absent when the provider stack returned none on an accepted dial; `idempotency_key?` (round 30; ≤ `MAX_IDEMPOTENCY_KEY_LEN` = 128 chars) rides the same `(method, session_id, key)`-partitioned dedup table as `call.acquire`: a same-key retry inside the `MAX_IDEMPOTENCY_KEYS_PER_SESSION` window returns the STORED result — including the minted media credentials — instead of re-dialing; CAVEAT (round 31, SA_swe2_review_1 item 6): the replayed `media_token` may already be CONSUMED — media credentials are one-shot, so a token stored in a dedup entry can never authenticate a second media socket; a caller whose replayed token no longer works must call `call.reattach_media` for a fresh pair |
| `call.accept` | `{session_id, call_token, idempotency_key?}` | `{lease_id, line_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE` — atomic: issues the lease, answers the ring, attaches media; the accepting session MUST hold `receive_inbound` intent (`INVALID_INTENT` otherwise, round 22 — `system:fallback_worker` exempt with its minted `fallback_call_token`). **Bounded command handshake + answer-pending ring (round 33):** the wire answer is withheld until the provider's `{accepted}` reply to `line.answer` resolves inside the same `provider_command_ack_sec` window — a refused, timed-out or unreachable handshake answers `PROVIDER_UNREACHABLE` like `call.dial`. Consuming `call_token` moves the ring into the kernel-internal `answer_pending` sub-state for the duration of the handshake: the burned token stays burned (a second `call.accept` still resolves `STALE_TOKEN`), the fallback decision MUST NOT run while the ring sits answer-pending — the `line.answer` handshake owns the ring's fate inside its bounded window — and a burned ring NEVER re-enters the fallback decision after the handshake fails either: the failed answer's `compensate_answer`/`ingest_command_ack` path ends the `answering` lease and keeps the line `RINGING_LOCKED` under the drain rule. The failure splits at the `ACTIVE` boundary (round 35, iteration-3 audit item 2): a refused/timed-out `{accepted}` while the lease still sits `answering` is RING-scoped — the ring-reject `line.hangup{call_token}` rides, the ring drains under the missed-call rules — but once a provider `active` fact has moved the lease `answering -> active` with the handshake still armed, a late `{accepted:false}` or ack-deadline lapse is LEASE-scoped: the kernel queues `line.hangup{lease_id}`, ends the lease `network_error`, and emits `call_state_changed` — an established call can never be retconned into a `missed_call`. Token survival at the dispatch edge (round 35, item 2): a `PROVIDER_UNREACHABLE` verdict rendered BEFORE `line.answer` was ever dispatched leaves the `call_token` unconsumed — the ring stays open for a later accept — while any post-dispatch failure burns the token and drains the ring as above. The minted lease starts in the internal `answering` phase (round 31, SA_swe2_review_1 item 2 — the `line.answer` command is in flight but the network has not yet confirmed the call is up; `answering` has no wire spelling, so it never appears as an `event.call_state_changed.state`) and reconciles to `active` or `disconnected` from the provider's `provider.call_state`/`state_snapshot` facts. `idempotency_key?` (round 30) records the STORED accept result so a same-key retry returns the minted `lease_id` and media credentials instead of racing a second `line.answer` at the modem — the same consumed-`media_token` caveat as `call.dial` applies: a replayed stored token is never reusable, fresh credentials come only from `call.reattach_media` |
| `call.hangup` | `{session_id, lease_id}` | `{ok}` — the agent call returns IMMEDIATELY after the `line.hangup` command is queued (round 32 — the kernel-side `terminating` leg is real: on `dialing`/`answering`/`alerting`/`active` the lease parks in `terminating` until the provider's `disconnected` confirmation OR the line's `hangup_hold_sec` (default 10 s, bound 1–120) lapses, at which `tick` ends it `network_error` as a kernel-side timeout — never a fabricated provider fact; on an `acquired` grant the lease ends directly, no command is queued — auto-release, round 22); a trailing `call.release` is an idempotent no-op |
| `call.send_dtmf` | `{session_id, lease_id, digits}` | `{ok}` — `digits` must match `^[0-9A-D*#,wW]{1,32}$` (`,`/`w`/`W` = ~500 ms pauses) else `INVALID_ARGUMENT`/`INVALID_DTMF`; `INVALID_STATE` unless the lease is (ACTIVE or ALERTING) AND `audio_ready` was received (round 27 — early-media IVR means SCO is up while alerting; a tone sent before `audio_ready` has no uplink to ride and the `0.3–0.6 s` `State=active`→SCO-up gap would swallow it anyway) — the `audio_ready` latch RESETS on `provider.audio_lost`, on `provider.reregister`, and on the provider media socket dropping, so a post-loss `send_dtmf` is `INVALID_STATE` again until a fresh `provider.audio_ready` (round 30); the DTMF mechanism is owned by the LineProvider, which CHOOSES the implementation (round 35, issue #89 — design §5.5/§6.4): a verified out-of-band path (oFono `SendTones` / `AT+VTS` over the modem channel) wherever the stack proves support, otherwise provider-side in-band synthesis — the wire contract is identical either way, no wire change; on the in-band path the provider substitutes tone frames for the air-side uplink for their duration — announcing the pause with a `0x05` DTMF-START frame carrying `pre_dtmf_consumed` and emitting a timestamped `0x02` flush afterwards (round 22; watermark semantics round 25, `pre_dtmf_consumed` disambiguation round 31, pacer-pause semantics round 32 — see Binary media protocol) |
| `call.reattach_media` | `{session_id, lease_id}` | `{lease_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH` — fresh AGENT media credentials for a live lease after the agent's media socket dropped while the control channel lived (round 25); the new pair obeys the same `MEDIA_PAIR_TTL_SEC` handshake deadline and SUPERSEDES the prior agent token, which is invalidated at issue time (round 26) — and the gateway FORCE-CLOSES the prior agent media socket if it is still live (close `4001` reason `superseded`, round 30 — a half-open predecessor must never block the new `media.auth`) |
| `call.claim_exclusive` | `{session_id, line_id, idempotency_key?}` | `{claim_id, status: "pending_approval"}` — `idempotency_key?` (round 30) dedups retries inside the `(method, session_id, key)` partition the same way: a same-key retry returns the stored `claim_id` instead of minting a second pending claim toward the per-agent cap; moderator approval required in admin, never self-granted; pending claims are bounded (≤8 per agent, ≤256 total — `RESOURCE_EXHAUSTED` beyond) and socket-bound (`drop_session` cancels them, round 22). Resolution arrives asynchronously: `event.claim_resolved` to the claiming session and, on approval, `event.assignment_granted` carrying the `assignment_id` the owner needs for `call.release{assignment_id}` (round 25). A line with NO claim-visible provider binding (`unbound`, or `unreachable` — the claim-visibility predicate, rounds 30/34: a `ws_disconnected` binding inside its orphan grace stays claim-VISIBLE so a reconnect does not flap claims, but it is NOT servable — it cannot receive a grant until a live channel is restored: `provider.reregister` on a newer `channel_seq` preserving the orphaned leases, or a plain `provider.register` on a new socket superseding the dead binding outright — a fresh attach whose orphaned leases end `provider_lost`, never recovery of the same binding, round 35) is `LINE_NOT_CLAIMABLE`, not `LINE_NOT_FOUND` (round 27 — it exists and is claimable, just cannot serve calls). A still-PENDING claim resolves automatically when its line's provider binding dies: `event.claim_resolved{status:"rejected", reason:"provider_lost"}` (round 28 — a dead line cannot honour an exclusive owner, so the claim must not sit pending until a moderator notices) |
| `call.get_lines` | `{session_id, intent?, offset?, limit?, cursor?}` | `{lines: LineSummary[], has_more, cursor}` — `intent?` filters by the line's declared caps; `intent:"exclusive_line"` has no provider cap to match — it returns the claimable-exclusive view: lines whose `assignment.claimable && assignment.mode == SHARED_POOL` (round 24) AND whose binding is CLAIM-VISIBLE — `liveness` is NEITHER `unbound` NOR `unreachable` (rounds 28/30 — `unbound`/`unreachable` rows are EXCLUDED from this filter: a providerless or dead-hardware line cannot serve a new exclusive owner, so offering it in the claim view is a lie). **Claimable vs servable (round 34, iteration-2 audit item 4):** the claim-visibility predicate above is what the LISTING and the claim use — a `ws_disconnected` binding inside orphan grace stays claim-visible — while a stricter SERVABLE predicate (`reachable`/`degraded` only — restored either by a `provider.reregister` preserving the binding, or by a plain `provider.register` superseding it fresh) gates every actual grant/dial/provider command: an orphan binding has no control socket, so it can be claimed toward but can never serve a new lease until a live channel is restored, i.e. the pool lines an agent may still file `call.claim_exclusive` against - and (round 33) the filter IS the claim's eligibility predicate verbatim: a line the listing offers is a line the claim accepts, so listing and claiming can never diverge on an edge. Provider-less configured lines appear as `{liveness:"unbound", caps:[]}` rows in the unfiltered/telecom views (round 27 — `unbound` is a `LivenessState` listing value, never a `LineState`; inside the teardown window such a row may still carry `state:"busy"|"ringing_locked"` while the leases drain). Owned EXCLUSIVE rows carry `assignment_id` (round 27 — the durable fallback to `event.assignment_granted` for `call.release{assignment_id}`). `LineSummary` carries NO `provider_meta` since round 28 — provider hardware metadata is privileged and lives only on the admin-scoped `AdminLineView` (`admin.get_line`; the `admin.list_lines` compact projection omits it too, round 30). Privacy (round 25): a session sees SHARED_POOL lines plus EXCLUSIVE lines it owns — other agents' exclusive lines are omitted entirely. Paged via the shared `{offset?, limit?}` window (round 29 — see Bounded list pagination below) |

**Bounded list pagination (round 29, review-1 B1; bound named
`MAX_LIST_PAGE`, PR #74 review; extended to the full admin surface
round 30):** every listing
method — `call.get_lines`, `admin.list_lines`, `admin.list_leases`,
`admin.list_tickets`, `admin.list_pending_claims`,
`admin.list_assignments`, `admin.list_provider_bindings`,
`admin.list_keys` (round 32) — accepts
an optional `{offset?: int >= 0,
limit?: int 1..MAX_LIST_PAGE}` window (`limit` defaults to
**`MAX_LIST_PAGE` = 64**,
`offset` to `0`; a negative `offset` or a `limit` outside
`1..MAX_LIST_PAGE`
is `INVALID_ARGUMENT`). The result is a STABLE-ORDERED slice of the
method's deterministic enumeration — `line_id` for line listings,
`(created_at, lease_id)` for `CallLease` rows (the lease carries no
`seq`; the mint time plus the unique lease id is deterministic),
`seq` for `QueueTicket` rows (the mandatory FIFO order),
`(created_at, claim_id)` for `PendingClaim` rows and
`line_id` for the two binding listings — so paging a
quiescent listing never skips or repeats a row. A full
`limit`-sized page means "more rows may exist — advance `offset`
by `limit`"; a short page (including an empty one) ends the list.
**`has_more`/`cursor` (round 32, SA_swe2_review_2 item 13;
single formula pinned round 34, iteration-2 audit item 21):**
every listing result additionally carries `has_more: bool` and
`cursor: str|null`, the adapter's opaque encoding of the
authoritative resume offset for the same stable-ordered
enumeration (`null` when `has_more` is false). The derivation
is ONE formula, not two readings:
`has_more` is `true` exactly when rows REMAIN after the returned
slice — `(cursor's resume offset) < (total rows matching the
query)` — so the wire `limit` caps the RETURNED row count while
the adapter's internal probe still resolves whether the tail was
reached — concretely, the probe is a LOCKED two-call pattern
(round 35, iteration-3 audit item 19): the kernel returns a bare
ordered row list, so the adapter issues its first list call for
the page rows and a SECOND call with
`offset = page_offset + len(rows), limit = 1` against the SAME
listing, both under one kernel serialization gate (one
locked mutation section — no interleaved liveness/assignment
mutation can change the answer between the two reads); a
non-empty second result means a tail exists —
a full page that consumed the enumeration's tail
reports `has_more: false` with no trailing empty page needed,
a genuinely short page reports `false` the same way, and a page
truncated by the 64 KiB byte-cap below keeps `cursor` at the
FIRST DROPPED row and reports `has_more: true` whenever rows
remain past it (never the arithmetic `offset+limit`, which would
skip the rows the cap removed). `cursor` exists
so a mid-page byte-cap truncation can publish the TRUE resume
point rather than the arithmetic `offset+limit` — a client
continues either by feeding the cursor back verbatim or by
advancing the ordinary `{offset?, limit?}` window (equivalently,
`offset += returned_count` on an untruncated page).
**Cursor as request input (round 33; context binding pinned
round 34, iteration-2 audit item 21):** every listing call
below ALSO accepts `cursor?` in its params — the opaque token
the previous reply emitted — INSTEAD of `offset` (the two are
mutually exclusive on one request: an `offset`+`cursor` pair is
`INVALID_ARGUMENT`), so a byte-cap-truncated page can resume at
the true drop point without the client decoding the token. The
cursor is BOUND to its listing context: the adapter encodes the
method identity plus the effective request parameters that
shape the enumeration (`intent`/`subject_id` and every other
filter the method carries) inside the token, and a cursor
presented to a DIFFERENT method or under DIFFERENT filter
parameters is a foreign cursor. A
malformed, expired or foreign cursor — one that does not decode
to a valid resume point for the SAME listing under the SAME
parameters — is
`INVALID_ARGUMENT` (fail-closed: a bad cursor never silently
rewinds to offset 0); the kernel keeps `offset`/`limit`
semantics only — the adapter translates its cursor into the
kernel window, so the opaque token never crosses the kernel
boundary.
The window is MANDATORY, not optional-polish: every JSON-RPC
message is exactly one WS TEXT frame hard-capped at **64 KiB** (see
Control-channel framing), so an unbounded `AdminLineView[]` or
`QueueTicket[]` could physically exceed one frame at scale — the
caller pages until a short page instead. **Page byte cap
(round 31, SA_swe2_review_1 item 4):** a `limit`-sized page is an
UPPER bound on row COUNT, not a size guarantee — the adapter
truncates EVERY returned page so its serialized wire body stays
inside the 64 KiB frame cap (`MAX_LIST_PAGE` = 64 bounds rows, the
byte cap bounds bytes; a page of maximum-length rows can be
short even when `limit` rows matched). The truncation is
slice-stable: the adapter drops whole rows from the tail of the
candidate slice until the payload fits, sets the `has_more`/
cursor state so the client continues at the first dropped row
(the `offset` window semantics above are unchanged — a client
that pages by `offset += returned_count` or uses the returned
cursor resumes exactly where the truncated page ended), and never
emits a partial row. **Compact projections
(round 30, review-19 B1):** `admin.list_lines` additionally returns
the COMPACT `AdminLineSummary` row — `AdminLineView` WITHOUT the
`provider_meta` mirror — and `admin.list_provider_bindings`
returns the compact binding listing projection WITHOUT `meta`
(same rule — the byte cap stays reachable at the page bound, and
privileged provider metadata belongs on the per-line
`AdminLineView`); the
full row including `provider_meta` is fetched per line via
`admin.get_line`. `AdminLineSummary` is a WIRE-PROJECTION type
of the admin namespace — it exists only on this wire surface
and requires no domain-contract counterpart; its field set is
exactly the `AdminLineView` enumeration above minus
`provider_meta`.

## APICallService → Agent — `event.*`

| event | payload |
|---|---|
| `event.incoming_call` | `{line_id, call_token, caller_id?, expires_at}` — first valid `call.accept` wins; **wake-audience payload split (round 33, PR #82 review):** the shape above is the TOKEN-BEARING `IncomingCallEvent` spelling legal on every live `agent`/`session`/`fanout` audience; a `wake`-scoped `incoming_call` envelope (the zero-live-member hook delivery leg) carries ONLY the tokenless `IncomingCallWakeEvent` projection `{line_id, caller_id?, expires_at}` — the one-shot `call_token` is absent BY CONSTRUCTION, not filtered at serialization, and the envelope validator rejects a token-bearing payload on `wake` (and the tokenless projection on a live audience); `expires_at` is the absolute accept-window deadline (RFC 3339 UTC — the earlier relative `expires_in_ms` would skew by queue delay at outbox drain, round 22). **Re-delivery deadline semantics (round 29, review-1 B11):** `expires_at` is stamped on the SERVER clock and is the ONLY authoritative deadline — agents reconcile it against the `server_time` returned by `agent.register_intent`, never their local wall clock. A re-delivered ring (round 25/27) carries the ORIGINAL `expires_at` — the accept window is NEVER extended by re-delivery, so a session reconnecting late legitimately receives a near-expired deadline: that is intentional, the ring's lifetime belongs to the physical call, not to the delivering session |
| `event.line_acquired` | `{session_id, ticket_id, line_id, lease_id, intent, expires_at}` — the grant for a queued ticket; `ticket_id` stays referenced until its lease ends (a preempted queue-origin grant reissues the SAME `ticket_id` at position 1 via `event.queue_updated`, round 22); `expires_at` is the grant TTL deadline — missing it ends the lease with `event.call_state_changed{reason: "grant_ttl_expired"}` (round 20) |
| `event.queue_updated` | `{session_id, ticket_id, position}` — `position` is the ticket's GLOBAL FIFO rank among ALL eligible tickets (per-line + pool compete in one order, round 27), never a per-line rank |
| `event.missed_call` | `{line_id, caller_id?, reason}` — `voicemail_ref` removed (round 27): no producer ever minted it; `reason` rides the closed `CallEndReason` vocabulary, which gained `max_call_duration` in round 28 (see the `call_state_changed` row below) — BUT `max_call_duration` is INADMISSIBLE on `missed_call` (round-28 fix): a max-duration end is a lease teardown (`call_state_changed{disconnected}` + ended lease), never an unanswered ring; **wake-audience payload split (round 35, iteration-3 audit item 7):** the shape above is the authenticated `MissedCallEvent` spelling legal on live `agent`/`session`/`fanout` audiences — its `caller_id` is a `RedactedString` whose JSON dump would emit the raw number, so a `wake`-scoped `missed_call` envelope carries ONLY the tokenless `MissedCallWakeEvent` projection `{line_id, caller_id?, reason}` whose `caller_id` is the MASKED plain-`str` projection (same rule as `IncomingCallWakeEvent`), and the envelope validator rejects a crossed pairing |
| `event.fallback_triggered` | `{line_id, call_token, caller_id?, policy, expires_at}` — THE fallback delivery channel (round 22): delivered to the `system:fallback_worker` session only, when `accept_window_sec` expires on a WORKER-policy line; `call_token` is the internal `fallback_call_token` (round 14); `expires_at` is the worker's absolute accept deadline bounded by `fallback_accept_ttl_sec` (~10 s, round 23 — a dead worker can never hold a ring open) |
| `event.line_state_changed` | `{line_id, old_state, new_state, reason?}` — broadcast ONLY for SHARED_POOL lines (round 27): on an agent-owned EXCLUSIVE line the event routes ONLY to the owner's `agent` audience plus an `admin` copy — broadcast is forbidden entirely, because even a `reason:null` broadcast copy still leaks the private line's activity (the round-25 broadcast+redaction scheme is superseded) |
| `event.line_health_changed` | `{line_id, old_state, new_state}` — provider liveness transition on the `LivenessState` vocabulary (`reachable`\|`degraded`\|`ws_disconnected`\|`unreachable`); replaces the removed DEGRADED/DOWN `LineState` members (round 25). `unbound` is legal ONLY as `old_state` (round 27): the first provider attach on a configured line emits `unbound -> reachable`; it is never a `new_state` — a dying binding reports `unreachable` instead. `degraded` (round 28) is the kernel-computed middle rung of the heartbeat ladder: it enters ONLY as `reachable -> degraded` and resolves into `degraded -> reachable` (a live ping restored health), `degraded -> unreachable` (elapsed-time escalation) or `degraded -> ws_disconnected` (control-socket loss — the socket-loss axis overrides the elapsed-time rung so the 10 s orphan grace stays reachable, round-28 fix; `ws_disconnected` remains the only state that arms that grace) — `ws_disconnected`/`unreachable` can never transition INTO `degraded`. Same exclusive-line routing as `line_state_changed`: broadcast for SHARED_POOL lines only; EXCLUSIVE-owned lines route owner-agent + admin (round 27) |
| `event.claim_resolved` | `{claim_id, line_id, status, assignment_id?, reason?}` — `status`: `"approved"` \| `"rejected"`; delivered to the claiming session; `assignment_id` present iff approved (round 25) |
| `event.assignment_granted` | `{assignment_id, line_id, mode}` — a durable `LineAssignment` was granted to the agent (approved claim or `admin.assign_line`); carries the `assignment_id` needed for `call.release{assignment_id}` (round 25). `mode` is always `"exclusive"` (round 27). Re-delivered to an owner session registering later — the original emit may predate every live session (round 27); the durable fallback is `LineSummary.assignment_id` on the owner's own `call.get_lines` rows |
| `event.queue_cancelled` | `{session_id, ticket_id, reason}` — a pending ticket was dropped server-side; `reason`: `line_exclusive` \| `provider_lost` \| `capability_removed` \| `claim_resolved` (round 25). Reason split on a claim approval (round 27): the CLAIMING session's own queued tickets on the line die as `claim_resolved` (moot now that the claimant owns it), FOREIGN sessions' tickets die as `line_exclusive` |
| `event.audio_ready` | `{lease_id, line_id}` — BT SCO path is up: early media (ringback) audible, uplink accepted without first-word clipping. The lease's `audio_ready` latch behind `call.send_dtmf` resets on `provider.audio_lost`, `provider.reregister`, or a provider media socket drop (round 30) |
| `event.call_state_changed` | `{lease_id, line_id, state, reason?, answer_confidence?, reissued_ticket_id?}` — `state`: `dialing` \| `alerting` \| `active` \| `disconnected` (the provider's `ringing` maps to `event.incoming_call`, not to a lease event; the kernel-internal `acquired`, `answering` and `terminating` lease phases have NO wire spelling — `answering` is the `call.accept`→network-confirmation window and `terminating` is the hangup-in-flight window, so the four-value vocabulary above is complete, round 31); `answer_confidence`: `"provisional"` \| `"confirmed"` — how sure the bridge is that `active` means a human answered (oFono `State=active` observed firing on remote answer, but the guarantee is provisional — design §5.5); agent may start its greeting only on `state == "active"` AND `audio_ready` received; `reissued_ticket_id` is present only on a `preempted_by_incoming` teardown of an immediate grant taken with `enqueue:true` — it names the NEW head-of-queue ticket that replaced the dead lease (round 23, so an agent with parallel acquires can correlate lease→ticket; the matching `event.queue_updated{position: 1}` names the same id); `reason` rides the closed `CallEndReason` vocabulary, which gained `max_call_duration` in round 28 — the wall-clock lease cap `lease.created_at + line.max_call_duration_sec` enforced by `tick` on DIALING/ANSWERING/ALERTING/ACTIVE/TERMINATING leases — the full call-bearing phase set (round 35, iteration-3 audit item 6: the internal `answering` phase counted since round 31, and a lease parked `terminating` still ends on the cap when the cap fires before its `hangup_hold_sec` lapse, so a wedged teardown can never outlive the cap) — an undialed `ACQUIRED` grant is exempt (the grant TTL owns its deadline); the same cap bounds `record_and_telegram` recording |
| `event.assignment_revoked` | `{assignment_id, line_id, owner_agent_id, reason?}` (round 19b; `owner_agent_id` REQUIRED since round 22) — a durable AGENT-owned `LineAssignment` ended: owner `call.release{assignment_id}` or admin `revoke_assignment` — ONLY explicit revoke paths (round 23: `idle_revoke_after` expiry is alert-only and emits `event.idle_alert`, never a revoke). Delivered ONLY to the revoked owner's session(s). Emitted ONLY for agent assignments — revoking a `ProviderBinding` has no agent audience and instead produces `line.revoke` + `event.line_health_changed{new_state:"unreachable"}` (round 22; DOWN is a liveness fact, not a LineState, since round 25). Without this event an exclusive owner would learn about the revoke only as an `OWNER_MISMATCH` on its next `call.dial` |
| `event.idle_alert` | `{assignment_id, line_id, owner_agent_id, idle_since}` (round 23) — an EXCLUSIVE line's `idle_revoke_after` elapsed. STRICTLY alert-only: routed to admin-scope subscribers and configured escalation channels, never to ordinary agent sessions and never implying a revoke; emitted at most once per continuous idle period — the alerted latch re-arms on the FIRST line activity or on the owner opening a session, so a later idle period produces a fresh single alert (round 27). `last_seen_at` — the idle clock — refreshes ONLY on owner line operations and on `drop_session` closing the owner's last session (round 28 — a bare provider-side `ingest_ring` never counts as owner activity: the phone ringing is not the owner using the line). Delivery in v1 (round 24): the EventBus pushes `admin`-audience events to the configured `escalation_hooks` (Webhook / Telegram / Exec targets from admin config) and the admin MCP stream (`c_admin`); `agent.register_intent` has no admin subscription path — an idle alert never requires an agent socket |
| `event.media_overflow` | `{lease_id, line_id}` (reserved round 30, semantics pinned round 31 — SA_swe2_review_1 item 8; promoted to this table round 32) — minted once per contiguous overflow episode when the agent's uplink produces `0x01` frames faster than the paced 20 ms egress can drain the router ring: the router keeps reading the socket, drops the NEWEST arriving PCM frame while the ring is full, and emits a `0x04 OVERFLOW` frame back to the producer (see Binary media protocol), so the agent MUST pause/throttle TTS preload for the rest of the episode. At most ONE event per contiguous overflow interval (per epoch between two non-overflowing windows) — never one per dropped frame |

**Event audience routing (round 21; `admin` scope added round 23;
`fanout` scope round 32, `wake` scope round 33).**
The kernel stamps a routing
audience on every event at emission time (`broadcast` \| `agent` \|
`session` \| `admin` \| `fanout` \| `wake`); the EventBus is a stateless router over it and never
re-enters kernel tables after the fact — a torn-down lease or
session must not strand its queued event, and payloads that carry
PII or a one-shot `call_token` must never reach uninvolved agents:

- `event.incoming_call` / `event.missed_call` — the line owner's
  agent on an exclusive line; on a shared-pool line, ONE
  `fanout`-scoped envelope carrying the whole live
  `receive_inbound` member list (round 32 — a line-wide ring wave
  is O(lines) envelopes, not O(sessions); a `dial_out`-only
  session can neither see caller PII nor hijack the one-shot
  `call_token`). ZERO live subscribers (round 33): when the
  fan-out resolves to no live `receive_inbound` session AND at
  least one wake hook is configured, the kernel stamps ONE
  `wake`-scoped envelope instead — socket fan-out is empty by
  construction and only the `escalation_hooks` delivery leg
  consumes it (the hook payload stays the masked wake shape
  below — never a live `call_token`); with no hooks either, NO
  `incoming_call` envelope is emitted at all — the ring still
  waits out `accept_window_sec` so a sleeping agent can wake and
  answer — and `wake` is legal ONLY on these two caller-PII
  events, never on session- or broadcast-scoped payloads.
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

**Hook/webhook payloads are wake signals only (round 31,
SA_swe2_review_1 item 28; caller masking pinned round 32,
SA_swe2_review_2 item 8):** the `escalation_hooks` delivery legs
(Webhook / Telegram / Exec file-drop) are OUTSIDE the
authenticated control path — a hook subscriber can never call
`call.accept` — so a hook payload serializes ONLY the
wake-relevant fields (event kind, `line_id`, the ring/call
reference ids and deadlines needed to locate the triggering
event), NEVER the full event JSON and NEVER a live one-shot
`call_token`. The token stays on
the authenticated `event.incoming_call`/event envelope where an
accepting session can use it; a `call_token` written into a
webhook POST body or a wake-spool file would be a live credential
sitting in an unauthenticated channel — forbidden by
construction, not by redaction. The domain contract pins this at
the model level (round 33, PR #82 review): the `wake` audience's
`incoming_call` payload is the dedicated `IncomingCallWakeEvent`
model — `call_token` simply does not exist on it, so no
serialization path can ever emit a live ring token to a hook.
`caller_id` on a hook payload is
ALWAYS the masked `RedactedString` rendering (`"+373****56"` —
last-4 style per the PII masking convention above): an
unauthenticated wake channel can never carry a complete phone
number, and an agent that needs the full `caller_id` fetches it
through its own authenticated `event.incoming_call` delivery —
the hook only says "a call exists", never "who it is".
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
  emit `O(queue depth)` `event.queue_updated` envelopes — a ticket
  emits at most ONE envelope carrying its FINAL position per
  mutation (round 34, iteration-2 audit item 14), so the
  total is bounded by `MAX_QUEUE_TICKETS_TOTAL` <
  `OUTBOX_HARD_CAP`.
- **Dedup scope (round 27; method partition round 31):** the
  `(method, session_id, idempotency_key)` dedup table is
  partitioned per method — a key under `call.acquire` never
  collides with the same key under `call.dial`/`call.accept`/
  `call.claim_exclusive` — and lives inside the LIVE session
  only — `drop_session`
  discards it entirely, so a key never dedups across a reconnect.
  Ticket caps are per-session (16) and global
  (`MAX_QUEUE_TICKETS_TOTAL` = 1024, `QUEUE_FULL` beyond).
- **In-flight idempotency (round 33, PR #82 review):** the adapter
  creates the `(method, session_id, idempotency_key)` entry BEFORE
  the kernel mutation or provider command is dispatched — dedup is
  an ingress gate, not a post-completion index. A retry carrying
  the same key with IDENTICAL parameters while the first request
  is still in flight JOINS/WAITS on that operation and receives
  the same outcome — it MUST NOT re-enter the kernel or dispatch a
  second `line.dial`/`line.answer`. The same key carrying
  DIFFERENT parameters is `INVALID_ARGUMENT` with
  `reason: "IDEMPOTENCY_CONFLICT"`. Once the first request
  resolves, the entry replays the recorded outcome verbatim until
  `IDEMPOTENCY_TTL_SEC` expiry or LRU eviction.
  `drop_session` kills any in-flight waiter together with the
  session's dedup table — a join never outlives the session and
  no dedup ever spans a reconnect.
- **Grant vs incoming-ring race (round 19; ticket lifecycle
  clarified round 22):** a physical `ingest_ring` on a line in the
  granted-but-undialed window PREEMPTS the grant (if the line
  profile admits inbound): the unused lease ends with
  `event.call_state_changed{state: "disconnected",
  reason: "preempted_by_incoming"}`; a queue-origin ticket is
  reissued with its `seq` RETAINED and rejoins at the position
  that `seq` earns — the head (`position=1`) unless a still-
  waiting ticket carries a LOWER `seq` (round 34, iteration-2
  audit item 22: reissue rank follows the global `seq` order, it
  grants no special head privilege over an earlier-queued
  waiter). A
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
- **Ephemeral-lease auto-release (round 22; `terminating` leg
  pinned round 32, SA_swe2_review_2 item 1):** a `CallLease` never
  outlives its call — `call.hangup` on an in-flight lease parks it
  in `terminating` (hangup-in-flight) and ends it when the
  provider's `disconnected` confirms teardown or the
  `hangup_hold_sec` lapse makes `tick` end it `network_error`
  kernel-side — never atomically with the reply — while
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
- `receive_inbound` — a registered inbound intent does NOT hold the line `BUSY` (round 11): the line stays `FREE` (available to the pool unless `exclusive_line`) and transitions to `RINGING_LOCKED` only while a physical call rings; `call.accept` consumes the ring and issues the lease; the lease ends with the call (auto-release, round 22). Only `receive_inbound` sessions see `event.incoming_call` on a shared pool and may `call.accept` (round 22). A caps flip never cancels an open ring (round 34, iteration-2 audit item 22): removing `receive_inbound` from a provider's `caps` via `provider.capability_update` — or losing sessions from the fan-out — affects only NEW ring eligibility and fan-out membership; a ring whose `call_token` was already minted drains under its own `accept_window_sec`/fallback lifecycle and a minted token stays acceptable for its TTL.
- `exclusive_line` — durable `LineAssignment` (survives disconnects, no heartbeat); granted via `call.claim_exclusive` + moderator approval or `admin.assign_line` (round 22); released voluntarily by the owner via `call.release{assignment_id}`; conflict → `LINE_EXCLUSIVE_NOT_OWNER` + `data.free_lines[]`.
- All lines busy → `call.acquire` returns `{granted: false, free_lines: []}` — routine result, not an error; client retries itself. No server-side parked requests (a stale parked request = zombie grant risk; client retry = freshness).
- Inbound: ring → `RINGING_LOCKED` → hooks + `event.incoming_call{call_token}` (live `agent`/`session`/`fanout` audiences; the `wake` hook leg carries the tokenless `IncomingCallWakeEvent` projection — round 33, PR #82 review) → first `call.accept` = atomic lease+answer. Accept window default **30 s**; ALL timeouts/windows admin-configurable. Timeout → per-line `fallback_policy`.
- Per-line inbound `fallback_policy` (admin config). CORE policies (executed inside LineManager, atomic telecom ops): `no_pickup` (carrier voicemail - default) | `reject`. WORKER policies (executed by the internal `FallbackWorker` - a system agent that connects via the standard agent API, ADR-01): `record_and_telegram:<channel>` | `answer_and_announce:<announcement_ref>` (only if an inbound agent is registered; `announcement_ref` is the admin-provisioned WAV/text selector the worker plays — carried inside the policy so the FallbackWorker needs no admin scope, round 22) — and `record_and_telegram` additionally carries an always-populated `announcement_ref` (the pre-record asset the worker MUST play BEFORE recording starts — a recording leg never runs unannounced, round 31) plus a bounded `max_recording_sec` (`0 < v <= 86400`): the SINGLE authoritative recording-length bound — the line's optional `max_call_duration_sec` still caps the WHOLE call independently and the stricter of the two wins; the compact `record_and_telegram:<channel>` spelling carries ONLY the channel — both fields fill from their defaults (`builtin:beep` / **300 s**), and the dict/JSON form `{"policy", "channel", "announcement_ref?", "max_recording_sec?"}` is the explicit-override path (round-31 fix) | `forward_via_line:<line_id>:<e164_number>:<country>` (round 23 — the policy must carry the full dial target: `<e164_number>` is strict E.164 and `<country>` the uppercase alpha-2 phonenumbers-region cross-check, both validated at `configure_line` AND again at model construction so a hydrated row cannot bypass them, round 24; the `system:fallback_worker` session bypasses `LINE_EXCLUSIVE_NOT_OWNER` when acquiring the target line, round 22; a self-loop `forward_via_line` whose `line_id` equals the configured line is rejected at `configure_line`). NOTE: no `spill_to_pool` - a live-but-slow owner = missed call.
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

**V1 transport baseline (round 35, issue #89):** WebSocket-over-UDS /
WSS binary PCM is THE V1 agent media baseline — WebRTC/Opus (SRTP,
data channels) is named only as a possible FUTURE transport profile
for WAN agents, not a replacement for the framing below.

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
   the `0x02`/`0x03`/`0x04`/`0x05` control frames alike) — a control frame
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
     (round 30): emitted once per contiguous uplink-overflow
     episode when the ring is saturated and the router drops the
     newest PCM frame (see Resource bounds);
     `timestamp_ms`/`seq` ride the router's own stream;
   - `0x05` DTMF-START — exactly **12 B**, provider→router only
     (round 31, SA_swe2_review_1 item 34): 8 B header + one
     `uint32` BE `pre_dtmf_consumed` field — the `timestamp_ms`
     of the last agent `0x01` frame the provider consumed before
     starting in-band tone synthesis, or the RESERVED
     `0xFFFFFFFF` = `none` when NO frame was consumed yet
     (tri-state `none|0|N`: `0` inside this field is a real
     stream stamp — the first frame — never a sentinel; the
     `0xFFFFFFFF` encoding is shared with the agent barge-in
     discard-all marker, which lives in a different frame's
     `timestamp_ms` slot and can never collide). On `0x05`
     receipt the router PAUSES the egress 20 ms pacer for the
     duration of the provider's tone synthesis (pacer semantics
     corrected round 32, SA_swe2_review_2 item 8): uplink
     `0x01` frames keep being read at socket speed and BUFFER in
     the ring — they are neither consumed nor muted; the "mute"
     the provider announces is its own substitution of tone
     frames into the air-side stream, never a discard of agent
     audio. The trailing post-DTMF `0x02` flush then applies the
     `pre_dtmf_consumed` watermark: it discards ONLY still-queued
     frames stamped at or before the watermark — pre-DTMF audio
     the provider never heard — while frames buffered during the
     pause and frames arriving after it SURVIVE: post-DTMF speech
     is preserved, never consumed by the tone leg. When
     `pre_dtmf_consumed = none`, the flush discards every uplink
     frame that was queued BEFORE the `0x05` arrived — all
     unheard by construction — and frames appended during or
     after the pause still survive. **The pause is bounded
     (round 34, iteration-2 audit item 17; exhaustion semantic
     unified round 35, iteration-3 audit item 22):** a pause whose
     trailing `0x02` flush never arrives within
     `DTMF_PAUSE_BUDGET_MS` = 30 000 ms of the `0x05` is a
     media-fault signal, not a resume cue — the router reports the
     provider role detached (`ingest_media_state`
     `attached:false`), the `MEDIA_PAIR_TTL_SEC` re-attach window
     arms, and the lease ends `network_error` if no fresh
     provider-role `media.auth` rebinds inside it (the budget
     covers the 32-digit `send_dtmf` maximum with `,`/`w`/`W`
     pause chars; a `0x05` whose provider never finishes
     synthesis can never wedge the agent's uplink forever). A
     pause whose `0x02` lands BEFORE the budget lapses resumes
     forwarding and contributes NOTHING to detach accounting —
     resumed pauses never accumulate into a phantom fault — and a
     SECOND `0x05` arriving while already paused RE-ARMS the
     watermark and budget — it never stacks a second pause or
     orphans the first watermark. `0x05` is
     consumed at the router and never forwarded onward;
   - any other length is a framing violation — the receiving side
     closes the socket with `1002 Protocol Error`.
   **Direction scoping (round 32, SA_swe2_review_2 item 9):**
   `0x04` OVERFLOW is legal ONLY router→producer and `0x05`
   DTMF-START ONLY provider→router — the same frame carried on the
   WRONG direction of a socket (an agent-originated `0x05`, a
   producer-originated `0x04`, a `0x05`/`0x04` relayed onward) is a
   protocol violation: the receiving side closes the socket with
   `1002 Protocol Error`. `0x01`/`0x02`/`0x03` are legal in both
   directions of their own socket — direction-scoped control
   frames are the only flags with a mandated direction.
   **DTMF implementation ownership (round 35, issue #89 — no wire
   change):** the DTMF implementation is owned by the provider:
   the control plane stays `call.send_dtmf -> line.send_dtmf`, and
   the provider MAY choose a verified out-of-band path (oFono
   `SendTones` / `AT+VTS`) when the stack proves support, falling
   back to provider-side in-band synthesis otherwise — the
   `0x05`/`0x02` frames above remain the in-band COORDINATION
   contract in every case (pause the pacer, drain the watermark);
   the MediaRouter never synthesizes tones.
   Flush is in-band — `clear_uplink_buffer`/`flush_audio_out` map to flag `0x02`, no control/media race. A `0x02` frame carries NO audio payload; it has PRIORITY over queued audio frames on the same lease — on receipt the bridge discards every still-queued uplink audio frame for that lease whose `timestamp_ms` is ≤ the flush's `timestamp_ms` (round 25 watermark semantics; round 28 clock-domain fix — the `0x02` `timestamp_ms` is a watermark IN THE AGENT'S UPLINK STREAM CLOCK: the `timestamp_ms` value of the last agent `0x01` frame the LineProvider consumed before it started DTMF synthesis (the `0x05` pause) — never a router-local wall-clock stamp). `timestamp_ms = 0` on a `0x02` is a REAL watermark (round 31, SA_swe2_review_1 item 34 — the first stream frame, stamped `0`, is a legitimate "consumed" stamp and MUST be dropped like any other ≤ watermark frame); the "no frame was consumed yet" case is NOT expressible in the watermark slot — it is declared on the dedicated `pre_dtmf_consumed` field of the `0x05` DTMF-START frame above, whose `none` value makes the trailing flush discard every frame that was queued BEFORE the `0x05` arrived (all unheard by construction — post-`0x05` arrivals survive). A provider that emits a post-DTMF `0x02` without a preceding `0x05` applies its own `timestamp_ms` watermark — a lone drain, never a pause watermark. **Discard scope (round 29, review-1 B7):** "still-queued" means frames buffered inside the MediaRouter's uplink ring that have NOT yet been written to the LineProvider socket by the egress pacer — a frame already delivered to the provider is out of the router's reach and cannot be recalled, while a frame still in the ring with `timestamp_ms` > the watermark SURVIVES the flush untouched. The watermark is a provider-consumed stamp in the agent-stream clock domain, so it may lag behind the freshest frame sitting in the ring — that gap is exactly the audio the provider never heard and the flush is meant to drop. Agent speech submitted during or after the synthesis pause — post-`0x05` arrivals, including frames stamped above the watermark — SURVIVES the post-DTMF flush (round 32, SA_swe2_review_2 item 8): the flush's discard set is only the unheard pre-DTMF prefix, and the paused pacer resumes draining the surviving tail when synthesis ends; the earlier "mute consumes the buffered audio" reading would erase brand-new speech the provider never asked to drop — an agent that wants its queued audio abandoned emits its own `0x02` barge-in. **Agent-originated `0x02` — barge-in (round 30):** the AGENT's own `0x02` (barge-in — the user/agent wants its queued TTS abandoned NOW, e.g. the callee started speaking) uses the RESERVED marker `timestamp_ms = 0xFFFFFFFF` and means DISCARD ALL still-queued uplink frames for the lease regardless of timestamp — the whole preloaded ring is flushed, not just the pre-watermark prefix. The marker value is deliberately outside the legal stream-clock range the serial-comparison rule could ever match (a real `0x01` stamp never reaches 2^32−1 inside a call), so a barge-in can never be misread as a provider DTMF watermark and vice versa: the provider's `0x02` always carries a real watermark in the agent's stream clock, the agent's `0x02` always carries the discard-all marker. Both directions are still consumed at the router and never forwarded onward.

3. **Resource bounds (round 14, CODING_HAZARDS rule 7; revised round 25):**
   - **Pair TTL** — a `media_token` expires **30 s**
     (`MEDIA_PAIR_TTL_SEC`) after its ARM instant if no peer completes
     `media.auth` — the arm instant is split by pair provenance
     (round 34, iteration-2 audit item 9): an initial pair minted
     for a `line.dial`/`line.answer` command arms only when the
     provider's `{accepted:true}` command acknowledgement lands —
     the `provider_command_ack_deadline_at` backstop bounds the
     pre-arm window, so credentials nobody could receive never
     burn their TTL mid-handshake — while every `reissue()` —
     `call.reattach_media`, `provider.reattach_media`, the
     `provider.reregister` re-pair — arms at the reissue instant
     (round 28: a rotated credential gets a
     fresh window, the dead credential's old deadline never carries
     over). The ack-arm is PER-ROLE (round 35, iteration-3 audit
     item 18): `{accepted:true}` arms the countdown only for the
     roles not already attached — an already-attached role keeps
     the attach stamp it earned and never starts a fresh countdown
     at ack time — and a role-specific detach (socket close, the
     DTMF pause-budget lapse below) arms THAT role's re-attach
     window alone while the sibling socket stays untouched. A
     half-connected pair (one peer authed, the other
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
     dedupe/no-op or `STALE_TOKEN`, never a minted external call,
     and a fact on an `acquired` (undialed) lease is
     `INVALID_STATE` — the grant is still unspent, no media pair
     exists to attach (round 32). The arming sources are
     EXHAUSTIVE and PER-ROLE (round 32, SA_swe2_review_2 item 11;
     initial arm unified round 35, iteration-3 audit item 18): the
     INITIAL arming event is the `{accepted:true}` command
     acknowledgement — `line.dial`/`line.answer` carry the pair,
     so a pair minted while the provider never answered must not
     burn TTL in a window where no media socket could legitimately
     connect — and the ack arms the countdown ONLY for roles not
     already attached (an already-attached role keeps the attach
     stamp it earned and never restarts at ack time); pair MINT
     alone never arms; the other sources are a role-specific
     DETACH — which arms THAT role's re-attach window alone — and
     a credential REISSUE — `provider.audio_lost`
     while the media socket stays attached NEVER arms the window
     (a BT SCO flap is latch bookkeeping, not a socket loss). The agent's media socket
     dropping while its control channel lives is recovered via
     `call.reattach_media`. The PROVIDER's media socket dropping while
     its CONTROL channel lives is recovered by
     `provider.reattach_media` (round 27) — fresh provider
     credentials on the same lease; the `provider.reregister` →
     `line.reattach_media` reissue inside the orphan-grace window
     covers only the case where the control socket dropped too. A
     never-authenticated (half-connected) pair stays under the same
     `MEDIA_PAIR_TTL_SEC` pairing deadline from its arm instant — no socket may
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
     always-read + drop-newest (round 31, SA_swe2_review_1 item 8 —
     replaces the round-30 "stop reading" rule):** the
     MediaRouter ALWAYS reads the agent's media socket — the
     kernel socket buffer is never used as a backpressure lever,
     because a socket left unread would strand the `0x02`/`0x03`
     control frames multiplexed on the same stream behind PCM
     noise. `0x02` flush and `0x03` keepalive frames are processed
     normally even while the ring is full; only `0x01` PCM pays
     for saturation — when the uplink ring is full the NEWEST
     arriving `0x01` frame is dropped (frames already queued are
     never evicted, the paced egress keeps delivering them in
     order, and normal acceptance resumes the instant ring space
     frees). Saturation signals are emitted ONCE PER CONTIGUOUS
     OVERFLOW EPISODE, not per dropped frame: ONE `0x04 OVERFLOW`
     control frame back to the producer plus ONE
     `event.media_overflow{lease_id, line_id}` on the agent
     event channel per episode (a new episode begins only after
     the ring has drained and a further `0x01` is dropped; the
     `media.uplink_overflow` counter still ticks per drop). The
     kernel learns the episode through the media adapter's
     `ingest_media_overflow(line_id, lease_id)` ingress (round
     32, SA_swe2_review_2 item 11 — the ONLY
     `event.media_overflow` source: the router reports the
     PCM-drop fact to the kernel, which emits the agent event;
     the `provider.audio_lost` SCO path never produces it). The
     producer contract: an agent MUST NOT stream audio
     faster than real-time minus jitter headroom — a TTS preload
     burst paces itself to ≤1 `0x01` frame per 20 ms once it sees
     `0x04`. Downlink keeps the drop-oldest bound. During in-band DTMF
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
`unix:///run/phonebridge-private/admin.sock` (mode `0600`, group
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
events and rotates no ids; restored provider bindings load DEAD
as `ws_disconnected` with an already-expired orphan deadline —
round 31 — so their leases cascade on the first `tick` unless a
fresh `provider.register` on a new socket supersedes them first;
the whole batch is loaded before
forward targets and cycles are validated, so a dangling target or
cycle rejects the ENTIRE batch; hydration replaces prior state
wholesale) and the write-through **rollback hook**
(`restore_line(line_id, snapshot)` — the only legal compensation
after a
failed durable write: `snapshot=None` removes a just-created
record, a previous snapshot is restored verbatim, compensating
events keep the outbox truthful, and the failure surfaces as
a `config_stale` condition plus an admin alert — a flagged
store/kernel-divergence state on the admin surface, not a
`domain.*` event, which does not exist. **Scope pin (round 34,
iteration-2 audit item 15 — removes the stale orphan-deadline
prose):** `restore_line` rolls back `LineAssignment` rows ONLY —
the earlier "the restored binding's orphan deadline is re-derived"
claim described a `ProviderBinding` this hook can never see: a
hydrated `ws_disconnected` binding's deadline is the KERNEL's
fact (expired at load, tombstoned on the first `tick`), not a
snapshot field a restore re-derives. The restore guards the
mutation result too — when the live record was re-mutated after
the snapshot was taken (version/stamp check), the hook reports
`config_stale` instead of blindly reverting newer state, and a
`snapshot=None` delete on a line that has since accrued live
references (binding, lease, ticket, claim, or another line's
`forward_via_line` target) refuses under the `admin.remove_line`
cross-reference rule rather than orphaning them).

**Wire methods (round 30 — every admin RPC and its response schema):**

| method | params | returns |
|---|---|---|
| `admin.auth` | `{api_key}` | `{ok, server_time}` — the connection handshake; the ONLY call a fresh admin socket accepts (see Pre-auth handshake) |
| `admin.provision_line` | `{line_id, profile?, fallback_policy?, accept_window_sec?, allowed_number_types?, claimable?, idle_revoke_after?, timeouts?, adapter_mac?, paired_phone_mac?, transport_profile?, line_home_country?, wake_hooks?}` — the trailing optional params are the hardware-identity set plus `wake_hooks?` (round 34, iteration-2 audit item 2 — the kernel-visible wake-hook capability set the zero-live-member `wake`-audience route consults: bounded at `MAX_WAKE_HOOKS_PER_LINE` = 16 checked identifiers; absent/`[]` disables wake delivery for the line) — spellings, types and semantics in the bullet below | `{assignment}` — the created `LineAssignment` / `INVALID_ARGUMENT`, `RESOURCE_EXHAUSTED` |
| `admin.configure_line` | `{line_id, fallback_policy?, profile?, accept_window_sec?, allowed_number_types?, claimable?, idle_revoke_after?, clear_idle_revoke_after?, timeouts?, adapter_mac?, paired_phone_mac?, transport_profile?, line_home_country?, wake_hooks?}` — the trailing optional params are the same hardware-identity set plus `wake_hooks?` as `admin.provision_line` (rounds 31/34 — semantics in the bullet below); a patch that narrows a constraint existing `forward_to`/fallback policies reference is `INVALID_ARGUMENT` until the referencing policy is cleared (round 32, item 24) | `{assignment}` — the patched `LineAssignment` / `INVALID_ARGUMENT`, `LINE_NOT_FOUND` |
| `admin.remove_line` | `{line_id}` | `{ok}` / `LINE_NOT_FOUND`, `INVALID_STATE` — `INVALID_STATE` while ANY live provider binding, lease, queue ticket or pending claim remains (or another line's `forward_via_line` still targets it); a live `ProviderBinding` is revoked explicitly via `admin.revoke_assignment` FIRST — `remove_line` never silently detaches a live provider (round 31); an agent-OWNED but otherwise quiescent line deletes normally — its durable exclusive assignment ends inside the removal with a pre-delete `assignment_revoked` to the owner |
| `admin.assign_line` | `{line_id, owner_agent_id}` | `{assignment}` — the FULL granted durable `LineAssignment` (`{assignment_id, line_id, owner_agent_id, mode:"exclusive", claimable, profile, adapter_mac, paired_phone_mac, transport_profile, line_home_country, last_seen_at, accept_window_sec, idle_revoke_after, fallback_policy, timeouts, allowed_number_types, wake_hooks}` — round-31 fix: the four hardware-identity fields are durable row members and ride every full record; `wake_hooks` (round 34, iteration-2 audit item 2) is the same kernel-visible wake-capability set `admin.provision_line`/`admin.configure_line` accept — bounded at `MAX_WAKE_HOOKS_PER_LINE` = 16) / `LINE_NOT_FOUND`, `LINE_EXCLUSIVE_NOT_OWNER` (the line is already held exclusive by a DIFFERENT owner — a same-owner re-grant is idempotent and returns the existing record, round 31), `INVALID_STATE`, `INVALID_ARGUMENT` |
| `admin.approve_claim` | `{claim_id}` | `{assignment}` — the granted `LineAssignment` / `CLAIM_NOT_FOUND`, `LINE_NOT_CLAIMABLE`, `INVALID_STATE` |
| `admin.reject_claim` | `{claim_id, reason?}` | `{ok}` / `CLAIM_NOT_FOUND` |
| `admin.revoke_assignment` | `{assignment_id, reason?}` | `{revoked}` — `true` when an agent-owned durable assignment was reverted to the pool; `false` when the id resolved to a provider binding (no ownership grant existed to revert) or when an idempotent retry hit an already-closed/tombstoned id (round 31); `STALE_TOKEN` on an id unknown in both spaces, `INVALID_STATE` on a `SHARED_POOL` assignment id |
| `admin.get_line` | `{line_id}` | `{line}` — the FULL `AdminLineView` row including `provider_meta`, the assignment's `wake_hooks` set (round 34, iteration-2 audit item 2 — the kernel-visible wake-capability identifiers the zero-live-member `wake` route consults) and the live binding's `orphan_deadline_at` mirror (round 32, SA_swe2_review_2 item 24 — the same verbatim deadline `admin.list_provider_bindings` projects: set on a `ws_disconnected` row, `null` on every other rung; re-derived per binding, never carried across a restore) / `LINE_NOT_FOUND` |
| `admin.list_lines` | `{offset?, limit?, cursor?}` | `{lines: AdminLineSummary[], has_more, cursor}` — the COMPACT projection (no `provider_meta`); `has_more`/`cursor` follow the shared pagination contract above |
| `admin.list_leases` | `{offset?, limit?, cursor?}` | `{leases: CallLease[], has_more, cursor}` |
| `admin.list_tickets` | `{offset?, limit?, cursor?}` | `{tickets: QueueTicket[], has_more, cursor}` |
| `admin.list_pending_claims` | `{offset?, limit?, cursor?}` | `{claims: PendingClaim[], has_more, cursor}` — `{claim_id, line_id, requester_agent_id, session_id, created_at}` rows |
| `admin.list_assignments` | `{offset?, limit?, cursor?}` | `{assignments: LineAssignment[], has_more, cursor}` |
| `admin.list_provider_bindings` | `{offset?, limit?, cursor?}` | `{bindings: ProviderBindingSummary[], has_more, cursor}` — the COMPACT binding projection WITHOUT `meta` (round 31 — privileged provider hardware metadata stays on the per-line `AdminLineView`) |
| `admin.pair_window` | `{line_id, duration_sec}` | `{line_id, expires_at}` — `PairWindowResponse`: the absolute RFC 3339 UTC deadline at which the pairing window closes (round 31 — a relative `duration_sec` echo would skew by supervisor scheduling delay) / `INVALID_ARGUMENT`, `LINE_NOT_FOUND` |
| `admin.issue_key` | `{agent_id?, scopes[], line_id?, expires_at?, subject_id?}` | `{key_id, api_key}` — the raw key material is returned ONCE here and never re-listed; `agent_id` is REQUIRED only for an `agent`-scope key and FORBIDDEN on `provider`/`admin` keys (round 31 — a provider/admin key is bound to its scope's own subject, not to an agent identity); `line_id` is the symmetric rule on the provider axis (round 32, SA_swe2_review_2 item 24): REQUIRED on a `provider`-scope key (a provider token binds to exactly ONE line — a fleet-wide provider credential would let a compromised provider socket drive every line) and FORBIDDEN on `agent`/`admin` keys, which have no per-line binding; `subject_id` is the symmetric rule on the admin axis (round 35, iteration-3 audit item 21): REQUIRED on an `admin`-scope key — it names the bound admin identity that `admin.list_keys` reports, since an admin key has no `agent_id`/`line_id` to derive a subject from and an unsubjected admin key would be unaccountable in an audit listing — and FORBIDDEN on `agent`/`provider` keys, whose subject derives from `agent_id`/`line_id`; `scopes` MUST carry exactly ONE scope class (`agent` \| `provider` \| `admin`) — combined-scope keys are rejected `INVALID_ARGUMENT`; a `system:*` subject is forbidden on every externally-issued key — such identities exist only on the internal provisioning path |
| `admin.revoke_key` | `{key_id}` | `{ok}` — idempotent; revocation cascades (round 31): the revoked key's live agent sessions are closed (`drop_session`), every `ProviderBinding` the key authenticated is torn down, and live admin connections authenticated by it are closed |
| `admin.list_keys` | `{subject_id?, offset?, limit?, cursor?}` | `{keys: KeyMetadata[], has_more, cursor}` — each row is `{key_id, scope, subject_id, created_at, last_used_at?, expires_at?, revoked}`: the key's one scope class (`agent` \| `provider` \| `admin`), the bound subject, the mint stamp, the last-use stamp when the key was ever used, the expiry stamp when `admin.issue_key` set one (round 32 — a time-boxed key's deadline is visible without holding its material), and the revocation flag. `subject_id` is ALWAYS a non-empty checked id (round 34): for `admin`-scope keys it names the bound admin identity — an unsubjected key would be unaccountable in an audit listing. **REDACTED (round 31 fix):** the listing NEVER returns key material — the emergency-revoke path needs a way to FIND a `key_id` without holding the raw `api_key`, which exists only in the `admin.issue_key` response; `subject_id?` filters the listing to one subject's keys; `{offset?, limit?}` is the shared `MAX_LIST_PAGE` window (round 32 — the key store is bounded like every other listing, never an unbounded dump) |

Semantics, bounds and error detail for each method:

- `admin.provision_line {line_id, profile?, fallback_policy?,
  accept_window_sec?, allowed_number_types?, claimable?,
  idle_revoke_after?, timeouts?, adapter_mac?, paired_phone_mac?,
  transport_profile?, line_home_country?, wake_hooks?}` →
  `LineAssignment`
  (round 28):
  creates a new configured line in the default pool shape —
  `SHARED_POOL` mode, `owner_agent_id == None`, `claimable`
  defaulting to `True` — visible immediately to agents as an
  `unbound` row and attachable by a provider. `line_id` must not
  already exist (`INVALID_ARGUMENT`); the configured-line set is
  capped at `MAX_LINES_TOTAL` = 256 — a provisioning attempt
  beyond the cap is `RESOURCE_EXHAUSTED` (round 29, keeps the
  forward-chain walk, admin listings and liveness sweeps finite); `profile?` (≤128 chars) is
  the administrative label; every other field follows the
  `configure_line` validation rules. **Hardware identity fields
  (round 31, SA_swe2_review_1 item 10):** `adapter_mac` /
  `paired_phone_mac` follow the checked `AA:BB:CC:DD:EE:FF` MAC
  grammar, `transport_profile` takes the `TransportProfile`
  vocabulary (`loopback` \| `direct_mtls` \| `wireguard`,
  default `loopback`) and `line_home_country` the uppercase alpha-2
  phonenumbers-region
  grammar (the same vocabulary `call.dial`'s `country` accepts —
  ISO-style regions plus telephony extensions like
  `"XK"`/`"AC"`/`"TA"`, not strict ISO 3166-1 only; a
  display/normalization hint only — `call.dial` still requires an
  explicit `country` assertion, round 34) — they persist on the durable `lines` row, the
  `admin.pair_window` SSP auto-accept is bound to
  `paired_phone_mac` (an unset field means the window confirms
  nothing; the MAC is UNIQUE across configured lines when set —
  `INVALID_ARGUMENT` on a collision, round 34), and the provider adapter picks its listener/dialer
  from `transport_profile`. `wake_hooks?` (round 34) carries the
  checked hook identifier set the zero-live-member wake route
  consults — see `event.incoming_call`/`missed_call` wake
  semantics. Kernel-first write-through
  ordering applies (same as `configure_line`): the kernel object is
  created, then the durable row is written.
- `admin.assign_line {line_id, owner_agent_id}` → grants a durable
  `exclusive_line` assignment WITHOUT a pending claim (round 22 —
  the §4.2 "admin assign" override path; pre-provisions an owner
  before the agent connects and files a claim) and returns the
  FULL `LineAssignment` record. `INVALID_STATE`
  while the line is `RINGING_LOCKED` (round 30 — an owner change
  mid-ring would break the ring's audience routing and the
  exclusive-owner checks the pending `call.accept` was evaluated
  against; the operator waits for the ring to resolve or tears it
  down first). `owner_agent_id` is a checked agent id — a
  `system:*` identity is `INVALID_ARGUMENT` (round 30: durable
  ownership can never belong to a service identity; the fallback
  worker executes admin policy through its own session, it does
  not own lines). `LINE_EXCLUSIVE_NOT_OWNER` when the line is
  already held exclusive by a DIFFERENT owner (round 31 —
  assigning over a live grant silently would strand the owner's
  `assignment_id`; the operator revokes first); re-applying the
  SAME `(line_id, owner_agent_id)` grant is idempotent and
  returns the existing `LineAssignment` verbatim.
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
  binding (exclusive owner OR provider attachment) and returns
  `{revoked}` (round 31, SA_swe2_review_1 item 23 — the boolean
  replaces the earlier post-revert snapshot: `true` = the id
  resolved to an agent-owned `LineAssignment` that was reverted
  to `SHARED_POOL`, `false` = the id resolved to a
  `ProviderBinding` — there is no ownership grant to revert — or
  an idempotent retry hit an already-closed tombstone id); an agent-owned
  assignment emits `assignment_revoked` to the owner, a
  `ProviderBinding` emits `line.revoke` +
  `event.line_health_changed{new_state:"unreachable"}` instead
  (round 22; DOWN is a liveness fact, not a `LineState`, since
  round 25). A `SHARED_POOL` `LineAssignment` id is `INVALID_STATE`
  (round 28 — a pool line has no owner to revoke and the line itself
  is retired via `admin.remove_line`); repeating a provider-binding
  revoke on an already-dead binding is an idempotent no-op, not an
  error (round 28); an id unknown in BOTH id spaces is
  `STALE_TOKEN`.
- `admin.configure_line {line_id, fallback_policy?, profile?,
  accept_window_sec?,
  allowed_number_types?, claimable?, idle_revoke_after?,
  clear_idle_revoke_after?, timeouts?, adapter_mac?,
  paired_phone_mac?, transport_profile?, line_home_country?,
  wake_hooks?}` →
  runtime patch, no restart; `timeouts` carries the remaining
  admin-configurable knobs `{grant_ttl_sec?, orphan_grace_sec?,
  heartbeat_interval_sec?, heartbeat_degraded_after?,
  heartbeat_down_after?, fallback_accept_ttl_sec?,
  ring_drain_ttl_sec?, max_call_duration_sec?,
  clear_max_call_duration?, hangup_hold_sec?,
  provider_command_ack_sec?}` (rounds 22-23; the
  `heartbeat_degraded_after`/`heartbeat_down_after` ladder
  multipliers are bounded `1..3600` — round 31: an unbounded
  huge value passed model validation and then exploded inside
  `tick`'s timedelta multiplication, killing the timer loop; ring-drain and
  max-duration knobs added round 25 — `ring_drain_ttl_sec` bounds
  the RINGING_LOCKED drain wait of design §5.2 step 6,
  `max_call_duration_sec` caps a single call's wall-clock length
  and bounds `record_and_telegram` recording, `None` effective
  value = no cap, `clear_max_call_duration:true` removes a set cap
  and passing it together with `max_call_duration_sec` is
  `INVALID_ARGUMENT`; `hangup_hold_sec` — round 32,
  SA_swe2_review_2 item 1 — bounds the `terminating` hold
  described on the `line.hangup` row above, default 10 s,
  tight `1..120` bound; `provider_command_ack_sec` — round 33 —
  bounds the `line.dial`/`line.answer` `{accepted}` handshake
  described on those rows above, default 2 s, `>0` and capped
  at 60 s). `heartbeat_interval_sec` is floored at
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
  The four hardware identity fields `adapter_mac` /
  `paired_phone_mac` / `transport_profile` / `line_home_country`
  patch the same contract-owned values `admin.provision_line`
  seeds (round 31 — same MAC/`TransportProfile`/alpha-2 grammars,
  `None` = unchanged, no clear op).
  `forward_via_line` policies are validated HERE: `target_number`
  (strict E.164) + `target_country` (uppercase alpha-2
  phonenumbers region) go through the
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
- `admin.list_lines {offset?, limit?, cursor?}` → `{lines: AdminLineSummary[], has_more, cursor}`
  (round 27; paged via the shared Bounded list pagination window,
  round 29; COMPACT projection round 30): the admin-scope
  superset of `LineSummary` — every configured line with its durable
  `assignment_id`, `owner_agent_id`, live `lease_id` and the global
  FIFO rank of its head queue ticket — PLUS the stored `profile`
  label, but WITHOUT the `provider_meta` mirror: `AdminLineSummary`
  is `AdminLineView` minus `provider_meta`, so a full
  `MAX_LIST_PAGE` = 64-row page stays inside the 64 KiB frame cap
  even at maximum field lengths (rounds 30/31 — the page bound
  dropped to 64 rows AND the adapter applies the shared
  ≤64 KiB page byte cap on every listing, so a heavy page is
  truncated short rather than killed on the transport's
  `1009 Message Too Big`; a `provider_meta`-carrying 64-row page
  would still reach ~256 KiB). The complete row —
  including the live binding's bounded `provider_meta` — is
  fetched per line via `admin.get_line {line_id}` → `AdminLineView`
  (`LINE_NOT_FOUND` on a configured line that does not exist).
  A kernel
  operation now:
  `CallLease`/`QueueTicket` state lives only inside the kernel, so
  the admin listings route to `LineAdminKernel` (round 27 — the
  earlier "non-kernel admin op" placement had no surface capable of
  answering them).
- `admin.list_pending_claims {offset?, limit?, cursor?}` → `{claims: PendingClaim[], has_more, cursor}`
  (round 30 — surfaced on the wire, paged): every claim awaiting
  moderator decision as `{claim_id, line_id, requester_agent_id,
  session_id, created_at}`, ordered by `(created_at, claim_id)`;
  bounded by `MAX_PENDING_CLAIMS_TOTAL` = 256.
- `admin.list_assignments {offset?, limit?, cursor?}` → `{assignments: LineAssignment[], has_more, cursor}`
  (round 30 — surfaced on the wire, paged): every durable
  agent-ownership record verbatim (`assignment_id`, `line_id`,
  `owner_agent_id`, `mode`, `claimable`, `profile`, `adapter_mac`,
  `paired_phone_mac`, `transport_profile`, `line_home_country`,
  `last_seen_at`,
  `accept_window_sec`, `idle_revoke_after`, `fallback_policy`,
  `timeouts`, `allowed_number_types` — round-31 fix: the four
  hardware-identity fields are durable row members and ride the
  verbatim listing), ordered by `line_id` — the same
  row shape `hydrate_lines` restores.
- `admin.list_provider_bindings {offset?, limit?, cursor?}` →
  `{bindings: ProviderBindingSummary[], has_more, cursor}` (round 30 — surfaced on the wire,
  paged; COMPACT projection round 31): every live provider
  attachment WITHOUT the `meta` map
  (`assignment_id`, `line_id`,
  `caps`, `profile`, heartbeat thresholds, `connected_at`,
  `last_seen_at`, `liveness`, `channel_seq`,
  `liveness_stamp_watermark`, `call_stamp_watermark` — round 33:
  the two DURABLE line-scoped provider-stamp watermarks the
  dedupe/ordering domains persist on the row and a reregistered
  binding inherits; `null` on a first attach,
  `orphan_deadline_at` — round 32, SA_swe2_review_2 item 24: the
  grace deadline is projected verbatim so a `ws_disconnected`
  row shows its remaining orphan window; `null` on every other
  rung; channel-local only, never durable), ordered by
  `line_id` — `meta` is privileged provider hardware metadata
  mirrored on the per-line `AdminLineView`, same
  projection rule as `AdminLineSummary`; the `liveness` column is the kernel-computed
  rung — `unbound` can never appear here (a missing binding is the
  absent row, `AdminLineView` projects `unbound` for it).
- `admin.list_leases {offset?, limit?, cursor?}` → `{leases: CallLease[], has_more, cursor}` (round 27;
  paged, round 29): all live kernel
  leases, bounded by live sessions × `MAX_LEASES_PER_SESSION`.
- `admin.list_tickets {offset?, limit?, cursor?}` → `{tickets: QueueTicket[], has_more, cursor}` (round 27;
  paged, round 29): all pending
  global-FIFO tickets, bounded by `MAX_QUEUE_TICKETS_TOTAL`.
- `admin.remove_line {line_id}` (round 27; quiescence rule pinned
  round 31): deletes a QUIESCENT
  configured line — it must exist and carry no provider binding, no
  live lease, no pending ticket and no pending claim
  (`INVALID_STATE` otherwise — the removal NEVER silently cascades
  a live-binding teardown: the operator ends a live
  `ProviderBinding` explicitly through `admin.revoke_assignment`
  first and removes the line only once quiescent), AND no OTHER configured line's
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

- `admin.pair_window {line_id, duration_sec}` →
  `PairWindowResponse{line_id, expires_at}` (round 31 —
  `expires_at` is the absolute RFC 3339 UTC close deadline the
  supervisor computed, same deadline-style convention as
  `IncomingCallEvent.expires_at`; a bare `{ok}` told the
  operator nothing about when the window actually ends); routed by
  the admin
  adapter to the line-bundle supervisor, which drives the container's
  BlueZ `Agent1` (see hci-agent.md §5); opens the pairing window on
  that line's adapter only. `duration_sec` is REJECTED when > 300 s
  — the `PairWindowRequest` model bound is `0 < duration_sec <= 300`,
  there is no clamp and no default (round 28; typed
  `PairWindowRequest` since round 27) — and during
  the window SSP confirmation is auto-accepted
  ONLY for the line's configured `paired_phone_mac` — never for an
  arbitrary neighboring Bluetooth device.
- Additional non-kernel admin operations (design §10, round 25;
  key rules pinned round 31, SA_swe2_review_1 item 29):
  API-key lifecycle — `admin.issue_key {agent_id?, scopes[],
  line_id?, expires_at?, subject_id?}` mints a SINGLE-scope key:
  `agent_id` is
  REQUIRED iff `scopes` is `["agent"]` (the key binds that
  identity) and FORBIDDEN on `provider`/`admin` keys; `line_id`
  is the symmetric provider-axis rule (round 32, SA_swe2_review_2
  item 24): REQUIRED on a `provider`-scope key — the key binds to
  exactly ONE line, and a `provider.register` naming a different
  `line_id` is `FORBIDDEN` — and FORBIDDEN on `agent`/`admin`
  keys, which carry no per-line binding; `subject_id` is the
  symmetric admin-axis rule (round 35, iteration-3 audit item
  21): REQUIRED on an `admin`-scope key — it names the bound
  admin identity `admin.list_keys` reports — and FORBIDDEN on
  `agent`/`provider` keys, whose subject derives from
  `agent_id`/`line_id`; `scopes`
  carries exactly ONE class — a combined-scope request is
  `INVALID_ARGUMENT`; a `system:*` subject is forbidden on every
  externally-issued key (system identities exist only on the
  internal provisioning path); the raw `api_key` material is
  returned ONCE in the response and never re-listed.
  `admin.revoke_key {key_id}` is idempotent and CASCADES: the
  revoked key's live agent sessions are closed
  (`drop_session`), every `ProviderBinding` it authenticated is
  torn down, and live admin connections authenticated by it are
  closed — a revoked key must not keep a channel alive anywhere.
  `admin.list_keys {subject_id?, offset?, limit?}` → `{keys,
  has_more, cursor}` — paged, redacted key metadata (round 32 —
  the key store rides the shared `MAX_LIST_PAGE` window like every
  other listing)
  (`{key_id, scope, subject_id, created_at, last_used_at?,
  revoked}` per row — the normative shape in the table above) for
  discovery — the
  emergency-revoke path needs a way to FIND a `key_id` without
  holding the raw material; the listing NEVER returns key
  material, only the redacted record fields. Bind-mounted sockets are created
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
   container over a `transport_profile` tunnel — the contract
   vocabulary is `loopback` / `direct_mtls` / `wireguard` (round 32:
   `loopback` is the model default for the co-located V1 agent,
   `wireguard` is the remote-line profile; `sstp`/`openconnect`/
   `vless_xray` remain reserved deployment profiles outside the
   contract enum). Two channels:
   control-TCP (HCI cmd/ACL/evt + agent mgmt) and media-UDP (SCO).
   See `docs/hci-agent.md` for the agent config format and wire
   protocol. The earlier "full provider on the Pi" shape remains an
   alternative for links too lossy for HCI-over-IP.
