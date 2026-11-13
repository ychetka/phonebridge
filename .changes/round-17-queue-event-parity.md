---
type: fix
scope: contract
user_visible: true
breaking: true
user_summary: >-
  New domain event QueueUpdatedEvent (EventName.queue_updated);
  QueueReadyEvent now carries session_id + intent so adapters can
  emit event.line_acquired without a ticket lookup table.
dev_summary: >-
  Architect review round 17 (SA_argon_review5): EventName +
  QueueUpdatedEvent model for FIFO position advances; QueueReadyEvent
  gains session_id + intent (wire event.line_acquired mapping);
  tests now call ingest_audio_ready/drop_session, instantiate all
  round-15 event models, and assert the FIXED_LINE_OR_MOBILE
  allowlist default + fallback_call_token field; CHANGELOG
  backfilled for 0.0.7/0.0.8.
why: >-
  Queue position advances originate inside LineManager
  (cancel_acquire, drop_session, grant-TTL tick, line release) —
  without a typed domain event the M1 kernel cannot emit them.
  And the review flagged that tests never called the new round-15
  methods or validated the new event models.
refs: SA_argon_review5; dev session 2026-11-13
---

## Spec impact
-
