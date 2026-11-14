---
type: fix
scope: contract
user_visible: true
breaking: true
user_summary: >-
  Admin kernel surface (approve/reject/revoke/configure) split from
  agent API; queue fairness + incoming-ring preemption rules;
  hci-agent gains SCO_STATE USB altsetting control; headless pairing
  via admin.pair_window; MCP is control-plane only.
dev_summary: >-
  Architect review round 19 (SA_carbon_review7 blind spots):
  LineAssignment.owner_agent_id nullable (None = pool/provider
  attachment); new LineAdminKernel ABC; global seq queue fairness +
  preempted_by_incoming grant race rule; deterministic D-Bus
  resolution chain adapter_mac->phone_mac->nodes; control kind 0x16
  SCO_STATE for USB Interface 1 altsetting; in-container BlueZ
  Agent1 + admin.pair_window; MCP adapter boundary documented.
why: >-
  Six M1-M5 implementation gaps found by review: pool lines had no
  legal owner_agent_id value and no admin mutation path; queue
  fairness and ring-vs-grant races unspecified; multi-line D-Bus
  object mapping undefined; USB SCO altsetting duties undocumented;
  no headless pairing path; MCP media boundary undefined.
refs: SA_carbon_review7; dev session 2026-11-14
---

## Spec impact
-
