# Architecture audit journals

Byte-for-byte archived copies of the `SA_*` review journals that were
previously kept only as untracked local artifacts in the repository root.
The original files remain in the root as local artifacts; the copies in
this directory are the canonical archived versions referenced by
`CHANGELOG`/`.changes` entries.

| File | Audit round | Baseline → produced |
|------|-------------|---------------------|
| SA_carbon_review6 | early | v0.0.8 → v0.0.10 — full architecture and code review |
| SA_carbon_review7 | early | v0.0.10 → v0.0.11–0.0.12 — verification of remaining blind spots |
| SA_carbon_review_changes_8 | 20 | PLAN_DELTA task spec for the Round-20 hardening fixes (applied in v0.0.13) — a work-order document, not a findings journal |
| SA_carbon_review_9 | 21 | v0.0.15 → v0.0.16 — full review after rounds 20/20b |
| SA_carbon_review_10_gemini | 22 | pre-freeze → v0.0.17 — Gemini fresh-pass review of the domain contract |
| SA_carbon_review_11_gemini | 22 | → v0.0.17 — Gemini verification pass confirming round-21/22 findings resolved |
| SA_carbon_review_12 | 23 | v0.0.17 → v0.0.18 — deep audit before the contract freeze |
| SA_carbon_review_13 | 24 | v0.0.18 → v0.0.19 — review of the round-23 fix package |
| SA_carbon_review_13_gemini | 24 | v0.0.18 → v0.0.19 — Gemini verification pass of round-23 fixes |
| SA_carbon_review_14 | 24 | v0.0.18 → v0.0.19 — paranoid-architect audit |
| SA_carbon_review_14-2 | 24 | v0.0.18 → v0.0.19 — second pass on the same baseline (deeper edge-case sweep) |
| SA_carbon_review_15 | 25 | v0.0.19 → v0.0.20 — blocker-level audit (Bluetooth netns, claim events, token redaction) |
| SA_carbon_review_16 | 26 | v0.0.20 → v0.0.21 — blind full-slice audit of the round-25 consolidation |
| SA_carbon_review_17 | 27 | v0.0.21 → v0.0.22 — Phase-0 paranoid audit, 80 findings |
| SA_carbon_review_18 | 28 | v0.0.22 → v0.0.23 — blind paranoid audit of the post-freeze draft (Gemini 4 Carbon High), 33 findings |
| SA_argon_review | parallel | early bootstrap/spec-stage review |
| SA_argon_review3 | parallel | rounds 11–14 slice vs CODING_HAZARDS/REVIEW_HAZARDS |
| SA_argon_review5 | parallel | v0.0.8 slice review |
| SA_hidrogen_review4 | parallel | rounds 11–14 re-check incl. pyproject.toml |

## Notes

- The `carbon` series is the main sequential audit lineage (rounds map to
  `CHANGELOG`/`docs/development-journal` entries).
- The `argon` and `hidrogen` files are parallel audit series by other
  reviewers on overlapping baselines; they are not part of the numbered
  round sequence.
- Files carry no extension by historical convention; content is Markdown
  prose in Russian or English depending on the reviewer.
- `SA_argon_review2` is referenced by `CHANGELOG.md` (round 14, issue #31)
  but the file itself was never preserved — neither in this archive nor in
  the working tree; the reference is kept for traceability and the journal
  is recorded here as lost.
- Version column reads `baseline → produced`: the release the audit was run
  against and the release its fixes shipped in (a single version means the
  audit and its fixes landed in the same release).
