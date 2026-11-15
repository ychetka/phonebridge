# phonebridge API contract (v0 draft)

Status: design draft (consolidated with design rounds 1-21,
2026-11-15; see docs/phone-bridge-service-design.md decision log).
Transport: **JSON-RPC 2.0 bidirectional over one connection per peer**.
- Local peers: Unix-domain socket (bind-mounted out of the container).
- Remote peers: **WSS** — providers/agents always dial *into* the gateway (NAT-safe).
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
from agents** (`call.acquire`, `call.get_lines`) as an alias for
"any suitable pool line". Every server response and every `event.*`
payload carries the canonical `line_id` (e.g. `"line-main"`) -
the alias never leaks into wire output (round 11).

## Common types

```ts
type Intent   = "dial_out" | "receive_inbound" | "exclusive_line";
                // earlier drafts named these outbound|inbound|exclusive
type LineState = "FREE"|"BUSY"|"RESERVED"|"RINGING_LOCKED"|"DEGRADED"|"DOWN";
type LineSummary = {
  line_id: string,
  caps: Intent[],
  state: LineState,
  profile?: string
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
| `STALE_TOKEN` | call_token/lease expired or consumed |
| `PROVIDER_UNREACHABLE` | line DEGRADED/DOWN |
| `DIAL_REJECTED` | dial string failed pre-line validation — `data.reason` carries a typed sub-code (see Dial validation) |
| `INVALID_STATE` | operation invalid in the current call/line phase — repeated `dial`, `send_dtmf` outside ACTIVE, approving a closed claim (round 20) |
| `INVALID_ARGUMENT` | invalid input parameters — DTMF alphabet/length violations (`data.reason: "INVALID_DTMF"`), empty intent sets (round 20) |
| `RESOURCE_EXHAUSTED` | queue capacity or per-session ticket/lease limits exceeded (`data.reason`: `QUEUE_FULL`, `SESSION_LIMIT_EXCEEDED`, `GRANT_TTL_EXPIRED` — round 20; these were previously folded into `STALE_TOKEN`) |

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
| `provider.register` | `{line_id, token, caps, profile?, meta?}` | `{assignment_id, heartbeat_interval_ms, media_endpoint?, server_time}` — `media_endpoint` is the static socket address; the per-lease `media_token` arrives later inside `line.dial`/`line.answer` (round 14). `meta` is bounded (round 22): ≤16 keys, keys ≤64 chars, values ≤256 chars — `INVALID_ARGUMENT` beyond |
| `provider.reregister` | same as register + `state_snapshot?` | idempotent; preserves lease state during the 10 s orphan-grace window — `state_snapshot` lists the calls the provider still sees on the D-Bus (`{lease_id?, state, caller_id?}[]`, `lease_id` absent for a call the gateway never issued) so the kernel can re-bind surviving leases (round 21, design §5.4); grace covers the WS-drop level only — provider-reported tunnel/USB death or DEGRADED→DOWN unreachable = immediate teardown, no grace (round 20) |
| `provider.ping` | `{line_id, assignment_id, state_snapshot?}` | `{ok, server_time}` — missed N pings → DEGRADED → DOWN |
| `provider.capability_update` | `{line_id, assignment_id, caps, profile?}` | `{ok}` — replaces caps/profile on the ACTIVE binding; carries NO `token` (the binding id authenticates the already-registered peer); an identical update is an idempotent no-op (round 22) |
| `provider.call_state` | `{line_id, assignment_id, call_token?, lease_id?, caller_id?, state, reason?}` | `{ok, call_token?}` — `state` is the canonical set `ringing` \| `dialing` \| `alerting` \| `active` \| `disconnected`. `caller_id` carries the inbound caller's E.164 on `ringing` (round 22 — previously the wire had no field for WHO rings). **`call_token` minting (round 22):** on `state:"ringing"` the gateway mints the one-shot `call_token` inside `ingest_ring` and returns it in `{ok, call_token}`; the provider MUST echo that `call_token` on every subsequent update for the same ring — including a pre-answer `disconnected` (early caller hangup) — until `line.answer` hands the call a `lease_id`. For rings the gateway discovered via `state_snapshot` (the provider never received a token), a `disconnected` update with NEITHER id on a `RINGING_LOCKED` line resolves to that line's one open ring. On every other state the response is `{ok}`. `lease_id` identifies an established call. Recognized `reason` values on `disconnected` — the provider-reported vocabulary is CLOSED (round 21): `normal`, `busy`, `no_answer`, `rejected`, `network_error`, `dongle_lost` (tunnel/USB death — immediate teardown, no orphan grace), `stack_restart` (vanished call found by `GetCalls()` reconciliation after a bus/name-owner restart). Gateway-minted reasons such as `provider_lost` (orphan-grace expiry or heartbeat timeout) are emitted on the agent event channel as `event.call_state_changed{reason:"provider_lost"}`, never reported here. An update carrying NEITHER `call_token` nor `lease_id` on a line NOT in `RINGING_LOCKED` reports a human-initiated call on the physical phone (round 21): the line goes `BUSY` with reason `external_call`, no lease is minted, and no `line.hangup` is ever sent for it (design §9) |
| `provider.audio_ready` | `{line_id, assignment_id, lease_id}` | `{ok}` — one-shot notification: the provider's local BT SCO path is up (early media flows, uplink accepted). Gateway emits `event.audio_ready` to the agent from this signal |

## Manager → Line — `line.*` (down-channel commands)

| method | params | returns |
|---|---|---|
| `line.dial` | `{assignment_id, lease_id, number, media_endpoint, media_token}` | `{accepted}` — `number` is the already-validated E.164 string, passed through unchanged (see Dial validation); `media_endpoint`/`media_token` authorize the provider's `media.auth` for this `lease_id` |
| `line.answer` | `{assignment_id, lease_id, call_token, media_endpoint, media_token}` | `{accepted}` — same per-lease media credentials as `line.dial` |
| `line.hangup` | `{assignment_id, lease_id}` | `{accepted}` |
| `line.send_dtmf` | `{assignment_id, lease_id, digits}` | `{accepted}` |
| `line.revoke` | `{assignment_id, reason?}` | `{accepted}` — **admin scope only** |

Sent as server-initiated JSON-RPC requests over the provider's own inbound connection — no reverse dial needed.

## APICallService — `agent.*` / `call.*` (Agent → gateway)

| method | params | returns / errors |
|---|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` | `{session_id}` / `INVALID_ARGUMENT` on an empty `intents` set; `INVALID_INTENT` when `intents` contains `exclusive_line` — durable exclusivity is requested only via `call.claim_exclusive` (round 21) |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?, idempotency_key?}` | `{granted: true, line_id, lease_id, expires_at}` or `{granted: false, free_lines[]}` or `{granted: false, queued: true, ticket_id, position}` — `intent` must be `dial_out` (round 14; other intents → `INVALID_INTENT`); `expires_at` is the grant TTL deadline (round 20; missing it → `event.call_state_changed{reason: "grant_ttl_expired"}`); retry dedup is scoped to a non-empty `(session_id, idempotency_key)` — calls without a key create independent tickets so multi-call agents are not merged; `LINE_EXCLUSIVE_NOT_OWNER` (+ `data.free_lines[]`) — except a `system:fallback_worker` session, which bypasses this check to execute admin-configured `forward_via_line` policy (round 22); `RESOURCE_EXHAUSTED`/`SESSION_LIMIT_EXCEEDED` on per-session limits |
| `call.cancel_acquire` | `{session_id, ticket_id}` | `{ok}` — withdraw a queued acquire |
| `call.release` | `{session_id, lease_id?, assignment_id?}` | `{ok}` / `OWNER_MISMATCH` — exactly ONE target (round 22): `lease_id` ends the EPHEMERAL lease (an undialed grant frees the line; an in-flight call is hung up first — the lease never outlives the call); `assignment_id` is the durable-binding path — the exclusive owner voluntarily releases its `LineAssignment` (line reverts to the claimable pool, `event.assignment_revoked` to the owner) |
| `call.dial` | `{session_id, lease_id, number, country}` | `{lease_id, media_endpoint, media_token}` / `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE`, `DIAL_REJECTED` |
| `call.accept` | `{session_id, call_token}` | `{lease_id, line_id, media_endpoint, media_token}` / `STALE_TOKEN`, `OWNER_MISMATCH` — atomic: issues the lease, answers the ring, attaches media; the accepting session MUST hold `receive_inbound` intent (`INVALID_INTENT` otherwise, round 22 — `system:fallback_worker` exempt with its minted `fallback_call_token`) |
| `call.hangup` | `{session_id, lease_id}` | `{ok}` — ends the call AND the ephemeral lease atomically (auto-release, round 22); a trailing `call.release` is an idempotent no-op |
| `call.send_dtmf` | `{session_id, lease_id, digits}` | `{ok}` — `digits` must match `^[0-9A-D*#,wW]{1,32}$` (`,`/`w`/`W` = ~500 ms pauses) else `INVALID_ARGUMENT`/`INVALID_DTMF`; `INVALID_STATE` without an ACTIVE call; in-band tones are synthesized by the LineProvider (oFono `SendTones` is broken on PipeWire Telephony, design §5.5/§6.4) which mutes agent uplink PCM for their duration and emits a `0x02` flush afterwards (round 22) |
| `call.claim_exclusive` | `{session_id, line_id}` | `{claim_id, status: "pending_approval"}` — moderator approval required in admin, never self-granted; pending claims are bounded (≤8 per agent, ≤256 total — `RESOURCE_EXHAUSTED` beyond) and socket-bound (`drop_session` cancels them, round 22) |
| `call.get_lines` | `{session_id, intent?}` | `{lines: LineSummary[]}` |

## APICallService → Agent — `event.*`

| event | payload |
|---|---|
| `event.incoming_call` | `{line_id, call_token, caller_id?, expires_at}` — first valid `call.accept` wins; `expires_at` is the absolute accept-window deadline (RFC 3339 UTC — the earlier relative `expires_in_ms` would skew by queue delay at outbox drain, round 22) |
| `event.line_acquired` | `{session_id, ticket_id, line_id, lease_id, intent, expires_at}` — the grant for a queued ticket; `ticket_id` stays referenced until its lease ends (a preempted queue-origin grant reissues the SAME `ticket_id` at position 1 via `event.queue_updated`, round 22); `expires_at` is the grant TTL deadline — missing it ends the lease with `event.call_state_changed{reason: "grant_ttl_expired"}` (round 20) |
| `event.queue_updated` | `{session_id, ticket_id, position}` |
| `event.missed_call` | `{line_id, caller_id?, reason, voicemail_ref?}` |
| `event.fallback_triggered` | `{line_id, call_token, caller_id?, policy}` — THE fallback delivery channel (round 22): delivered to the `system:fallback_worker` session only, when `accept_window_sec` expires on a WORKER-policy line; `call_token` is the internal `fallback_call_token` (round 14) |
| `event.line_state_changed` | `{line_id, old_state, new_state, reason?}` |
| `event.audio_ready` | `{lease_id, line_id}` — BT SCO path is up: early media (ringback) audible, uplink accepted without first-word clipping |
| `event.call_state_changed` | `{lease_id, line_id, state, reason?, answer_confidence?}` — `state`: `dialing` \| `alerting` \| `active` \| `disconnected` (the provider's `ringing` maps to `event.incoming_call`, not to a lease event); `answer_confidence`: `"provisional"` \| `"confirmed"` — how sure the bridge is that `active` means a human answered (oFono `State=active` observed firing on remote answer, but the guarantee is provisional — design §5.5); agent may start its greeting only on `state == "active"` AND `audio_ready` received |
| `event.assignment_revoked` | `{assignment_id, line_id, owner_agent_id, reason?}` (round 19b; `owner_agent_id` REQUIRED since round 22) — a durable AGENT-owned `LineAssignment` ended: owner `call.release{assignment_id}`, admin `revoke_assignment`, or idle escalation. Delivered ONLY to the revoked owner's session(s). Emitted ONLY for agent assignments — revoking a `ProviderBinding` has no agent audience and instead produces `line.revoke` + `event.line_state_changed{new_state:"DOWN", reason:"admin_revoked"}` (round 22). Without this event an exclusive owner would learn about the revoke only as an `OWNER_MISMATCH` on its next `call.dial` |

**Event audience routing (round 21).** The kernel stamps a routing
audience on every event at emission time (`broadcast` \| `agent` \|
`session`); the EventBus is a stateless router over it and never
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
  `event.line_state_changed{new_state:"DOWN",
  reason:"admin_revoked"}`, round 22).
- `event.line_state_changed` — broadcast.
- `event.line_acquired`, `event.queue_updated`, `event.audio_ready`,
  `event.call_state_changed` — the owning session only.

## Queue semantics (mandatory FIFO)

- `call.acquire` with `enqueue:true` that cannot be served joins the
  FIFO queue (per-line + global pool) and returns
  `{granted:false, queued:true, ticket_id, position}`.
- Position advances via `event.queue_updated`; on grant the agent
  gets `event.line_acquired` and must act within an admin-configured
  grant TTL (~5 s illustrative) or the next ticket is served.
- **Fairness across scopes (round 19):** one monotonic `seq` counter
  covers ALL queue scopes. When line `L` frees, the grant goes to the
  lowest-`seq` eligible ticket — per-line tickets (`line_id == L`)
  and pool tickets (`line_id` unspecified / `None`) compete in one
  global order; no starvation of either scope.
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
  with `enqueue:true` materializes a NEW ticket at the head and
  the session learns its `ticket_id` inside that
  `event.queue_updated` — the caller's enqueue wish is honored;
  `enqueue:false` grants are torn down without reissue.
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
- Per-line inbound `fallback_policy` (admin config). CORE policies (executed inside LineManager, atomic telecom ops): `no_pickup` (carrier voicemail - default) | `reject`. WORKER policies (executed by the internal `FallbackWorker` - a system agent that connects via the standard agent API, ADR-01): `record_and_telegram:<channel>` | `answer_and_announce:<announcement_ref>` (only if an inbound agent is registered; `announcement_ref` is the admin-provisioned WAV/text selector the worker plays — carried inside the policy so the FallbackWorker needs no admin scope, round 22) | `forward_via_line:<line_id>` (the `system:fallback_worker` session bypasses `LINE_EXCLUSIVE_NOT_OWNER` when acquiring the target line, round 22). NOTE: no `spill_to_pool` - a live-but-slow owner = missed call.
- **Fallback token mechanics (round 14; delivery channel fixed round 22):** when `accept_window_sec` expires on a WORKER-policy line, the original `call_token` is already stale and an exclusive-owner check would reject the worker's `call.accept`. LineManager therefore re-issues the ring as an internal `fallback_call_token` bound to `agent_id: "system:fallback_worker"` (bypasses `OWNER_MISMATCH` on accept and `LINE_EXCLUSIVE_NOT_OWNER` on a `forward_via_line` acquire, still single-use and TTL-bound) and delivers `event.fallback_triggered{line_id, call_token: fallback_call_token, caller_id?, policy}` to the FallbackWorker session only — `event.fallback_triggered` is THE delivery channel (round 22; the earlier "or `event.incoming_call`" hedge is removed because the worker needs the `policy` field).

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
as `"provider"` and hijack the phone-side stream.

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
   Flush is in-band — `clear_uplink_buffer`/`flush_audio_out` map to flag `0x02`, no control/media race. A `0x02` frame carries NO audio payload; it has PRIORITY over queued audio frames on the same lease — on receipt the bridge discards every still-queued older uplink audio frame for that lease (round 20, §6.4), so a flush never plays back buffered stale audio after e.g. a synthesized-DTMF segment.

3. **Resource bounds (round 14, CODING_HAZARDS rule 7):**
   - **Pair TTL** — a `media_token` expires ~5 s after issue if no peer
     completes `media.auth`; a half-connected pair (one peer authed,
     the other never arrives) is torn down on the same timer. No
     socket may outlive its `lease_id`.
   - **Bounded queues** — the MediaRouter keeps a bounded ring per
     direction (≈500 frames = 10 s); overflow drops oldest and counts
     `media.dropped` — an agent that floods faster than realtime is
     rate-limited by drop+telemetry, not by memory growth. The router
     never buffers unboundedly between mismatched-rate peers.

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
  `event.line_state_changed{new_state:"DOWN",
  reason:"admin_revoked"}` instead (round 22).
- `admin.configure_line {line_id, fallback_policy?, accept_window_sec?,
  allowed_number_types?, claimable?, idle_revoke_after?, timeouts?}` →
  runtime patch, no restart; `timeouts` carries the remaining
  admin-configurable knobs `{grant_ttl_sec?, orphan_grace_sec?,
  heartbeat_interval_sec?, heartbeat_degraded_after?,
  heartbeat_down_after?}` (round 22, design §10).

Non-kernel admin operations route to their owning subsystem instead
(they are NOT `LineAdminKernel` methods — the kernel owns call/line
state only, not provisioning or bus plumbing):

- `admin.pair_window {line_id, duration_sec}` → routed by the admin
  adapter to the line-bundle supervisor, which drives the container's
  BlueZ `Agent1` (see hci-agent.md §5); opens the pairing window on
  that line's adapter only.

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
