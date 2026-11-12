# phonebridge - BT telephony service design (draft for approval)

Status: DRAFT for developer approval (developer directive 2026-11-12,
issue #305-context). Not yet implemented.

Purpose: a standalone service through which AI agents **place and
receive phone calls and send/receive SMS** over a Bluetooth HFP bridge
to a paired Android phone. Extracted from `devin-telegram`, which
becomes a client only.

Partner consultations (2026-11-12): gh-copilot-partner
software-architect (service boundaries, D-Bus surface, packaging) and
gemini-product (MVP scope, agent API contract). Their recommendations
are reflected below; the one intentional divergence - audio transport -
is decided in section 2.3 with rationale.

Verification basis: `docs/dialer-phase0-report.md` §6 - Phase-0
checks passed on VarenikxPc + Mi 9
(mSBC, SCO 0.47 s, reconnect ~20 s, locked-screen dialing,
`org.ofono.VoiceCallManager` call control, human-confirmed two-way
audio). Host-reboot persistence and `setAudioRoute` remain
NOT EXECUTED / DEFERRED in that report and are not claimed here.

## §1. Service boundary

### Inside `phonebridge` (owns the BT telephony plane)

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
- Reconnect watchdog: `AudioGateway1` presence polling, re-arm on BT
  reconnect, emits `link_state_changed` events (clients must see
  "reconnecting" vs "down" distinctly).
- Single-writer line lease: one phone line, one owner. Lease with
  TTL/heartbeat; a dead client loses the lease and its call is hung
  up (dead-man switch). Second `dial` during an active lease -> structured
  `409 LineBusy`, never a queue (calls are not queueable).
- SMS adapter behind a capability flag (§2.4).
- Pairing/runbook as operational scripts (migrated from
  `docs/dialer-phase0-runbook.md`, `scripts/dialer_phase0_probe.sh`
  as the service's health check).

### Stays in clients (devin-telegram and future agents)

- TTS/STT, LLM call logic, conversation state, business rules for
  whom/when to call.
- The decision to dial/answer - the service never decides.
- Any per-call audio processing (echo handling, barge-in policy).
- `tg_*` notification/escalation surface - devin-telegram consumes
  `link_state_changed`/`incoming_call` events and decides what to
  tell the operator.

D-Bus remains internal plumbing toward PipeWire Telephony; the
agent-facing contract is the phonebridge API, not D-Bus.

## §2. Agent-facing API contract

### §2.1 Transport

One Unix-domain socket carrying:

- **Control plane**: JSON-RPC 2.0 (line-delimited), requests +
  server-push notifications (no client polling).
- **Data plane**: binary frames, 16 kHz 16-bit mono PCM (native mSBC
  rate), tagged per active call.

UDS chosen over a self-owned D-Bus interface (D-Bus is internal
plumbing) and over gRPC (single host, single trust boundary, thin
dependency graph). The same JSON-RPC + binary-frame contract maps
1:1 onto WSS for the A-REMOTE topology without a rewrite - this is
why the API does not leak local PipeWire node names.

### §2.2 Call control

```
acquire_line(ttl)        -> {lease_id}          # single-writer
renew_lease(lease_id)    -> {expires_at}        # heartbeat
release_line(lease_id)   -> ok
dial(number, timeout)    -> {call_id}           # requires lease
answer(call_id)          -> ok                  # v1.1, requires lease
hangup(call_id|"all")    -> ok
send_dtmf(call_id,tones) -> ok                  # IVR navigation, v1
clear_uplink_buffer()    -> ok                  # drop queued playback
flush_audio_out(call_id) -> ok                  # barge-in, see §2.3
get_calls()              -> [CallState]
get_capabilities()       -> {sms, codecs, incoming: "v1.1"}
```

Events (pushed): `call_state_changed{call_id,state,reason}`,
`link_state_changed{bt_connected|reconnecting|down}`,
`audio_ready{call_id}` (implicit audio attach when call reaches
`alerting` so early media/ringback is audible), `incoming_call`
(passive log event in v1, answerable in v1.1), `sms_received` (if
capable).

Mandatory for v1: `send_dtmf` - an agent cannot survive IVR trees
without it. Hold/swap/conference/PBAP contacts: deferred (zero value
for single-task agents).

Open validation spike (30 min, pre-implementation): does
`State=active` fire on remote answer or at cellular setup? If early,
the agent greets a ringback tone - needs a `remote_answered` signal
or early-media heuristic. `SendTones` is already listed in the
runbook's expected VoiceCallManager surface, so the spike only
verifies it *works* during a live call (tones audible to the callee).

### §2.3 Audio: raw PCM over the API (decided)

Two partner recommendations diverged:

- *PipeWire node names in the RPC response, clients `pw-play`/
  `pw-record` directly* (architect: simplest on one host, zero added
  latency, no proxy).
- *Bidirectional PCM stream over the API* (product: no PipeWire
  plumbing for clients, A-REMOTE-compatible, enables barge-in
  `flush_audio_out`).

**Decision: streaming over the API is the v1 contract**, with direct
PipeWire attachment documented as an optional local optimization for
latency-critical clients on the same host. Rationale:

- A-REMOTE is already a sanctioned topology (dialer-topology.md §2)
  and there the daemon "never proxies raw SCO - the node terminates
  it" and audio rides a binary channel; node-name exposure cannot
  survive that boundary.
- Agent clients should not each implement PipeWire subprocess
  management; a socket contract is uniformly implementable.
- Barge-in (`flush_audio_out` to stop TTS mid-utterance when the
  callee speaks) needs the bridge to own the uplink buffer.
- Latency budget: bridge must add < 100 ms node<->socket; verified
  by a loopback test in CI.

Implicit attach: when a call reaches `alerting`, the bridge starts
pumping downlink frames to the lease owner and accepts uplink
frames; `clear_uplink_buffer` truncates queued playback.

### §2.4 SMS - capability-flagged, SPIKE RISK flagged

SMS is in the API contract from day one (`send_sms`,
`sms_received`, `get_capabilities().sms`) but routed internally
through a `SmsBackend` interface:

- `OfonoMessageManagerBackend` - probed at startup; **untested in
  Phase-0** and structurally doubtful: Android serves SMS over the
  Bluetooth MAP profile rather than HFP AT commands on many OEM
  builds, and PipeWire's Telephony surface is narrower than full
  oFono (MessageManager may be absent). If probing fails ->
  `UnsupportedBackend`, `send_sms` returns `{status: unsupported}`
  cleanly and no client re-architecture is needed.
- A third backend (Android-side bridge via companion app/KDE
  Connect/ADB) can be added later behind the same contract.

Spike-0 for SMS: introspect the ag1 object for `org.ofono.MessageManager`
before writing any SMS code; if absent, MAP (`obexd`/BlueZ
`org.bluez.obex.MessageAccess1`) is the next candidate - both are
unverified on Mi 9.

## §3. Daemon model

- **systemd user service** `phonebridge.service`, with
  `loginctl enable-linger <user>` so it survives reboots without a
  login session. NOT a system unit: `org.pipewire.Telephony` lives on
  the USER session bus - a system-level service cannot reach it and
  fails silently with `ServiceUnknown`. This constraint is the #1
  redeployment hazard and is documented in the ops runbook.
- Lifecycle: start -> probe ag1 (absent = `link down`, keep running,
  watchdog re-arms) -> serve UDS. Restart policy `on-failure`; no
  auto-stop on phone absence.
- Single-writer applies twice: (a) lease arbitration between
  clients, (b) exactly one phonebridge instance per user session
  (socket path under `$XDG_RUNTIME_DIR/phonebridge.sock`, refuse to
  start if held).
- A-REMOTE: same binary runs as a user service on the SBC node; the
  UDS contract rides over WSS with the control/media channel split
  from `dialer-topology.md` §2 preserved (control JSON-RPC vs media
  frames on separate logical channels) - no new architecture.
- Observability: structured logs, `link_state_changed`/call events
  on the event stream, `phonebridgectl` CLI for manual dial/hangup/
  status (mirrors the busctl commands used in Phase-0).

## §4. Extraction plan

New repository **`phonebridge`** (capability name, not mechanism:
"dialer" undersells SMS; "bt-telephony" oversells the transport
which is an implementation detail).

```
phonebridge/
  phonebridge/
    dbus_client.py      # PipeWire Telephony/oFono wrapper
    call_state.py       # call-object -> CallState machine
    lease.py            # single-writer line lease, dead-man switch
    watchdog.py         # ag1 presence + reconnect events
    audio.py            # PipeWire nodes <-> PCM frame pump, flush
    backends/sms_ofono.py, sms_map.py, sms_unsupported.py
    rpc_server.py       # JSON-RPC control + binary PCM + events
    cli.py              # phonebridgectl
  systemd/phonebridge.service
  docs/
    pairing-runbook.md      # from docs/dialer-phase0-runbook.md
    phase0-report.md        # from docs/dialer-phase0-report.md
    topology.md             # from docs/dialer-topology.md
    api.md                  # this contract, formalized
  scripts/phase0_probe.sh   # from scripts/dialer_phase0_probe.sh
  tests/
```

Migration order:

1. Extract D-Bus client + call-state machine + watchdog into the new
   package; drive it standalone with busctl-equivalent CLI
   (re-prove Phase-0 outcomes).
2. Add the UDS JSON-RPC server with dial/hangup/lease + event push.
3. Wire devin-telegram as the first client behind a feature flag;
   remove in-daemon telephony code after soak.
4. systemd unit + linger + migrated runbook/probe.
5. `get_capabilities` + SMS backend probe (spike) + `send_sms`
   behind `unsupported` fallback.
6. v1.1: incoming-call answering; v2: multi-tenant isolation if a
   second trust domain ever appears (today: same Linux user, PipeWire
   node access is acceptable).

What does NOT migrate: `tg_escalate`, TTS/STT, sessions-bridge
mailboxes, the Telegram control plane - all stay in devin-telegram.

## §5. Test strategy without a real call

- **Fake-D-Bus harness** (primary): a test double implementing
  `org.pipewire.Telephony.*`/`org.ofono.*` on a private session bus
  (`dbus-daemon --session` in pytest fixtures) - scripted call
  objects, state transitions, `RejectSCO`, codec values. Mirrors the
  existing fake-SSH approach in devin-telegram tests. Covers: call
  FSM, lease arbitration, watchdog transitions, hangup-race handling.
- **ofono-phonesim**: optional deeper fidelity for the oFono dialect
  (call states, DTMF, MessageManager surface) - heavier setup,
  gate it as a manual/integration job, not per-commit CI.
- **Audio path**: generated sine/tone clips through PipeWire null
  sinks/sources instead of bluez nodes - the same deterministic-clip
  technique Phase-0 used (523/659/784 Hz partial detection) verifies
  the PCM pump end-to-end without RF.
- **Protocol conformance**: golden JSON-RPC transcripts; a reference
  client in tests replays a scripted call (dial -> alerting ->
  active -> DTMF -> hangup) against the fake bus.

## §6. Open questions for the developer

1. MVP line confirmation: outbound voice + DTMF + line lease +
   passive `incoming_call` event in v1; SMS capability-flagged with
   `unsupported` fallback; active incoming-call answering in v1.1.
   Approve or widen?
2. If the MessageManager spike fails, is an Android-side fallback
   (companion app / KDE Connect / ADB) in scope for v2, or is SMS
   out of product scope until the custom-dialer app (variant D)
   exists anyway?
3. Repo hosting: new repo `ychetka/phonebridge` under the same fleet
   conventions (gates, .changes, issue_N_vX.Y.Z branches) - confirm
   it joins the canonical rollout set from day one.
4. Multi-client trust: all agent clients run as the same Linux user
   today - acceptable for v1, or is any tenant isolation required
   before a second agent ships?
5. devin-telegram's existing streaming STT/TTS operates on WAV
   chunks today; the bridge streams PCM - a small client-side
   buffering adapter is assumed acceptable for v1 (batch behavior
   preserved), streaming-STT later. Confirm.
