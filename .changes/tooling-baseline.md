---
type: chore
scope: tooling
user_visible: false
breaking: false
user_summary: "-"
dev_summary: >-
  Tooling baseline: pydantic and websockets runtime dependencies,
  pytest-asyncio and mypy dev dependencies, strict mypy config with
  the pydantic plugin, asyncio test mode, and a mandatory mypy step
  in the lint workflow.
why: >-
  Roadmap item_02 (issue #6): the provider and agent milestones need
  pydantic models plus the new websockets asyncio API, and strict
  typechecking must gate every PR before typed code lands.
refs: issue #6, task 332
---

## Spec impact
-
