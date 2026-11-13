---
type: fix
scope: contract
user_visible: true
breaking: true
user_summary: >-
  LineManager.dial() gains the mandatory `country` param; new domain
  events (audio_ready, fallback_triggered, line_state_changed,
  missed_call); new drop_session + ingest_audio_ready kernel methods.
dev_summary: >-
  Architect review round 15 (SA_argon_review3): `country` added to
  LineManager.dial() so per-line allowed_number_types can gate
  UNSUPPORTED_NUMBER_TYPE inside the kernel; LineAssignment gains
  allowed_number_types; EventName expanded + 4 event models;
  FallbackDecision.fallback_call_token; LineManager.drop_session
  (socket-bound tickets + dead-man switch) and ingest_audio_ready;
  RedactedString explicit json serializer via to_wire();
  MAX_CTRL_FRAME_LEN note in hci-agent.md; CHANGELOG 0.0.6 entry.
why: >-
  Third architect review found signature/model gaps between
  contract.py and the round-12/14 docs that would force M1 rework:
  missing country param, missing event vocabulary, no session-drop
  lifecycle method, and a pydantic serialization edge for
  RedactedString.
refs: issue #33; SA_argon_review3; dev session 2026-11-13
---

## Spec impact
-
