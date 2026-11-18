# Changelog

## [0.0.28] - 2026-11-18

### User-facing

#### fix
- The provider command handshake is now a first-class, deadline-bounded kernel fact: call.dial/call.accept replies wait for the bounded line.dial/line.answer acknowledgement and answer PROVIDER_UNREACHABLE on refusal or timeout, rings carry an answer_pending state that never re-enters the fallback decision, unqualified provider call updates need positive identity evidence before binding to a live lease, zero-subscriber rings emit a typed wake-audience envelope to configured hooks, provider stamp watermarks are durable across restarts, and every listing accepts the opaque cursor input instead of offset. Review fixes additionally pin that the wake-audience incoming_call envelope carries a dedicated tokenless wake payload with no call_token, that pending-command teardown clears its deadline atomically and a late acknowledgement is stale, that a same-key retry of an in-flight request joins the original operation instead of re-dispatching, that an answer acknowledgement never carries provider_call_id, and that the accepted country vocabulary is the phonenumbers-supported region set (including telephony extensions like XK, AC and TA), not strict ISO 3166-1. `domain` (issue #81, iteration-1 findings registry)

### Codebase

#### domain
- [fix] ProviderCommandAck + ingest_command_ack + pending_command/ provider_command_ack_deadline_at + provider_command_ack_sec + WakeAudience + durable binding watermarks + caller_id positive evidence + adapter-owned cursor + media_epoch + provisioning validation hardening + version-tag/spec-anchor tool fixes. PR #82 review round: IncomingCallWakeEvent tokenless wake projection + EventEnvelope audience/payload split enforcement, dial-only provider_call_id on ProviderCommandAck (answer acks reject any provider handle, reason stays refusal-only), atomic pending_command/provider_command_ack_deadline_at teardown on every DIALING/ANSWERING exit path with stale-ack no-resurrection semantics and lease- vs ring-scoped teardown hangups, in-flight idempotency join-before-dispatch semantics, and corrected phonenumbers-region vocabulary for country fields (XK/AC/TA accepted, ZZ/AA still rejected). Why: Iteration-1 architecture audit (gemini-4-carbon, 33 findings) confirmed contract drift across the ack handshake, ring correlation, watermark persistence, wake fan-out, pagination, and release tooling; the fix re-freezes the contract at 0.0.28 with executable GWT coverage rather than implementation-only patches. (issue #81, iteration-1 findings registry)

## [0.0.27] - 2026-11-17

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [fix] Round-32 audit fixes (SA_swe2_review_2, 27 findings triaged, five fix batches across contract/tests/api/design/hci/tools): hangup gains the TERMINATING leg - call.hangup on an in-flight phase parks the lease until the provider's disconnected or the hangup_hold_sec lapse (default 10 s, bound 1..120) ends it network_error kernel-side, only ACQUIRED grants end directly; the outbox fan-out mints ONE FanoutAudience envelope per multi-session delivery and tick deadline processing is chunked under MAX_TICK_DEADLINES with next-tick continuation (the 256-deadline livelock fix); durable writes ride a pending-write marker with bounded admin waits and a guarded restore_line that degrades to config_stale + admin alert when the record was re-mutated instead of blindly restoring a stale snapshot; ensure_aware_utc bounds stamps absolutely (now +- window) so an RTC-less provider can never poison dedup keys or deadline arithmetic; line.answer failure rides the ring-scoped compensate_answer(line_id, assignment_id, lease_id, call_token) entry (hangup-on-call_token reject vehicle, ANSWERING lease ends network_error, line stays RINGING_LOCKED until drain) - distinct from lease-scoped hangup-first compensate_dial, which now dedupes against TERMINATING; external_call attribution is evidence-agreement based - a present-and-conflicting provider_call_id/caller_id/direction mints a NEW external_call record instead of guessing; provider.audio_lost is latch bookkeeping only (it never arms MEDIA_PAIR_TTL_SEC while the socket is attached and never produces event.media_overflow - that event is minted solely by ingest_media_overflow); provider-loss teardowns unify on provider_lost and a dead or tombstoned binding receives NO provider commands at all; listings carry has_more/cursor and admin.list_keys paginates like every other admin list; provider_bindings rows are a best-effort cache - a failed write is config_stale + admin alert with no kernel compensation (restore_line is LineAssignment-only), last_seen_at persists debounced at >=30 s, stamp watermarks persist on the row, channel_seq reseeds max(stored)+1 across restarts, and the hydrated-dead cascade suppresses external escalation hooks; the queue re-check trigger set is pinned (lease end, provider register, capability update, claim/assignment release, enqueue, tick expiry) and QueueTicket.intent is model-validated to DIAL_OUT; idempotency records expire at IDEMPOTENCY_TTL_SEC=3600 under a 256-entry per-session LRU and a same-key different-params retry is IDEMPOTENCY_CONFLICT, while register_intent returns the existing session only for an identical (agent_id, intents, profile) triple on the same socket; provider_call_id threads from line.dial's reply through DialResult to the ring-identity/evidence checks; announcement_ref rejects '..' traversal and leading '/' via the _CheckedAnnouncementRef grammar; all bounded strings/tokens reject surrogate and control characters and MediaCredentials mints two DISTINCT role tokens; a CallLease carries grant_expires_at only while ACQUIRED (the consumed deadline is None on every later phase); the api drift test is un-exempted onto the admin.* cells, ProviderRegistration.meta mirrors immutably onto the binding, and orphan_deadline_at reaches the admin projections; docs/hci-agent.md pins loopback media to a CONNECTED SOCK_STREAM UDS at /run/phonebridge/media/<line_id>.sock with SO_PEERCRED at accept() (feeder listens, agent connects, uint16 rec_len framing), rewrites MTU alignment - hci_vhci fixes sco_mtu at adapter init so the feeder answers the kernel's first HCI_Read_Buffer_Size LOCALLY with target-profile geometry (60 mSBC / 48 CVSD) while the real 0x1005 is still proxied for geometry learning only, and restricts the agent-originated H4 allowlist to 0x02/0x03/0x04 (0x01 commands flow only kernel-to-agent); tools hardening - check_version_tag verifies the REMOTE tag marks the remote main tip and that main's pyproject carries the same version (a merge without version bump can no longer pass against the previous tag), check_spec_anchors --base requires per-FILE coverage (every changed docs/specs/*.md file needs at least one in-diff anchor), and release_notes unquotes multiline quoted scalars correctly and passes generated notes through a temp file instead of the GENERATED env var (MAX_ARG_STRLEN). Why: The swe-2 second-pass audit (SA_swe2_review_2, issue #79) found a hangup path that ended leases before modem teardown was confirmed, write-through compensation that could revert newer kernel state with a stale snapshot, provider-binding persistence treated as a second source of truth, provider-loss reasons split across two spellings, a kernel-side SCO MTU answer that proxied geometry the virtual adapter could never renegotiate, and tooling that verified tags against local ancestry only - plus a run of smaller contract/test drift; all are closed ahead of the M1 implementation milestone so the Phase-0 contract, its spec, the wire docs and the tool gates are unambiguous. (issue #79, SA_swe2_review_2)

## [0.0.26] - 2026-11-17

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [fix] Round-31 audit fixes (SA_swe2_review_1, 40/40 accepted, five fix batches across contract/spec/design/hci/tools): domain contract gains CallPhase.ANSWERING (call.accept mints the lease in the internal answer-in-flight phase, covered by max_call_duration, no wire spelling), the adapter-scoped compensate_dial(line_id, assignment_id, lease_id) saga entry (hangup-first ProviderHangupCommand, lease ends network_error, no fabricated provider.call_state fact), and the ingest_audio_lost kernel ingress (audio_ready latch reset in kernel-side media state); restore_line gains the line_id parameter and returns verbatim tombstone-aware compensation; provider_bindings becomes a persisted table and hydrate_lines loads it DEAD (ws_disconnected, expired orphan_deadline_at, first-tick orphan cascade unless a fresh provider.register supersedes; channel_seq store backfill generation 1); LineAdminKernel gains get_line, revoke_assignment returns bool, and every listing is paginated at MAX_LIST_PAGE=64 under the 64 KiB page cap; provider_call_id is the exact ring-identity key with RING_DEDUP_MIN_GAP_SEC (30 s) as the best-effort (line_id, caller_id) fallback; the advisory-stamp watermark splits clamp-vs-drop (sub-watermark ping applied clamped, sub-watermark state fact dropped as duplicate); tick runs the orphan cascade atomically (leases network_error, tickets queue_cancelled provider_lost, claims auto-reject, ring and external_call reset, binding tombstone; heartbeat-down suspended while ws_disconnected); uplink overflow is a single always-read policy (0x02/0x03 processed normally, drop-newest PCM only, one 0x04 plus one event.media_overflow per contiguous episode); the admin.* wire surface realigns (hardware fields on provision_line/configure_line, list_keys, revoke_assignment bool, hooks no longer mint call_token); secure_compare owns every AUTH-decision comparison and free-form fields get the control-character/media_endpoint scheme gate; cancel_acquire gets the three-class ownership oracle plus MAX_CLOSED_TICKETS and queue re-check on every eligibility-affecting mutation; MEDIA_PAIR_TTL_SEC arms at mint; the media flush ambiguity resolves onto the 0x05 DTMF-START pre_dtmf_consumed field (0xFFFFFFFF = none; a 0 watermark is a real stamp); docs/hci-agent.md pins the vhci write-then-read index discovery (feeder writes [0xff,0x00] HCI_PRIMARY first, then reads the index event), corrects altsetting-3 mSBC to the same 60 B payload as alt 6 (63 B HCI packet over ~3x25 B microframes), puts loopback media on connected UDS datagram sockets under /run/phonebridge/media/ with token+SO_PEERCRED on both sockets, and allowlists agent-originated H4 types to 0x01/0x02/0x04; tools hardening - check_spec_anchors rejects a '-' noop inside a diff that touches docs/specs/*, release_notes strips only matching surrounding quotes, check_changes_entry validates --pr numeric before URL interpolation, check_version_tag makes TIMEOUT=0 a single immediate check; drift-test coverage is extended to the new wire/admin surfaces. Round-31 fix round (PR #78 review, 4 warnings + 4 minors): CALL_PHASE_FORWARD_TRANSITIONS gains TERMINATING targets on the DIALING/ANSWERING/ALERTING rows (hangup parks every possibly-live call leg in TERMINATING; ACQUIRED still ends directly); the provider.call_state params cell gains provider_call_id? covering both the IncomingRingIngress and ProviderCallStateUpdate distillations; admin.list_keys lands as a normative wire row returning only redacted {key_id, scope, subject_id, created_at, last_used_at?, revoked} metadata - never key material; RecordAndTelegramFallback gives announcement_ref the "builtin:beep" default and max_recording_sec the 300 s default so the compact record_and_telegram:<channel> spelling and pre-round-31 persisted policy JSON always construct a valid policy (dict/JSON is the explicit-override path; the ref grammar widens to a ':'/'/'-permitting announcement selector since it never rides the compact split); hydrated-dead wording corrected - a CallLease never persists, only provider_bindings rows do, so a fresh provider.register supersedes the dead row rather than "restored leases re-binding"; the FULL LineAssignment enumerations list all four hardware fields; ensure_aware_utc bounds stamps SYMMETRICALLY to now +- 3650 days so a far-past stale stamp can never poison dedup keys, watermarks or idle-deadline arithmetic; and the event.media_overflow pin is structural (EventName/EVENT_PAYLOAD_TYPES + {lease_id, line_id}), not a doc-substring check. Why: The swe-2 paranoid audit (SA_swe2_review_1, software-engineer-2 registry, issue #77) again found the dominant class was normative semantics living only in prose or contradicted across documents - a missing internal accept phase, a saga compensation that fabricated provider facts, provider bindings documented as never-persisted, an orphan deadline whose cascade was not spelled atomically, and a media overflow policy stated three different ways - plus real drift (alt-3 payload, vhci discovery order, loopback auth, lock-held I/O) and tooling gaps that let stale anchors and unsafe interpolations through; all are closed ahead of the M1 implementation milestone so the Phase-0 contract, its spec, and the tool gates are unambiguous. (issue #77, SA_swe2_review_1)

## [0.0.25] - 2026-11-16

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [fix] Round-30 blind paranoid audit fixes (SA_carbon_review_19, four fix batches across contract/spec/design/hci docs): domain contract gains the ingress/hydration/rollback surface - ingest_media_state(lease_id, role, attached) pairs live lease_id + media role under a media_token, hydrate_lines and restore_line close the write-through persistence loop (config_stale + single-transaction lines+line_assignments), ProviderRegisterResult carries pending_rings + channel_seq so closed-assignment rings redeliver and socket generations are distinguishable; the outbox becomes a two-level bounded model (OUTBOX_SOFT_CAP pre-flight + OUTBOX_HARD_CAP safety bound); liveness hardening - unreachable is terminal at the mutation, unbound is a listing-only listing state, dedup keys re-scope to the LINE, and the advisory-stamp watermark is a monotone max over (line_id, fact_kind); ring-drain burned-token edges and snapshot ringing ambiguity resolved deterministically; cancel_acquire verifies ticket ownership; saga compensation hangs up before releasing resources; heartbeat_interval_ms bounded by MAX_HEARTBEAT_INTERVAL_MS; admin listing surfaces paginated. Wire/docs: api-contract adds the admin.* method table, ping gains a state field, and the media protocol pins 0x02 flush (agent barge-in marker + watermark), 0x04 overflow, media.auth single-consume token, and force-close semantics; phone-bridge-service-design pins SSRF redirect policy, media-file quotas (max_recording_sec, per-line caps, ENOSPC isolation), the single mutation lock covering kernel call + drains + bus.publish on every adapter path, forward_via_line second-leg FSM, answer_and_announce WAV-only + drain deadline, record_and_telegram token storage + dead-letter, state-file call-id binding + teardown + staleness, dongle_lost sibling- liveness ordering, PII coverage for hook streams + DIAL_REJECTED.detail + phonebridgectl, remove_line INVALID_STATE on forward references, exec-hook latest-per-(hook,line) coalescing, host-shared vhci.lock. docs/hci-agent.md gets a transport overhaul: ctrl_seq init 0/mod-2^32 + empty-payload rule, HELLO_ACK reject schema + HEARTBEAT_ACK, STATUS agent-only + LINK_DEGRADED, SCO_STATE sourced from 0x2c only, real altsetting geometry + SCO_ALT_UNSUPPORTED + alt-0 on pipeline death, HELLO timeout/preemption, symmetric session_nonce + media_seq rebind rule, isoc pacing + drift compensation + silence fill per codec, vhci flock deadline + HCI_VENDOR_PKT index discovery + proxied HCI_Read_Buffer_Size + frame validation + BD_ADDR verification + handle-matched SCO disconnect, a loopback transport profile with SO_PEERCRED, direct_mtls DTLS-CID rebind handling, weak-host bind enforcement, usb_path-primary matching + adapter_mac pin, bounded USB reset, Agent1 pairing semantics, mandatory mask_pii. Tests: fake-kernel conformance for the new ingress/admin surface, liveness 25-pair matrix, enum dicts, ERROR_CODE_WIRE pin, bounds, RedactedString, apply_patch, phase validators, pagination. Why: The carbon paranoid audit (SA_carbon_review_19, slice B2a + swe-2 registry) found the dominant defect class again was normative semantics living only in prose plus real protocol gaps: the persistence write-through had no documented compensation or batch hydration, media attach/detach had no kernel fact at all, the outbox had a soft cap but no hard bound, the media/flush/overflow wire names were asserted in design docs but never pinned in the api contract, and the hci-agent transport spec had silently drifted from the implementation-facing details (frame invariants, altsetting numbers, index discovery, pairing semantics, PII masking). (issue #75, SA_carbon_review_19)

## [0.0.24] - 2026-11-16

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [fix] Round-29 Quant Partner audit fixes (SA_quant_review_1, 39 findings triaged to the verified set): kernel-driver surface hardened - tick/decide_fallback/drain_* are documented kernel-internal and adapters MUST NOT route wire calls to them; ring-drain sub-phase after missed_call declared kernel-internal bookkeeping (RINGING_LOCKED stays wire-visible); AnswerAndAnnounceFallback eligibility pinned as a decision-time normative requirement; single clamp_provider_stamp helper unifies advisory-stamp normalization (only future skew > MAX_CLOCK_SKEW_SEC clamps); reregister determinism - always supersedes the current live binding (last-writer-wins, racing reregisters resolve sequentially), provider.* facts on superseded assignment_id resolve STALE_TOKEN and the adapter MUST close the superseded socket; SNAPSHOT_RECONCILED_PHASES exports the dialing-or-later set; ProviderBinding.heartbeat_interval_ms bounded le=86400000; drop_session ordering - allocator side-effects complete inside the mutation before drained events; bounded list pagination (offset/limit<=256, MAX_LIST_PAGE) on admin listings and get_lines; dedupe scope pinned to (assignment_id, call, stamp); MAX_IDEMPOTENCY_KEYS_PER_SESSION=256 fail-closed; tick(backwards now) MUST reject INVALID_ARGUMENT; MAX_LINES_TOTAL=256; media.auth token consume declared atomic (one auth_ok, concurrent losers rejected); /dev/vhci open+index discovery serialized under a per-host flock; 0x02 watermark discard scoped to undelivered buffered frames; worker-dies- mid-forward rides the ordinary dead-man path; feeder/LineProvider sibling liveness -> dongle_lost; webhook SSRF pins the resolved IP at connect; single control socket = one live binding; partial TCP frame = transport error, never written to vhci; state-file atomic write = tmp+fsync+rename; admin mutations serialized single-writer. Tests: per-role reissue isolation + never-allocated negative, RedactedString hash/eq consistency, explicit wire-None phase mapping, id-carrier boundary lengths, aggregate-limit consistency, reconciled-phases pin, clamp helper pin, heartbeat_ms bound, backwards-tick pin. Why: The Quant Partner paranoid audit (SA_quant_review_1, claude-sonnet-5 software-architect, three stateless slices) found 39 defects: the dominant domain pattern was critical semantics living only in prose (driver-vs-wire surface, drain sub-phase, fallback eligibility, skew clamp) plus real wire/design gaps (unpaginated listings vs the 64 KiB frame cap, post-supersede fact acceptance, media.auth TOCTOU, /dev/vhci open races, unbounded per-session dedup, SSRF TOCTOU) and test-coverage blind spots on the per-role reissue invariant and wire mappings. (issue #73, SA_quant_review_1)

## [0.0.23] - 2026-11-16

### User-facing

_No user-visible changes._

### Codebase

#### docs
- [chore] Archived all 18 SA_* architecture review journals byte-for-byte into docs/audits/ with a README index mapping each journal to its audit round/version; the untracked root copies are left untouched as local artifacts. Why: CHANGELOG and .changes entries reference SA_* journals that lived only as untracked files in the repository root, so every link was dead for anyone but the local machine; archiving them under docs/audits/ makes the audit trail reproducible without touching the originals. (issue #69)

#### domain
- [fix] Round-28 blind paranoid audit fixes (SA_carbon_review_18, 33 findings): LivenessState gains the kernel-computed DEGRADED rung (reachable -> degraded -> unreachable, elapsed-time ladder; degraded -> ws_disconnected legal on control-socket loss); CALL_PHASE_FORWARD_TRANSITIONS pins the forward-only CallPhase matrix including the alerting-less DIALING -> ACTIVE answer; MediaAllocator.reissue(lease_id, role) supersedes one role's media credentials without touching the other half; ProviderRegistration carries the adapter-distilled reregister flag (derived from the RPC method name only - a `reregister` wire-params key is ignored) so a reregister on a live REACHABLE or DEGRADED binding is a legal half-open supersede (fresh assignment_id, leases preserved, snapshot reconciles only >=DIALING leases - ACQUIRED grants exempt); dead-binding outbox cleanup moved from drain time to the killing/superseding mutation (surviving-lease commands retargeted to the new assignment_id, drain keeps only a defensive drop); configure_line(claimable=False) no longer cancels shared-pool dial-out tickets (only pending claims auto-reject); LineAdminKernel.provision_line creates quiescent UNBOUND lines and configure_line patches profile; CallEndReason.MAX_CALL_DURATION enforced by tick on DIALING+ leases (created_at + effective cap, inadmissible on missed_call); provider checked_at/observed_at stamps are advisory dedupe keys only - only future skew beyond MAX_CLOCK_SKEW_SEC is normalized to kernel now, past stamps stay verbatim; handset-answered rings (call_state active + call_token on RINGING_LOCKED) surface as external_call BUSY with no lease/hangup/bridge, and ring-drain best-effort hangup only rides reject-path outcomes; 0x02 flush watermark defined in the agent uplink stream's own timestamp domain; forward-chain validation walks both directions at MAX_FORWARD_HOPS and revalidates dependents + hydration parity; idle_revoke_after bounded at 86400 s and last_seen_at stamped by owner ops + last-session drop_session only; queue HoL ticket at MAX_LEASES_PER_SESSION is skipped, preempted reissue exempt from ticket caps, pool tickets cancel when their eligibility set empties, idempotency dedup records outcomes; CallLease.profile + QueueTicket.profile (granted lease inherits the ticket label) + AdminLineView.profile/provider_meta added, provider_meta removed from LineSummary; get_lines(EXCLUSIVE_LINE) excludes UNBOUND; pending claims auto-reject on provider loss; forward target must resolve an existing line; remove_line refuses inbound forward refs and emits assignment_revoked for owned quiescent lines; SHARED_POOL revoke is INVALID_STATE and binding revoke idempotent; token/caller_id/media_endpoint fields bounded; acquire requires session dial_out intent; ring fanout is receive_inbound-scoped; free_lines predicate + BUSY->BUSY no-event rule; confirmed answer_confidence gated to active/disconnected; vhci-feeder answers HCI_Read_Buffer_Size locally (no double 0x0e); WirePlumber scoped off the oFono-owned HFP AG profile; loopback hci-agent auth model documented; webhook SSRF re-validates resolved IP at connect; docs/audits archives SA_carbon_review_18. Why: The blind post-freeze audit (SA_carbon_review_18, Gemini 4 Carbon High) found six kernel-contract blockers that the M1 implementer could not satisfy — an unexpressible degraded heartbeat rung, a media port that can only reissue both roles at once, an indistinguishable register/reregister ingress with a half-open supersede race, drain-time dead-binding cleanup whose teardown events missed the same-cycle drain_events, a claimable flag that destroyed live pool queues, and a missing line-provisioning surface — plus two media-protocol blockers (per-role socket loss vs whole-pair teardown, an incomparable 0x02 watermark clock) and a verified Major/Minor set including stale M1/M2 acceptance notes in .devin/roadmap.md that contradicted the frozen contract. (issue #71, SA_carbon_review_18)

## [0.0.22] - 2026-11-16

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [fix] Round-27 paranoid audit fixes (SA_carbon_review_17, 80 findings triaged to the verified set): kernel owns dead-binding command teardown inside drain_provider_commands (ProviderRevokeCommand exempt while the control channel is open); LineSummary.assignment_id + assignment_granted replay on owner session open fix pre-assignment delivery; LineAdminKernel gains list_lines/list_leases/list_tickets/remove_line + AdminLineView; new provider.reattach_media ingress covers media-socket death on a live control channel; tick(now) is the sole timer entry sourced from the injected Clock; ring-drain docstrings aligned to ring_drain_ttl_sec; exclusive-line state/health events no longer broadcast (owner-agent + admin only); UNBOUND legal only as old_state of line_health_changed (unbound->reachable on first attach); pool ring redelivery to new receive_inbound sessions; ProviderActiveCall call_token/lease_id XOR; ProviderCallStateUpdate rejects ringing + caller_id; send_dtmf requires audio_ready in ALERTING too; LineAssignment.owner_agent_id rejects system:*; strict _check_id charset [A-Za-z0-9._-]; ProviderRegistration.token removed (adapter authenticates before distilling the fact) and meta persists onto ProviderBinding; voicemail_ref dropped; exported bounds (OUTBOX_SOFT_CAP, MAX_CLOSED_*, MAX_SESSIONS_TOTAL, MAX_DETAIL_LEN, MAX_REASON_LEN); reason/detail/profile strings bounded; AssignmentGrantedEvent mode=EXCLUSIVE; ProviderLiveness.state_snapshot only on reachable; halves-up heartbeat_interval_ms; typed PhonebridgeError from validate_e164_country; CallLease.audio_ready_received; MediaTransport literal; PairWindowRequest (<=300 s); configured UNBOUND lines listed but not claimable; ProviderRegistration.meta mirrored on LineSummary.provider_meta; check_changes_entry.sh validates fragment shape and check_spec_anchors.sh gains --base mode proving the anchored spec file changed. Why: The post-v0.0.21 paranoid audit (SA_carbon_review_17) found five blockers that would have broken the M1 implementer outright — a dead-binding drop rule the adapter could not execute, an assignment_id that could be lost before the owner ever connected, wire-admin ops with no kernel surface, no provider-side media recovery on a live control channel, and a dual Clock/tick time authority contradicting the single-timer rule — plus a round-24 docstring contradicting the normative ring_drain_ttl_sec spec and a tail of verified Major/Minor gaps; all are closed here so the Phase-0 contract is unambiguous before implementation starts. (issue #67)

## [0.0.21] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [fix] Round-26 post-release audit fixes (SA_carbon_review_16): EventEnvelope now validates the (name -> payload type) leg via EVENT_PAYLOAD_TYPES so a broadcast-scoped envelope can never carry a PII payload; MAX_FORWARD_HOPS=8 contract constant pins the transitive forward_via_line cycle bound the docs already required; ProviderCallStateUpdate rejects reason on non-disconnected states; profile strings bounded (MAX_PROFILE_LEN=128) on ProviderRegistration/ProviderCapabilityUpdate/LineSummary; free_lines goes through _check_line_id; new LivenessState.UNBOUND for provider-less lines (LineSummary caps==() iff liveness==unbound; never a reported fact or event endpoint); media-token reissue documented as superseding the prior pair; dead-binding provider commands documented fail-closed with network_error teardown; admin.assign_line documents the system:* owner ban; design/api-contract stale DEGRADED-DOWN and idle_revoke_after:null vocabulary corrected; .changes scope list fixed to actual repo scopes. Why: The independent post-v0.0.20 audit (SA_carbon_review_16) found two Major gaps — an envelope could stamp a PII payload under a broadcast-legal event name because only (name, audience.scope) was validated, and the documented 8-hop forwarding bound had no contract-level anchor an M1 implementer would see — plus a set of Minor hygiene drift items; all are closed here ahead of the M1 implementation milestone. (issue #65)

## [0.0.20] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [feat] Round-25 architect audit (SA_carbon_review_15, 45 findings) consolidated ahead of M1: production deployment ratified as --network=host + /dev/vhci on a dedicated host (AF_BLUETOOTH is init_net-only; micro-VM kept as an optional post-MVP hardened profile) with a phonebridge doctor pre-flight; LineState split into occupancy (FREE/BUSY/RINGING_LOCKED) vs LivenessState + event.line_health_changed; claim_resolved / assignment_granted / queue_cancelled events deliver assignment_id and ticket fates; MediaCredentials split per role (agent/provider endpoints) with reattach_media token reissue on both sides; ring correlation by line_id (fallback tokens, snapshot reconcile, 1:1 ambiguity rule); global queue seq minted at acquire ingress; provider call_state echo/observed_at/checked_at closed vocabularies; system:* identities rejected externally; clear_idle_revoke_after / clear_max_call_duration tombstone flags; forward-cycle bound 8 hops; ring_drain_ttl_sec + max_call_duration_sec timeouts; MEDIA_PAIR_TTL_SEC=30 with network_error teardown; DTMF timestamped 0x02 watermark flush + ALERTING early-media gate; exclusive-line privacy filtering in get_lines and redacted broadcast reasons; numeric JSON-RPC error codes + typed error_code (+UNAUTHORIZED/FORBIDDEN/SESSION_NOT_FOUND); tokens moved to non-string secret containers; tooling fixes (release_notes YAML block scalars, #requirement- spec anchors, version-tag cancel-in-progress, CI matrix 3.10-3.13, graceful-skip tooling tests, reserved example phone numbers). Why: Close the verified contract and deployment gaps the Carbon audit surfaced before M1-M5 implementation starts: eleven Blockers (undelivered assignment_id, token log leaks, non-JSON-RPC error codes, single media_endpoint, queue-seq underflow, restart/media re-attach races, missing admin drain) plus the major/minor set that would otherwise be re-litigated during implementation. Rejected on scope: DSP clock-drift resampling (30-45 ms jitter buffer absorbs it between bursts), DTLS connection-ID roaming (WireGuard covers V1), per-line bluetoothd SPOF split (YAGNI under thin-slice supervision). (SA_carbon_review_15)

## [0.0.19] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [feat] Round-24 architect review (SA_carbon_review_13), final Phase-0 contract/doc freeze v0.0.19: MediaRouter pacing direction corrected - the agent-socket reader drains continuously into the 1500-frame uplink ring (0x02 flushes it immediately) while a monotonic pacer writes ONE 640 B frame per 20 ms tick to the LineProvider; new frozen LineTimeouts effective model (defaults 5.0/10.0/5.0/2/3/10.0, down>degraded validator) exposed via LineAssignment.timeouts for SQLite write-through; ProviderBinding.heartbeat_interval_ms stamps the per-session cadence (default 5000, gt>0); TimeoutConfig gains the same ordering validator; LineTimeouts.apply_patch merges a TimeoutConfig patch atomically via full re-validation of the merged dict (round-24b residual: a lone-field patch that breaks the heartbeat ladder maps to INVALID_ARGUMENT, no partial state); validate_e164_country + ForwardViaLineFallback model validator + LineAssignment self-loop ban move dial-target validation into constructors; provider caps are non-empty telecom-only (no exclusive_line); docstrings freeze the ingest_ring glare matrix, tick-driven decide_fallback and fallback TTL expiry (MissedCallEvent ACCEPT_WINDOW_EXPIRED), error-code mapping (CLAIM_NOT_FOUND/STALE_TOKEN/ INVALID_ARGUMENT) and AssignmentId rotation with a bounded _closed_assignments ring; hci-agent 0x16 SCO_STATE gains handle?:uint16 (required on open) + continuous 2-bit H2 sync counter rewritten on every outbound mSBC packet including silence; spec-impact noop literal unified to bare "-"; interaction-flow/architecture-unified HTML updated (expires_at, ticket_id, event.idle_alert, admin audience). Round 24c (post-review nits): the heartbeat_interval_sec -> heartbeat_interval_ms conversion is pinned - heartbeat_interval_ms_from_sec (round(sec*1000), min 1 ms) + ge=0.001 granularity floor on LineTimeouts and TimeoutConfig - so a valid config can never stamp a zero cadence; LineTimeouts.apply_patch ErrorData.detail now joins ALL validation messages; design.md aligns to the 0x16 SCO_STATE wire field name "handle". Why: Close the last contract/documentation drift before the M1-M5 implementation milestones: the review found two critical defects (inverted MediaRouter pacing that would strand the 0x02 barge-in flush inside SO_RCVBUF; missing effective-timeout fields on the kernel DTOs so M2 could not answer heartbeat_interval_ms or persist timeouts to SQLite), four high (hci-agent silence/H2 phase defect, tick vs decide_fallback lifecycle, ingest_ring glare matrix, idle_alert delivery channel), five medium and two low findings; Phase-0 items only - implementation notes for M1-M5 are deferred to the milestone acceptance notes. (issue #59)

#### docs
- [docs] Doc alignment after PR #56 review MINOR: the outbox SOFT_CAP pre-flight enumeration in docs/specs/line-manager.md and the round-23 decision-log row now lists `accept` alongside `acquire`/`dial`/`claim_exclusive`, matching the kernel contract. Why: The round-23 doc text enumerated only three of the four load-creating operations; contract.py already requires a pre-flight check on `accept` too, so the docs understated the contract. One-line alignment, no contract change. (issue #57)

## [0.0.18] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### domain
- [feat] Round-23 architect review (SA_carbon_review_12), Phase-0 contract freeze: ProviderHangupCommand XOR target {lease_id?|call_token?}; ForwardViaLineFallback gains target_number/target_country with config-time self-loop ban; MediaAllocator lifecycle (allocate at dial/accept, idempotent revoke on lease ENDED); JSON-safe _mask_for_log (datetime/timedelta/Enum); outbox SOFT_CAP pre-flight semantics + teardown-never-raises; saga compensation on line.dial/line.answer; FallbackTriggeredEvent.expires_at + TimeoutConfig.fallback_accept_ttl_sec; event.idle_alert + AdminAudience (idle escalation alert-only); reissued_ticket_id on call_state_changed; bounded idempotency_key (1-128); phone_mac-to-agN resolution + Call Waiting auto-reject; long-lived hci-agent token + uplink playout jitter buffer; MediaRouter 20 ms pacing + asymmetric rings (ADR-A); M1/M2 storage split; capability matrix; pinned PipeWire format; execve-only hooks + webhook retry deadline; informational profile; py.typed + docs/specs skeleton; HTML/doc drift fixes. Why: Freeze the domain contract and architecture spec before the M1-M5 implementation milestones: the review found one critical (unexecutable RejectFallback), six high and seven medium gaps in the contract and its docs; Phase 0 items only - M1-M5 sections are implementation notes, not runtime behavior. (issue #55)

## [0.0.17] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### daemon
- [refactor] Round-22 architect review (SA_carbon_review_10_gemini): LineManager.update_provider_capabilities + ProviderCapabilityUpdate ingress; CallLease.session_id ((session_id, lease_id) ownership); gateway-minted call_token on provider.call_state ringing + caller_id? on ring/state reports; event schema/wire alignment (caller_id, expires_at RFC 3339, WireCallState, ticket_id on event.line_acquired, required owner_agent_id on assignment_revoked); ephemeral CallLease auto-release vs durable call.release{assignment_id}; receive_inbound-scoped inbound events and accept; admin.assign_line + TimeoutConfig patch on configure_line; LineProvider owns DTMF synthesis/uplink muting; announcement_ref + fallback_worker acquire bypass + event.fallback_triggered as the sole delivery; bounded pending claims and ProviderRegistration.meta. Why: The review found the contract unsafe to freeze for M1-M5: no provider capability-update path, ambiguous multi-session lease ownership, an uncorrelatable early caller hangup on inbound rings, and event models drifted from the documented wire names/types; this PR closes the two critical, six high and six medium findings before implementation starts. (issue #53)

## [0.0.16] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### daemon
- [refactor] Round-21 architect review (SA_carbon_review_9): ProviderCommand brought to line.* wire parity (PROVIDER_COMMAND_WIRE_NAMES + drift test, media_endpoint/media_token on dial/answer, mask_clir removed), two independent per-role media tokens per lease (MediaCredentials), state_snapshot orphan-grace input + LivenessState.WS_DISCONNECTED, closed reason enums, frozen DTOs + EventAudience routing, ClaimId/PendingClaim + admin list reads, Clock/MediaAllocator ports; docs synced (SCO altsetting per BT Core Vol 4 Part B, deterministic external-call rule, RFC-3339 wire-time convention, media frame lengths). Review follow-up: CallEndReason is now wired into the event payloads (call_state_changed/missed_call/line_state_changed, incl. the external_call member) and ProviderLiveness carries state_snapshot for provider.ping. Why: The review found the provider command models had drifted from the documented line.* wire contract (missing lease_id/media credentials, a nonexistent mask_clir field) and that a single media_token let any holder claim either role in media.auth; this PR makes the domain contract 1:1 with the wire spec before M1. (issue #51)

## [0.0.15] - 2026-11-15

### User-facing

_No user-visible changes._

### Codebase

#### docs
- [chore] Release-prep fold: all 15 accumulated .changes fragments (shipped under v0.0.2-v0.0.14) folded into CHANGELOG.md — new sections [0.0.4],[0.0.5],[0.0.9]-[0.0.14] created with their tag dates, canon-version-gate note appended to [0.0.2], consumed fragments deleted per the release_notes.sh convention. Why: Nine releases went out without the release-prep fold step; CHANGELOG.md topped out at [0.0.8] while fragments accumulated. (issue #49)

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
