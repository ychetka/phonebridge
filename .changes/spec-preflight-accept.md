---
type: docs
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Doc alignment after PR #56 review MINOR: the outbox SOFT_CAP
  pre-flight enumeration in docs/specs/line-manager.md and the
  round-23 decision-log row now lists `accept` alongside
  `acquire`/`dial`/`claim_exclusive`, matching the kernel contract.
why: >-
  The round-23 doc text enumerated only three of the four
  load-creating operations; contract.py already requires a
  pre-flight check on `accept` too, so the docs understated the
  contract. One-line alignment, no contract change.
refs: issue #57
---

## Spec impact
- modifies: docs/specs/line-manager.md#requirement-outbox-soft-cap-pre-flight-and-drain-discipline
