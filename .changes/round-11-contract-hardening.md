---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Apply architecture-review (round 11) hardening: unified id
  taxonomy (lease_id only), agent media_endpoint/media_token on
  dial/accept, binary 20 ms media framing (ADR-02), FallbackWorker
  split (ADR-01), BDADDR + persistent volumes (ADR-03), WAN media
  reclassified to prod gate.
why: >-
  SA architecture review found contract gaps (agent media attach
  undefined, call.accept signature impossible, receive_inbound
  locking lines BUSY, unmasked PII fields). Developer approved all
  three ADRs with calibrated severity (WAN transport = prod gate
  for remote Pi, not v1 blocker).
refs: SA_argon_review; devin-session-bridge PR #162 context
---

## Spec impact
-
