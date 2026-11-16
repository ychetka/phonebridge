---
type: fix
scope: domain
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Round-31 audit fixes (SA_swe2_review_1, 40/40 accepted, five fix
  batches across contract/spec/design/hci/tools): domain contract
  gains CallPhase.ANSWERING (call.accept mints the lease in the
  internal answer-in-flight phase, covered by max_call_duration,
  no wire spelling), the adapter-scoped
  compensate_dial(line_id, assignment_id, lease_id) saga entry
  (hangup-first ProviderHangupCommand, lease ends network_error,
  no fabricated provider.call_state fact), and the
  ingest_audio_lost kernel ingress (audio_ready latch reset in
  kernel-side media state); restore_line gains the line_id
  parameter and returns verbatim tombstone-aware compensation;
  provider_bindings becomes a persisted table and hydrate_lines
  loads it DEAD (ws_disconnected, expired orphan_deadline_at,
  first-tick orphan cascade unless a fresh provider.register
  supersedes; channel_seq store backfill generation 1);
  LineAdminKernel gains get_line, revoke_assignment returns bool,
  and every listing is paginated at MAX_LIST_PAGE=64 under the
  64 KiB page cap; provider_call_id is the exact ring-identity
  key with RING_DEDUP_MIN_GAP_SEC (30 s) as the best-effort
  (line_id, caller_id) fallback; the advisory-stamp watermark
  splits clamp-vs-drop (sub-watermark ping applied clamped,
  sub-watermark state fact dropped as duplicate); tick runs the
  orphan cascade atomically (leases network_error, tickets
  queue_cancelled provider_lost, claims auto-reject, ring and
  external_call reset, binding tombstone; heartbeat-down
  suspended while ws_disconnected); uplink overflow is a single
  always-read policy (0x02/0x03 processed normally, drop-newest
  PCM only, one 0x04 plus one event.media_overflow per
  contiguous episode); the admin.* wire surface realigns
  (hardware fields on provision_line/configure_line, list_keys,
  revoke_assignment bool, hooks no longer mint call_token);
  secure_compare owns every AUTH-decision comparison and
  free-form fields get the control-character/media_endpoint
  scheme gate; cancel_acquire gets the three-class ownership
  oracle plus MAX_CLOSED_TICKETS and queue re-check on every
  eligibility-affecting mutation; MEDIA_PAIR_TTL_SEC arms at
  mint; the media flush ambiguity resolves onto the 0x05
  DTMF-START pre_dtmf_consumed field (0xFFFFFFFF = none; a 0
  watermark is a real stamp); docs/hci-agent.md pins the vhci
  write-then-read index discovery (feeder writes [0xff,0x00]
  HCI_PRIMARY first, then reads the index event), corrects
  altsetting-3 mSBC to the same 60 B payload as alt 6 (63 B HCI
  packet over ~3x25 B microframes), puts loopback media on
  connected UDS datagram sockets under /run/phonebridge/media/
  with token+SO_PEERCRED on both sockets, and allowlists
  agent-originated H4 types to 0x01/0x02/0x04; tools hardening -
  check_spec_anchors rejects a '-' noop inside a diff that
  touches docs/specs/*, release_notes strips only matching
  surrounding quotes, check_changes_entry validates --pr
  numeric before URL interpolation, check_version_tag makes
  TIMEOUT=0 a single immediate check; drift-test coverage is
  extended to the new wire/admin surfaces. Round-31 fix round
  (PR #78 review, 4 warnings + 4 minors):
  CALL_PHASE_FORWARD_TRANSITIONS gains TERMINATING targets on the
  DIALING/ANSWERING/ALERTING rows (hangup parks every
  possibly-live call leg in TERMINATING; ACQUIRED still ends
  directly); the provider.call_state params cell gains
  provider_call_id? covering both the IncomingRingIngress and
  ProviderCallStateUpdate distillations; admin.list_keys lands as
  a normative wire row returning only redacted
  {key_id, scope, subject_id, created_at, last_used_at?, revoked}
  metadata - never key material; RecordAndTelegramFallback gives
  announcement_ref the "builtin:beep" default and
  max_recording_sec the 300 s default so the compact
  record_and_telegram:<channel> spelling and pre-round-31
  persisted policy JSON always construct a valid policy (dict/JSON
  is the explicit-override path; the ref grammar widens to a
  ':'/'/'-permitting announcement selector since it never rides
  the compact split); hydrated-dead wording corrected - a
  CallLease never persists, only provider_bindings rows do, so a
  fresh provider.register supersedes the dead row rather than
  "restored leases re-binding"; the FULL LineAssignment
  enumerations list all four hardware fields;
  ensure_aware_utc bounds stamps SYMMETRICALLY to now +- 3650 days
  so a far-past stale stamp can never poison dedup keys,
  watermarks or idle-deadline arithmetic; and the
  event.media_overflow pin is structural
  (EventName/EVENT_PAYLOAD_TYPES + {lease_id, line_id}), not a
  doc-substring check.
why: >-
  The swe-2 paranoid audit (SA_swe2_review_1, software-engineer-2
  registry, issue #77) again found the dominant class was
  normative semantics living only in prose or contradicted
  across documents - a missing internal accept phase, a saga
  compensation that fabricated provider facts, provider bindings
  documented as never-persisted, an orphan deadline whose
  cascade was not spelled atomically, and a media overflow
  policy stated three different ways - plus real drift (alt-3
  payload, vhci discovery order, loopback auth, lock-held I/O)
  and tooling gaps that let stale anchors and unsafe
  interpolations through; all are closed ahead of the M1
  implementation milestone so the Phase-0 contract, its spec,
  and the tool gates are unambiguous.
refs: issue #77, SA_swe2_review_1
---

## Spec impact
- adds: docs/specs/line-manager.md#requirement-call-lease-phases-and-the-grant-ttl-round-31
- adds: docs/specs/line-manager.md#requirement-the-audio_ready-latch-is-kernel-side-media-state-round-31
- adds: docs/specs/line-manager.md#requirement-tick-runs-the-orphan-cascade-atomically-round-31
- modifies: docs/specs/line-manager.md#requirement-saga-compensation-on-linedial-and-lineanswer
- modifies: docs/specs/line-manager.md#requirement-max_call_duration-is-enforced-by-tick-round-28
- modifies: docs/specs/line-manager.md#requirement-media-credentials-are-split-per-role-and-reissuable-round-25
- modifies: docs/specs/line-manager.md#requirement-queue-sequence-is-minted-at-acquire-ingress-round-25
- modifies: docs/specs/line-manager.md#requirement-ring-correlation-is-by-line-not-by-token-round-25
- modifies: docs/specs/line-manager.md#requirement-provider-side-stamps-are-advisory-only-round-28
- modifies: docs/specs/line-manager.md#requirement-heartbeat-liveness-ladder-is-elapsed-time-and-kernel-computed-round-28
- modifies: docs/specs/line-manager.md#requirement-reregister-supersedes-a-live-binding-register-does-not-round-28
- modifies: docs/specs/line-manager.md#requirement-line-lifecycle-admin-operations-round-28
- modifies: docs/specs/line-manager.md#requirement-wire-secrets-are-non-string-containers-round-25
- modifies: docs/specs/line-manager.md#requirement-dial-target-and-caps-validation-is-model-level
- modifies: docs/specs/line-manager.md#requirement-forwarding-policies-are-loop-safe-and-bounded-round-25
- modifies: docs/specs/line-manager.md#requirement-the-0x02-flush-watermark-rides-the-agent-stream-clock-round-28
