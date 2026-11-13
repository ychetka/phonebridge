# phonebridge - BT telephony service design (consolidated)

Status: CONSOLIDATED SPEC (issue #1, 2026-11-12). Supersedes the
founding draft in place: all decisions from partner addendum
rounds 1-3, REQUIREMENT UPDATEs 1-2, UNIFIED ARCH, the refined
intent model, and decision rounds 5-10 are folded into the
sections below. Where a later decision replaced an earlier one,
only the final state is normative; superseded choices are noted
inline as "(superseded: ...)" for traceability. Not yet implemented.

Purpose: a standalone service through which AI agents **place and
receive phone calls** over a Bluetooth HFP bridge to paired phones.
Extracted from `devin-telegram`, which becomes a client only.
**SMS is out of scope** - see §8.

Verification basis: `docs/dialer-phase0-report.md` §6 (devin-telegram
repo) - Phase-0 checks passed on the dev host + Mi 9
(mSBC, SCO 0.47 s, reconnect ~20 s, locked-screen dialing,
`org.ofono.VoiceCallManager` call control, human-confirmed two-way
audio). Host-reboot persistence and `setAudioRoute` remain
NOT EXECUTED / DEFERRED in that report and are not claimed here.

Companion documents:

- `docs/api-contract.md` - wire-level JSON-RPC contract (methods,
  payloads, error codes). This document is the architectural truth;
  the contract file is the protocol truth.
- `docs/architecture-unified.html` - unified architecture diagram.
- `docs/interaction-flow.html` - outbound/inbound call flows.

## Decision log (all rounds, 2026-11-12)

Compact ledger of every accumulated decision; details live in the
referenced sections.

| Round | Decision | Where |
|---|---|---|
| partner addendum 1 | Mandatory FIFO call queue (queued-acquire + `ticket_id` + position events + `cancel_acquire`, socket-bound tickets); `line_id:"default"` forward-compat in all RPCs/events; atomic answer with implicit lease grant + outbound-queue preemption; `audio_ready` gate vs SCO lag | §3.4, §5 |
| spike-0 / backlog | SMS OUT OF SCOPE - no `org.ofono.MessageManager` on the bus | §8 |
| REQUIREMENT UPDATE 1 | Multi-dongle/multi-phone first-class (Line entity, LineManager, per-line + global queue; single-line implementation OK); prod MUST be containerized for host isolation | §2.1, §2.4 |
| REQUIREMENT UPDATE 2 | Entire phonebridge = ONE Docker container, USB dongle passthrough, no reuse of devin-telegram daemon; universal event fan-out (any agent type); Devin = just one subscriber | §2.1, §7 |
| partner round 2 | Inbound routing = gateway-managed wake-and-grant (RINGING_LOCKED, hooks, `call_token`, first `call.accept` wins); profiles as config presets; event delivery = 3 primitives; remote line = LineProvider on Pi dialing OUT | §4, §5.2, §7, §2.2 |
| partner round 3 | Exclusive ownership = two orthogonal state machines: durable LineAssignment vs ephemeral CallLease | §4.2 |
| UNIFIED ARCH / round 4 | One LineProvider codebase registering by address; provider-initiated registration (NAT-safe), per-line token, reconnect = re-register, ping -> DEGRADED -> DOWN; two namespaces + separate token scopes; WAN-only jitter/watchdog; local provider = separate process in the same container | §2.2, §2.3 |
| intent model refined | Line allocation by intent `{dial_out|receive_inbound|exclusive_line}`; free lines offered per intent; no server queue block for intents (client retry); exclusive conflict = explicit error + free-line list | §4.1 |
| round 5 | Two sockets per remote line (control WSS + dedicated media); orphan-lease grace 10 s; AUTH = dedicated workstream; ADMIN PANEL required | §6.2, §5.4, §10 |
| round 6 | Media path FULLY UNIFIED - every line streams packet audio (LocalLine targets 127.0.0.1); one MediaSink implementation | §6.1 |
| round 7 | Media endpoint SERVER-ISSUED (gateway allocates addr/port per line/call); media frames carry stream identity + auth; admin start = SQLite keys DB + config files, web UI deferred | §6.2, §10 |
| round 8 | `call.standby` REMOVED - no server-side parked requests; client retry = freshness guarantee | §3.4 |
| round 9 | `NO_FREE_LINE` error REMOVED - busy is a normal result: `call.acquire` returns `{granted:true,...}` or `{granted:false, free_lines[]}` | §3.3 |
| round 10 | Accept window default 30 s, ALL timeouts admin-configurable; per-line inbound fallback policy set; idle_revoke = alert-only via escalation channels; exclusive binding = claim + moderator approval; admin = private MCP server; `line.revoke` admin-only | §5.3, §4.2, §10 |
| round 11 (architecture review hardening) | Unified ID taxonomy (`lease_id` is the only call id); `call.dial`/`call.accept` return `media_endpoint`+`media_token`; `call.accept` takes only `{session_id, call_token}`; `receive_inbound` keeps line `FREE` until a physical ring; `line_id:"default"` input-only alias; `RedactedString` PII masking; ADR-01 FallbackWorker owns complex fallbacks; ADR-02 binary 20 ms media framing with in-band flush; ADR-03 BDADDR binding + mandatory `/var/lib/bluetooth` + `/var/lib/phonebridge` volumes; WAN media reclassified to prod gate | §2.1, §3.3, §4.1, §5.3, §5.5, §6.2, §12 |
| round 12 (live probes + unified agent model, 2026-11-13) | Live-verified on the real stack: `VoiceCall.Answer` works (incoming→`active` ~0.3 s, SCO ~0.6 s), inbound `Hangup` rejects a ringing call, full E.164 terminates via `Dial` unchanged, `SendTones` is broken on this stack (fallback = in-band DTMF in uplink), WirePlumber auto-routes SCO to the default sink+monitor (echo loop; service must own routing via per-line virtual endpoints + static `node.autoconnect=false` on bluez5 HFP nodes). Architecture: UNIFIED AGENT MODEL — every BT dongle sits behind a dumb `hci-agent` (libusb, btusb detached) feeding a kernel `vhci` adapter inside the container over a pluggable `transport_profile` tunnel (control-TCP + media-UDP; QUIC is a V2 track); container is isolated-netns, sees only virtual adapters; thin-slice per-line worker bundles + shared bluetoothd/PipeWire; `call.dial` requires `number` (strict E.164) + `country` (ISO alpha-2) with phonenumbers cross-validation and a per-line number-type allowlist; agent surface reachable via MCP adapter | §2.1, §2.2, §5.5, §6.4, §9, §10, §12, docs/hci-agent.md |

## §1. Service boundary

### Inside `phonebridge` (owns the telephony plane)

- oFono-compatible call control on `/org/pipewire/Telephony/ag1`:
  `VoiceCallManager.Dial/HangupAll/GetCalls`, per-call
  `org.ofono.VoiceCall.Answer/Hangup`, `AudioGatewayTransport1.Codec`.
  Call objects expose BOTH `org.pipewire.Telephony.Call1` and
  `org.ofono.VoiceCall` interfaces (verified live); a `Hangup` racing
  remote teardown can return "method doesn't exist" - treat as
  already-gone, verify via `GetCalls`/`HangupAll` fallback.
- Call-state machine: oFono call objects -> stable internal
  `CallState` (`idle/dialing/alerting/active/disconnected`), dedup of
  call-object churn.
- SCO audio plumbing: PipeWire node lifecycle for
  `bluez_input.*`/`bluez_output.*`, mSBC assumption-checked at call
  setup (warn if CVSD negotiated).
- Line liveness: per-line transport session + ping; missed pings ->
  `DEGRADED` -> `DOWN`, surfaced as `line_state_changed` events
  (clients must see "degraded" vs "down" distinctly).
- Line ownership: intent-resolver + LineAssignment (durable) +
  CallLease (ephemeral, dead-man switch) - §4.
- Universal event fan-out: EventBus in-daemon (§7).
- Pairing/runbook as operational scripts (migrated from
  `docs/dialer-phase0-runbook.md`, `scripts/dialer_phase0_probe.sh`
  as the service's health check).

### Stays in clients (devin-telegram and future agents)

- TTS/STT, LLM call logic, conversation state, business rules for
  whom/when to call.
- The decision to dial/answer - the service never decides.
- Any per-call audio processing (echo handling, barge-in policy
  decisions; the bridge only owns the uplink buffer mechanics).
- Notification/escalation UX - a client consumes `incoming_call` /
  `line_state_changed` events and decides what to tell the operator.

D-Bus remains internal plumbing toward PipeWire Telephony inside the
container; the agent-facing contract is the phonebridge JSON-RPC API,
not D-Bus.

## §2. Architecture

### §2.1 Deployment artifact: ONE container (prod)

Production deployment is a **single Docker container**
(`phonebridge-gateway`) that contains the full telephony stack:

- the gateway daemon (Manager + APICallService + LineManager +
  EventBus - §2.3),
- one **per-line worker bundle** per line (thin-slice model, round
  12): a `vhci-feeder` + shim/audio-forwarder + `LineProvider`
  process, supervised independently so a dead line never takes the
  gateway or other lines down; BlueZ, PipeWire and dbus-daemon are
  shared, hardened with watchdogs and fast restart,
- BlueZ + PipeWire + dbus-daemon inside the container,
- **no USB dongles passed through** (round 12, replaces the earlier
  `--device` model): every adapter inside the container is a kernel
  `vhci` device fed by an `hci-agent` on the dongle's machine — see
  §2.2 and `docs/hci-agent.md`. The container runs in an isolated
  network namespace; its declared external exposure is exactly the
  listeners it serves: the agent-facing API surface (UDS/WSS + media,
  §2.3) and one control-TCP + media-UDP tunnel endpoint per remote
  line (the hci-agent dials in - §2.2, `docs/hci-agent.md`).

Motive is host isolation: the host system can be wiped and
reinstalled and the telephony stack survives entirely inside the
container — including its Bluetooth subsystem, because no adapter in
the container is real hardware. A host-side dongle is served by an
hci-agent running *on that host* exactly like a remote Pi's agent;
the host's own `bluetoothd` is left free for host devices.

Persistence requirement (ADR-03, round 11): "wipe-safe" is only true
with two MANDATORY persistent volumes:

- `/var/lib/bluetooth` - BlueZ link keys; without it recreating the
  container drops phone pairing and demands manual re-pairing.
- `/var/lib/phonebridge` - SQLite (keys, LineAssignments, configs)
  and any recorded media.

Line identity binding is by `adapter_mac` (BDADDR), never `hciN` -
indices are assigned in enumeration order and swap across reboots.
Under the agent model the BDADDR reported by the agent is the
physical dongle's address on the remote host. Hardware constraint
note: with >2 physical dongles ON ONE AGENT HOST place them on
different root USB controllers/hubs - multiple SCO audio streams on
one USB 2.0 hub hit the isochronous bandwidth allocation limit. The
container side has no such limit: virtual adapters are pure kernel
objects.

(Superseded: founding spec's "systemd user service on the host" as
the production model - host-native deployment remains legal ONLY as a
prototype-validation path.)

### §2.2 Unified LineProvider: one codebase, registration by address

LocalLine and RemoteLine are the **same code**. A line provider's
entire identity is its config:

```ts
interface LineProviderConfig {
  manager_endpoint: string;   // wss://127.0.0.1:7001/provider | wss://<wan>:7001/provider
  line_id: string;
  token: string;              // per-line, provisioned out-of-band via admin
  caps: Intent[];             // which intents the line can serve
  profile?: string;           // config preset, see §4.4
  reconnect: { backoff_ms: [number, number]; jitter_pct: number };
  heartbeat_interval_ms: number;
  media_transport: "rtp_wss"; // ALWAYS packetized; LocalLine targets 127.0.0.1
}
```

- **Provider-initiated registration** (`provider.register`) makes the
  topology NAT-safe: the provider always dials INTO the gateway;
  `line.*` commands ride the provider's own inbound connection as
  server-initiated JSON-RPC - no reverse dial ever happens.
- **Reconnect = re-register** (`provider.reregister`, idempotent;
  preserves lease state during the grace window).
- Liveness: `provider.ping`; missed N pings -> line `DEGRADED` ->
  `DOWN` (interval and N admin-configurable).
- In-flight call on provider drop = treated as hangup; orphaned agent
  leases get a **10 s grace** window to re-bind through the
  re-registered provider before teardown.

RemoteLine example - Raspberry Pi site (round 12): the Pi no longer
runs the provider. It runs the **hci-agent** — a dumb USB↔HCI relay
that owns the dongle in userspace (libusb, `btusb` detached), opens a
`transport_profile` tunnel to the container, and streams HCI frames:
control-TCP (commands/ACL/events + heartbeat/reset) and media-UDP
(SCO). Inside the container the line's `vhci-feeder` re-materializes
the adapter as a kernel `hciN`; `bluetoothd`, the telephony shim and
the per-line LineProvider process treat it exactly like a local
dongle. Remoteness is invisible above the transport layer. A
"smart-remote" deployment — the full provider stack running on the
remote host — remains a documented alternative for links too hostile
for HCI-over-IP timing.

(Superseded: the early "A-REMOTE runs the same binary as a user
service on the SBC" sketch - remote nodes now run the line-provider
code only, and registration is provider-initiated rather than the
gateway reaching in. Superseded again round 12: remote nodes now run
the hci-agent only; the provider is always container-local.)

### §2.3 Two surfaces, one daemon

The gateway daemon exposes two listeners over one shared in-process
LineManager kernel:

| Surface | Listener (example) | Peer | Namespace |
|---|---|---|---|
| **Manager** | `wss://gw:7001/provider` | Line providers (LocalLine, RemoteLine) | `provider.*` (up), `line.*` (down) |
| **APICallService** | `wss://gw:7002/agent` | AI agents | `agent.*`, `call.*` (up), `event.*` (down) |

Deliberate asymmetries inside the otherwise symmetric "everything is
registration" model:

| Aspect | Choice |
|---|---|
| Transport | One: JSON-RPC 2.0 bidirectional + packetized media. Local peers use UDS (bind-mounted out of the container); remote peers use WSS |
| Namespaces | Two: `provider.*`/`line.*` vs `agent.*`/`call.*`/`event.*`, with **separate token scopes** - a provider token never grants agent methods and vice versa |
| Intent vs assignment | Intent is a declarative wish; LineAssignment is the durable truth; conflicts are explicit errors; admin assign remains as override |
| Media hardening | Jitter buffer + audio watchdog exist ONLY on the hostile-link legs: the hci-agent tunnel media-UDP leg (§6.3) and - if ever used - a WAN agent media socket |
| Deployment | One container, several processes; the local provider is a separate process, NOT a separate container |

### §2.4 Multi-dongle / multi-phone first-class

`Line` is a first-class entity from day one:
`{line_id, adapter_mac (BDADDR of the BT dongle - hciN names are
unstable), paired phone, oFono modem, caps, profile,
state}`. `LineManager` owns N lines, with a per-line queue plus a
global pool queue (§5.1). The implementation MAY run a single line -
the architecture must not assume it. `line_id:"default"` is accepted
only in inbound agent RPC requests as a forward-compat alias for
single-line clients (round 11: every response/event carries the
canonical `line_id`).

## §3. Agent-facing API surface

The authoritative wire contract is `docs/api-contract.md`; this
section records the architectural semantics.

### §3.1 Transport

- Local peers: Unix-domain socket (bind-mounted out of the
  container). Remote peers: WSS. Both carry JSON-RPC 2.0
  bidirectionally - requests, responses, and server-pushed
  events on one connection per peer; no client polling.
- Media NEVER shares the control socket: §6.

### §3.2 Types and errors

```ts
type Intent   = "dial_out" | "receive_inbound" | "exclusive_line";
type LineState = "FREE"|"BUSY"|"RESERVED"|"RINGING_LOCKED"|"DEGRADED"|"DOWN";
type LineSummary = { line_id: string, caps: Intent[], state: LineState, profile?: string };
```

Error codes (contract-level): `OWNER_MISMATCH`,
`LINE_EXCLUSIVE_NOT_OWNER`, `LINE_BUSY_INCOMING`, `LINE_NOT_FOUND`,
`INVALID_INTENT`, `STALE_TOKEN`, `PROVIDER_UNREACHABLE`,
`DIAL_REJECTED` (typed `data.reason` - see Dial validation in
`docs/api-contract.md`).

`NO_FREE_LINE` was REMOVED as an error (round 9): a busy pool is a
normal result, not an exception.

### §3.3 Agent namespace (`agent.*` / `call.*`)

| method | semantics |
|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` -> `{session_id}`; declarative wish resolved by the intent-resolver into a LineAssignment (§4) |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?}` -> `{granted:true, line_id, lease_id}` or `{granted:false, free_lines[]}`; `free_lines:[]` = nothing available - routine result, the client retries itself. Optional `enqueue` joins the FIFO queue (§3.4) |
| `call.release` | release a lease; `OWNER_MISMATCH` on foreign lease |
| `call.dial` | `{session_id, lease_id, number, country}` -> `{lease_id, media_endpoint, media_token}`; strict E.164 + ISO alpha-2 cross-check BEFORE the line (round 12) - `DIAL_REJECTED` with a typed `data.reason`, otherwise the validated E.164 passes to `line.dial` unchanged |
| `call.accept` | `{session_id, call_token}` - atomic: issues the lease, answers the ring, attaches media -> `{lease_id, line_id, media_endpoint, media_token}`; `STALE_TOKEN` if the accept window expired, `LINE_BUSY_INCOMING`/`OWNER_MISMATCH` as applicable |
| `call.hangup` / `call.send_dtmf` / `call.get_lines` | call control and line listing |

DTMF is mandatory from v1 - an agent cannot survive IVR trees
without it. Hold/swap/conference/PBAP contacts: deferred (zero value
for single-task agents).

### §3.4 Call queue (required) and what was removed

- **FIFO call queue is mandatory.** A queued acquire returns
  `{granted:false, queued:true, ticket_id, position}`; the ticket
  advances via `event.queue_updated`; on grant the agent receives
  `event.line_acquired{lease_id}` and must react within a short
  admin-configurable grant TTL (~5 s illustrative) or the next ticket
  is served. `call.cancel_acquire{ticket_id}` withdraws. Tickets are
  **socket-bound**: a disconnected agent's tickets die with the
  connection.
- `call.standby` (and the round-2 `line.standby` preferred-handler
  idea) is **REMOVED** (round 8): no server-side parked requests -
  a stale parked request is a zombie-grant risk, and client-side
  retry is the freshness guarantee (a dead/disconnected agent cannot
  hold a stale claim).
- (Superseded: founding spec's "second `dial` -> `409 LineBusy`,
  never a queue" - reversed by partner addendum round 1.)

### §3.5 Provider namespace (`provider.*` up / `line.*` down)

Up: `provider.register`, `provider.reregister`, `provider.ping`,
`provider.capability_update`, `provider.call_state`.
Down (server-initiated on the provider's own inbound connection):
`line.dial`, `line.answer`, `line.hangup`, `line.send_dtmf` -
each carrying `{assignment_id, lease_id, ...}` and returning
`{accepted}`. `line.revoke` exists but is **admin-only** (round 10f).

### §3.6 Events (`event.*` down to agents)

`event.incoming_call{line_id, call_token, caller_id?,
expires_in_ms}` (first valid `call.accept` wins),
`event.line_acquired`, `event.queue_updated`,
`event.missed_call{line_id, caller_id?, reason, voicemail_ref?}`,
`event.line_state_changed{line_id, old_state, new_state, reason?}`,
`audio_ready{lease_id, line_id}` (§5.5),
`call_state_changed{lease_id, line_id, state, reason}` - `lease_id`
is the single call identifier everywhere; no `call_id` exists
(round 11).

`sms_received` and every other SMS surface are removed (§8).

## §4. Lines, intents, and ownership

### §4.1 Intent model (refined)

Agents self-register `INTENT` sets
`{dial_out|receive_inbound|exclusive_line}` via
`agent.register_intent`. The gateway's intent-resolver turns intents
+ line caps into **LineAssignment** records - the durable truth of
who owns what.

- `dial_out` - line granted from the pool for dialing; released
  after `call.hangup` + `call.release`.
- `receive_inbound` - a registered inbound intent does NOT hold the
  line `BUSY` (round 11): the line stays `FREE` (pool-eligible
  unless `exclusive_line`) and only locks to `RINGING_LOCKED` while
  a physical call rings; `call.accept` consumes the ring and issues
  the lease; the agent calls `call.release` after the call.
- `exclusive_line` - durable LineAssignment (survives disconnects,
  no heartbeat).

Free lines are offered per intent; reserved lines are never offered.
No free line for an intent -> `{granted:false}` and the agent
retries later (client-side retry, NO server queue block for intents).
Conflict on exclusive = explicit error
(`LINE_EXCLUSIVE_NOT_OWNER`) **plus a list of FREE lines offered
back**.

### §4.2 Two orthogonal state machines

| | **LineAssignment** | **CallLease** |
|---|---|---|
| Nature | durable, `agent_id`-bound | ephemeral, socket/heartbeat-bound |
| Lifetime | survives agent sleep/disconnect | only during RINGING/DIALING/ACTIVE |
| Ends by | release / `line.revoke` / idle escalation | hangup, release, dead-man switch, orphan grace |

LineAssignment fields: `owner_agent_id`, `mode
(exclusive|shared_pool)`, `claimable`, `accept_window_sec`,
`fallback_policy`, `last_seen_at`, `idle_revoke_after`.

- **Exclusive binding = claim + approval** (round 10d): the agent
  files a CLAIM REQUEST; a moderator approves it in the admin
  surface - it is NOT self-granted. Admin may also assign directly
  (override path).
- Inbound on an owned line: owner is woken via hooks ->
  `RINGING_LOCKED` scoped to the owner -> `call.accept(call_token)`;
  non-owner accept = `OWNER_MISMATCH`; non-owner dial =
  `LINE_EXCLUSIVE_NOT_OWNER`. Timeout -> `fallback_policy`
  (default `no_pickup`/missed-call hook). There is NO
  `spill_to_pool` - a live-but-slow owner simply misses the call
  (round-3's opt-in spill idea was dropped in favor of the explicit
  per-line fallback policies of §5.3).
- Release = future calls only; an in-flight call finishes.
- `line.revoke` = admin-only (confirmed round 10f).
- `idle_revoke_after` = **alert-only** (round 10c): notifications via
  admin-configured escalation channels ("line broken / agent
  broken"); pool lines may auto-recover. Whether auto-reassign should
  ever apply is an OPEN question (§12).

### §4.3 Wake-and-grant inbound routing

(See §5.2 for the full sequence.) Gateway-managed wake-and-grant was
chosen over both a persistent listen-lease and a broadcast-race:
incoming -> `RINGING_LOCKED` -> hooks fire (wake sleepers) +
`incoming_call` event with `call_token` to live subscribers -> first
`call.accept(call_token)` performs atomic lease+answer.

### §4.4 Profiles

Profiles are **config presets** over primitive policies
(`inbound_policy`, `outbound_access`, `priority`,
`missed_call_hook`), not hardcoded agent classes. Reference presets:

- `outbound_campaign`: reject/ignore inbound, pool member.
- `secretary`: wake_and_route inbound, explicit-only outbound,
  inbound preempts the outbound queue.

## §5. Call lifecycle

### §5.1 Outbound

1. `call.acquire{intent:"dial_out", enqueue?}` - granted immediately
   (`{granted:true, line_id, lease_id}`), refused cleanly
   (`{granted:false, free_lines[]}`), or queued
   (`{queued:true, ticket_id, position}` + `queue_updated` events).
2. `call.dial{lease_id, number, country}` -> gateway validates the
   pair (`DIAL_REJECTED` on failure, never reaches the line) and
   issues `line.dial{assignment_id, lease_id, number}` with the
   validated E.164 to the owning provider (identical path for every
   line - remoteness lives below, in the hci-agent transport).
3. Call reaches `alerting` -> implicit media attach (§5.5) ->
   `audio_ready`.
4. Duplex media flows (§6); `call.send_dtmf` for IVR navigation.
5. `call.hangup` + `call.release` -> lease ends, queue advances.

### §5.2 Inbound: wake-and-grant

1. Phone rings -> provider reports `provider.call_state{RINGING}` ->
   gateway marks the line `RINGING_LOCKED` (outbound `call.acquire`
   on it fails `LINE_BUSY_INCOMING`; an inbound intent on a secretary
   profile preempts the outbound queue).
2. Gateway consults LineAssignment: owner exists -> wake owner via
   configured hooks; no owner -> event to live subscribers per the
   line's `inbound_policy`.
3. `event.incoming_call{line_id, call_token, caller_id?,
   expires_in_ms}` is emitted.
4. First `call.accept(call_token)` = atomic lease + answer + media
   attach. Owner-scoped lines reject non-owner accepts with
   `OWNER_MISMATCH`; a second accept gets `STALE_TOKEN` /
   `LINE_BUSY_INCOMING`.
5. **Accept window: default 30 s; ALL timeouts and windows are
   admin-configurable** (round 10a).
6. Timeout -> the line's `fallback_policy` (§5.3) -> release back to
   `FREE`.

### §5.3 Per-line inbound fallback policy (round 10b, split round 11)

Admin-configured per line; on accept-window expiry (or no
subscribed/owner agent). Two classes (ADR-01):

**Core policies** - atomic telecom ops executed inside LineManager:

- `no_pickup` - let the carrier handle it (carrier voicemail /
  missed call on the phone itself; default),
- `reject` - actively reject the call.

**Worker policies** - executed by `FallbackWorker`, an internal
system agent that connects through the STANDARD agent API
(`call.accept` + media socket); LineManager never touches Telegram
HTTP, WAV files, or call bridging:

- `forward_via_line:<line_id>` - the gateway dials OUT on another
  line and bridges (e.g. a call to a personal number is redirected
  out through a second line),
- `record_and_telegram:<channel>` - answer, record the call/
  voicemail ourselves, post to a Telegram channel,
- `answer_and_announce` - answer and play an announcement (only if
  the line has an agent registered for inbound; params via admin).

### §5.4 Lease teardown and orphan handling

- Dead-man switch: a dead/disconnected agent loses its CallLease and
  its call is hung up.
- Orphan-lease grace on provider reconnect: **10 s** (round 5b).
- Missed-call path emits `event.missed_call{reason, voicemail_ref?}`.

### §5.5 Audio attach semantics

When a call reaches `alerting`, the bridge starts pumping downlink
frames to the lease owner and accepts uplink frames, so early
media/ringback is audible. `audio_ready` is the "you may speak"
gate: the first uplink word sent before it can be truncated by
Bluetooth SCO bring-up lag. It signals ONLY that the local SCO path
is up - it does NOT mean the remote party answered (round 11).
`call_state_changed{state:"active"}` is the remote-answer signal.
Agent rule: start the greeting only when `state == "active"` AND
`audio_ready` was received. `clear_uplink_buffer` /
`flush_audio_out` truncate queued playback (barge-in mechanics -
the bridge owns the uplink buffer; in-band flush flag 0x02 on the
media frame, ADR-02).

Live-probe results (2026-11-13, real stack, run-3 artifacts): the
open validation questions are RESOLVED on the current shim:

- `VoiceCall.Answer` on an incoming call works: `incoming`→`active`
  in ~0.3 s, SCO node up ~0.6 s — the `call.accept` fast path is
  implementable as designed.
- `Hangup` on a ringing incoming call rejects it cleanly (call
  object gone ~1.4 s; caller sees a normal rejection) — the
  `reject` fallback policy is implementable.
- `State=active` coincides with remote answer (human answered; the
  ringback gap matches). Data is provisional (one confirmed sample);
  `answer_confidence` remains in the contract as a hedge.
- `SendTones` is BROKEN on this stack (`invalid characters in text
  string` for valid DTMF) — in-band DTMF synthesis in uplink PCM is
  the fallback; `call.send_dtmf` must not assume the bus method.
- Full E.164 passes `Dial` and terminates correctly (previously
  believed false — the failures were malformed `+`-hybrids without
  a country code). Pass-through of validated E.164 is the canonical
  dial path (api-contract §Dial validation).
- WirePlumber auto-routes SCO to the default sink + its monitor:
  the remote party hears themselves (echo loop). The service must
  own the audio graph explicitly — §6.4.

## §6. Media path

### §6.1 Always packetized (fully unified, round 6)

EVERY line streams audio over the network media channel; LocalLine
just targets `127.0.0.1`. There is ONE `MediaSink` implementation -
packet audio - and NO local PipeWire fast-path, so provider code is
truly identical across local and remote deployments (only
`manager_endpoint`/`token` differ).

(Superseded: founding spec's "raw PCM frames multiplexed onto the
single UDS control socket, with direct PipeWire attachment as a
local optimization", and the intermediate `pipewire_local` variant.)

### §6.2 Two sockets per remote line, server-issued endpoint

(Round 12 rescope: this section describes the provider↔gateway
media leg, which applies verbatim to container-local providers on
loopback and — under the unified agent model — only to the fallback
"smart-remote" deployment. The remote hci-agent↔container legs are a
different pair: control-TCP + media-UDP inside the `transport_profile`
tunnel, `docs/hci-agent.md` §2 — the server-issued endpoint idea is
preserved there as the per-line tunnel port pair.)

- A remote line uses TWO sockets: one control WSS + one dedicated
  media channel.
- The media endpoint is **SERVER-ISSUED** (round 7a): the gateway
  allocates the media address/port per line or per call and returns
  it in the register-ack / call grant. Media frames carry stream
  identity + auth so the gateway routes them to the right call
  (muxing/auth INSIDE the media protocol, or a per-call ephemeral
  port).

### §6.3 Codecs and WAN hardening

- Baseline payload: 16 kHz 16-bit mono PCM (native mSBC rate).
- WAN transport under evaluation (round 5a): **RTP+Opus** (standard
  for audio streaming) vs framed PCM on the dedicated socket.
  WSS+raw PCM ships first; Opus/WebRTC is the upgrade path if real
  links glitch.
- **Hostile-link** leg hardening (round 12 rescope): the WAN leg is
  the hci-agent tunnel's media-UDP channel - stale SCO frames are
  dropped by design, so the jitter buffer (~60-120 ms) + audio
  watchdog live at the container-side media-UDP ingress (vhci-feeder),
  NOT on the agent↔gateway socket. The local loopback leg needs
  neither.
- Latency budget: bridge must add < 100 ms node<->socket; verified
  by a loopback test in CI.

### §6.4 Audio-graph ownership and the echo loop (round 12)

Live `pw-link` capture during an active call proved WirePlumber
auto-routes SCO nodes like a desktop headset:

- downlink `bluez_input.<mac>` → the host's DEFAULT sink playback
  (the far end's voice played on the host's speakers),
- uplink ← that same sink's `monitor` → `bluez_output.<mac>`
  (the remote party hears itself — the echo loop observed live).

Design consequences:

- The service creates **per-line virtual endpoints**
  (`phonebridge_uplink_<line_id>`, `phonebridge_downlink_<line_id>`)
  and links them explicitly for the duration of a lease:
  `bluez_input → downlink node → media pipeline → uplink node →
  bluez_output`. Agent-facing audio (the media socket) attaches to
  these endpoints, never directly to transient bluez nodes.
- Auto-routing is suppressed with a **static WirePlumber rule**
  matching bluez5 HFP nodes (`node.autoconnect=false`), shipped by
  the package — NOT by per-call unlinking, which would leak ~50 ms
  of audio to the default sink before the service could react.
- Multi-line: endpoints are created/destroyed per lease lifecycle
  of their line; one SCO channel exists per HFP line, so concurrent
  calls on ONE line (call waiting) share the audio path — the node
  proves "line has media", the binding to a call is via
  `line_id`+`lease_id`, never via the node.
- In-band DTMF (SendTones is broken on the bus, §5.5) is generated
  by mixing synthesized tones into the uplink endpoint.

### §6.5 Remote-line audio transport (round 12)

Between the hci-agent and the container, SCO rides the **media-UDP**
channel of the line's tunnel profile (isochronous semantics: stale
frames are dropped, never retransmitted); control flows on
control-TCP. A QUIC/multiplexed-streams unification is a V2 track.

## §7. Events and universal fan-out

Event delivery is **universal** (any agent type, not just Devin) and
implemented by an `EventBus` module inside the gateway daemon.
Three primitives only - no broker in v1:

1. **Socket subscribe** - broadcast to live subscribers on the
   agent/control socket.
2. **Webhook** - HTTP POST per event type with retry, idempotency
   `delivery_id`, and HMAC signature.
3. **Exec / file-drop** - thin hook: run a command or drop a file
   per event type (wake mechanisms for sleeping agents).

Devin integration is just ONE subscriber: a host-side forwarder
bridges the bind-mounted socket directory to sessions-bridge. No
Devin-specific code lives in phonebridge.

## §8. SMS - OUT OF SCOPE (dropped)

SMS is **removed from scope** (developer decision 2026-11-12, after
spike-0): `org.ofono.MessageManager` is absent on the bus - Android
serves SMS over the Bluetooth MAP profile rather than HFP AT
commands on many OEM builds, and PipeWire's Telephony surface is
narrower than full oFono. `send_sms`, `sms_received`, and the
`sms` capability are deleted from the API contract.

(Superseded: founding spec §2.4's capability-flagged `SmsBackend`
with `unsupported` fallback.)

If SMS is ever needed it is a HARD path - backlog note only, not
scheduled: ADB/KDE Connect over a USB cable, an Android companion
app, MAP profile implementation (`obexd` /
`org.bluez.obex.MessageAccess1`), a dedicated GSM modem/SIM
gateway, or a SIP SMS aggregator.

## §9. Topology summary

| Deployment | Shape |
|---|---|
| **Prod** | ONE container (isolated netns): gateway daemon + per-line worker bundles + BlueZ/PipeWire/dbus inside; every adapter is kernel `vhci` fed by an hci-agent — host-local agents feed dongles on the same machine too; agents via bind-mounted socket dir (UDS) or WSS/MCP |
| **Prototype** | Host-native systemd user service - capability validation ONLY, never prod (`org.pipewire.Telephony` lives on the USER session bus - a system-level unit cannot reach it and fails silently with `ServiceUnknown`) |
| **Remote site** | Raspberry Pi etc. runs ONLY the hci-agent (USB↔HCI relay, no bluetoothd); control-TCP + media-UDP over a `transport_profile` tunnel (wireguard default; direct_mtls; sstp/openconnect/vless_xray reserved profiles); heartbeat + remote USB reset; in-flight call on tunnel drop = hangup |
| **Agent host (any)** | One hci-agent instance per dongle; owns USB via libusb with `btusb` detached; host `bluetoothd` unaffected |

Single-writer applies twice: (a) lease/assignment arbitration inside
LineManager, (b) exactly one gateway instance per deployment
(socket path / listen port held exclusively, refuse to start if
held).

Observability: structured logs, `line_state_changed`/call events on
the event stream, `phonebridgectl` CLI for manual dial/hangup/
status (mirrors the busctl commands used in Phase-0).

## §10. Security and administration

- **Auth is a dedicated workstream** (round 5c). Token scopes are
  separate per surface: provider tokens never grant
  `agent.*`/`call.*`, agent tokens never grant `provider.*`/`line.*`;
  `admin` scope is distinct again. Agent keys are issued at the
  API-request stage; provider keys authenticate line registration.
  Per-line and per-Pi tokens are provisioned out-of-band through the
  admin surface.
- **Admin surface is a requirement** (round 5d) and starts as:
  - a **SQLite keys DB** (agents AND lines) + config files for
    client configs; web UI deferred (round 7b);
  - a **private MCP server interface** so the developer's trusted AI
    agent can administer the system (round 10e): key
    generation/revocation, caps/profile/claimable/
    `accept_window_sec`/`fallback_policy` per line, claim approvals,
    escalation channels.
- **ALL timeouts and windows are admin-configurable** (round 10a):
  accept window (default 30 s), queue grant TTL, ping/DEGRADED/DOWN
  thresholds, orphan grace.
- **Escalation channels** (round 10c): admin-configured (e.g.
  Telegram) for idle/broken-line alerts; `idle_revoke_after` fires
  alerts only - resolved question `idle_revoke` (§12).
- **Per-line dial policy** (round 12): `allowed_number_types`
  defaults to `{FIXED_LINE, MOBILE, VOIP, TOLL_FREE}`; an admin may
  additionally enable `{PREMIUM_RATE, SHARED_COST, PAGER,
  PERSONAL_NUMBER}`. `line_home_country` (ISO alpha-2) is config +
  cross-check against the live registration. Short codes and
  emergency numbers are rejected on the ordinary path.
- **Per-line transport profile** (round 12): the admin selects
  `transport_profile` (`wireguard`/`direct_mtls`/`sstp`/
  `openconnect`/`vless_xray`) for each remote line; protocol
  parameters are canonical system packs, not user-editable. Line
  onboarding lifecycle: `PROVISIONING → PAIRING → READY → IN_CALL →
  DEGRADED → FAULTED`.
- **Exclusive assignment requires moderator approval** in admin -
  an agent's claim request is never self-granted (round 10d).
- Webhook events are authenticated via HMAC + `delivery_id`
  idempotency (§7).
- Secrets/PII (tokens, caller numbers) must never reach logs.

## §11. Test strategy without a real call

- **Fake-D-Bus harness** (primary): a test double implementing
  `org.pipewire.Telephony.*`/`org.ofono.*` on a private session bus
  (`dbus-daemon --session` in pytest fixtures) - scripted call
  objects, state transitions, `RejectSCO`, codec values. Covers:
  call FSM, lease arbitration, watchdog transitions, hangup-race
  handling. Runs on plain CI runners - unit tests MUST NOT spawn
  real Bluetooth sessions, oFono/PipeWire daemons, or touch the
  system D-Bus.
- **Fake LineProvider**: a WSS client double exercising
  register/ping/reregister, DEGRADED->DOWN transitions, media-frame
  routing and stream-auth - the local provider's loopback path is
  tested identically since code is unified.
- **ofono-phonesim**: optional deeper fidelity for the oFono dialect
  (call states, DTMF) - heavier setup, gate it as a
  manual/integration job, not per-commit CI.
- **Audio path**: generated sine/tone clips through PipeWire null
  sinks/sources instead of bluez nodes - the same deterministic-clip
  technique Phase-0 used (523/659/784 Hz partial detection) verifies
  the media pump end-to-end without RF.
- **Protocol conformance**: golden JSON-RPC transcripts; a reference
  client replays a scripted call (acquire -> dial -> alerting ->
  active -> DTMF -> hangup; and incoming -> RINGING_LOCKED ->
  call.accept) against the fake bus; intent-resolver and queue
  position/cancel cases are unit-tested.
- **Live tests** against real hardware sit behind the `e2e` pytest
  marker and require an opt-in environment; CI runs
  `pytest -m "not e2e"` only.

## §12. Open questions (still open - marked as such)

1. **WAN media transport**: largely RE-POSED by round 12 — under the
   unified agent model the WAN leg carries raw HCI SCO over the
   tunnel's media-UDP channel (isochronous, drop-stale by design);
   the RTP+Opus-vs-framed-PCM question now applies only to the
   agent↔gateway media socket (ADR-02), where framed PCM already
   ships. QUIC/multiplexed streams is the V2 unification track.
   The remaining WAN question is SCO-over-UDP quality on hostile
   links — to be measured by the vhci experiment (§12.3).
2. **`remote_answered` fidelity** (carried from founding spec):
   RESOLVED provisionally 2026-11-13 — live probes show `active`
   fires on remote answer (one confirmed sample); `answer_confidence`
   stays in the contract as hedge. Also resolved: `Answer`/`Hangup`
   on incoming calls work; `SendTones` is broken (in-band DTMF
   fallback); the WirePlumber echo loop requires owned routing (§6.4).
3. **vhci end-to-end feasibility** (NEW, round 12): does the full
   path `dongle → hci-agent → tunnel → vhci → live call with SCO
   audio` hold, incl. the isochronous endpoint forwarding? This is
   the gate for the unified agent model — must be proven by the
   dedicated experiment before the model is locked.

### Questions resolved by developer ruling 2026-11-12

- **Remote Pi lines - outbound too?** RESOLVED: YES - remote
  providers expose the full bidirectional `line.*` surface
  (`line.dial` included); a remote Pi line is usable for both
  inbound and outbound cold-calling, not notify-only.
- **Secretary fallback - mandatory or configurable?** RESOLVED:
  CONFIGURABLE - `fallback_policy` is per-line admin configuration
  (§5.3) with `no_pickup` as default; nothing is hard-mandated, all
  options and parameters live in the admin surface.
- **idle_revoke: alert-only vs auto-reassign?** RESOLVED:
  alert-only for exclusive lines - a personal/secretary line is
  NEVER silently reassigned by the machine; alerts go to
  admin-configured escalation channels. Pool lines (no owner) may
  auto-recover/rejoin the pool.

### Founding-spec questions resolved by later rounds

- MVP line scope -> replaced by the intent model (§4.1).
- SMS fallback scope -> SMS is out of scope entirely (§8).
- Repo hosting under canonical conventions -> confirmed; this is
  that repo (`ychetka/phonebridge`).
- Multi-client trust -> token scopes + admin key lifecycle (§10);
  deeper tenant isolation deferred until a second trust domain
  appears.
- PCM streaming vs PipeWire attach -> media is always packetized
  (§6.1); client-side WAV-chunk buffering adapters are acceptable
  for v1, streaming-STT later.

## §13. Implementation roadmap (round 11)

Sequenced into narrow, independently testable PRs:

1. **M1 - Core domain & state machines** (pure Python, no I/O):
   `RedactedString`, `Intent`, `LineState`, `LineSummary`; in-memory
   `LineManager` (LineAssignment + CallLease + FIFO queue +
   RINGING_LOCKED + atomic accept). Unit tests only.
2. **M2 - Auth/admin SQLite store + bidirectional JSON-RPC**:
   key scopes (provider|agent|admin) + line configs in SQLite;
   UDS/WSS JSON-RPC server with Manager vs APICallService
   namespaces and ownership checks; EventBus (socket subscribers,
   HMAC webhooks with retry/timeout, exec/file-drop).
3. **M3 - MediaRouter (loopback binary framing, ADR-02)**:
   `media.auth` handshake via `media_token`; 20 ms frame routing
   provider<->agent; flag 0x02 flush. CI loopback test: generated
   sine from fake provider to fake agent, latency < 100 ms.
4. **M4 - LineProvider (BlueZ/oFono/PipeWire) + Fake-D-Bus harness**:
   `adapter_mac` binding (ADR-03), D-Bus adapters
   (`org.ofono.VoiceCallManager` / `org.pipewire.Telephony`), PipeWire
   node <-> media socket PCM pump. Tests on isolated
   `dbus-daemon --session` + PipeWire null-sink.
5. **M5 - FallbackWorker + phonebridgectl + MCP admin + Docker**:
   internal system agent for worker fallback policies (ADR-01);
   admin CLI and private MCP server; single Dockerfile
   (supervisord: gateway + localline + dbus/bluez/pipewire) with
   mandatory volumes `/var/lib/bluetooth` + `/var/lib/phonebridge`.
