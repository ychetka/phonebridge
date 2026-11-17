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
| round 12 (live probes + unified agent model, 2026-11-13) | Live-verified on the real stack: `VoiceCall.Answer` works (incoming→`active` ~0.3 s, SCO ~0.6 s), inbound `Hangup` rejects a ringing call, full E.164 terminates via `Dial` unchanged, `SendTones` is broken on this stack (fallback = in-band DTMF in uplink), WirePlumber auto-routes SCO to the default sink+monitor (echo loop; service must own routing via per-line virtual endpoints + static `node.autoconnect=false` on bluez5 HFP nodes). Architecture: UNIFIED AGENT MODEL — every BT dongle sits behind a dumb `hci-agent` (libusb, btusb detached) feeding a kernel `vhci` adapter inside the container over a pluggable `transport_profile` tunnel (control-TCP + media-UDP; QUIC is a V2 track); container is isolated-netns, sees only virtual adapters; thin-slice per-line worker bundles + shared bluetoothd/PipeWire; `call.dial` requires `number` (strict E.164) + `country` (ISO alpha-2) with phonenumbers cross-validation and a per-line number-type allowlist; agent surface reachable via MCP adapter. *(Deployment portion SUPERSEDED by round 25 — `AF_BLUETOOTH` is `init_net`-only, so the production profile is `--network=host` + `/dev/vhci`, not isolated netns.)* | §2.1, §2.2, §5.5, §6.4, §9, §10, §12, docs/hci-agent.md |
| round 13 (vhci experiment — protocol hardening, 2026-11-13) | One-dongle end-to-end experiment PASSED (issue #29): dongle → hci-agent → control-TCP/media-UDP → `/dev/vhci` → bluetoothd/oFono → live call with bidirectional SCO. Silent-audio incident diagnosed and fixed live — media frames outrun the sync-connection event across the split channels, and raw USB isoc buffers are not HCI-frame-aligned. Locked invariants: control channel is strictly lossless + `ctrl_seq` sequenced (gap/overflow = fatal session reset); agent reassembles complete HCI SCO packets at the isoc boundary (`media_seq`, one packet per datagram); feeder gates SCO on `0x2c`/`0x03` events, closes before `0x05`, caps unknown-handle holds at ≤4 frames; loud media health events `SCO_UNEXPECTED_HANDLE`/`SCO_RX_STARVATION`/`SCO_FRAMING_ERROR` | docs/hci-agent.md §2 |
| round 14 (architect review — edge cases, 2026-11-13) | Second review pass (issue #31): `line.dial`/`line.answer` now carry per-lease `{media_endpoint, media_token}` for the provider's `media.auth`; `call.acquire` restricted to `dial_out` only (inbound via `call.accept`, exclusive via `call.claim_exclusive`); `provider.call_state` uses the canonical state set `ringing\|dialing\|alerting\|active\|disconnected` + `lease_id?`; new `provider.audio_ready` notification feeds `event.audio_ready`; MediaRouter gains pair TTL (~5 s) and bounded frame queues; `fallback_call_token` mechanics defined for FallbackWorker on `accept_window_sec` expiry; `adapter_mac` added to `LineProviderConfig` | §2.2, §5.3, §5.5, docs/api-contract.md |
| round 15-16 (contract parity + doc tails, 2026-11-13) | contract.py aligned to api-contract (issue #33): `country` in `dial()`, `allowed_number_types` on LineAssignment (+mandatory `FIXED_LINE_OR_MOBILE` for NANP), full EventName vocabulary + event models, `fallback_call_token`, `drop_session`, `ingest_audio_ready`, explicit `RedactedString` JSON serializer, `MAX_CTRL_FRAME_LEN=4096`. Review 4 (SA_hidrogen): `answer_confidence` carried on `event.call_state_changed`; §3.5/§3.6/§13 updated for provider.audio_ready, event.fallback_triggered and the hci-agent/vhci line-bundle topology | §3.5, §3.6, §5.5, §13, docs/api-contract.md |
| round 17-18 (review tails 5-6, 2026-11-13) | `EventName.QUEUE_UPDATED` + `QueueUpdatedEvent`; `QueueReadyEvent` gains `session_id`+`intent` (wire `event.line_acquired` needs no adapter-side ticket table); full round-15 test coverage; `_mask_for_log` hardened for `set`/`frozenset`; local media endpoint specified as **WebSocket over AF_UNIX** (raw SOCK_STREAM has no message boundaries); `0x04 MEDIA_PING` uplink endpoint binding + WireGuard `/32` peer isolation on the container side | docs/api-contract.md, docs/hci-agent.md §2/§6 |
| round 19 (architect review 7 — blind spots, 2026-11-14) | `LineAssignment.owner_agent_id` nullable (`None` = pool/provider attachment, no magic strings); `LineAdminKernel` separate admin interface (`approve_claim`/`reject_claim`/`revoke_assignment`/`configure_line`) — agent surface has no privileged mutations; queue fairness = one global monotonic `seq` (lowest eligible seq wins, no scope starvation); incoming ring PREEMPTS an undialed grant (`preempted_by_incoming`, ticket reissued at head); deterministic D-Bus resolution chain `adapter_mac → Adapter1.Address → Device1.Adapter → phone_mac → bluez_input/output.<phone_mac>`; `0x16 SCO_STATE` feeder→agent USB altsetting control; in-container BlueZ `Agent1` + `admin.pair_window` for headless pairing; MCP adapter = control plane only (media via agent audio worker / MediaSidecar) | §2.1, §3.4, §4.2, §10, docs/api-contract.md, docs/hci-agent.md §2/§5 |
| round 19b (event-wire parity) | `event.assignment_revoked` wire event + authoritative `EVENT_WIRE_NAMES` map + bidirectional doc-drift test | contract.py, docs/api-contract.md |
| round 20 (hardening spec `SA_carbon_review_changes_8`, 2026-11-14) | `RedactedString` rewritten fail-CLOSED (non-str container — json.dumps/concat leak impossible); bounded transactional outbox on `LineManager` (`drain_events`/`drain_provider_commands`, typed `ProviderCommand` union, no network await inside mutations); `ProviderBinding` split from `LineAssignment` (two assignment_id object spaces); acquire `idempotency_key` dedup + `INVALID_STATE`/`INVALID_ARGUMENT`/`RESOURCE_EXHAUSTED` codes; `AcquireGranted.expires_at` + `CallPhase.ACQUIRED` + `CALL_PHASE_WIRE_STATES`; `DTMF_PATTERN` + provider-side tone synthesis muting agent uplink; `ensure_aware_utc` runtime helper (mandatory in `tick()`); media-UDP `session_nonce` binding + `SCO_STATE` air_mode/lengths; orphan-grace two-level failure model; D-Bus privilege split + stack-restart reconciliation; `phonenumbers` runtime dep | contract.py, docs/api-contract.md, docs/hci-agent.md, §4.2, §5.4, §6.4, §9 |
| round 21 (architect review 9 — `SA_carbon_review_9`, 2026-11-15) | ProviderCommand/MediaToken wire parity: `PROVIDER_COMMAND_WIRE_NAMES` maps every command to its `line.*` wire method + drift test; `line.dial`/`line.answer` carry `media_endpoint`/`media_token`, `mask_clir` removed; TWO independent per-role `media_token`s per `lease_id` (`MediaCredentials`, media.auth role-spoofing fix); `ProviderActiveCall`/`state_snapshot` + `LivenessState.WS_DISCONNECTED` close the orphan-grace path; `ProviderDisconnectReason`/`CallEndReason` closed reason vocabularies; deterministic external-call rule (human-initiated call → BUSY `external_call`, never Hangup); `ContractModel` frozen + tuple fields; `EventAudience` routing stamped on `EventEnvelope`; `register_intent` rejects empty intents/`exclusive_line`; `ClaimId`/`PendingClaim` + `LineAdminKernel` list reads; `Clock`/`MediaAllocator` injected ports; hci-agent SCO altsetting selection per BT Core Vol 4 Part B §2.1.1 (`SCO_PEER_MISMATCH`/`SCO_SESSION_MISMATCH` counters split) | contract.py, docs/api-contract.md, docs/hci-agent.md §2, §3.3, §5.2, §9 |
| round 22 (architect review 10 — `SA_carbon_review_10_gemini`, 2026-11-15) | `LineManager.update_provider_capabilities` + `ProviderCapabilityUpdate` (capability_update ingress, no token); `CallLease.session_id` ((session_id, lease_id) ownership, SessionAudience routing, sibling-session safety); `provider.call_state` gains `caller_id?` + `{ok, call_token}` on `ringing` (gateway-minted token echoed for early caller hangups; `IncomingRingIngress` no longer takes call_token as input); event schema/wire alignment (`caller`→`caller_id`, `expires_in_ms`→`expires_at` RFC 3339, `CallStateChangedEvent.state: WireCallState`, `ticket_id` on `event.line_acquired`, `AssignmentRevokedEvent.owner_agent_id` required — ProviderBinding revoke = `line.revoke` + `line_state_changed{DOWN, admin_revoked}`); ephemeral CallLease auto-release (hangup/disconnected end the lease; `call.release{lease_id}` mid-call = hangup-intent; `call.release{assignment_id}` = durable owner release); `receive_inbound`-scoped inbound events + accept gate (shared pool fans out per-session); `admin.assign_line` + `configure_line{idle_revoke_after, timeouts}` (`TimeoutConfig`); LineProvider owns in-band DTMF synthesis + uplink mute + `0x02` flush emission (MediaRouter drains its uplink buffer on receipt); `AnswerAndAnnounceFallback.announcement_ref`, `fallback_worker` acquire bypass, `event.fallback_triggered` as THE delivery; bounded `PendingClaim` (8/agent, 256 total, socket-bound) + bounded `ProviderRegistration.meta` | contract.py, docs/api-contract.md, §3.2-§3.6, §4.1, §4.2, §5.1-§5.4, §6.4, §10 |
| round 23 (architect review 12 — `SA_carbon_review_12`, Phase-0 contract freeze) | `line.hangup` XOR target `{lease_id? \| call_token?}` (reject-fallback executability); `ForwardViaLineFallback` gains `target_number`/`target_country` + config-time self-loop ban; `MediaAllocator` lifecycle fixed (allocate at dial/accept + idempotent `revoke(lease_id)` on lease ENDED); `_mask_for_log` serializes datetime/timedelta/Enum (json.dumps-safe logs); two-level outbox overflow model (SOFT_CAP pre-flight on acquire/dial/accept/claim, teardown never raises, drain after every call); Saga compensation on `line.dial`/`line.answer` (`{accepted}` ~2 s deadline → `disconnected`/`network_error`); `FallbackTriggeredEvent.expires_at` + `TimeoutConfig.fallback_accept_ttl_sec`; `event.idle_alert`/`IdleAlertEvent` + `AdminAudience` (idle escalation is alert-only, never a revoke); `reissued_ticket_id` on `call_state_changed`; bounded `idempotency_key` (1-128); deterministic `phone_mac → agN` chain + Call Waiting auto-reject; hci-agent token = long-lived per-line pre-shared secret + uplink playout jitter buffer; MediaRouter 20 ms uplink pacing + asymmetric rings (ADR-A); M1↔M2 storage split (SQLite durable config vs in-memory lease/queue state, admin write-through under `asyncio.Lock`); capability matrix (`CAP_NET_ADMIN`/`CAP_NET_RAW` only bluetoothd + wg-supervisor, vhci-feeder zero-caps on wg0 IP); pinned PipeWire node format 16 kHz/S16LE/mono; exec-hooks execve-only + webhook retry deadline `expires_at - 1s`; `profile` informational only (eligibility via `RECEIVE_INBOUND` in caps); `py.typed` + `docs/specs/` skeleton + doc drift fixes | contract.py, docs/api-contract.md, docs/hci-agent.md, §1, §3.5-§3.6, §4.2-§4.5, §5.2-§5.3, §6.3-§6.4, §7, §9-§10 |
| round 24 (architect review 13 — `SA_carbon_review_13`, final Phase-0 contract freeze v0.0.19, issue #59) | MediaRouter pacing direction CORRECTED (ADR-24.1): the agent-socket reader drains continuously at socket speed (`0x01` → 1500-frame ring, `0x02` → immediate ring drain), pacing applies on EGRESS — one 640 B frame per monotonic 20 ms tick toward the LineProvider; `LineTimeouts` effective-value model (ADR-24.2) split from the `TimeoutConfig` patch — `LineAssignment.timeouts` for SQLite write-through + `ProviderBinding.heartbeat_interval_ms` stamps the negotiated per-session cadence; `heartbeat_down_after > heartbeat_degraded_after` validated on both models; `validate_e164_country` + `ForwardViaLineFallback` model validator + `LineAssignment` self-loop ban move dial-target validation from `configure_line` into the constructors; provider `caps` are non-empty telecom-only (`dial_out`/`receive_inbound` — `exclusive_line` is a `LineAssignment.claimable`/`mode` property, never a cap); docstrings freeze the `ingest_ring` glare matrix (BUSY/DOWN → `INVALID_STATE`, provider Hangup), `tick(now)` as the sole timer driver (`decide_fallback` before `expires_at` → `INVALID_STATE`; `fallback_accept_ttl_sec` expiry → `RINGING_LOCKED -> FREE` + `MissedCallEvent{ACCEPT_WINDOW_EXPIRED}`), error mapping (`CLAIM_NOT_FOUND` under `INVALID_STATE`, unknown `assignment_id` → `STALE_TOKEN`, `call.release` XOR breach → `INVALID_ARGUMENT`) and `AssignmentId` rotation on owner/mode change with bounded `_closed_assignments`; hci-agent `0x16 SCO_STATE` gains `handle?: uint16` (required on open, ADR-24.3) + continuous 2-bit H2 counter rewritten on ALL outbound mSBC packets including silence | contract.py, docs/api-contract.md, docs/hci-agent.md, §3.3, §3.6, §6.3, §6.6, §9 |
| round 25 (`SA_carbon_review_15` — pre-M1 contract consolidation) | Deployment: `--network=host` + `/dev/vhci` on a dedicated host ratified as THE production profile (`AF_BLUETOOTH` is `init_net`-only; isolated netns impossible), micro-VM documented as optional hardened profile; `phonebridge doctor` pre-flight; UDS `0660`/`0700` state dirs. Contract: `LineState`/`LivenessState` split (`event.line_health_changed`, RESERVED/DEGRADED/DOWN removed from occupancy); `event.claim_resolved`/`event.assignment_granted`/`event.queue_cancelled` added (`assignment_id` finally reaches the owner); agent/provider media endpoint split (`MediaCredentials`); `line.reattach_media`/`call.reattach_media` token reissue; ring correlates by line not token (fallback token + snapshot reconcile + 1:1 ambiguity rule + provider `/run` state file); queue `seq` minted at acquire ingress; provider `call_state` gains `observed_at`/`call_token`/`caller_id`/`reason` echo + provider disconnect-reason closed vocab; `call.release` XOR `{lease_id\|assignment_id}` + `STALE_TOKEN`; `system:*` identities rejected on external sockets; `clear_idle_revoke_after`; forwarding cycle bound 8 hops; `max_call_duration_sec`/`ring_drain_ttl_sec` timeouts; media pair TTL 30 s + `network_error` teardown on timeout; DTMF `0x02` timestamped watermark flush + ALERTING early-media gate; `call.get_lines` privacy filter + redacted broadcast `line_state_changed` reasons for exclusive lines; JSON-RPC numeric error codes + `error_code`/`ErrorData` + `UNAUTHORIZED`/`FORBIDDEN`/`SESSION_NOT_FOUND`; WS-over-UDS framing 64 KiB | contract.py, docs/api-contract.md, docs/hci-agent.md, §2.1, §4.5, §5.2-§5.4, §6.3-§6.4, §7, §9-§10, §13 |
| round 30 (`SA_carbon_review_19` slice B2a — design-doc hardening) | Webhook HTTP client never follows `3xx` (redirect = delivery failure under the retry budget); `/var/lib/phonebridge-media` third volume + media quotas (`max_recording_sec`, per-line cap, delete-after-send, retention TTL, ENOSPC isolation from SQLite WAL); `restore_line` kernel compensation + single-transaction `lines`+`line_assignments` writes + hydration as last-resort reconciler; single mutation lock covers kernel call→drain→`bus.publish` on every adapter path (per-mutation FIFO); `forward_via_line` second-leg FSM (`enqueue` on busy, terminal on unreachable/`DIAL_REJECTED`, ~20 s budget, either-leg teardown); `answer_and_announce` decision-time eligibility (EXCLUSIVE or live `receive_inbound`, degrade `no_pickup`), WAV-only ref validated at `configure_line`, hangup only after MediaRouter drain + jitter budget; `record_and_telegram` bot token in SQLite secrets injected by supervisor, mandatory beep before record, dead-letter spool + restart resume; `/run/phonebridge/<line_id>.json` bound to call-instance id + deleted on teardown + stale-age rejection; sibling-liveness check precedes D-Bus-loss `call_state` reports (`dongle_lost` ordering, supervisor flush window); phone numbers PII-masked incl. exec-hook stdio, `DIAL_REJECTED.detail`, `phonebridgectl` (default masked, `--reveal`); `remove_line` → `INVALID_STATE` while referenced by another line's `forward_via_line`; exec-hook coalescing keyed per `(hook, line_id)` keeps latest payload (cross-line payloads never merge); `vhci.lock` on a host-shared path (bind-mount or host-side lock service) | §2.1, §4.5, §5.3-§5.4, §7, §10 |
| round 31 (`SA_swe2_review_1` — 40/40 accepted, design batch 3) | `provider_bindings` becomes a persisted table (`channel_seq`, `last_seen_at`, heartbeat stamps) — `hydrate_lines` loads bindings DEAD (`ws_disconnected` + expired `orphan_deadline_at`) and the first `tick` runs the full orphan cascade unless a fresh `provider.register` supersedes; orphan cascade spelled atomically (UNREACHABLE → lease `network_error` teardowns → ticket/claim `provider_lost` cancels → ring/`external_call` reset → binding tombstone; heartbeat-down suspended while `ws_disconnected`); uplink overflow single policy — socket ALWAYS read, `0x02`/`0x03` processed normally, drop-NEWEST PCM only, `0x04` + `event.media_overflow` once per contiguous episode; mutation lock NARROWED to kernel mutation + drains (in-memory) — SQLite write-through and `bus.publish` ride a single ordered writer task preserving per-mutation FIFO, `restore_line` compensation re-enters the lock deadlock-free; `compensate_dial(line_id, assignment_id, lease_id)` binding-scoped saga entry (hangup-first `network_error`, no fabricated provider facts); `CallPhase.ANSWERING` internal accept phase; provider-stamp watermark — ping facts below the watermark are applied CLAMPED, state facts are deduped; `provider_call_id` exact ring identity + `RING_DEDUP_MIN_GAP_SEC` best-effort fallback; `record_and_telegram` gains mandatory `announcement_ref` + `max_recording_sec` policy fields; stale-surface refresh (four listeners, `LineSummary.assignment_id`, full method/event inventories, `remove_line` quiescent-only) | §2.2-§3.6, §4.2, §4.5, §5.2-§5.4, §6.3-§6.4, §6.6, §7, §9-§10 |
| round 32 (`SA_swe2_review_2` — design batch 3) | `hangup_hold_sec` (default 10 s, bound 1–120) + `CallPhase.TERMINATING` — `call.hangup` parks an in-flight lease until provider `disconnected` or a kernel-side `network_error` lapse, only `ACQUIRED` grants end directly; pending-write marker + bounded admin wait + guarded `restore_line` (re-mutated record degrades to `config_stale` + admin alert, never a blind old-snapshot overwrite); `provider_bindings` = best-effort cache (failed write = `config_stale`, NO kernel compensation; `restore_line` is `LineAssignment`-only; `last_seen_at` debounced `>= 30 s`; stamp watermarks persisted; `channel_seq` reseeds `max(stored)+1`; hydrated-dead cascade suppresses external escalation hooks); orphan-cascade reason unified `provider_lost` + NO commands to dead/tombstoned bindings; DTMF `0x05` = egress-pacer PAUSE (frames buffer, flush keeps post-DTMF speech; `pre_dtmf_consumed`/`none` semantics); SCO MTU — fixed `hci_vhci.sco_mtu`, feeder answers the kernel's first `HCI_Read_Buffer_Size` locally (60 mSBC/48 CVSD), real `0x1005` proxied for learning only; `compensate_answer` ring-scoped reject vs `compensate_dial` lease-scoped hangup-first; `ingest_media_overflow` sole `event.media_overflow` source + `audio_lost` never arms pair TTL; external-call attribution = evidence-agreement rule; queue — terminal `grant_ttl_expired`, `seq` ordering, `DIAL_OUT`-only tickets, full re-check trigger set; idempotency TTL 3600 s + LRU 256 + `IDEMPOTENCY_CONFLICT`; `register_intent` identity-triple matching; `grant_expires_at` ACQUIRED-only; four hardware-identity fields on `lines` (`adapter_mac`, `paired_phone_mac`, `transport_profile` `loopback\|direct_mtls\|wireguard`, `line_home_country` display-region only) | §3.3-§3.5, §4.2, §4.5, §5.1-§5.4, §6.3-§6.4, §6.6, §9-§10 |

## §1. Service boundary

### Inside `phonebridge` (owns the telephony plane)

- Multi-line object resolution (round 19; agN leg completed round
  23, review-12 H5): with N virtual adapters
  the D-Bus graph holds N AG objects (`ag1..agN`) whose ordinals
  follow reconnect order - LineProvider resolves its own line
  deterministically, never by `hciN`/`agN` indices:
  `config.adapter_mac → org.bluez.Adapter1.Address (→ /org/bluez/hciX)
  → paired org.bluez.Device1` filtered by `Adapter == hciX AND
  Connected == true` (the Connected filter excludes stale pairing
  records left under `/var/lib/bluetooth/<adapter_mac>` from a
  previously paired phone; an optional `paired_phone_mac` pin in the
  line config narrows the match to exactly one device) `(→
  phone_mac) → the /org/pipewire/Telephony/agN` object whose
  `org.pipewire.Telephony` `AudioGateway` device/address property
  equals that `phone_mac` → PipeWire
  nodes bluez_input.<phone_mac> / bluez_output.<phone_mac>`.
- Call Waiting policy (round 23, review-12 H5): if the carrier
  delivers a second call during an active call (`+CCWA` → a
  `waiting`/`incoming` oFono call object on the line), the
  LineProvider MUST auto-`Hangup` that secondary call object and
  emit a loud telemetry counter (`call_waiting_rejected`) — it MUST
  NEVER report `ringing` to the gateway over an active lease: a
  `RINGING_LOCKED` transition on a `BUSY` line would corrupt the
  live CallLease. The policy is unconditional in v1; exposing
  call-waiting to agents would be a future contract item.
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
  degraded -> down on the LIVENESS axis (reachable / degraded /
  ws_disconnected / unreachable — `degraded` is the kernel-computed
  middle rung of the heartbeat ladder `reachable -> degraded ->
  unreachable`, never a provider-reported fact, round 28), surfaced as
  `event.line_health_changed` (round 25 — call occupancy
  `LineState` and provider health were split into orthogonal axes;
  a missed ping can never overwrite a `BUSY` call fact).
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

### §2.1 Deployment artifact: ONE container image, host networking (prod, round 25)

Production deployment is a **single Docker container**
(`phonebridge-gateway`) that contains the full telephony stack:

- the gateway daemon (Manager + APICallService + LineManager +
  EventBus - §2.3),
- one **per-line worker bundle** per line (thin-slice model, round
  12): a `vhci-feeder` + shim/audio-forwarder + `LineProvider`
  process, supervised independently so a dead line never takes the
  gateway or other lines down; BlueZ, PipeWire and dbus-daemon are
  shared, hardened with watchdogs and fast restart — **sibling
  liveness (round 29, review-1 B9):** the bundle supervisor watches
  BOTH the `vhci-feeder` and the `LineProvider` children, and the
  feeder↔provider bundle-local IPC is itself supervised — feeder
  death, or loss of that IPC channel, is reported by the
  LineProvider as `dongle_lost` (immediate teardown, NO orphan
  grace — the same consequence class as tunnel/USB death), so a
  dead feeder can never leave its provider happily pinging
  `reachable` while the control tunnel is already gone.
  **Sibling-liveness precedes D-Bus facts (round 30, review-19
  B2a#11):** when a `vhci-feeder` dies its `/dev/vhci` fd closes
  and the kernel destroys `hciN` INSTANTLY — `Adapter1`/
  `Device1`/`VoiceCall` removal signals can reach the LineProvider
  BEFORE the feeder↔provider IPC EOF does, tempting it to report
  an ordinary `disconnected`/`stack_restart` instead of
  `dongle_lost`. The ordering rule is therefore explicit: on ANY
  D-Bus object loss the LineProvider MUST check sibling liveness
  (feeder IPC health) FIRST and classify the cascade as
  `dongle_lost` when the feeder is gone — a D-Bus removal storm is
  evidence of feeder death, not an independent call fact. The
  bundle supervisor applies the symmetric rule: on feeder death it
  gives the LineProvider a bounded flush window (~1 s) to emit its
  `dongle_lost` report over the control socket BEFORE restarting
  or killing the sibling — a supervisor that reaps the
  LineProvider first turns an immediate `dongle_lost` into a
  gateway-side `WS_DISCONNECTED` and wrongly arms the 10 s orphan
  grace on calls whose media path is already dead,
- BlueZ + PipeWire + dbus-daemon inside the container,
- **no USB dongles passed through** (round 12, replaces the earlier
  `--device` model): every adapter inside the container is a kernel
  `vhci` device fed by an `hci-agent` on the dongle's machine — see
  §2.2 and `docs/hci-agent.md`.

**Kernel constraint (round 25, verified live — replaces the earlier
"isolated network namespace" wording, which is physically impossible):**
the Linux Bluetooth subsystem rejects `AF_BLUETOOTH` sockets
(`BTPROTO_HCI`/`BTPROTO_SCO`/`BTPROTO_RFCOMM`) outside the root
network namespace `init_net` (`bt_sock_create` → `EAFNOSUPPORT`), and
`/dev/vhci` registers `hciN` adapters globally in `init_net`. The
container therefore runs with **`--network=host`** and an explicit
`/dev/vhci` device pass-through — the image is packaging for the
fragile `BlueZ`/`oFono`/`PipeWire` ABI, not a network boundary.

**`/dev/vhci` attach serialization (round 29, review-1 B4):**
`/dev/vhci` is a single device node that mints `hciN` indices
globally in `init_net`, so an `open("/dev/vhci")` + hci-index
discovery performed concurrently by two per-line `vhci-feeder`
processes is a race — two feeders starting (or restarting)
together can cross their line↔hciN mapping before either reads
its assigned index back. Every `vhci-feeder` MUST therefore hold a
per-host exclusive lock — a `flock` on a lock file such as
`/run/phonebridge/vhci.lock` — across the ENTIRE attach sequence:
`open("/dev/vhci")` through index discovery. Parallel line-bundle
starts/restarts serialize on the file, so the mapping is
deterministic no matter how many bundles the supervisor launches
at once. **The lock path MUST be host-shared (round 30,
review-19 B2a#4):** `/dev/vhci` is global to the host kernel
(`init_net`), but a container-private `/run` is mount-namespaced
— a `flock` taken inside the container can never serialize
against a still-dying previous container instance, a second
gateway container, or any host-side `/dev/vhci` consumer
(a host-native `hci-agent` debugging session, `btattach`). The
deployment therefore MUST do one of:

- bind-mount a host-visible state dir (e.g. host
  `/run/phonebridge/` or `/var/lib/phonebridge-run/`) into the
  container at `/run/phonebridge/` so the lock file is ONE
  inode shared by every consumer on the host — the default
  deployment recipe; the mount is mandatory, `phonebridge doctor`
  fails fast when the path is not on a host-shared mount — or
- take the lock from a host-side lock service (a tiny host helper
  holding the flock and vending an attach slot over a UDS
  handshake), so no container process touches the lock file at
  all.

A container-private `/run/phonebridge/vhci.lock` is a DEPLOYMENT
BUG, not a fallback: it silently degrades the guarantee to
"serialized within one container instance", which is exactly the
restart-window race the lock exists to close. (hci-agent.md
specifies the same logical lock path for agent-side tooling — the
host-visibility requirement above applies wherever the file is
taken.)

Canonical deployment profile — **dedicated telephony host/VPS**
(recommended, ratified round 25):

- the host runs NO other Bluetooth stack: host `bluetoothd` is
  disabled (`systemctl mask bluetooth.service`) — two `bluetoothd`
  instances in one `init_net` race for `HCI_CHANNEL_CONTROL`;
- physical dongles plugged into that same host are served by a
  host-side `hci-agent` per dongle over libusb with `btusb`
  detached, exactly like a remote Pi's agent;
- network perimeter is enforced outside the container: listeners
  bind to `127.0.0.1` (local control/media UDS) and/or the `wg0`
  interface IP (remote peers), plus host firewall rules — the
  container shares the host loopback, so local access control lives
  on the filesystem: the agent socket directory is bind-mounted
  `0660` under a dedicated `phonebridge` group;
- state directories (`/var/lib/bluetooth`, `/var/lib/phonebridge`)
  are `0700`;
- a `phonebridge doctor` pre-flight check (entrypoint) verifies
  `/dev/vhci` presence, `hci_vhci` module state, and the ABSENCE of
  a live host `bluetoothd`, and fails fast with remediation text —
  never a cryptic `EAFNOSUPPORT` mid-startup.

Optional hardened profile — **shared workstation / multi-tenant**
(round 25, deferred past MVP): the same OCI image inside a
lightweight guest micro-VM (Kata/QEMU/cloud-hypervisor) whose guest
kernel owns its own `init_net` and `/dev/vhci`, leaving the host's
native Bluetooth untouched. Requires `/dev/kvm` and a guest kernel
with `CONFIG_BT`+`CONFIG_BT_VHCI` — gated on an audio-jitter spike
(real-time `PipeWire` timing inside a micro-VM is unproven) and on
actual demand; NOT an MVP deliverable.

Persistence requirement (ADR-03, round 11; third volume added round
30): "wipe-safe" is only true with these MANDATORY persistent
volumes:

- `/var/lib/bluetooth` - BlueZ link keys; without it recreating the
  container drops phone pairing and demands manual re-pairing.
- `/var/lib/phonebridge` - SQLite (keys, LineAssignments, configs).
- `/var/lib/phonebridge-media` - recorded call audio and the
  `record_and_telegram` dead-letter spool — a THIRD mandatory
  volume, on a SEPARATE filesystem/mount from the SQLite volume
  (round 30, review-19 B2a#3): recordings grow unbounded relative
  to config and must never share an `ENOSPC` fate with the WAL —
  a full media volume fails recording, never an admin mutation;
  see §10 "Media file quotas and lifecycle".

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
  adapter_mac: string;        // BDADDR binding (ADR-03) — identity, never hciN
  token: string;              // per-line, provisioned out-of-band via admin
  caps: Intent[];             // which intents the line can serve
  profile?: string;           // config preset, see §4.4
  reconnect: { backoff_ms: [number, number]; jitter_pct: number };
  heartbeat_interval_ms: number;
  media_transport: "ws_pcm";  // ALWAYS packetized: framed PCM over WebSocket; LocalLine targets 127.0.0.1
}
```

- **Provider-initiated registration** (`provider.register`) makes the
  topology NAT-safe: the provider always dials INTO the gateway;
  `line.*` commands ride the provider's own inbound connection as
  server-initiated JSON-RPC - no reverse dial ever happens.
- **Reconnect = re-register** (`provider.reregister`, idempotent;
  preserves lease state during the grace window).
- Liveness: `provider.ping`; missed pings escalate the binding's
  `LivenessState` `reachable` -> `degraded` -> `unreachable` via the
  `heartbeat_degraded_after`/`heartbeat_down_after` ladder and
  surface as `event.line_health_changed` — elapsed silence is
  measured on kernel receipt time (`now - binding.last_seen_at`),
  the comparison is inclusive (`>=`), and `degraded` is a
  kernel-computed rung, never a provider-reported fact (round 28;
  interval and rungs
  admin-configurable; `DEGRADED`/`DOWN` are no longer `LineState`
  members since round 25).
- **Provider-side stamps are advisory dedup keys, never clocks
  (rounds 28/31):** `provider.ping`'s `checked_at` and
  `provider.call_state`'s `observed_at` travel on provider-clock
  time and are used ONLY as dedup/ordering keys — all deadline
  arithmetic (orphan grace, heartbeat ladder) runs on kernel
  receipt time. A stamp deeper than `MAX_CLOCK_SKEW_SEC` (30 s)
  into the FUTURE is normalized to kernel `now` at ingestion so an
  NTP-skewed peer can neither arm a permanent dedup barrier nor
  suspend the ladder; a PAST stamp is retained verbatim as the
  dedup key. The dedup watermark is MONOTONE-MAX per line and
  survives a reregister. **Clamp-vs-drop split (round 31,
  SA_swe2_review_1 item 19 — the NTP-rewind rule):** a
  sub-watermark PING still lands as a live-channel heartbeat —
  it is APPLIED with its stamp clamped to the recorded watermark
  (`last_seen_at` refreshes to kernel `now`, the stored ordering
  key never rewinds) — while a sub-watermark STATE-CARRYING fact
  (`provider.call_state`, a `state_snapshot` element) is DROPPED
  as a duplicate and never applied backwards. Facts naming a
  superseded `assignment_id` never reach the watermark at all —
  they resolve `STALE_TOKEN` through the closed-assignment ring
  first.
- Two distinct failure levels (round 20 — resolves the §5.4
  contradiction): (a) provider **WS drop** while the underlying
  telephony path survives -> in-flight calls stay up at the provider,
  orphaned agent leases get a **10 s grace** window to re-bind
  through the re-registered provider; (b) provider-reported tunnel/
  USB death (`provider.call_state{state:"disconnected",
  reason:"dongle_lost"}`) or a provider that stays unreachable past
  the heartbeat `down_after` threshold on the liveness axis ->
  in-flight calls are treated as hangup immediately, no grace.

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

### §2.3 Four surfaces, one daemon

The gateway daemon exposes four listeners over one shared in-process
LineManager kernel (the admin and internal surfaces were always
implicit; round 31 pins the full inventory):

| Surface | Listener (example) | Peer | Namespace |
|---|---|---|---|
| **Manager** | `wss://gw:7001/provider` | Line providers (LocalLine, RemoteLine) | `provider.*` (up), `line.*` (down) |
| **APICallService** | `wss://gw:7002/agent` | AI agents | `agent.*`, `call.*` (up), `event.*` (down) |
| **AdminService** | `unix:///run/phonebridge/admin.sock` (mode 0600) or `wss://gw:7004/admin` | Admin clients (`phonebridgectl`, admin MCP) | `admin.*` |
| **InternalService** | `unix:///run/phonebridge/internal.sock` (mode 0600, never bind-mounted or network-exposed) | In-container service identities (`system:*`, e.g. the FallbackWorker) | `agent.*`, `call.*` (up), `event.*` (down) |

The internal listener is the ONLY socket family where a `system:*`
identity may authenticate — its peer credential (`SO_PEERCRED`)
attests the local service process; every externally issued key is
forbidden from carrying a `system:*` subject, so an agent socket can
never mint a worker identity.

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
// Occupancy axis ONLY (round 25 — RESERVED had no transitions;
// DEGRADED/DOWN moved to the liveness axis):
type LineState     = "FREE"|"BUSY"|"RINGING_LOCKED";
type LivenessState = "reachable"|"degraded"|"ws_disconnected"|"unreachable"|"unbound";
// "degraded" is the kernel-computed middle rung of the heartbeat
// ladder (round 28) — never a provider-reported fact.
// "unbound" is a LineSummary listing value for provider-less lines —
// never an event.line_health_changed endpoint (round 26).
type LineSummary = { line_id: string, caps: Intent[], state: LineState,
                     liveness: LivenessState, profile?: string,
                     assignment_id?: string };
// assignment_id is present ONLY on an EXCLUSIVE row owned by the
// calling agent (round 27) — the durable fallback to
// event.assignment_granted for call.release{assignment_id}.
```

Error codes (contract-level; each maps to a numeric JSON-RPC
`error.code` via `ERROR_CODE_WIRE`, the string travels as
`error.message`, and typed detail rides `error.data`
`{reason?, detail?, free_lines?}` — round 25): `UNAUTHORIZED`,
`FORBIDDEN`, `SESSION_NOT_FOUND`,
`OWNER_MISMATCH`, `LINE_EXCLUSIVE_NOT_OWNER`, `LINE_BUSY_INCOMING`,
`LINE_NOT_FOUND`, `INVALID_INTENT`, `STALE_TOKEN`,
`PROVIDER_UNREACHABLE`, `DIAL_REJECTED` (typed `data.reason` - see
Dial validation in `docs/api-contract.md`), `INVALID_STATE`,
`INVALID_ARGUMENT`, `RESOURCE_EXHAUSTED`. Sub-code strings such as
`INVALID_DTMF` or `SESSION_LIMIT_EXCEEDED` are `ErrorReason`
values inside `error.data.reason` of their parent code — never
top-level `error.code`/`error.message` members.

`NO_FREE_LINE` was REMOVED as an error (round 9): a busy pool is a
normal result, not an exception.

### §3.3 Agent namespace (`agent.*` / `call.*`)

| method | semantics |
|---|---|
| `agent.register_intent` | `{agent_id, token, intents[], profile?}` -> `{session_id}`; declarative wish resolved by the intent-resolver into a LineAssignment (§4) |
| `call.acquire` | `{session_id, intent, line_id?, profile?, enqueue?, idempotency_key?}` -> `{granted:true, line_id, lease_id, expires_at}` or `{granted:false, queued:true, ticket_id, position}` or `{granted:false, free_lines[]}`; `free_lines:[]` = nothing available - routine result, the client retries itself. `expires_at` is the grant TTL deadline (round 20). Optional `enqueue` joins the FIFO queue (§3.4); a non-empty `(method, session_id, idempotency_key)` triple scopes retry dedup — the keyspace is partitioned by METHOD (round 31), so the same key under `call.acquire`/`call.dial`/`call.accept`/`call.claim_exclusive` names independent operations; `idempotency_key` bounded 1-128 chars, round 23) |
| `call.release` | `{session_id, lease_id?, assignment_id?}` - exactly ONE target (round 22): `lease_id` ends the ephemeral lease (undialed grant frees the line; in-flight call is hung up first); `assignment_id` is the durable-binding path - the exclusive owner voluntarily releases its LineAssignment. `OWNER_MISMATCH` on a foreign target; zero or BOTH targets = `INVALID_ARGUMENT` (round 24) |
| `call.dial` | `{session_id, lease_id, number, country, idempotency_key?}` -> `{lease_id, media_endpoint, media_token}`; strict E.164 + ISO alpha-2 cross-check BEFORE the line (round 12) - `DIAL_REJECTED` with a typed `data.reason`, otherwise the validated E.164 passes to `line.dial` unchanged. A same-key retry returns the STORED result — including media credentials whose one-shot `media_token` may already be consumed; fresh credentials come only from `call.reattach_media` (round 31) |
| `call.accept` | `{session_id, call_token, idempotency_key?}` - atomic: issues the lease, answers the ring, attaches media -> `{lease_id, line_id, media_endpoint, media_token}`; `STALE_TOKEN` if the accept window expired or the token was already consumed, `OWNER_MISMATCH` as applicable (round 21). The minted lease starts in the internal `answering` phase (round 31 — `line.answer` in flight, no wire spelling) and reconciles to `active`/`disconnected` from provider facts; the same stored-result/consumed-token idempotency rule as `call.dial` applies |
| `call.claim_exclusive` | `{session_id, line_id, idempotency_key?}` -> `{claim_id, status:"pending_approval"}` — durable exclusivity by CLAIM + moderator approval in admin (never self-granted, §4.2); resolution arrives asynchronously as `event.claim_resolved` (+`event.assignment_granted` on approval) |
| `call.cancel_acquire` | `{session_id, ticket_id}` -> `{ok}` — withdraw a queued ticket; idempotent no-op for an id that is not a LIVE ticket of the calling session (never-queued, granted, cancelled or expired ids all resolve `{ok}`), `OWNER_MISMATCH` only for a LIVE foreign ticket (round 31) |
| `call.reattach_media` | `{session_id, lease_id}` -> `{lease_id, media_endpoint, media_token}` — fresh AGENT media credentials for a live lease after its media socket dropped while the control channel lived (round 25); the new pair supersedes and invalidates the prior agent token |
| `call.hangup` / `call.send_dtmf` / `call.get_lines` | call control and line listing |

`call.hangup` on an in-flight lease returns IMMEDIATELY after the
`line.hangup` command is queued (round 32 — the `terminating` leg
is real: the lease parks until the provider's `disconnected` or
the `hangup_hold_sec` lapse ends it `network_error` kernel-side;
an `ACQUIRED` grant ends directly, no command queued), and a
second `call.hangup`/`call.release{lease_id}` while parked is an
idempotent no-op — the reply names no new state.

**Idempotency cache bounds (round 32, SA_swe2_review_2 item 17):**
every `(method, session_id, idempotency_key)` dedup entry lives at
most `IDEMPOTENCY_TTL_SEC` (3600 s) and per-session storage is a
bounded LRU of at most `MAX_IDEMPOTENCY_KEYS_PER_SESSION` (256) — oldest
entries evict first, so a long-lived chatty session can never grow
the cache unboundedly; the whole cache is session-scoped and dies
with `drop_session`. A replay carrying the same key but DIFFERENT
parameters is `IDEMPOTENCY_CONFLICT` — never a silent replay of
the stored result — while an identical retry returns the stored
result verbatim (including already-consumed one-shot credentials,
per `call.dial`/`call.accept` above).

**`agent.register_intent` identity matching (round 32):** a NEW
transport socket always mints a NEW `session_id`; a retry on the
SAME socket returns the existing session only when the
`(agent_id, intents, profile)` triple matches EXACTLY — any
difference is rejected `INVALID_ARGUMENT` with the
`IDEMPOTENCY_CONFLICT` reason (re-declaring intents on a live
socket is not a negotiation, it is a bug). The registration dedup
state is session-scoped and dies on `drop_session`.

DTMF is mandatory from v1 - an agent cannot survive IVR trees
without it. Hold/swap/conference/PBAP contacts: deferred (zero value
for single-task agents).

### §3.4 Call queue (required) and what was removed

- **FIFO call queue is mandatory.** A queued acquire returns
  `{granted:false, queued:true, ticket_id, position}`; the ticket
  advances via `event.queue_updated`; on grant the agent receives
  `event.line_acquired{lease_id}` and must react within a short
  admin-configurable grant TTL (~5 s illustrative). The minted
  lease records the deadline as `grant_expires_at`, which is
  present ONLY while the lease is `ACQUIRED` — the field is
  consumed (`None`) the instant a dial issues, so every later
  phase MUST carry `grant_expires_at=None` (round 32,
  SA_swe2_review_2 item 21 — a consumed deadline is not
  documentation). The TTL lapse
  is TERMINAL (round 32, SA_swe2_review_2 item 16): `tick` ends the
  grant with `call_state_changed{reason:"grant_ttl_expired"}` and
  serves the next ticket — a lapsed grant is NEVER silently
  requeued, so a sleeping agent can never double-occupy the head.
  Queue order is the global monotonic `seq` minted at acquire
  ingress (round 25) — authoritative for every re-check, never the
  agent's reported position. `QueueTicket.intent` is `DIAL_OUT`
  only (round 32 — inbound rides wake-and-grant, never the queue,
  and the kernel rejects any other intent at model validation).
  Eligibility is re-evaluated against ALL currently free lines on
  every trigger: lease end / line going free, provider registration
  on a previously unbound line, a provider `capability_update`
  widening OR narrowing caps, claim resolution or assignment
  release, a new ticket enqueue, and every `tick` deadline expiry —
  a narrower cap re-check is what evicts a ticket the line can no
  longer serve. `call.cancel_acquire{ticket_id}` withdraws. Tickets
  are
  **socket-bound**: a disconnected agent's tickets die with the
  connection. When an incoming ring preempts a granted-but-undialed
  lease (round 19), the grant collapses back into a ticket reissued
  at the head of the queue and the `event.call_state_changed` for
  the preempted lease carries `reissued_ticket_id` so the agent can
  correlate its new queue position with the dead lease (round 23).
- `call.standby` (and the round-2 `line.standby` preferred-handler
  idea) is **REMOVED** (round 8): no server-side parked requests -
  a stale parked request is a zombie-grant risk, and client-side
  retry is the freshness guarantee (a dead/disconnected agent cannot
  hold a stale claim).
- (Superseded: founding spec's "second `dial` -> `409 LineBusy`,
  never a queue" - reversed by partner addendum round 1.)

### §3.5 Provider namespace (`provider.*` up / `line.*` down)

Up: `provider.register`, `provider.reregister`, `provider.ping`,
`provider.capability_update`, `provider.call_state`,
`provider.audio_ready` (round 14), `provider.audio_lost` (round 30 —
`lease_id?` omitted for a line-scoped loss that resets every live
lease on the line; resets the lease's `audio_ready` latch in
kernel-side media state, not on the frozen `CallLease` DTO, so the
latch re-arms on the next `audio_ready`; latch bookkeeping ONLY —
round 32, SA_swe2_review_2 item 11: while the media socket stays
attached the fact arms NO `MEDIA_PAIR_TTL_SEC` re-attach window —
mint/detach/reissue alone own that deadline — and never produces
`event.media_overflow`, which is minted only by the MediaRouter's
`ingest_media_overflow(line_id, lease_id)` PCM-drop ingress),
`provider.reattach_media`
(round 27 — fresh provider media credentials for a live lease after
the provider's media socket dropped while its control channel
lived).
Down (server-initiated on the provider's own inbound connection):
`line.dial`, `line.answer`, `line.hangup`, `line.send_dtmf` -
each returning
`{accepted}`; `line.dial`/`line.hangup`/`line.send_dtmf` carry
`{assignment_id, lease_id, ...}` while `line.answer` carries
`{assignment_id, call_token}` — it names the RING, no lease
exists yet (round 31) — and `line.hangup` alternatively carries
`{assignment_id, call_token}` to reject a still-unanswered ring
(XOR target, round 23). `line.dial`'s reply additionally carries
`provider_call_id?` (round 32 — the provider's own call handle,
echoed back to the agent's `call.dial` result so both sides' call
logs name the same leg). `line.revoke` exists but is **admin-only**
(round 10f). The `{accepted}` reply on `line.dial`/`line.answer`
is awaited by the adapter with a ~2 s deadline (round 23, Saga
compensation; mechanism pinned round 31, SA_swe2_review_1 item 5):
`{accepted:false}` / timeout / transport error MUST drive the
kernel's binding-scoped `compensate_dial(line_id, assignment_id,
lease_id)` entry — an adapter/kernel-internal call, never a wire
method and never a fabricated `provider.call_state` fact (the
provider reported nothing; a synthesized `disconnected` would lie
about an observation that never happened). `compensate_dial`
enqueues a best-effort `line.hangup{lease_id}` FIRST — air-side
teardown of the call leg that may have physically reached the
phone — then ends the lease with `network_error` in the same
atomic mutation; if the provider socket is dead the command drops
silently and the lease still ends. Unlike owner-scoped
`call.hangup` it requires no agent `session_id` — the adapter
holds none and must not forge one — and a genuine provider
`disconnected` arriving later dedupes against the closed lease.
A late rejected-dial report racing an already-queued hangup is a
dedupe/no-op (round 32): a lease already in `terminating` — or
already ended — mints no second `line.hangup` and never ends
twice. `line.answer` failure rides the DISTINCT ring-scoped
`compensate_answer(line_id, assignment_id, lease_id, call_token)`
entry (round 32, SA_swe2_review_2 item 9): it enqueues
`line.hangup{call_token}` — the frozen ring-scoped REJECT vehicle,
there is deliberately no separate reject command in the provider
vocabulary — ends the `answering` lease `network_error` in the
same mutation, and keeps the line `RINGING_LOCKED` until the
provider's `disconnected` or the `ring_drain_ttl_sec` lapse
resolves the drain, because the physical inbound ring can outlive
the failed answer; `compensate_dial` is lease-scoped
hangup-first by contrast — it frees the line in the same step.
The kernel answers the agent's `call.dial`/`call.accept` without
waiting for this reply.

### §3.6 Events (`event.*` down to agents)

`event.incoming_call{line_id, call_token, caller_id?,
expires_at}` (first valid `call.accept` wins),
`event.line_acquired`, `event.queue_updated`,
`event.missed_call{line_id, caller_id?, reason}` (`voicemail_ref`
removed round 27 — no producer ever minted it),
`event.line_state_changed{line_id, old_state, new_state, reason?}`,
`event.audio_ready{lease_id, line_id}` (§5.5),
`event.call_state_changed{lease_id, line_id, state, reason?,
answer_confidence?, reissued_ticket_id?}` - `lease_id`
is the single call identifier everywhere; no `call_id` exists
(round 11). `reissued_ticket_id?` is present only on a
`preempted_by_incoming` teardown of an undialed grant taken with
`enqueue:true` (round 23) - it names the NEW head-of-queue ticket
that replaced the dead lease. `event.fallback_triggered{line_id, call_token,
caller_id?, policy, expires_at}` goes to the `system:fallback_worker` session
only, when the accept window expires on a WORKER-policy line
(round 14; `expires_at` is the worker's accept deadline bounded by
`fallback_accept_ttl_sec`, round 23).

`event.assignment_revoked{assignment_id, line_id, owner_agent_id,
reason?}` (round 19b; `owner_agent_id` required since round 22 -
emitted only for agent LineAssignment revocation) is delivered to
the revoked owner's session when an owner
`call.release{assignment_id}` or an admin
`revoke_assignment`/`line.revoke`
ends the durable binding - ONLY explicit revoke paths (round 23:
`idle_revoke_after` expiry is alert-only, see `event.idle_alert`)
- without it the owner would learn only via
`OWNER_MISMATCH` on the next `call.dial`.

`event.idle_alert{assignment_id, line_id, owner_agent_id,
idle_since}` (round 23) is the alert-only escalation for an
EXCLUSIVE line idle past `idle_revoke_after`: routed to admin-scope
subscribers and the configured escalation channels, never to
ordinary agent sessions, and NEVER implying a revoke; emitted at
most once per continuous idle period.

The liveness/claim/queue companions (rounds 25-28; inventory pinned
round 31): `event.line_health_changed{line_id, old_state,
new_state}` reports provider liveness transitions on the
`LivenessState` axis (§2.2 — `reachable`/`degraded`/
`ws_disconnected`/`unreachable`; `unbound` is legal only as the
initial `old_state` of a first attach). `event.claim_resolved
{claim_id, line_id, status, assignment_id?, reason?}` resolves a
`call.claim_exclusive` filing to the claiming session
(`assignment_id` present iff approved; `status:"rejected"` also
fires automatically when the claimed line's provider dies —
`reason:"provider_lost"`). `event.assignment_granted{assignment_id,
line_id, mode}` hands the approved exclusive owner the
`assignment_id` it needs for `call.release{assignment_id}` —
re-delivered to a later-registering owner session.
`event.queue_cancelled{session_id, ticket_id, reason}` reports a
server-side ticket drop (`line_exclusive`/`provider_lost`/
`capability_removed`/`claim_resolved`). `event.media_overflow
{lease_id, line_id}` (round 31) is emitted ONCE per contiguous
uplink-overflow episode — the pacing/buffer semantics live in
§6.6.

`sms_received` and every other SMS surface are removed (§8).

## §4. Lines, intents, and ownership

### §4.1 Intent model (refined)

Agents self-register `INTENT` sets
`{dial_out|receive_inbound}` via `agent.register_intent` -
`exclusive_line` is NOT a registrable intent (passing it raises
`INVALID_INTENT`, round 21): durable exclusivity is requested ONLY
through `call.claim_exclusive` + moderator approval or the admin
override `admin.assign_line` (round 22). The gateway's
intent-resolver turns intents + line caps into **LineAssignment**
records - the durable truth of who owns what.

- `dial_out` - line granted from the pool for dialing; the
  ephemeral lease ends on `call.hangup` (auto-release, round 22)
  or `call.release{lease_id}`.
- `receive_inbound` - a registered inbound intent does NOT hold the
  line `BUSY` (round 11): the line stays `FREE` (pool-eligible
  unless `exclusive_line`) and only locks to `RINGING_LOCKED` while
  a physical call rings; `call.accept` consumes the ring and issues
  the lease; the lease ends with the call (auto-release, round 22).
  Only `receive_inbound` sessions see `event.incoming_call` on a
  shared pool and may `call.accept` (round 22).
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
| Nature | durable, `agent_id`-bound | ephemeral, socket/heartbeat-bound AND `session_id`-bound (round 22: one `agent_id` may hold several sessions; leases route and die per session) |
| Lifetime | survives agent sleep/disconnect | only during ringing/dialing/active |
| Ends by | owner `call.release{assignment_id}` / admin `revoke_assignment` (round 23: idle expiry is alert-only via `event.idle_alert`, never a revoke) | auto-release on `call.hangup` or provider `disconnected`; `call.release{lease_id}` (mid-call = hangup-intent); dead-man switch; orphan grace |

LineAssignment fields (round 27 — refreshed against the contract
model; hardware set completed round 32, SA_swe2_review_2 item 22):
`assignment_id`, `line_id`, `owner_agent_id`, `profile`
(stored admin label), `mode (exclusive|shared_pool)`, `claimable`,
`accept_window_sec`, `fallback_policy`, `timeouts` (resolved
`LineTimeouts`), `allowed_number_types`, `last_seen_at`,
`idle_revoke_after`, and the four hardware-identity fields
`adapter_mac` (the BT dongle's BDADDR — hciX names are unstable),
`paired_phone_mac` (the handset the pairing window's SSP
auto-accept may confirm), `transport_profile` (the hci-agent
tunnel profile vocabulary `loopback|direct_mtls|wireguard` the
provider adapter picks its listener/dialer from) and
`line_home_country` (ISO alpha-2 display/caller-id normalization
region only — `call.dial` still requires its explicit `country`
param, so the field never normalizes dial input).

**ProviderBinding is a third, distinct object space** (round 20 —
the ≤0.0.12 conflation where `register_provider` returned a
LineAssignment is removed): the provider attachment record
(`assignment_id` in the *binding* space, `caps`, `profile`,
`liveness`, `connected_at`, `last_seen_at`,
`heartbeat_interval_ms`, `heartbeat_degraded_after`,
`heartbeat_down_after`, `channel_seq`, `orphan_deadline_at` — the
heartbeat stamps and `channel_seq` are frozen at register time, and
the record persists on the `provider_bindings` table since round 31,
§4.5 — `liveness`/`orphan_deadline_at` still re-derive on load
because the sockets they describe died with the process).
LineAssignment = who owns the line for
AGENT purposes; ProviderBinding = which provider physically
serves it. Both share the `AssignmentId` tag; admin operations
disambiguate by which object the id resolves to — revoking a
LineAssignment reverts the line to the shared pool, revoking a
ProviderBinding drops the binding's liveness to `unreachable`
(`event.line_health_changed{new_state:"unreachable"}`) with lease
teardown.

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
- Release semantics differ per object (round 22, review-10 HIGH-3):
  `call.release{assignment_id}` on the DURABLE binding = future
  calls only; an in-flight call finishes (the lease completes and
  cannot renew). `call.release{lease_id}` on the EPHEMERAL lease
  during an ACTIVE call = hangup-intent - the call is hung up
  first, then the lease ends; a call NEVER outlives its lease.
  `call.hangup` queues the modem teardown and parks the in-flight
  lease in `terminating` — it ends on the provider's
  `disconnected` or the `hangup_hold_sec` lapse (`tick`,
  kernel-side `network_error`), never atomically with the reply
  (round 32, SA_swe2_review_2 item 1; an undialed `ACQUIRED`
  grant still ends directly) - no
  trailing `call.release` is needed, and a forgotten release can
  never wedge the line BUSY.
- `line.revoke` = admin-only (confirmed round 10f).
- `idle_revoke_after` = **alert-only** (round 10c): notifications via
  admin-configured escalation channels ("line broken / agent
  broken"); pool lines may auto-recover. Whether auto-reassign should
  ever apply is an OPEN question (§12).

### §4.3 Wake-and-grant inbound routing

(See §5.2 for the full sequence.) Gateway-managed wake-and-grant was
chosen over both a persistent listen-lease and a broadcast-race:
incoming -> `RINGING_LOCKED` -> hooks fire (wake sleepers) +
`incoming_call` event with `call_token` to the eligible acceptors
(the exclusive owner's agent, or each `receive_inbound` session on
a shared pool - round 22) -> first
`call.accept(call_token)` performs atomic lease+answer.

### §4.4 Profiles

Profiles are **config presets** over primitive policies
(`inbound_policy`, `outbound_access`, `priority`,
`missed_call_hook`), not hardcoded agent classes - and round 23
(review-12 M6) makes them strictly INFORMATIONAL labels: the
`profile` string on a `ProviderBinding`/`LineAssignment` is an
admin display/config-preset tag only. Inbound eligibility and
grant preemption are decided EXCLUSIVELY by
`Intent.RECEIVE_INBOUND in binding.caps` - never by matching a
profile name, so renaming or inventing profiles can never silently
change call routing. Reference presets:

- `outbound_campaign`: reject/ignore inbound, pool member.
- `secretary`: wake_and_route inbound, explicit-only outbound,
  inbound preempts the outbound queue.

### §4.5 Persistence split — durable config vs in-memory call state (round 23, review-12 §4)

The kernel is pure in-memory (M1); durable objects are reconciled
with a SQLite store by the admin adapter (M2) without ever doing
I/O inside a kernel mutation (CODING_HAZARDS rule 4).

- **SQLite (`WAL` mode, `synchronous=NORMAL`) holds ONLY durable
  configuration, keys and provider-attachment rows:** `api_keys`
  (SHA-256/HMAC token hashes +
  scope + subject_id), `lines` (`line_id`, `adapter_mac`,
  `paired_phone_mac`, `transport_profile`, `line_home_country`,
  `claimable`,
  `accept_window_sec`, `fallback_policy_json`,
  `allowed_number_types_json`, `timeouts_json`, `profile` — the
  administrative label carried on the assignment, round 28),
  `line_assignments` (`assignment_id`, `line_id`,
  `owner_agent_id`, `mode`, `idle_revoke_after_sec`, `profile`,
  `last_seen_at` — required since round 25: without it the
  alert-only idle clock restarts from zero on every daemon restart),
  `provider_bindings` (round 31, SA_swe2_review_1 item 3 —
  `assignment_id`, `line_id`, `caps_json`, `profile`, `meta_json`,
  `connected_at`, `last_seen_at`, `channel_seq`,
  `heartbeat_interval_ms`, `heartbeat_degraded_after`,
  `heartbeat_down_after`: the persisted identity card lets a
  restarted kernel distinguish "provider that re-registers" from
  "provider that never came back" and keeps the line-scoped stamp
  watermark across restarts; `liveness`/`orphan_deadline_at` are
  NOT durable columns — they re-derive on load, see the
  hydrated-dead rule below) and `escalation_hooks`.
- **Memory-only (never persisted):** `CallLease`, `QueueTicket`,
  `PendingClaim`, and the
  `_closed_leases` tombstone ring. Active calls are NOT restored
  from the DB on restart - they are rebuilt through the
  `GetCalls()`/`state_snapshot` reconciliation protocol (§9), so
  the store can never disagree with the real modem state.
- **Sync pattern (no I/O in the kernel; widened round 25;
  `hydrate_lines` naming round 31):** at
  startup the gateway reads `lines` + `line_assignments` +
  `provider_bindings` and hands the validated rows to the kernel in
  ONE batch `hydrate_lines(rows, bindings)` call — never per-row
  `assign_line`/`register_provider` replays. Binding mutations
  (`register`/`reregister`/supersede/revoke and their `channel_seq`/
  `last_seen_at` stamps) upsert the `provider_bindings` row through
  the same ordered writer path (round 31) — the provider adapter
  owns those writes the way the admin adapter owns line config.
  EVERY `LineAssignment` mutation — not only admin calls: the
  agent-side `call.release{assignment_id}` path included —
  serializes these rare writes through the SAME two-phase path
  (lock narrowed round 31, see the drain-discipline bullet below):
  first the kernel call under the single in-memory mutation lock,
  then the returned immutable `LineAssignment` snapshot is
  committed to SQLite by the shared ordered writer task — no disk
  I/O ever inside the lock.
  **The durable write is ONE transaction (round 30, review-19
  B2a#5):** a line-level mutation that touches both tables — the
  `lines` row and its `line_assignments` row(s), e.g.
  `provision_line`'s insert or `remove_line`'s delete — commits as
  a single SQLite `BEGIN IMMEDIATE`…`COMMIT`; a half-written pair
  (a `lines` row without its assignment, or an orphan assignment
  row) is impossible, so hydration can never observe a torn
  record.
  **Admin mutations serialize globally (round 29, review-1 B15):**
  ALL admin mutation calls — `configure_line` (including its
  cross-line forward-chain cycle/depth revalidation of every line
  whose stored chain passes through the changed one),
  `remove_line`, `assign_line`, `revoke_assignment`, claim
  approve/reject — serialize through the kernel's single-writer
  mutation path, so the multi-line chain check and the mutation it
  guards execute atomically: two concurrent `configure_line` calls
  on different lines of one chain can never each pass the check in
  isolation and combine into a cycle — the lock covers the
  revalidation, not only the row write
  (`upsert` by `line_id`). `revoke_assignment` returns a BOOLEAN
  (round 31 — `True` when an agent-owned grant reverted to the
  pool, `False` for a provider-binding id or an idempotent retry
  on a tombstoned id). The kernel stays I/O-free; a crash between
  the two writes leaves SQLite one mutation behind — bounded and
  surfaced: the store rows are reloaded through `hydrate_lines` on
  next
  boot, and a failed write-through marks the line `config_stale`
  in observability until a succeeding write resyncs it. The same
  write-through path carries `last_seen_at` refreshes — stamped on
  `drop_session` closing the owner's last session and on owner line
  operations (round 28 — a stamp kept only in memory would restart
  the idle clock on every reboot; a bare `ingest_ring` never stamps
  it). Line-lifecycle admin ops follow the same kernel-first
  ordering (round 28): `admin.provision_line` creates the kernel
  record then upserts the `lines` row; `admin.remove_line` deletes
  the kernel record then drops the row — a failed precondition
  never reaches the store.
  `config_stale` (round 27) is an ADAPTER-side observability flag,
  not a contract field: the admin adapter keeps a per-line
  write-behind marker in its own bookkeeping and surfaces it on the
  admin diagnostics stream — neither `LineAssignment` nor
  `LineSummary` carries it, because the kernel never observes
  store-write outcomes.
- **Write-through failure = kernel compensation (round 30,
  review-19 B2a#1; `line_id` parameter added round 31):** a failed
  durable write AFTER the kernel
  mutation committed is rolled back through
  `LineAdminKernel.restore_line(line_id, snapshot)` — the only legal
  compensation path. The adapter captures the pre-mutation
  `LineAssignment` snapshot before every write-through; on a
  failed `lines`-row upsert/delete it passes that snapshot back
  (`snapshot=None` un-creates the record at `line_id`, a prior
  snapshot is restored verbatim for a patched/revoked/removed
  one — including a tombstoned `assignment_id`: the
  `_closed_assignments` ring is consulted on agent/provider
  INGRESS only, never on hydration or restore), the kernel emits
  the COMPENSATING events the rollback
  implies so the already-drained outbox never claims a durable
  change the store does not reflect, and the failure surfaces as
  `config_stale` plus an admin alert. This closes the lifecycle
  hole `config_stale` alone could not: a failed `provision_line`
  write no longer leaves a lease-serving ghost that vanishes at
  restart, and a failed `remove_line` delete no longer resurrects
  the line at next hydration — the
  compensation re-enters through the single ordered writer's
  mutation queue (see the single-writer drain contract below), so
  the kernel and the store re-converge in FIFO order instead of
  diverging until the
  next boot. Hydration is the LAST-resort reconciler: boot-time
  `hydrate_lines` loads the stored rows verbatim from the store
  (the store is authoritative on cold start — a kernel-only record
  that never persisted is simply absent), so any residue a crashed
  compensation left is re-converged at restart, never mid-flight.
- **`provider_bindings` persistence is a BEST-EFFORT CACHE
  (round 32, SA_swe2_review_2 items 14/15):** the row mirrors the
  live record, it is never a second source of truth — a
  write-through failure on a `provider_bindings` row raises the
  `config_stale` condition plus an admin alert and NOTHING else:
  no kernel compensation and no `restore_line` call (that hook is
  `LineAssignment`-shaped only), because the provider's next
  successful `provider.register`/`provider.reregister` rewrites
  the row anyway. `last_seen_at` writes are DEBOUNCED through the
  writer task — at most one persisted stamp per `>= 30 s`
  coalesce window, never one row write per `provider.ping`; the
  kernel's in-memory copy still updates per fact (liveness math
  reads memory, never the row). The persisted row ALSO carries
  the provider-stamp watermark (the highest applied
  `observed_at`/`checked_at` per binding) so a restart cannot
  re-apply replayed facts below the watermark, and `channel_seq`
  reseeds as `max(stored) + 1` across restarts — a hydrated
  generation is never re-issued, so a stale socket's facts can
  never pass the live-generation check against a restored
  binding.
- **Hydrated-dead bindings (round 31, SA_swe2_review_1 items
  3/18; hook suppression round 32):** every persisted
  `provider_bindings` row loads through
  `hydrate_lines` as a DEAD binding — liveness forced to
  `ws_disconnected` regardless of the stored rung (the sockets it
  described died with the process), `last_seen_at` preserved, and
  `orphan_deadline_at` ALREADY EXPIRED — so the first `tick`
  after hydration runs the standard orphan cascade on the row
  (§5.4): live-lease teardown is a no-op (no `CallLease` rows
  persist), pending tickets/claims resolve `provider_lost`, ring
  and `external_call` bookkeeping resets, and the binding id moves
  into the `_closed_assignments` tombstone ring. A fresh
  `provider.register` arriving BEFORE that first tick legally
  supersedes the dead row — it arrives on a NEW control socket,
  so the kernel mints a fresh `assignment_id` and `channel_seq`
  and the dead row's pending cascade never races the live binding
  (reconnect inside a LIVE orphan-grace window still uses
  `provider.reregister`; a `provider.register` is `INVALID_STATE`
  only against a live-channel binding). The hydrated-dead `tick`
  cascade SUPPRESSES the external escalation hooks a live-binding
  teardown would fire (round 32): the restart window is tagged by
  the hydration epoch, so webhooks never re-alert on provider
  deaths that predate the process. Legacy rows written before
  `channel_seq` existed are backfilled at the STORE layer
  (generation `1`) before `hydrate_lines` is called — the kernel
  hydrates only fully-validated rows and never normalizes a
  missing generation itself.
- **Drain discipline + event ordering (round 25, clarified;
  ordering pinned round 30, review-19 B2a#5-6; lock NARROWED
  round 31, SA_swe2_review_1 item 15):** whichever adapter
  called the kernel, the drained outbox items feed the SHARED
  EventBus and provider-command executor — never the draining
  adapter's own connection. An admin-adapter drain after
  `approve_claim` therefore still delivers `event.claim_resolved`
  to the agent's session through the same bus; this is why
  `LineAdminKernel` exposes `drain_events()`/
  `drain_provider_commands()` too. The SINGLE mutation lock now
  covers ONLY the in-memory phase for EVERY adapter path — agent
  RPC, provider RPC, `tick(now)`, and admin RPC alike: kernel call
  → `drain_events()` → `drain_provider_commands()`, then the
  lock RELEASES before any I/O. The I/O phase rides a SINGLE
  ORDERED WRITER TASK shared by all adapters: each mutation hands
  the writer one ordered bundle `{write-through job?, drained
  events, drained provider commands}`; the writer performs the
  SQLite write-through first, then `bus.publish`/command dispatch,
  strictly in submission order — so per-mutation FIFO survives
  without a socket or a disk write ever sitting inside the lock
  (an `await` on the provider channel or the WAL can no longer
  park a `tick` behind a slow mutation). Failure handling keeps
  the same ordering: on a failed write-through the WRITER — not
  the mutating adapter — invokes `restore_line(line_id,
  snapshot)`, which re-enters the mutation lock as an ordinary
  locked mutation (the lock is already free: no deadlock, the
  writer never waits on itself) and drains its compensating
  items onto the SAME writer queue behind the failed mutation's
  own bundle — observers always see mutation-then-compensation in
  order, never a compensation that outruns the mutation it
  corrects. A lock-free drain (e.g. a
  `tick` racing a parked writer) could interleave
  events and provider commands across mutation boundaries —
  forbidden: ordering is part of the single-writer contract, not
  a best-effort property of whichever adapter happened to call
  last.
  **Pending-write marker (round 32, SA_swe2_review_2 item 3):**
  a mutation that carries a write-through job marks its bundle —
  and the `line_id` records it touches — PENDING-WRITE until the
  writer's SQLite confirmation lands. The marker is a
  coordination fact, not an isolation level: concurrent
  mutations seeing the still-uncommitted kernel state is the
  PINNED design (the kernel commits in memory first and the store
  converges behind it), never a bug — what the marker pins is
  ORDERING and COMPENSATION, not visibility. While a record sits
  pending-write, an ADMIN mutation that touches the same record
  waits a bounded deadline for the write to confirm before
  reading its pre-mutation snapshot (fail-closed `config_stale`
  on timeout, never an unbounded park) — provider and agent
  ingress paths are UNAFFECTED: their facts never read durable
  snapshots, so they proceed against the in-memory state
  immediately. On write-through failure the writer submits
  `restore_line(line_id, snapshot)` THROUGH the mutation queue
  like any other locked mutation — never inline inside the
  writer — and the restore is GUARDED: if the record was
  re-mutated between the failed write and the compensation
  landing, `restore_line` does NOT blindly overwrite the newer
  state with the stale snapshot; it degrades to `config_stale`
  plus an admin alert and lets hydration reconcile the residue
  at next boot.

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
5. `call.hangup` -> the modem teardown is queued and the
   in-flight lease parks in `terminating` until the provider's
   `disconnected` or the `hangup_hold_sec` lapse ends it
   `network_error` kernel-side (round 32 — only an undialed
   `ACQUIRED` grant still ends atomically; auto-release,
   round 22), queue advances on the END. `call.release{lease_id}` is needed
   only to surrender a granted-but-undialed lease or to end an
   in-flight call early (hangup-intent).

### §5.2 Inbound: wake-and-grant

1. Phone rings -> provider reports
   `provider.call_state{ringing, caller_id?}` -> gateway marks the
   line `RINGING_LOCKED` (outbound `call.acquire` on it fails
   `LINE_BUSY_INCOMING`; a ring preempts a granted-but-undialed
   outbound grant when the line's binding carries
   `Intent.RECEIVE_INBOUND` in `caps` - decided strictly by caps,
   never by the profile label, round 23) and mints the one-shot `call_token`
   inside `ingest_ring`, returned to the provider in the
   `{ok, call_token}` response (round 22) - the provider echoes it
   on subsequent reports for the ring so a pre-answer caller
   hangup correlates. **Ring identity (round 31,
   SA_swe2_review_1 item 20):** the ring report (and each
   `state_snapshot` ringing element) may carry the provider's own
   `provider_call_id` handle — when present it is the EXACT dedup
   key: distinct handles are distinct calls no matter how close
   together, and a re-report under the same handle can never mint
   a second `call_token`. Without the handle dedup is best-effort
   on `(line_id, caller_id)`: two same-number ring reports within
   `RING_DEDUP_MIN_GAP_SEC` (30 s) merge as ONE physical ring,
   while reports further apart are genuinely distinct calls — the
   residual ambiguity is documented (a fast same-number redial
   inside the window merges; an adapter that always fills
   `provider_call_id` removes it).
2. Gateway consults LineAssignment: owner exists -> wake owner via
   configured hooks; no owner -> event to the live
   `receive_inbound`-intent sessions (round 22 - a shared pool
   never broadcasts rings to dial_out-only agents).
3. `event.incoming_call{line_id, call_token, caller_id?,
   expires_at}` is emitted (RFC 3339 absolute deadline since
   round 22 - a relative `expires_in_ms` would skew by outbox
   queue delay). **Ring re-delivery (round 25):** a hook wakes the
   owner but the EVENT itself fires once — so when the exclusive
   owner (re)opens a session while its line still holds a live,
   unconsumed ring, the kernel re-emits `event.incoming_call` to
   the new session with the SAME `call_token` and original
   `expires_at`; an agent that missed the emission by milliseconds
   is never locked out of its own ring.
4. First `call.accept(call_token)` = atomic lease + answer + media
   attach. Owner-scoped lines reject non-owner accepts with
   `OWNER_MISMATCH`; a second accept gets `STALE_TOKEN` (round 21 -
   `LINE_BUSY_INCOMING` never applies here: the accept consumes the
   ring, it does not contend with one). The minted lease starts in
   the kernel-internal `CallPhase.ANSWERING` (round 31): the
   `line.answer` command is in flight but the network has not yet
   confirmed — the phase has NO wire spelling (it never appears as
   an `event.call_state_changed.state`), reconciles to `active` or
   `disconnected` from the provider's reports, and is covered by
   `max_call_duration_sec` like any other post-acquire phase.
5. **Accept window: default 30 s; ALL timeouts and windows are
   admin-configurable** (round 10a).
6. Timeout -> the line's `fallback_policy` (§5.3). **Ring-drain rule
   (round 25):** deciding `no_pickup` — or a `fallback_accept_ttl_sec`
   lapse — emits `event.missed_call` immediately, but the line does
   NOT return to `FREE` while the physical call is still ringing on
   the modem: the line stays `RINGING_LOCKED` until the provider
   reports the ring's `disconnected` (caller hangup / carrier
   voicemail timeout) or a bounded `ring_drain_ttl_sec` (~60 s)
   elapses, whichever first — plus a best-effort `line.hangup` may
   be issued to shorten the drain ONLY on paths where the gateway
   rejects the call (the `reject` fallback policy, round 28 — never
   on `no_pickup`/missed outcomes where the caller already left,
   and never on the handset-answered `external_call` path). Without this the FIFO queue could
   hand a still-ringing line to the next `call.dial`, and a
   `state_snapshot` would re-register the same physical ring as a
   NEW `incoming_call`.

### §5.3 Per-line inbound fallback policy (round 10b, split round 11)

Admin-configured per line; on accept-window expiry. The window
ALWAYS elapses first - even when zero live subscribed/owner
sessions exist, the kernel waits the full `accept_window_sec` so
configured wake-and-grant hooks (webhook/exec) can wake a sleeping
agent (round 23, review-12 H6): "no live session right now" is NOT
an immediate-fallback trigger. Two classes (ADR-01):

**Core policies** - atomic telecom ops executed inside LineManager:

- `no_pickup` - let the carrier handle it (carrier voicemail /
  missed call on the phone itself; default),
- `reject` - actively reject the call.

**Worker policies** - executed by `FallbackWorker`, an internal
system agent that connects through the STANDARD agent API
(`call.accept` + media socket); LineManager never touches Telegram
HTTP, WAV files, or call bridging:

- `forward_via_line:<line_id>:<e164_number>:<country>` - the
  worker dials OUT on another line and bridges (e.g. a call to a
  personal number is redirected out through a second line). Round
  23 (review-12 C2): the policy carries the COMPLETE dial target
  (`<e164_number>` strict E.164 + `<country>` ISO alpha-2,
  pre-validated at `configure_line` through the same checks
  `call.dial` applies) because the FallbackWorker has no admin
  scope to query config; a self-loop (`forward.line_id ==
  line_id` of the configured line) is rejected at
  `configure_line`. The `system:fallback_worker` session
  BYPASSES `LINE_EXCLUSIVE_NOT_OWNER` when acquiring the target
  line (round 22) - the admin who configured the policy accepted
  that override; the worker gets no other privileges. Transitive
  forward cycles (`A → B → … → A`) are rejected at `configure_line`
  by walking the stored policy chain (bounded at 8 hops, round 25).
  **Second-leg FSM (round 30, review-19 B2a#7):** the worker's
  `call.accept` on the source line answers the PHYSICAL call
  first — that is inherent to the accept-first flow and means the
  caller is connected (and, on metered carriers, billed) from that
  instant; the target leg MUST therefore resolve inside a bounded
  `forward_leg_ttl_sec` budget (~20 s) or the caller pays for
  silence. Leg-B outcomes: target `BUSY`/`RINGING_LOCKED` → the
  worker issues `call.acquire{enqueue:true}` and waits ONE queue
  turn up to the budget — a granted lease proceeds, an expired
  wait is terminal; target `unreachable`/`LINE_NOT_FOUND`/
  `DIAL_REJECTED`/`PROVIDER_UNREACHABLE` → terminal immediately,
  no queue (the target cannot serve at all — and a configured
  `forward_via_line` target can no longer be deleted out from
  under the policy, see `remove_line`). Terminal path: the worker
  plays the line's announcement (the `answer_and_announce`
  machinery, so the caller hears "unreachable, try later" rather
  than dead air) and hangs up the answered leg. Bridging: both
  legs wait for `audio_ready` before the worker cross-connects
  PCM; each leg's `0x02` flush semantics stay leg-local (a flush
  on A never drops B's queue and vice versa). Teardown is
  symmetric — loss of EITHER leg ends BOTH: leg B drops →
  `call.hangup` on A, leg A drops → `call.hangup`+release on B;
  the worker's own death is covered by the generic
  dead-man-switch on BOTH sessions' leases (see "Worker dies
  mid-forward" below),
- `record_and_telegram:<channel>` - answer, record the call/
  voicemail ourselves, post to a Telegram channel (recording
  architecture expanded round 30, review-19 B2a#9): **beep first** —
  the policy carries an always-populated `announcement_ref` field
  (round 31, SA_swe2_review_1 item 26 — the asset selector of the
  provisioned WAV the worker MUST play BEFORE recording starts, the
  `answer_and_announce` machinery; a recording leg never runs
  unannounced, so a recorded party never hears dead air and hangs
  up thinking the line is dead); recording length is bounded by
  the policy's own `max_recording_sec` field (round 31 — a
  `RecordAndTelegramFallback` model field, not a `timeouts` knob:
  `0 < v <= 86400`, the SINGLE authoritative recording bound; the
  line's optional `max_call_duration_sec` still caps the WHOLE call
  independently and the stricter of the two wins — see §10 media
  quotas for the storage side). **Two config spellings (round-31
  fix):** the compact `record_and_telegram:<channel>` string carries
  ONLY the channel selector — `announcement_ref` and
  `max_recording_sec` fill from their defaults (`builtin:beep`, the
  packaged beep asset / 300 s), so the compact form always
  constructs a valid policy and pre-round-31 persisted
  `fallback_policy_json` lacking the new fields hydrates unchanged;
  the dict/JSON form `{"policy": "record_and_telegram", "channel":
  ..., "announcement_ref": ..., "max_recording_sec": ...}` is the
  explicit-override path for a provisioned asset or a non-default
  bound (the ref grammar permits `:`/`/` namespaced asset ids —
  it never rides the compact split, unlike the `:`-banned
  `answer_and_announce:<ref>` selector).
  **Bot-token storage:** the Telegram bot token is a SECRET —
  stored in the SQLite `escalation_hooks`/`secrets` row alongside
  the channel id, never in `fallback_policy_json` and never on
  the worker's argv/env-visible process surface: the supervisor
  injects it into the FallbackWorker through its private config
  channel at process start (the worker holds no `admin` scope and
  never opens the SQLite DB itself). **Delivery guarantee:**
  uploads go through the SAME hardened egress path as webhook
  delivery (explicit timeouts, SSRF guard where the endpoint is
  configurable, `api.telegram.org` allowlisted) and through a
  DEAD-LETTER spool on the `/var/lib/phonebridge-media` volume —
  a recording whose send fails (Telegram outage, worker crash
  mid-upload) lands as a spool entry `{file, channel, attempts,
  next_retry_at}` and is retried with bounded backoff; a
  restarted FallbackWorker re-scans the spool at startup and
  resumes pending sends, so a voicemail can never die silently on
  a worker restart (retention still applies — §10),
- `answer_and_announce:<announcement_ref>` - answer and play an
  announcement. **Eligibility (round 30, review-19 B2a#8 —
  replaces the ambiguous "only if the line has an agent
  registered for inbound"):** the predicate is evaluated at
  DECISION time on live kernel state, not at `configure_line`
  time — the policy is eligible when the line's
  `assignment.mode == EXCLUSIVE` OR at least one live session
  holds `receive_inbound` intent on it (the
  `system:fallback_worker` session itself never counts toward
  that set); when neither holds, `decide_fallback` degrades to
  `no_pickup` — the fallback worker never burns a token for an
  announcement it could not have been expected to replace.
  `announcement_ref` is the admin-provisioned **WAV-asset
  selector** carried INSIDE the policy (round 22) — WAV only, no
  text/TTS selector: TTS lives client-side by design (§1), so a
  configured ref MUST name an existing provisioned audio asset
  validated at `configure_line` (a missing/invalid asset fails
  config, not the ring) - the FallbackWorker
  connects via the standard agent API and has no admin scope to
  query line config. **Hangup pacing (round 30):** the worker
  streams the WAV paced at real-time (≤1 `0x01` frame per 20 ms —
  dumping the file into the MediaRouter ring at socket speed
  would buffer seconds of unplayed audio), then waits for the
  MediaRouter uplink ring to DRAIN plus the jitter-buffer budget
  deadline before issuing `call.hangup` — an early hangup would
  cut the announcement mid-word: the queued frames the provider
  never heard would be silently dropped, so the caller must hear
  the tail, not just the head, of the message.

**Fallback token mechanics (round 14; delivery fixed round 22):**
when `accept_window_sec` expires on a worker-policy line, the
original `call_token` is already stale and an exclusive-owner check
would reject the worker's `call.accept` with `OWNER_MISMATCH`.
LineManager therefore re-issues the ring internally as a
`fallback_call_token` bound to `agent_id: "system:fallback_worker"`
(bypasses the owner check, remains single-use and TTL-bound by
`fallback_accept_ttl_sec`, ~10 s default - the deadline travels on
the event as `expires_at`, round 23) and
delivers `event.fallback_triggered` to the FallbackWorker session
only - `event.fallback_triggered` is THE delivery channel (round
22): the worker needs the `policy` field that `event.incoming_call`
cannot carry, so the earlier "or `event.incoming_call`" hedge is
removed.

**Worker dies mid-forward (round 29, review-1 B8).** The instant the
worker's `call.accept` burns the `fallback_call_token`, the inbound
ring becomes an ORDINARY `CallLease` owned by the
`system:fallback_worker` session — no special fallback liveness
path exists or is needed. If the worker process dies, or its
session socket drops, between that accept and the bridged dial
completing on the target line, the generic dead-man switch
(§5.4) applies verbatim: `drop_session` ends the worker's accepted
lease — the still-ringing/answered inbound call is hung up through
the same compensation path as any agent-initiated teardown — and
any target-line lease the worker already minted dies with the same
session. The caller on the source line is never left
unaccounted-for: its physical call ends exactly like a normal
agent-initiated hangup.

### §5.4 Lease teardown and orphan handling

- Dead-man switch: a dead/disconnected agent loses its CallLease and
  its call is hung up - scoped by `CallLease.session_id` (round 22):
  one `agent_id` may hold several sessions, and a dead session
  tears down only ITS leases, never a sibling session's call.
- Ephemeral-lease auto-release (round 22; `terminating` leg
  pinned round 32, SA_swe2_review_2 item 1): a CallLease never
  outlives its call — `call.hangup` on an in-flight lease parks
  it in `terminating` and returns immediately; the lease ends
  when the provider's `disconnected` confirms teardown or the
  line's `hangup_hold_sec` lapse makes `tick` end it
  `network_error` kernel-side — never atomically with the reply
  (an undialed `ACQUIRED` grant ends directly, no command
  queued); `call.release{lease_id}` during an
  ACTIVE call is the same hangup-intent path.
- Orphan-lease grace on provider reconnect: **10 s** (round 5b) —
  applies ONLY to the WS-drop level above; a provider that reports
  tunnel/USB death, or stays unreachable past the
  `heartbeat_down_after` rung of the liveness ladder,
  tears its calls down immediately (round 20). Mechanics (round 20b):
  on WS drop — ingested as `LivenessState.WS_DISCONNECTED` (round 21,
  distinct from heartbeat-timeout `UNREACHABLE`) — the binding's
  `ProviderBinding.orphan_deadline_at` is
  set to `now + orphan_grace_sec` (admin-configurable, default 10 s);
  a `provider.reregister` inside the window whose
  `state_snapshot` confirms the call still lives on the D-Bus
  (`GetCalls()`) re-binds the lease seamlessly and clears the
  deadline; expiry in `tick(now)` — or a snapshot already missing
  the call — ends the lease as
  `call_state_changed{reason:"provider_lost"}`. Every preserved
  lease then gets FRESH provider media credentials via
  `line.reattach_media` — media tokens are one-shot and did not
  survive the dropped socket (round 25).
  **While the binding sits `WS_DISCONNECTED` the heartbeat-down
  check is SUSPENDED (round 31, SA_swe2_review_1 item 22):** the
  elapsed-silence ladder (`degraded`/`unreachable`) does not run on
  a socket-less binding — `orphan_deadline_at` is its SOLE clock.
  Expiry resolves the whole orphan atomically inside one `tick`
  mutation — the **orphan cascade**: the binding escalates
  `ws_disconnected -> unreachable`, every live lease on it ends
  `call_state_changed{reason:"provider_lost"}` — the ONE unified
  gateway-minted reason for "the provider channel died" (round 32,
  SA_swe2_review_2 item 12; identical across orphan-grace expiry,
  heartbeat-down `unreachable`, `admin.revoke_assignment` and
  `dongle_lost` teardowns — the earlier `network_error` spelling
  for the same fact is removed, `network_error` now names only
  the saga-compensation and deadline-lapse paths) — and NO
  `line.hangup`, nor any other `line.*` command, is queued FOR the
  dead binding: a tombstoned binding can never receive commands —
  the dead-binding outbox purge below wins — so the modem-side
  call is left to the provider's own reconnect/caller-side
  cleanup. Every pending queue ticket the binding served cancels
  `event.queue_cancelled{reason:"provider_lost"}`, every pending
  claim on the line auto-rejects `provider_lost`, any ring or
  `external_call` bookkeeping the binding owned resets (an expired
  `fallback_call_token`/ring timer burns with its `MissedCallEvent`),
  and the binding id tombstones into `_closed_assignments` so later
  facts resolve `STALE_TOKEN`. A **hydrated-dead** persisted
  `provider_bindings` row (§4.5) hits the identical cascade on the
  first `tick` after load — its `orphan_deadline_at` loads already
  expired — unless a fresh `provider.register` supersedes it
  first.
- **Half-open supersede (round 28):** a `provider.reregister` on a
  live REACHABLE binding — the provider's fresh control channel
  arrived before the old socket's death was even detected — is a
  legal supersede, not an error: the old binding is marked
  superseded, a FRESH `assignment_id` is minted, and live leases
  are preserved (reconciled against `state_snapshot` when present,
  preserved unreconciled when absent — the snapshot is
  authoritative only for `dialing`-or-later leases, so an undialed
  `ACQUIRED` grant survives an empty snapshot). A `provider.register`
  on an already-live binding is `INVALID_STATE`. The supersede is
  DETERMINISTIC (round 29, review-1 B2): `provider.reregister`
  always replaces the current live binding — REACHABLE, DEGRADED,
  or a binding minted moments earlier by a racing reregister —
  sequential last-writer-wins, never first-come-holds. A reregister
  arrives on a NEW control connection (one socket carries at most
  one live `assignment_id` for its lifetime, api-contract B12), so
  the adapter MUST close the superseded control socket after
  answering the superseding reregister; any `provider.*` fact still
  in flight on the old socket (`ping`, `call_state`,
  `capability_update`, `audio_ready`, `reattach_media`) carries a
  superseded `assignment_id`, resolves through the closed
  `_closed_assignments` ring, and is rejected `STALE_TOKEN` — a
  racing TCP FIN in flight never lets stale facts land on the
  successor binding.
- **Dead-binding cleanup at the mutation (round 28):** commands
  queued for a binding that dies or is superseded are handled
  INSIDE the killing/replacing mutation — entries for leases that
  survive a re-bind (`line.hangup`, `line.send_dtmf`,
  `line.reattach_media`) are retargeted to the new `assignment_id`,
  undeliverable in-flight `line.dial`/`line.answer` are dropped
  fail-closed with lease teardowns minted in the same transaction;
  `drain_provider_commands` keeps only a defensive drop for a
  vanished-binding command (`line.revoke` bypasses it while the
  control channel is still open).
- **Provider-process restart mid-call (round 25):** a `LineProvider`
  restarted by its supervisor inside the grace window returns from
  `GetCalls()` with EMPTY memory — it cannot echo `lease_id`. To
  prevent the call degrading to an unkillable `external_call` while
  the agent's lease still waits, TWO rules apply: (a) the provider
  MUST keep a small process-restart state file
  (`/run/phonebridge/<line_id>.json`, atomic write on every call
  mint/transition — write to a temp file in the SAME directory,
  `fsync`, then `rename` over the target; a same-filesystem rename
  is atomic, so a crash mid-write leaves either the old or the new
  JSON, never a torn file — round 29, review-1 B14) mapping
  `lease_id` → D-Bus object path, so a restart re-binds ids exactly
  — the file survives a provider-process restart only: `/run` is
  tmpfs (round 27), so a container/host reboot legitimately finds
  it absent and falls back to rule (b). **Call-instance binding
  (round 30, review-19 B2a#10):** D-Bus call object paths are
  REUSED — `/org/pipewire/Telephony/agN/callM` numbering restarts
  after a stack restart or a phone re-dial, so the bare path is
  not an identity. The state file MUST bind each mapping to a
  unique call-instance id (the modem call uid where the stack
  exposes one, else a provider-minted per-call instance token
  recorded at first sighting) and MUST carry a `recorded_at`
  stamp; the file is DELETED on the call's `disconnected`
  transition and on clean provider shutdown, and a stale file —
  older than the line's `max_call_duration_sec` ceiling (or a
  bounded 24 h default when unset), or whose call-instance id
  does not match the live object under the same path — is
  rejected at rehydrate time and treated as absent (rule (b)
  applies). Without that binding a leftover `lease_id → call1`
  entry could silently re-bind yesterday's lease onto today's
  unrelated call that happens to reuse `call1`;
  (b) when the file is absent/cold, the kernel applies the 1:1
  ambiguity rule — a snapshot call with `lease_id:null` on a line
  holding EXACTLY ONE orphaned lease binds to that lease (at most
  one call exists per line); an ambiguous or zero-match snapshot
  falls back to the `external_call` fact.
- Provider-side teardown commands are idempotent: a Hangup racing a
  remote teardown may surface as "method doesn't exist" — that is
  treated as already-gone success, verified via GetCalls/HangupAll.
- Missed-call path emits `event.missed_call{reason}` — `voicemail_ref` removed (round 27): no producer ever minted it.

### §5.5 Audio attach semantics

When a call reaches `alerting`, the bridge starts pumping downlink
frames to the lease owner and accepts uplink frames, so early
media/ringback is audible. `audio_ready` is the "you may speak"
gate: the first uplink word sent before it can be truncated by
Bluetooth SCO bring-up lag. It signals ONLY that the local SCO path
is up - it does NOT mean the remote party answered (round 11).
The gateway derives `event.audio_ready` from the provider's one-shot
`provider.audio_ready {line_id, assignment_id, lease_id}`
notification (round 14) - the provider reports when its local BT SCO
path is actually up.
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
- Uplink playout (agent→phone leg): the WAN tunnel's media-UDP
  channel hands frames to the hci-agent's **uplink playout jitter
  buffer** (30-45 ms, `docs/hci-agent.md` §2) on the dongle host,
  which owns HCI SCO packet formation - 3-byte HCI SCO header with
  the negotiated `handle` (the `0x16 SCO_STATE` wire field,
  `docs/hci-agent.md`), plus the continuous 2-bit H2 sync
  counter (`0x08 -> 0x38 -> 0xC8 -> 0xF8`) rewritten on every
  outbound USB mSBC packet, including generated silence on
  underrun (round 24, review-13 H1). The SCO packet SIZE the feeder
  emits MUST match the virtual adapter's declared synchronous
  geometry — behind `/dev/vhci` the feeder IS the controller, and
  `hci_vhci` fixes the kernel-visible `sco_mtu` ONCE at adapter
  init, so the declared geometry is the line's TARGET-PROFILE
  geometry, not a negotiated value (round 32,
  SA_swe2_review_2 item 6 — replaces the round-30/31
  "proxy verbatim" rule): the feeder answers the kernel's FIRST
  `HCI_Read_Buffer_Size` (`0x1005`) LOCALLY with the line's
  target-profile geometry — `sco_data_packet_len` 60 B for the
  mSBC profile, 48 B for CVSD — because the kernel's SCO MTU was
  already fixed at init and a verbatim real-dongle answer would
  declare a geometry the virtual adapter can never re-negotiate
  (the kernel-side buffer-count fabrication hazard the round-30
  rule guarded against is sidestepped entirely: the local answer
  declares exactly the geometry `hci_vhci` registered, so
  `hdev->cmd_cnt` and the buffer counts agree with what the
  kernel was told at init). The REAL `0x1005` is STILL proxied
  through the tunnel to the physical controller like any other
  `HCI_PASSTHRU` — but for geometry LEARNING only: the returned
  `sco_data_packet_len`/ACL values drive the feeder's pacing and
  buffer validation, they are NEVER forwarded verbatim as the
  kernel's `0x0e` answer. The strict mismatch check compares the
  REAL dongle geometry against the line's required profile —
  a real answer that cannot carry the required payload length
  refuses sync setup with `SCO_ALT_UNSUPPORTED`. The declared length intersects the selected USB
  altsetting's real payload: 48 B under altsetting 2 (CVSD),
  60 B under altsetting 6 (mSBC), 60 B under altsetting 3 — the
  alt-3 mSBC fallback carries the same 60 B mSBC payload, the
  63 B HCI SCO packet (3 B header + 60 B) simply rides ~3
  consecutive 25 B isoc microframes (`hci_vhci` has no
  `btusb` quirks, so a length negotiated for a real USB controller
  would misalign the 1-frame-per-packet invariant the H2 counter
  rewrite assumes). The agent's numeric `air_mode` code (`0x03` =
  transparent/mSBC on BT Core Vol 4 Part B §2.1) is taken from the
  `0x2c` SCO-connection event, never from a string field.
- **Playout start prefill (round 25, replaces any DSP-resampling
  idea):** the uplink jitter buffer starts each burst with a
  2-packet (~2 ms) prefill; oscillator drift between the two
  endpoints (±20..100 ppm) is absorbed by the 30-45 ms buffer and
  re-synced naturally between speech bursts — a continuous
  adaptation/resampler is out of scope for v1.
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
  these endpoints, never directly to transient bluez nodes. Node
  naming (round 25): PipeWire/WirePlumber node.name uses
  underscores and no `<`/`>` — the canonical names are
  `phonebridge_uplink_<line_id>` and `phonebridge_downlink_<line_id>`
  (dots appear only in the bluez-native `bluez_input.<mac>`/
  `bluez_output.<mac>` node names which PipeWire generates, not in
  ours). The virtual nodes pin their format explicitly (round 23,
  review-12 M4): `audio.rate=16000, audio.channels=1,
  audio.format=S16LE` —
  if the air link degrades from mSBC (16 kHz) to narrowband CVSD
  (8 kHz), PipeWire resamples instead of switching the node format,
  so the wire contract `640 B = 20 ms @ 16 kHz S16_LE` frame can
  never be broken by a codec renegotiation.
- Auto-routing is suppressed with a **static WirePlumber rule**
  matching bluez5 HFP nodes (`node.autoconnect=false`), shipped by
  the package — NOT by per-call unlinking, which would leak ~50 ms
  of audio to the default sink before the service could react.
- Multi-line: endpoints are created/destroyed per lease lifecycle
  of their line; one SCO channel exists per HFP line, so concurrent
  calls on ONE line (call waiting) share the audio path — the node
  proves "line has media", the binding to a call is via
  `line_id`+`lease_id`, never via the node.
- In-band DTMF (SendTones is broken on the bus, §5.5) is owned
  ENTIRELY by the **LineProvider** (round 22, review-10 MEDIUM-1 -
  it owns the uplink endpoint): it synthesizes the PCM tones and
  mixes them into `phonebridge_uplink_<line_id>`, substituting them
  for the agent's uplink PCM for the tone duration (round 20) so a
  tone never races the agent's audio stream; tone ~70 ms, pause
  ~50 ms, `,`/`w`/`W` = ~500 ms pauses. On the ROUTER side the
  substitution is a PAUSE, not a consumption (round 32,
  SA_swe2_review_2 item 8): the provider's `0x05` DTMF-START frame
  pauses the egress 20 ms pacer for the duration of the
  synthesis — agent `0x01` frames keep being read at socket speed
  and buffer in the uplink ring, they are neither consumed nor
  muted. The `0x02` flush path is
  SPLIT: the LineProvider emits the `0x02` frame on its provider
  media socket after a DTMF segment, and the MediaRouter — which
  holds the bounded per-lease uplink ring buffer — drops the
  still-queued frames the watermark names on receipt. That
  split keeps the coordination explicit: pausing the provider feed
  without the router-side drain would replay 2-3 s of queued
  pre-DTMF agent speech — audio the provider never consumed —
  delayed after the tones.
  **Watermark semantics (round 25; clock domain fixed round 28):**
  the post-DTMF `0x02` frame is timestamped — the MediaRouter
  discards only queued uplink frames whose `timestamp_ms` ≤ the
  flush's `timestamp_ms`, so TTS chunks the agent submitted AFTER
  `call.send_dtmf` returned survive the flush;
  a timestamp-less drain would silently eat fresh speech. The
  flush's `timestamp_ms` rides the AGENT'S UPLINK STREAM clock
  (round 28): the LineProvider stamps the `timestamp_ms` value of
  the last agent `0x01` frame it consumed before it started DTMF
  synthesis (the `0x05` pause) — never a
  router-local wall-clock stamp, so the two clocks need no
  alignment for the comparison to be meaningful. **"No frame
  consumed yet" rides the `0x05` DTMF-START frame, not the
  watermark (round 31, SA_swe2_review_1 item 34):** the provider
  announces the pause with a 12 B `0x05` control frame carrying
  `pre_dtmf_consumed` — the stamp of the last consumed agent
  frame, or the reserved `0xFFFFFFFF` (`none`) when none was
  consumed yet, which makes the trailing flush discard every
  frame that was queued BEFORE the `0x05` arrived (all unheard
  by construction — frames appended during or after the pause
  survive). `timestamp_ms = 0` on a `0x02` is a REAL
  watermark — the first stream frame legitimately stamps `0` —
  never a sentinel; the `0xFFFFFFFF` discard-all encoding is
  shared with the agent-side barge-in `0x02`, which occupies a
  different frame's `timestamp_ms` slot and cannot collide with
  `pre_dtmf_consumed`.

### §6.5 Remote-line audio transport (round 12)

Between the hci-agent and the container, SCO rides the **media-UDP**
channel of the line's tunnel profile (isochronous semantics: stale
frames are dropped, never retransmitted; the `loopback` profile
substitutes a CONNECTED `SOCK_STREAM` Unix socket at
`/run/phonebridge/media/<line_id>.sock` with the peer verified at
`accept()` — `docs/hci-agent.md` §2); control flows on
control-TCP. A QUIC/multiplexed-streams unification is a V2 track.

### §6.6 MediaRouter pacing and buffer policy (round 23 ADR-A; pacing direction corrected round 24)

- **Uplink (agent → phone):** the MediaRouter reads the agent's
  media socket CONTINUOUSLY at socket speed (non-blocking drain —
  the reader loop NEVER sleeps on a timer, and it NEVER stops
  reading under saturation either — round 31, SA_swe2_review_1
  item 8: the kernel socket buffer is never used as a backpressure
  lever, because an unread socket would strand the `0x02`/`0x03`
  control frames multiplexed on the same stream behind PCM noise).
  Each `0x01` frame is
  appended to a bounded, asymmetric uplink ring of **1500 frames
  (30 s), drop-NEWEST** on overflow; `0x02` flush and `0x03`
  keepalive frames are processed normally even while the ring is
  full (a `0x02` drains it IMMEDIATELY on receipt) — only `0x01`
  PCM pays for saturation: the NEWEST arriving frame is dropped
  and frames already queued are never evicted. Pacing applies strictly on EGRESS: a
  dedicated pacer task steps on a monotonic 20 ms schedule and
  writes ONE 640 B frame per tick from the ring into the
  LineProvider socket. Pacing the socket READ instead (round 24,
  review-13 C1) would strand an agent-originated `0x02` barge-in
  behind hundreds of queued audio frames inside `SO_RCVBUF` for
  seconds while the router ring idles near-empty — so the drain
  side is deliberately unthrottled. Each dropped frame increments
  the `media.uplink_overflow` counter (plus a
  warning log at first occurrence per lease) — the counter exists
  so the "agent wrote 30 s of audio in 100 ms" bug class is
  observable instead of invisible latency. **Saturation signals
  are per-EPISODE, not per-frame (round 31):** a contiguous
  overflow episode emits exactly ONE `0x04 OVERFLOW` control frame
  back to the producer plus ONE `event.media_overflow{lease_id,
  line_id}` on the agent event channel — a new episode begins only
  after the ring has drained and a further `0x01` is dropped; the
  kernel learns the episode through the media adapter's
  `ingest_media_overflow(line_id, lease_id)` ingress (round 32 —
  the ONLY `event.media_overflow` source; the `provider.audio_lost`
  SCO-flap path never produces it and never arms the pair TTL). The
  producer contract: an agent MUST NOT stream faster than
  real-time minus jitter headroom — a TTS preload burst paces
  itself to ≤1 `0x01` frame per 20 ms once it sees `0x04`.
- **Downlink (phone → agent):** frames are written through
  IMMEDIATELY (no pacing) through a bounded ring of **100 frames
  (2 s), drop-OLDEST** — a slow reader hears the newest audio, not
  a growing backlog of stale speech.
- **`0x02` is bidirectional and consumed at the router, never
  bridged:** both roles may send `0x02` on the uplink media socket.
  An agent-originated `0x02` (the in-band `clear_uplink_buffer`/
  `flush_audio_out` barge-in flush) drains the router's 1500-frame
  uplink ring and is consumed there; a provider-originated `0x02`
  (provider's own post-DTMF path) is likewise consumed by the
  MediaRouter and MUST NEVER be forwarded to the agent — there is
  no `0x02` travelling toward the agent on downlink. Agent-side
  DTMF itself travels exclusively via `call.send_dtmf`, and the
  LineProvider substitutes the tones into the uplink stream
  locally. The `0x02` watermark clock domain is the sender's stream
  clock (round 28 — see §6.4): the provider-emitted flush carries
  the `timestamp_ms` of the last agent `0x01` frame the
  LineProvider consumed before the DTMF pause, so the router drops
  exactly the frames the provider never heard — frames buffered
  during or after the `0x05` pause survive the flush. Discard scope
  (round 29, review-1 B7): the drop applies to queued uplink frames
  with `timestamp_ms` ≤ the watermark that have NOT yet been
  delivered to the LineProvider socket — frames still inside the
  router's egress ring; a frame already written to the provider is
  unrecoverable by design, and a buffered frame stamped above the
  watermark survives the flush.

## §7. Events and universal fan-out

Event delivery is **universal** (any agent type, not just Devin) and
implemented by an `EventBus` module inside the gateway daemon.
Three primitives only - no broker in v1:

1. **Socket subscribe** - broadcast to live subscribers on the
   agent/control socket.
2. **Webhook** - HTTP POST per event type with retry, idempotency
   `delivery_id`, and HMAC signature. Retry budget is bounded by
   the event's own deadline (round 23, review-12 M7): for
   `incoming_call`-class events retries MUST stop at
   `expires_at - 1 s` — a wake-up POST landing after the accept
   window closed only generates noise. Webhook egress is hardened
   (round 25): every POST carries an explicit connect/total timeout
   (default 2 s/5 s), HTTPS only for remote hosts, and an SSRF
   egress guard — configured URLs resolving to loopback/link-local/
   RFC-1918 are rejected at admin-config time unless the admin set
   `allow_private_webhooks:true` on the hook, AND the check is
   repeated on the RESOLVED IP at connection time for every POST
   (round 28 — DNS rebinding: a hostname that resolved publicly at
   config save can re-resolve to a private address by delivery
   time, so the config-time check alone is insufficient; the
   connect path pins and validates the actual peer address).
   **Pinning is the atomic part (round 29, review-1 B10):** the
   guard MUST pin the connection to the resolved+validated IP at
   connect time — the TCP/TLS connect targets the CHECKED address
   with `Host`/SNI still naming the original hostname; a second
   resolution between the check and `connect()` is FORBIDDEN, so
   there is no resolve-check-connect window for a short-TTL
   DNS-rebind TOCTOU to slip through. **No redirect following
   (round 30, review-19 B2a#2 — the picked rule):** the webhook
   HTTP client MUST NOT follow `3xx` redirects AT ALL — any
   `301`/`302`/`307`/`308` response is a DELIVERY FAILURE fed into
   the normal bounded-retry budget, never a new request. Following
   a redirect would re-enter the whole validate-resolve-pin
   pipeline per hop — a chain `public → 127.0.0.1` would sail
   straight into host loopback under `--network=host` past every
   pinned check — so the fail-closed rule is to treat a redirect
   as a dead endpoint rather than to re-validate a moving target.
   The
   gateway itself
   binds only loopback/wg0; a webhook is the one daemon-originated
   egress an operator can point anywhere.
3. **Exec / file-drop** - thin hook: run a command or drop a file
   per event type (wake mechanisms for sleeping agents). Exec
   hooks are hardened (round 23, review-12 M7; limits round 25):
   the configured argv is execve'd DIRECTLY — never through a
   shell (`sh -c` is forbidden, so hook config cannot smuggle
   command injection); the event JSON is delivered on the child's
   stdin, never in argv/environment (keeps caller PII out of
   `/proc/*/cmdline` and the process table); every hook run carries
   a hard process timeout (default 10 s, admin-cap 60 s), a
   per-hook concurrency cap of 1 queued run (later triggers
   coalesce), and cgroup-less memory discipline via a bounded
   stdin payload (≤64 KiB). **Coalescing never loses a call
   (round 30, review-19 B2a#14):** the queue is keyed per
   `(hook, line_id)` and holds the LATEST complete payload for
   that key — a second trigger on the same line replaces the
   still-queued payload with the newest event wholesale, so the
   run always fires with current data; payloads for DIFFERENT
   `line_id`s are separate queue entries and NEVER coalesce into
   each other, so two simultaneous rings on two pool lines always
   produce two hook executions. A same-line `incoming_call` then
   `missed_call` burst coalesces to the freshest payload — by
   design: the newest event is the actionable one, and the
   wake-up semantics of these hooks only need "something happened
   on this line" to reach the sleeper once. Child `stdout`/`stderr`
   are CAPTURED, byte-bounded and PII-scrubbed before they can
   reach the daemon log (§10): a hook that echoes its stdin JSON
   to stderr must never write `caller_id` into the service log —
   captured output is truncated at a small cap and dropped from
   INFO-level logs entirely (diagnostic surface only, opt-in).
   File-drop hooks write `0600` files into
   a tmpfs-backed `0700` spool dir owned by the `phonebridge`
   group, with a
   TTL sweep (default 5 min) so stale wake files never pile up
   (tmpfs keeps wake payloads — which can carry caller PII — off
   durable storage entirely, round 28).

Audience routing (round 23): `event.idle_alert` is `admin`-audience
only — socket fan-out delivers it to sessions holding the `admin`
scope plus configured escalation channels; ordinary agent sessions
never receive it. `event.fallback_triggered` remains
`system:fallback_worker`-only (§5.3). Everything else follows the
existing owner/broadcast rules.

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
| **Prod** | ONE OCI container, **`--network=host`** + `/dev/vhci` pass-through on a dedicated host/VPS (round 25 — `AF_BLUETOOTH` lives only in `init_net`, isolated netns is impossible): gateway daemon + per-line worker bundles + BlueZ/PipeWire/dbus inside; every adapter is kernel `vhci` fed by an hci-agent — host-local agents feed dongles on the same machine too; agents via bind-mounted socket dir (UDS 0660, `phonebridge` group) or WSS/MCP bound to `127.0.0.1`/`wg0`; host `bluetoothd` masked, host firewall owns the perimeter |
| **Prod (hardened, optional)** | the same OCI image inside a guest micro-VM (Kata/QEMU) for shared workstations where the host's own Bluetooth must stay alive — deferred past MVP, §2.1 |
| **Prototype** | Host-native systemd user service - capability validation ONLY, never prod (`org.pipewire.Telephony` lives on the USER session bus - a system-level unit cannot reach it and fails silently with `ServiceUnknown`) |
| **Remote site** | Raspberry Pi etc. runs ONLY the hci-agent (USB↔HCI relay, no bluetoothd); control-TCP + media-UDP over a `transport_profile` tunnel — the contract vocabulary is `loopback` / `direct_mtls` / `wireguard` (round 32 — `loopback` covers host-local agents feeding the same machine's vhci feeder; `sstp`/`openconnect`/`vless_xray` stay reserved deployment profiles outside the contract enum; V1 default is per deployment, not per line); heartbeat + remote USB reset; in-flight call on tunnel drop = hangup |
| **Agent host (any)** | One hci-agent instance per dongle; owns USB via libusb with `btusb` detached; host `bluetoothd` unaffected |

Single-writer applies twice: (a) lease/assignment arbitration inside
LineManager, (b) exactly one gateway instance per deployment
(socket path / listen port held exclusively, refuse to start if
held).

D-Bus split and restart reconciliation (round 20, detailed round
20b): the container runs TWO buses. `bluetoothd`/oFono ride the
SYSTEM bus; PipeWire + WirePlumber (`org.pipewire.Telephony`) require
a SESSION bus with `XDG_RUNTIME_DIR`. The privilege boundary inside
the container (capability matrix tightened round 23, review-12 M5):
`CAP_NET_ADMIN`/`CAP_NET_RAW` are held by exactly TWO processes —
`bluetoothd` (needs them for HCI socket management) and the
`wg-supervisor` (needs them to bring the tunnel interface up) — and
by NOTHING else. The `vhci-feeder` runs with ZERO capabilities: it
reaches `/dev/vhci` purely through group access (a dedicated
`vhci` group owning the device node), not through caps, and binds
its media/control sockets exclusively to the `wg0` interface IP —
its attack surface is reachable only through the authenticated
tunnel. `pipewire`, `wireplumber`, the N `LineProvider` workers and
the `gateway` all run as the unprivileged `phonebridge` user
sharing one `XDG_RUNTIME_DIR` and one session D-Bus — with an
explicit system-bus policy granting UID `phonebridge` access to
`org.bluez` (and nothing else privileged). Restart reconciliation protocol: on ANY bus
reconnect or name-owner change of `org.pipewire.Telephony` /
`org.bluez`, every affected `LineProvider` MUST perform a full
`GetCalls()` reconciliation and report
`provider.call_state{state:"disconnected", reason:"stack_restart"}`
for every call that no longer exists on the bus. The reconciliation
is BIDIRECTIONAL (round 20b) with a DETERMINISTIC unknown-call rule
(round 21): a call found on the bus that the kernel does not know is
NEVER silently left ringing and NEVER force-hung-up — an unknown
call in `incoming` state is fed to `ingest_ring` like any physical
ring; an unknown call in `dialing`/`alerting`/`active` state is a
human-initiated EXTERNAL call (the owner dialed or answered on the
handset itself): the line goes `BUSY` with reason `external_call`,
no agent lease is minted and no PipeWire bridge is attached (audio
stays on the phone), and the gateway sends NO `Hangup` — the line
returns to `FREE` when the provider reports the call gone.
**Attribution evidence rule (round 32, SA_swe2_review_2 item 10):**
an update carrying NEITHER `call_token` nor `lease_id` on a line
NOT in `RINGING_LOCKED` is correlated against the line's live
leases BEFORE the external mint — with EXACTLY ONE live
candidate (e.g. a `DIALING` lease whose provider omitted the
`lease_id` echo) every attributable field the update carries must
AGREE with the lease's evidence: `provider_call_id` vs the
lease's recorded handle, `caller_id` vs the dialed number, and
the direction implied by the lease's phase. A field ABSENT on
either side is unverifiable and never blocks correlation, but a
field PRESENT on both sides that DISAGREES is conflicting
evidence: the kernel correlates NOTHING, mints the
`external_call` hold for the call the update truly describes, and
leaves the unmatched lease on its normal deadline/orphan-grace
expiry — never ended early by evidence it cannot answer for. Two
or more live candidates with no token make the update
unattributable and it is likewise minted `external_call` rather
than guessed. The
symmetric handset-answered-ring case (round 28): an `active`
update carrying a `call_token` on a still `RINGING_LOCKED` line
means a human answered the ringing call on the physical handset
before any `call.accept` burned the token — the ring closes
WITHOUT minting a lease and the line goes `BUSY{reason:
"external_call"}` exactly like a dialed external call (no lease,
no bridge, no `line.hangup`); it clears on the later
`disconnected` report. Each
LineProvider re-resolves its own line's deterministic D-Bus chain
(`adapter_mac →
Adapter1.Address → Device1.Adapter → phone_mac → bluez_*` nodes,
§1 - the kernel has no D-Bus visibility; the provider owns the
oFono view for its line), the gateway rebuilds PipeWire links for
surviving leases, and tears down
the reported-gone ones — a restarted stack never leaves phantom
lines, links, or leases behind.

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
    escalation channels;
  - kernel-side the admin surface binds to the narrow
    `LineAdminKernel` interface (round 19; `assign_line` added
    round 22; line lifecycle and listings added rounds 27-28):
    `provision_line`/`assign_line`/`approve_claim`/`reject_claim`/
    `revoke_assignment`/`configure_line`/`remove_line`/
    `get_line`/`list_lines`/`list_leases`/`list_tickets`/
    `list_pending_claims`/`list_assignments`/
    `list_provider_bindings`, plus the store-facing
    `hydrate_lines`/`restore_line` pair (§4.5) — every listing is
    paginated at `MAX_LIST_PAGE` 64 rows per page (round 30/31,
    same 64-row/64 KiB list-page bound as the agent listings) —
    the agent-facing
    `LineManager` deliberately exposes no privileged mutations.
    `admin.provision_line` (round 28) is the "create" half of the
    line lifecycle: it mints a configured, quiescent `SHARED_POOL`
    line (`claimable` default `True`) that appears immediately as
    `unbound` on the listings until a provider attaches —
    `configure_line` could only patch an existing row;
  - **headless pairing** (round 19): an in-container BlueZ
    `org.bluez.Agent1` rejects all pairing by default;
    `admin.pair_window {line_id, duration_sec}` opens `Pairable` on
    one adapter (by `adapter_mac`) and auto-confirms SSP within the
    window — no `docker exec bluetoothctl` per line.
- **ALL timeouts and windows are admin-configurable** (round 10a):
  accept window (default 30 s), queue grant TTL,
  heartbeat liveness thresholds, orphan grace, `idle_revoke_after` —
  `clear_idle_revoke_after:true` in the PATCH disables the idle
  clock entirely (round 25; an absent `idle_revoke_after` key keeps
  the current value — PATCH-without-tombstone ambiguity closed via
  the explicit flag, both keys together → `INVALID_ARGUMENT`).
  The kernel surface for all of them is
  `admin.configure_line{..., idle_revoke_after?,
  clear_idle_revoke_after?, timeouts?}` where `timeouts` carries
  `{grant_ttl_sec?, orphan_grace_sec?, heartbeat_interval_sec?,
  heartbeat_degraded_after?, heartbeat_down_after?,
  fallback_accept_ttl_sec?, ring_drain_ttl_sec?,
  max_call_duration_sec?, clear_max_call_duration?}` (round 22;
  `fallback_accept_ttl_sec`
  added round 23, ~10 s default, bounds the FallbackWorker accept
  deadline; `ring_drain_ttl_sec` ~60 s default bounds the
  ring-drain wait of §5.2 step 6; `max_call_duration_sec` — round
  25 — caps any single call's wall-clock length, default off, ≤
  86400 s when set, and bounds `record_and_telegram` recording
  length). All timeout values are finite numbers (`NaN`/`Infinity`
  rejected at the boundary) with sane upper bounds per field (round
  25). `heartbeat_interval_sec` is floored at 0.001 s
  (millisecond granularity — the wire unit is ms) and stamped as
  `heartbeat_interval_ms = int(sec * 1000 + 0.5)`, min 1 (round 24c;
  halves up — Python `round()` is banker's rounding, round 27).
  Changing `heartbeat_interval_sec` NEVER kills a live
  provider session (round 23, review-12 M6): the new interval is
  negotiated at the provider's next
  `provider.reregister`/`provider.register`, so reconfiguring a
  line mid-call cannot drop the call over a heartbeat window
  mismatch.
- **`admin.remove_line` refuses a forward target (round 30,
  review-19 B2a#13; quiescent-only pinned round 31):** removing a
  line that ANOTHER configured
  line's `forward_via_line` policy still references is rejected
  `INVALID_STATE` — a dangling policy would only surface as a
  `LINE_NOT_FOUND` at ring time, long after the misconfiguration
  was made. Deterministic reject was chosen over cascade-clear:
  silently rewriting another line's `fallback_policy` inside a
  delete would hide an admin's intent behind a side effect; the
  operator retargets or clears the referencing policy first
  (`configure_line`), then removes the line. The same
  `INVALID_STATE` refusal covers EVERY live attachment — a live
  `ProviderBinding`, any `CallLease`, pending tickets or pending
  claims (round 31, SA_swe2_review_1 item 6): `remove_line` never
  mints a `provider.revoke` or an implicit teardown, so the
  operator detaches through the existing revoke/hangup paths and
  retries; only an OWNED-but-quiescent line emits an
  `assignment_revoked` to its owner before the durable
  `LineAssignment` is deleted (§4.5 write-through ordering).
- **Escalation channels** (round 10c): admin-configured (e.g.
  Telegram) for idle/broken-line alerts; `idle_revoke_after` fires
  `event.idle_alert` (admin-audience, §3.6/§7) - alerts only,
  resolved question `idle_revoke` (§12).
- **Per-line dial policy** (round 12): `allowed_number_types`
  defaults to `{FIXED_LINE, MOBILE, FIXED_LINE_OR_MOBILE, VOIP,
  TOLL_FREE}` — the composite `FIXED_LINE_OR_MOBILE` is mandatory
  because `phonenumbers.number_type()` returns it for NANP numbers
  (round 15); an admin may
  additionally enable `{PREMIUM_RATE, SHARED_COST, PAGER,
  PERSONAL_NUMBER}`. `line_home_country` (ISO alpha-2, round 31;
  display scope pinned round 32, SA_swe2_review_2 item 22) is the
  line's display/caller-id normalization region ONLY — it renders
  numbers for admin/diagnostic surfaces; `call.dial` still
  requires its explicit `country` parameter and no dial input is
  ever normalized from this field. Short codes and
  emergency numbers are rejected on the ordinary path.
- **Per-line transport profile** (round 12; vocabulary pinned to
  the contract round 32): the admin selects `transport_profile`
  from the closed hci-agent tunnel vocabulary `loopback` |
  `direct_mtls` | `wireguard` for each line — `loopback` is the
  in-container default for host-local agents; `sstp`/
  `openconnect`/`vless_xray` remain reserved deployment profiles,
  not contract spellings; protocol
  parameters are canonical system packs, not user-editable. Line
  onboarding lifecycle (dongle-level FSM — a separate vocabulary
  from `LineState`/`LivenessState`): `PROVISIONING → PAIRING →
  READY → IN_CALL → LINK_DEGRADED → FAULTED`.
- **Exclusive assignment requires moderator approval** in admin -
  an agent's claim request is never self-granted (round 10d).
- Webhook events are authenticated via HMAC + `delivery_id`
  idempotency (§7).
- **Control-plane wire framing (round 25):** the JSON-RPC API runs
  over WebSocket on BOTH legs — `ws://` on the UDS control socket
  and `wss://` on the loopback/wg0 listener — so message boundaries
  are protocol-owned and equal on every transport; per-message cap
  64 KiB. Error envelopes follow JSON-RPC 2.0 (round 25):
  `error.code` is the integer from `ERROR_CODE_WIRE` (application
  band `-32099..-32000`), `error.message` carries the `ErrorCode`
  string (`UNAUTHORIZED`/`FORBIDDEN`/`SESSION_NOT_FOUND` included),
  `error.data` is the `ErrorData` JSON `{reason?, detail?,
  free_lines?}` — see `docs/api-contract.md` error map.
- **Media file quotas and lifecycle (round 30, review-19 B2a#3):**
  recorded audio lives ONLY on the dedicated
  `/var/lib/phonebridge-media` volume (§2.1 — separate mount from
  the SQLite WAL volume, so recording `ENOSPC` can never stall an
  admin write-through) and is bounded end to end:
  `max_recording_sec` (admin-configurable, default 300 s) caps a
  single `record_and_telegram` recording INDEPENDENTLY of
  `max_call_duration_sec` — the call may continue past it, the
  file stops; a per-line storage cap (default 100 MiB) bounds
  accumulated recordings plus that line's pending spool, with
  oldest-first eviction and an immediate `ENOSPC`-equivalent
  refusal path on a full volume (record fails closed, call and
  config writes unaffected); sent recordings are deleted right
  after a confirmed Telegram send (delete-after-successful-send —
  the spool entry, not the audio, is the retry unit); unsent
  spool entries carry a bounded retention TTL (default 72 h) with
  sweeper expiry + an admin alert on drop, so a permanently dead
  endpoint cannot accumulate voicemail forever; and the spool
  itself is file-per-entry `{file, channel, attempts,
  next_retry_at}` under `0600`/`0700` — audio is PII and obeys the
  same filesystem discipline as the wake spool.
- Secrets/PII (tokens, caller numbers) must never reach logs:
  `CallToken`/`MediaToken` travel inside `RedactedString`-style
  containers that refuse `str()`/`json.dumps` without an explicit
  `.expose()`, and `safe_dump()` masks them in every structured
  log line (round 25 — a `NewType(str)` token was a log leak).
  **Phone numbers get the same treatment (round 30, review-19
  B2a#12):** `caller_id`/`number`/`target_number` are PII in
  EVERY surface — wrapped/masked in event models and structured
  logs like the tokens, carried masked through exec-hook handling
  (the child sees the full JSON on stdin because it needs it;
  the child's captured `stdout`/`stderr` is byte-bounded and
  PII-scrubbed before the daemon log can record it — §7), kept
  OUT of `DIAL_REJECTED`'s `data.detail` (the typed `reason`
  says WHAT failed; the rejected number is not echoed back into
  an error string that lands in client logs), and masked in
  `phonebridgectl` output by default (an explicit `--reveal`
  flag is required to print raw numbers).

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
  register/ping/reregister, `LivenessState` transitions
  (`reachable` -> `ws_disconnected`/`unreachable`), media-frame
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
   audio` hold, incl. the isochronous endpoint forwarding? RESOLVED
   2026-11-13 — the one-dongle experiment PASSED (issue #29, verdict
   GO): live outgoing call to `active`, ~8k bidirectional SCO frames,
   zero kernel unknown-handle errors after feeder gating. The four
   protocol invariants it exposed are locked in
   `docs/hci-agent.md` §2.

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
4. **M4 - Line bundle: vhci-feeder + shim/audio-forwarder +
   LineProvider + Fake-D-Bus harness** (round 13 topology): per-line
   kernel `vhci` adapter fed by the line's `hci-agent`
   (control-TCP/media-UDP per `docs/hci-agent.md` §2), `adapter_mac`
   binding (ADR-03), D-Bus adapters
   (`org.ofono.VoiceCallManager` / `org.pipewire.Telephony`), PipeWire
   node <-> media socket PCM pump with owned routing
   (`node.autoconnect=false`, §6.4). Tests on isolated
   `dbus-daemon --session` + PipeWire null-sink; the vhci feeder is
   loopback-testable without a dongle.
5. **M5 - FallbackWorker + phonebridgectl + MCP admin + Docker**:
   internal system agent for worker fallback policies (ADR-01);
   admin CLI and private MCP server; single OCI image run with
   `--network=host` + `/dev/vhci` on a dedicated host (round 25 —
   §2.1; supervisord: gateway + per-line bundles + shared
   dbus/bluez/pipewire; hci-agents run OUTSIDE the container, one
   per dongle) with mandatory volumes `/var/lib/bluetooth` +
   `/var/lib/phonebridge` + `/var/lib/phonebridge-media` (all
   `0700` — three volumes, round 30/31; the media volume rides a
   SEPARATE filesystem from the SQLite volume, §1 persistence), plus the `phonebridge
   doctor` pre-flight check in the entrypoint.
