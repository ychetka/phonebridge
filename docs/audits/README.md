# Architecture audit journals

Byte-for-byte archived copies of the `SA_*` review journals that were
previously kept only as untracked local artifacts in the repository root.
The original files remain in the root as local artifacts; the copies in
this directory are the canonical archived versions referenced by
`CHANGELOG`/`.changes` entries.

| File | Audit round / version |
|------|-----------------------|
| SA_carbon_review_changes_8 | Round pre-v0.0.x — PLAN_DELTA task spec accompanying round 8 changes |
| SA_carbon_review_9 | Round 21 — full review of v0.0.15 after rounds 20/20b |
| SA_carbon_review_10_gemini | Round 22 — Gemini fresh-pass review of domain contract (pre-freeze) |
| SA_carbon_review_11_gemini | Round 22 → v0.0.17 verification — Gemini pass confirming round-21/22 findings resolved |
| SA_carbon_review_12 | Round 23 / v0.0.17 — deep audit before contract freeze |
| SA_carbon_review_13 | Round 24 / v0.0.18 — review of round-23 fix package |
| SA_carbon_review_13_gemini | Round 24 / v0.0.18 — Gemini verification pass of round-23 fixes |
| SA_carbon_review_14 | Round 24 / v0.0.19 — paranoid-architect audit |
| SA_carbon_review_14-2 | Round 24 / v0.0.19 — second pass on the same baseline (deeper edge-case sweep) |
| SA_carbon_review_15 | Round 25 / v0.0.20 — blocker-level audit (Bluetooth netns, claim events, token redaction) |
| SA_carbon_review_16 | Round 26 / v0.0.21 — blind full-slice audit of the round-25 consolidation |
| SA_carbon_review_17 | Round 27 / v0.0.21→v0.0.22 — Phase-0 paranoid audit, 80 findings |
| SA_carbon_review6 | Early round / v0.0.8 — full architecture and code review |
| SA_carbon_review7 | Early round / v0.0.10 — verification of remaining blind spots |
| SA_argon_review | Parallel audit series — early bootstrap/spec-stage review |
| SA_argon_review3 | Parallel audit series — rounds 11–14 slice vs CODING_HAZARDS/REVIEW_HAZARDS |
| SA_argon_review5 | Parallel audit series — v0.0.8 slice review |
| SA_hidrogen_review4 | Parallel audit series — rounds 11–14 re-check incl. pyproject.toml |

## Notes

- The `carbon` series is the main sequential audit lineage (rounds map to
  `CHANGELOG`/`docs/development-journal` entries).
- The `argon` and `hidrogen` files are parallel audit series by other
  reviewers on overlapping baselines; they are not part of the numbered
  round sequence.
- Files carry no extension by historical convention; content is Markdown
  prose in Russian or English depending on the reviewer.
