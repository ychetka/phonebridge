---
type: chore
scope: docs
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Archived all 18 SA_* architecture review journals byte-for-byte into
  docs/audits/ with a README index mapping each journal to its audit
  round/version; the untracked root copies are left untouched as local
  artifacts.
why: >-
  CHANGELOG and .changes entries reference SA_* journals that lived only
  as untracked files in the repository root, so every link was dead for
  anyone but the local machine; archiving them under docs/audits/ makes
  the audit trail reproducible without touching the originals.
refs: issue #69
---

## Spec impact
-
