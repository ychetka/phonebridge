---
type: fix
scope: contract
user_visible: true
breaking: true
user_summary: >-
  Contract hardening round 20: fail-closed RedactedString,
  transactional event/provider outbox on LineManager, ProviderBinding
  split from LineAssignment, acquire idempotency keys, grant-TTL
  expiry on the wire, new INVALID_* / RESOURCE_EXHAUSTED error codes,
  DTMF validation, media-UDP session nonce, richer SCO_STATE.
dev_summary: >-
  SA_carbon_review_changes_8 applied: RedactedString is a non-str
  container (json.dumps/concat can no longer leak PII; pydantic
  accepts instance-or-str, JSON serializes to_wire()); LineManager
  gains drain_events()/drain_provider_commands() + a discriminated
  ProviderCommand union (dial/answer/hangup/send_dtmf/revoke); a
  bounded per-manager outbox replaces ad-hoc emission (rule 4);
  register_provider returns ProviderBinding (distinct assignment_id
  space); acquire gains idempotency_key dedup +
  INVALID_STATE/INVALID_ARGUMENT/RESOURCE_EXHAUSTED codes and
  GRANT_TTL_EXPIRED/SESSION_LIMIT_EXCEEDED/CLAIM_NOT_FOUND/
  INVALID_DTMF reasons; AcquireGranted.expires_at +
  CallPhase.ACQUIRED + CallLease.grant_expires_at +
  CALL_PHASE_WIRE_STATES; DTMF_PATTERN/MAX_DTMF_DIGITS;
  ensure_aware_utc() public helper; media-UDP header gains a
  session_nonce bound to HELLO_ACK; SCO_STATE gains
  air_mode/rx_pkt_len/tx_pkt_len; SCO_SESSION_MISMATCH telemetry;
  orphan grace resolved into a two-level failure model; D-Bus
  split + stack-restart reconciliation documented; phonenumbers
  added as a runtime dependency.
why: >-
  Three CRITICAL findings closed: RedactedString(str) leaked PII
  through C-level string paths; LineManager had no safe
  event/provider-command channel (network awaits inside mutations
  violate rule 4); register_provider conflated provider attachment
  with agent ownership. Plus seven HIGH/MEDIUM contract gaps ahead
  of M1.
refs: SA_carbon_review_changes_8; dev session 2026-11-14
---

## Spec impact
- none
