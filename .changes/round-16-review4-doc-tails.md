---
type: fix
scope: contract
user_visible: true
breaking: false
user_summary: >-
  event.call_state_changed now documents and carries
  answer_confidence (provisional|confirmed).
dev_summary: >-
  Fourth architect review (SA_hidrogen_review4): answer_confidence
  added to event.call_state_changed wire payload and
  CallStateChangedEvent model; design doc §3.5 lists
  provider.audio_ready, §3.6 documents event.fallback_triggered +
  answer_confidence, §13 M4/M5 rewritten for the hci-agent +
  vhci-feeder line-bundle topology. (Round-15 items from the same
  review snapshot were already shipped in PR #34.)
why: >-
  Review 4 verified contract.py parity and flagged remaining doc
  drift: answer_confidence existed in CallLease/design but never
  reached the wire contract; §3.5/§3.6/§13 still described the
  pre-hci-agent surface.
refs: SA_hidrogen_review4; dev session 2026-11-13
---

## Spec impact
-
