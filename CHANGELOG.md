# Changelog

## [0.0.14] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### docs
- [docs] SA_carbon_review_changes_8 tails: design §5.4 documents ProviderBinding.orphan_deadline_at (set on WS drop, cleared on a state_snapshot-confirmed re-register, expiry in tick() ends the lease as provider_lost); api-contract provider.call_state lists recognized teardown reasons dongle_lost / stack_restart / provider_lost; design §9 details the container privilege boundary (vhci-feeder+tunnel only hold /dev/vhci + CAP_NET_ADMIN; pipewire/wireplumber/LineProvider/gateway run as unprivileged user phonebridge with shared XDG_RUNTIME_DIR and a session D-Bus + a policy granting UID access to org.bluez on the system bus) and the GetCalls() reconciliation on bus/name-owner restart. Why: Verbatim spec details that did not fit the round-20 diff: without them M1/M4 implementers would re-invent the orphan-grace deadline mechanics and the container UID/bus split. (SA_carbon_review_changes_8 tasks 8-9; issue #47)

## [0.0.13] - 2026-11-15

### User-facing

#### fix
- Contract hardening round 20: fail-closed RedactedString, transactional event/provider outbox on LineManager, ProviderBinding split from LineAssignment, acquire idempotency keys, grant-TTL expiry on the wire, new INVALID_* / RESOURCE_EXHAUSTED error codes, DTMF validation, media-UDP session nonce, richer SCO_STATE. `contract` **[breaking]** (SA_carbon_review_changes_8; dev session 2026-11-14)

### Codebase

#### contract
- [fix] SA_carbon_review_changes_8 applied: RedactedString is a non-str container (json.dumps/concat can no longer leak PII; pydantic accepts instance-or-str, JSON serializes to_wire()); LineManager gains drain_events()/drain_provider_commands() + a discriminated ProviderCommand union (dial/answer/hangup/send_dtmf/revoke); a bounded per-manager outbox replaces ad-hoc emission (rule 4); register_provider returns ProviderBinding (distinct assignment_id space); acquire gains idempotency_key dedup + INVALID_STATE/INVALID_ARGUMENT/RESOURCE_EXHAUSTED codes and GRANT_TTL_EXPIRED/SESSION_LIMIT_EXCEEDED/CLAIM_NOT_FOUND/INVALID_DTMF reasons; AcquireGranted.expires_at + CallPhase.ACQUIRED + CallLease.grant_expires_at + CALL_PHASE_WIRE_STATES; DTMF_PATTERN/MAX_DTMF_DIGITS; ensure_aware_utc() public helper; media-UDP header gains a session_nonce bound to HELLO_ACK; SCO_STATE gains air_mode/rx_pkt_len/tx_pkt_len; SCO_SESSION_MISMATCH telemetry; orphan grace resolved into a two-level failure model; D-Bus split + stack-restart reconciliation documented; phonenumbers added as a runtime dependency. Why: Three CRITICAL findings closed: RedactedString(str) leaked PII through C-level string paths; LineManager had no safe event/provider-command channel (network awaits inside mutations violate rule 4); register_provider conflated provider attachment with agent ownership. Plus seven HIGH/MEDIUM contract gaps ahead of M1. (SA_carbon_review_changes_8; dev session 2026-11-14)

## [0.0.12] - 2026-11-14

### User-facing

#### fix
- New agent event event.assignment_revoked closes the last domain↔wire event gap; EVENT_WIRE_NAMES mapping table + drift test freeze the parity. `contract` (SA_carbon_review7 follow-up; dev session 2026-11-14)

### Codebase

#### contract
- [fix] Pre-freeze parity pass: api-contract gains event.assignment_revoked {assignment_id, line_id, owner_agent_id?, reason?} (delivered to the revoked owner only); contract.py gains EVENT_WIRE_NAMES authoritative mapping (queue_ready→line_acquired deliberate rename documented); test_event_wire_name_parity asserts full EventName coverage. Why: The last known EventName↔wire drift: ASSIGNMENT_REVOKED had no wire event — an exclusive owner would only learn about a revoke via OWNER_MISMATCH on its next call.dial. Closed ahead of the c_gateway contract freeze (item_05). (SA_carbon_review7 follow-up; dev session 2026-11-14)

## [0.0.11] - 2026-11-14

### User-facing

#### fix
- Admin kernel surface (approve/reject/revoke/configure) split from agent API; queue fairness + incoming-ring preemption rules; hci-agent gains SCO_STATE USB altsetting control; headless pairing via admin.pair_window; MCP is control-plane only. `contract` **[breaking]** (SA_carbon_review7; dev session 2026-11-14)

### Codebase

#### contract
- [fix] Architect review round 19 (SA_carbon_review7 blind spots): LineAssignment.owner_agent_id nullable (None = pool/provider attachment); new LineAdminKernel ABC; global seq queue fairness + preempted_by_incoming grant race rule; deterministic D-Bus resolution chain adapter_mac->phone_mac->nodes; control kind 0x16 SCO_STATE for USB Interface 1 altsetting; in-container BlueZ Agent1 + admin.pair_window; MCP adapter boundary documented. Why: Six M1-M5 implementation gaps found by review: pool lines had no legal owner_agent_id value and no admin mutation path; queue fairness and ring-vs-grant races unspecified; multi-line D-Bus object mapping undefined; USB SCO altsetting duties undocumented; no headless pairing path; MCP media boundary undefined. (SA_carbon_review7; dev session 2026-11-14)

## [0.0.10] - 2026-11-13

### User-facing

#### fix
- Media socket clarified as WebSocket-over-UDS (message boundaries for flush/keepalive frames); hci-agent gains MEDIA_PING endpoint binding + WireGuard /32 peer isolation; RedactedString masking now covers set/frozenset containers. `contract` (SA_carbon_review6; dev session 2026-11-13)

### Codebase

#### contract
- [fix] Architect review round 18 (SA_carbon_review6): _mask_for_log handles set/frozenset; api-contract documents WS-over-UDS framing for the local media endpoint; hci-agent.md adds MEDIA_PING (kind=0x04) uplink endpoint binding + server-side /32 peer isolation; design §12 vhci question marked RESOLVED; api-contract header updated to rounds 1-18. Why: Raw SOCK_STREAM on the local media socket has no message boundaries — an 8-byte flush frame would desync the 648-byte parser; UDP is connectionless so the feeder cannot return uplink audio until it learns the agent source address; _mask_for_log missed set/frozenset (allowed_number_types is a frozenset). (SA_carbon_review6; dev session 2026-11-13)

## [0.0.9] - 2026-11-13

### User-facing

#### fix
- New domain event QueueUpdatedEvent (EventName.queue_updated); QueueReadyEvent now carries session_id + intent so adapters can emit event.line_acquired without a ticket lookup table. `contract` **[breaking]** (SA_argon_review5; dev session 2026-11-13)

### Codebase

#### contract
- [fix] Architect review round 17 (SA_argon_review5): EventName + QueueUpdatedEvent model for FIFO position advances; QueueReadyEvent gains session_id + intent (wire event.line_acquired mapping); tests now call ingest_audio_ready/drop_session, instantiate all round-15 event models, and assert the FIXED_LINE_OR_MOBILE allowlist default + fallback_call_token field; CHANGELOG backfilled for 0.0.7/0.0.8. Why: Queue position advances originate inside LineManager (cancel_acquire, drop_session, grant-TTL tick, line release) — without a typed domain event the M1 kernel cannot emit them. And the review flagged that tests never called the new round-15 methods or validated the new event models. (SA_argon_review5; dev session 2026-11-13)

## [0.0.8] - 2026-11-13

### User-facing

- `event.call_state_changed` now carries `answer_confidence`
  (`"provisional"|"confirmed"`) — the wire reflects how sure the
  bridge is that `active` means a human answered.

### Codebase

#### domain contract
- [contract] `CallStateChangedEvent.answer_confidence` field
  (additive). (issue #35)

#### docs
- [docs] §3.5 lists `provider.audio_ready`; §3.6 documents
  `event.fallback_triggered` + `answer_confidence`; §13 M4/M5
  rewritten for the hci-agent + vhci-feeder line-bundle topology.
  (issue #35)

## [0.0.7] - 2026-11-13

### User-facing

- `call.dial` requires `country` (ISO 3166-1 alpha-2) — breaking
  signature change on the domain kernel.

### Codebase

#### domain contract
- [contract] `LineManager.dial()` `country` param;
  `LineAssignment.allowed_number_types` (incl. mandatory
  `FIXED_LINE_OR_MOBILE` for NANP); `EventName` +
  `AudioReadyEvent`/`FallbackTriggeredEvent`/`LineStateChangedEvent`/
  `MissedCallEvent`; `FallbackDecision.fallback_call_token`;
  `LineManager.drop_session` + `ingest_audio_ready`;
  `RedactedString` explicit JSON serializer. (issue #33)

#### docs
- [docs] `MAX_CTRL_FRAME_LEN = 4096` in `docs/hci-agent.md` §2.
  (issue #33)

## [0.0.6] - 2026-11-13

### User-facing

- `provider.call_state` uses the canonical vocabulary
  `ringing|dialing|alerting|active|disconnected`; `call.acquire`
  serves `dial_out` only; new `provider.audio_ready` and
  `event.fallback_triggered` wire elements; `line.dial`/
  `line.answer` carry per-lease media credentials.

### Codebase

#### domain contract
- [contract] `ProviderCallState` canonical vocabulary (breaking);
  `ProviderCallStateUpdate.lease_id`. (issue #31)
- [fix] Architect review round 14 (issue #31): per-lease media
  credentials for the provider on `line.dial`/`line.answer`;
  `provider.audio_ready` feeds `event.audio_ready`; `call.acquire`
  limited to `dial_out`; MediaRouter pair TTL + bounded queues;
  `fallback_call_token` mechanics for FallbackWorker; `adapter_mac`
  added to `LineProviderConfig`; HTML diagrams clarified. Why:
  the second architect review found wire-level edge cases that
  would break in M1-M3 (media auth per lease, early-media gating,
  fallback worker acceptance after accept-window expiry, resource
  bounds). (issue #31; SA_argon_review2; dev session 2026-11-13)

#### docs
- [docs] Unified hci-agent model: `docs/hci-agent.md` agent spec +
  YAML config format, transport profiles, lossless `ctrl_seq`
  control channel, agent-side SCO isoc reassembly, feeder SCO handle
  gate, media health events — locked by the live vhci experiment.
  (issues #22, #29, #31)
- [docs] Round-12 architecture decisions: unified hci-agent model
  (kernel vhci fed by dumb USB-HCI relay agents over
  transport_profile tunnels: control-TCP + media-UDP), thin-slice
  per-line worker bundles, strict E.164+ISO dial validation with
  `DIAL_REJECTED` and per-line number-type allowlist, MCP adapter
  for the agent surface, live-probe results; new
  `docs/hci-agent.md` agent spec. Why: live probes (Answer/Reject/
  SendTones/WirePlumber echo loop) and the USB-passthrough
  blast-radius analysis invalidated the `--device` and
  remote-provider deployment sketches. (issue #22, #25; dev
  session 2026-11-13)
- [docs] hci-agent wire-protocol invariants proven by the
  one-dongle vhci end-to-end experiment: lossless sequenced control
  channel (`ctrl_seq`; gap/overflow = fatal session reset),
  agent-side USB isochronous reassembly into complete HCI SCO
  packets (`media_seq`, one packet per datagram), feeder SCO handle
  gate (open on `0x2c`/`0x03` status 0, close before `0x05`,
  <=4-frame unknown-handle hold), media health events
  `SCO_UNEXPECTED_HANDLE` / `SCO_RX_STARVATION` /
  `SCO_FRAMING_ERROR`. Why: the live experiment showed
  silent-audio failure modes (media frames outrunning
  sync-connection events; unaligned USB isoc buffers) that must be
  impossible in the production protocol. (issue #29; dev session
  2026-11-13)

## [0.0.5] - 2026-11-13

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [feat] Domain contract module (src/phonebridge/domain/contract.py): wire enums, identifier NewTypes, RedactedString, FallbackPolicy union, LineAssignment/CallLease/QueueTicket records, AcquireResult union, event + provider-ingress payloads, PhonebridgeError, and the abstract async LineManager kernel - types only, no implementation. Why: Roadmap item_03 (issue #7): freeze the typed domain surface ahead of the M1 in-memory LineManager so implementation, JSON-RPC adapters and tests all build against one contract. (issue #7, task item_03)

## [0.0.4] - 2026-11-13

### User-facing

_No user-visible changes._

### Codebase

#### docs
- [docs] Apply architecture-review (round 11) hardening: unified id taxonomy (lease_id only), agent media_endpoint/media_token on dial/accept, binary 20 ms media framing (ADR-02), FallbackWorker split (ADR-01), BDADDR + persistent volumes (ADR-03), WAN media reclassified to prod gate. Why: SA architecture review found contract gaps (agent media attach undefined, call.accept signature impossible, receive_inbound locking lines BUSY, unmasked PII fields). Developer approved all three ADRs with calibrated severity (WAN transport = prod gate for remote Pi, not v1 blocker). (SA_argon_review; devin-session-bridge PR #162 context)

#### tooling
- [chore] Tooling baseline: pydantic and websockets runtime dependencies, pytest-asyncio and mypy dev dependencies, strict mypy config with the pydantic plugin, asyncio test mode, and a mandatory mypy step in the lint workflow. Why: Roadmap item_02 (issue #6): the provider and agent milestones need pydantic models plus the new websockets asyncio API, and strict typechecking must gate every PR before typed code lands. (issue #6, task 332)

## [0.0.2] - 2026-11-12

### User-facing

_No user-visible changes._

### Codebase

#### docs
- [docs] Consolidated `docs/phone-bridge-service-design.md` with all
  accumulated design decisions (partner addendum rounds 1-3,
  REQUIREMENT UPDATEs 1-2, UNIFIED ARCH, refined intent model,
  decision rounds 5-10): unified LineProvider registration model,
  Manager + APICallService surfaces, intent-based LineAssignment vs
  CallLease, wake-and-grant inbound, always-packetized server-issued
  media, mandatory FIFO queue, single-container prod topology,
  universal event fan-out, SQLite+MCP admin, SMS dropped.
  (issue #1)
- [docs] Added `docs/api-contract.md` (wire-level JSON-RPC contract)
  and English versions of `docs/architecture-unified.html` and
  `docs/interaction-flow.html`. (issue #1)

#### tools
- [chore] Synced canonical `repo_merge.sh` (v0.15.5): the pre-merge
  `version_not_bumped` gate blocks shipping-path merges without a
  `[project].version` bump (ruling: every merged PR bumps the
  version unless exempt). (devin-session-bridge PR #162)

## [0.0.1] - 2026-11-12

### User-facing

_No user-visible changes._

### Codebase

#### tooling
- [chore] Initial canonical bootstrap: repository skeleton, .devin
  orchestration layer, CI workflows, and the founding design spec
  (docs/phone-bridge-service-design.md). (issue #0)
