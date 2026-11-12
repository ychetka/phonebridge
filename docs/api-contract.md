# phonebridge API contract (v0 draft)

Status: design draft (consolidated with design rounds 1-10,
2026-11-12; see docs/phone-bridge-service-design.md decision log).
Transport: **JSON-RPC 2.0 bidirectional over one connection per peer**.
- Local peers: Unix-domain socket (bind-mounted out of the container).
- Remote peers: **WSS** — providers/agents always dial *into* the gateway (NAT-safe).

Two endpoint surfaces, one gateway daemon, shared in-process `LineManager` kernel:

| Surface | Listener (example) | Peer | Namespace |
|---|---|---|---|
| **Manager** | `wss://gw:7001/provider` | Line providers (LocalLine, RemoteLine) | `provider.*` (up), `line.*` (down) |
| **APICallService** | `wss://gw:7002/agent` | AI agents | `agent.*`, `call.*` (up), `event.*` (down) |

SMS: OUT OF SCOPE (spike-0, 2026-11-12: no org.ofono.MessageManager
on bus; Android exposes SMS via MAP not HFP). Backlog note only -
design doc §8 lists the hard paths.

Token scopes are separate: provider tokens never grant `agent.*`/`call.*`, agent tokens never grant `provider.*`/`line.*`; `admin` scope is a third class.

`line_id:"default"` is accepted in every RPC and emitted event as a
forward-compat alias for single-line clients (multi-dongle is
first-class in the architecture; implementation may run one line).

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
```

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

`NO_FREE_LINE` was REMOVED (round 9): a busy pool is a normal
result, not an exception - see `call.acquire`.

## Manager namespace — `provider.*` (Line → Manager)

| method | params | returns |
|---|---|---|
| `provider.register` | `{line_id, token, caps, profile?, meta?}` | `{assignment_id, heartbeat_interval_ms, media_endpoint?, server_time}` |
| `provider.reregister` | same as register | idempotent; preserves lease state during grace window |
| `provider.ping` | `{line_id, assignment_id, state_snapshot?}` | `{ok, server_time}` — missed N pings → DEGRADED → DOWN |
| `provider.capability_update` | `{line_id, assignment_id, caps, profile?}` | `{ok}` |
| `provider.call_state` | `{line_id, assignment_id, call_token?, state, reason?}` | `{ok}` — RINGING/ANSWERED/ENDED/FAILED |

## Manager → Line — `line.*` (down-channel commands)

| method | params | returns |
|---|---|---|
| `line.dial` | `{assignment_id, lease_id, number}` | `{accepted}` |
| `line.answer` | `{assignment_id, lease_id, call_token}` | `{accepted}` |
| `line.hangup` | `{assignment_id, lease_id}` | `{accepted}` |
| `line.send_dtmf` | `{assignment_id, lease_id, digits}` | `{accepted}` |
| `line.revoke` | `{assignment_id}` | `{accepted}` — **admin scope only** |

Sent as server-initiated JSON-RPC requests over the provider's own inbound connection — no reverse dial needed.

## APICallService — `agent.*` / `call.*` (Agent → gateway)

| method | params | returns / errors |
|---|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` | `{session_id}` |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?}` | `{granted: true, line_id, lease_id}` or `{granted: false, free_lines[]}` or `{granted: false, queued: true, ticket_id, position}` — empty free_lines = nothing available, normal result not an error; `LINE_EXCLUSIVE_NOT_OWNER` (+ `data.free_lines[]`) |
| `call.cancel_acquire` | `{session_id, ticket_id}` | `{ok}` — withdraw a queued acquire |
| `call.release` | `{session_id, lease_id}` | `{ok}` / `OWNER_MISMATCH` |
| `call.dial` | `{session_id, lease_id, number}` | `{call_token}` / `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE` |
| `call.accept` | `{session_id, lease_id, call_token}` | `{ok}` / `STALE_TOKEN`, `LINE_BUSY_INCOMING`, `OWNER_MISMATCH` — atomic: lease+answer+media attach |
| `call.hangup` | `{session_id, lease_id}` | `{ok}` |
| `call.send_dtmf` | `{session_id, lease_id, digits}` | `{ok}` |
| `call.claim_exclusive` | `{session_id, line_id}` | `{claim_id, status: "pending_approval"}` — moderator approval required in admin, never self-granted |
| `call.get_lines` | `{session_id, intent?}` | `{lines: LineSummary[]}` |

## APICallService → Agent — `event.*`

| event | payload |
|---|---|
| `event.incoming_call` | `{line_id, call_token, caller_id?, expires_in_ms}` — first valid `call.accept` wins |
| `event.line_acquired` | `{session_id, line_id, lease_id, intent}` — also the grant for a queued ticket; agent must act within grant TTL |
| `event.queue_updated` | `{session_id, ticket_id, position}` |
| `event.missed_call` | `{line_id, caller_id?, reason, voicemail_ref?}` |
| `event.line_state_changed` | `{line_id, old_state, new_state, reason?}` |
| `event.audio_ready` | `{call_id}` — "you may speak" gate vs SCO bring-up lag |
| `event.call_state_changed` | `{call_id, state, reason}` |

## Queue semantics (mandatory FIFO)

- `call.acquire` with `enqueue:true` that cannot be served joins the
  FIFO queue (per-line + global pool) and returns
  `{granted:false, queued:true, ticket_id, position}`.
- Position advances via `event.queue_updated`; on grant the agent
  gets `event.line_acquired` and must act within an admin-configured
  grant TTL (~5 s illustrative) or the next ticket is served.
- Tickets are **socket-bound**: disconnect kills them.
- `call.standby` / `line.standby` REMOVED (round 8): no server-side
  parked requests - client retry is the freshness guarantee.

## Intent / allocation semantics

- `dial_out` — line granted from pool for dialing; released after `call.hangup` + `call.release`.
- `receive_inbound` — line granted and marked `BUSY` while the agent waits for a ring; `call.accept` consumes the ring; agent calls `call.release` after the call.
- `exclusive_line` — durable `LineAssignment` (survives disconnects, no heartbeat); granted only via `call.claim_exclusive` + moderator approval or admin assign; conflict → `LINE_EXCLUSIVE_NOT_OWNER` + `data.free_lines[]`.
- All lines busy → `call.acquire` returns `{granted: false, free_lines: []}` — routine result, not an error; client retries itself. No server-side parked requests (a stale parked request = zombie grant risk; client retry = freshness).
- Inbound: ring → `RINGING_LOCKED` → hooks + `event.incoming_call{call_token}` → first `call.accept` = atomic lease+answer. Accept window default **30 s**; ALL timeouts/windows admin-configurable. Timeout → per-line `fallback_policy`.
- Per-line inbound `fallback_policy` (admin config): `no_pickup` (carrier voicemail - default) | `forward_via_line:<line_id>` | `record_and_telegram:<channel>` | `reject` | `answer_and_announce` (only if an inbound agent is registered). NOTE: no `spill_to_pool` - a live-but-slow owner = missed call.

## LineProvider contract (one codebase)

```ts
interface LineProviderConfig {
  manager_endpoint: string;      // wss://127.0.0.1:7001/provider  |  wss://<wan>:7001/provider
  line_id: string;
  token: string;                 // per-line, provisioned out-of-band
  caps: Intent[];
  profile?: string;
  reconnect: { backoff_ms: [number, number]; jitter_pct: number };
  heartbeat_interval_ms: number;
  media_transport: "rtp_wss";  // UNIFIED: always packet audio (LocalLine -> 127.0.0.1)
}
```

LocalLine and RemoteLine run identical logic end-to-end, including media: audio is ALWAYS packetized and sent over the dedicated media channel (loopback for LocalLine). No local PipeWire fast-path - one MediaSink implementation.

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
4. Remote line (e.g. Raspberry Pi): same provider code, dials OUT to
   gateway WSS (NAT-safe), per-line token, reconnect = re-register,
   in-flight call on drop = hangup; WAN leg adds jitter buffer
   ~60-120 ms + audio watchdog.
