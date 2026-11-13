# phonebridge API contract (v0 draft)

Status: design draft (consolidated with design rounds 1-18,
2026-11-13; see docs/phone-bridge-service-design.md decision log).
Transport: **JSON-RPC 2.0 bidirectional over one connection per peer**.
- Local peers: Unix-domain socket (bind-mounted out of the container).
- Remote peers: **WSS** — providers/agents always dial *into* the gateway (NAT-safe).
- The agent surface is also reachable through an **MCP transport
  adapter** (round 12): the same `agent.*`/`call.*` namespaces and
  the same token scopes, exposed as MCP tools for AI agents that
  natively speak MCP (e.g. the developer's secretary agent). The
  admin UI is just one consumer of the same API — nothing in the
  contract is UI-specific.

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
```

## Identifier taxonomy (round 11)

| id | issued by | lifetime | purpose |
|---|---|---|---|
| `assignment_id` | LineManager | durable | line<->owner/provider binding; survives reconnects |
| `call_token` | LineManager | TTL = `accept_window_sec` (default 30 s) | one-shot token for an inbound ring (`RINGING_LOCKED`); burns on `call.accept` or timeout |
| `lease_id` | LineManager | ephemeral (one call / one hold) | single id of the active line hold AND the call - replaces any `call_id` everywhere (methods and events) |
| `media_token` | MediaRouter | ephemeral (bound to `lease_id`) | authorizes the binary media socket handshake |

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
| `provider.register` | `{line_id, token, caps, profile?, meta?}` | `{assignment_id, heartbeat_interval_ms, media_endpoint?, server_time}` — `media_endpoint` is the static socket address; the per-lease `media_token` arrives later inside `line.dial`/`line.answer` (round 14) |
| `provider.reregister` | same as register | idempotent; preserves lease state during grace window |
| `provider.ping` | `{line_id, assignment_id, state_snapshot?}` | `{ok, server_time}` — missed N pings → DEGRADED → DOWN |
| `provider.capability_update` | `{line_id, assignment_id, caps, profile?}` | `{ok}` |
| `provider.call_state` | `{line_id, assignment_id, call_token?, lease_id?, state, reason?}` | `{ok}` — `state` is the canonical set `ringing` \| `dialing` \| `alerting` \| `active` \| `disconnected`; `call_token` identifies the inbound ring, `lease_id` an established call |
| `provider.audio_ready` | `{line_id, assignment_id, lease_id}` | `{ok}` — one-shot notification: the provider's local BT SCO path is up (early media flows, uplink accepted). Gateway emits `event.audio_ready` to the agent from this signal |

## Manager → Line — `line.*` (down-channel commands)

| method | params | returns |
|---|---|---|
| `line.dial` | `{assignment_id, lease_id, number, media_endpoint, media_token}` | `{accepted}` — `number` is the already-validated E.164 string, passed through unchanged (see Dial validation); `media_endpoint`/`media_token` authorize the provider's `media.auth` for this `lease_id` |
| `line.answer` | `{assignment_id, lease_id, call_token, media_endpoint, media_token}` | `{accepted}` — same per-lease media credentials as `line.dial` |
| `line.hangup` | `{assignment_id, lease_id}` | `{accepted}` |
| `line.send_dtmf` | `{assignment_id, lease_id, digits}` | `{accepted}` |
| `line.revoke` | `{assignment_id}` | `{accepted}` — **admin scope only** |

Sent as server-initiated JSON-RPC requests over the provider's own inbound connection — no reverse dial needed.

## APICallService — `agent.*` / `call.*` (Agent → gateway)

| method | params | returns / errors |
|---|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` | `{session_id}` |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?}` | `{granted: true, line_id, lease_id}` or `{granted: false, free_lines[]}` or `{granted: false, queued: true, ticket_id, position}` — `intent` must be `dial_out` (round 14; other intents → `INVALID_INTENT`); empty free_lines = nothing available, normal result not an error; `LINE_EXCLUSIVE_NOT_OWNER` (+ `data.free_lines[]`) |
| `call.cancel_acquire` | `{session_id, ticket_id}` | `{ok}` — withdraw a queued acquire |
| `call.release` | `{session_id, lease_id}` | `{ok}` / `OWNER_MISMATCH` |
| `call.dial` | `{session_id, lease_id, number, country}` | `{lease_id, media_endpoint, media_token}` / `OWNER_MISMATCH`, `PROVIDER_UNREACHABLE`, `DIAL_REJECTED` |
| `call.accept` | `{session_id, call_token}` | `{lease_id, line_id, media_endpoint, media_token}` / `STALE_TOKEN`, `LINE_BUSY_INCOMING`, `OWNER_MISMATCH` — atomic: issues the lease, answers the ring, attaches media |
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
| `event.fallback_triggered` | `{line_id, call_token, caller_id?, policy}` — delivered to the `system:fallback_worker` session only, when `accept_window_sec` expires on a WORKER-policy line; `call_token` is the internal `fallback_call_token` (round 14) |
| `event.line_state_changed` | `{line_id, old_state, new_state, reason?}` |
| `event.audio_ready` | `{lease_id, line_id}` — BT SCO path is up: early media (ringback) audible, uplink accepted without first-word clipping |
| `event.call_state_changed` | `{lease_id, line_id, state, reason?, answer_confidence?}` — `state`: `dialing` \| `alerting` \| `active` \| `disconnected` (the provider's `ringing` maps to `event.incoming_call`, not to a lease event); `answer_confidence`: `"provisional"` \| `"confirmed"` — how sure the bridge is that `active` means a human answered (oFono `State=active` observed firing on remote answer, but the guarantee is provisional — design §5.5); agent may start its greeting only on `state == "active"` AND `audio_ready` received |

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

- `call.acquire` serves **only `dial_out`** (round 14): inbound rings are consumed atomically by `call.accept(call_token)` without a prior grant, and durable exclusive ownership goes through `call.claim_exclusive` — so `acquire` never needs the other intents, which halves the LineManager grant state machine. `call.get_lines` still accepts any `intent?` for capability filtering.
- `dial_out` — line granted from pool for dialing; released after `call.hangup` + `call.release`.
- `receive_inbound` — a registered inbound intent does NOT hold the line `BUSY` (round 11): the line stays `FREE` (available to the pool unless `exclusive_line`) and transitions to `RINGING_LOCKED` only while a physical call rings; `call.accept` consumes the ring and issues the lease; agent calls `call.release` after the call.
- `exclusive_line` — durable `LineAssignment` (survives disconnects, no heartbeat); granted only via `call.claim_exclusive` + moderator approval or admin assign; conflict → `LINE_EXCLUSIVE_NOT_OWNER` + `data.free_lines[]`.
- All lines busy → `call.acquire` returns `{granted: false, free_lines: []}` — routine result, not an error; client retries itself. No server-side parked requests (a stale parked request = zombie grant risk; client retry = freshness).
- Inbound: ring → `RINGING_LOCKED` → hooks + `event.incoming_call{call_token}` → first `call.accept` = atomic lease+answer. Accept window default **30 s**; ALL timeouts/windows admin-configurable. Timeout → per-line `fallback_policy`.
- Per-line inbound `fallback_policy` (admin config). CORE policies (executed inside LineManager, atomic telecom ops): `no_pickup` (carrier voicemail - default) | `reject`. WORKER policies (executed by the internal `FallbackWorker` - a system agent that connects via the standard agent API, ADR-01): `record_and_telegram:<channel>` | `answer_and_announce` (only if an inbound agent is registered) | `forward_via_line:<line_id>`. NOTE: no `spill_to_pool` - a live-but-slow owner = missed call.
- **Fallback token mechanics (round 14):** when `accept_window_sec` expires on a WORKER-policy line, the original `call_token` is already stale and an exclusive-owner check would reject the worker's `call.accept`. LineManager therefore re-issues the ring as an internal `fallback_call_token` bound to `agent_id: "system:fallback_worker"` (bypasses `OWNER_MISMATCH`, still single-use and TTL-bound) and delivers `event.incoming_call{call_token: fallback_call_token}` — or `event.fallback_triggered` — to the FallbackWorker session only.

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
  media_transport: "rtp_wss";  // UNIFIED: always packet audio (LocalLine -> 127.0.0.1)
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
(provider, round 14).

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
2. **Streaming** — all subsequent frames are BINARY, fixed 648 B:
   - `byte 0` `uint8` flags: `0x01`=audio, `0x02`=flush/clear-uplink-buffer, `0x03`=keepalive;
   - `bytes 1..3` `uint24` BE `seq` (monotonic, loss detection);
   - `bytes 4..7` `uint32` BE `timestamp_ms` (stream-relative);
   - `bytes 8..647` payload: raw PCM 16 kHz S16_LE mono = 640 B = 20 ms. Flags `0x02`/`0x03` may carry 0-byte payload.
   Flush is in-band — `clear_uplink_buffer`/`flush_audio_out` map to flag `0x02`, no control/media race.

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
