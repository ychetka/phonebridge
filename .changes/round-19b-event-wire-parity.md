---
type: fix
scope: contract
user_visible: true
breaking: false
user_summary: >-
  New agent event event.assignment_revoked closes the last
  domain↔wire event gap; EVENT_WIRE_NAMES mapping table +
  drift test freeze the parity.
dev_summary: >-
  Pre-freeze parity pass: api-contract gains
  event.assignment_revoked {assignment_id, line_id,
  owner_agent_id?, reason?} (delivered to the revoked owner only);
  contract.py gains EVENT_WIRE_NAMES authoritative mapping
  (queue_ready→line_acquired deliberate rename documented);
  test_event_wire_name_parity asserts full EventName coverage.
why: >-
  The last known EventName↔wire drift: ASSIGNMENT_REVOKED had no
  wire event — an exclusive owner would only learn about a revoke
  via OWNER_MISMATCH on its next call.dial. Closed ahead of the
  c_gateway contract freeze (item_05).
refs: SA_carbon_review7 follow-up; dev session 2026-11-14
---

## Spec impact
-
