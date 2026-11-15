# docs/specs — requirement anchors

This directory holds the normative requirement specs that
`.changes/` fragments reference from their `## Spec impact` section
(the contract is enforced by `tools/check_spec_anchors.sh`, which
the CI lint job runs).

## Anchor convention

Each requirement is a third-level heading of the form:

```markdown
### Requirement: <descriptive-name>
```

The fragment then references it as
`docs/specs/<domain>.md#<requirement-anchor>`, where the anchor is
produced by GitHub's heading rule applied to the WHOLE heading text
— the `Requirement:` prefix is part of it, so every requirement
anchor starts with `requirement-`: lowercase, all punctuation
dropped (only letters, digits, spaces, hyphens and underscores
survive), spaces replaced by hyphens, duplicate headings suffixed
`-1`, `-2`, ...

Example: `### Requirement: Outbox soft-cap pre-flight` yields the
anchor `docs/specs/<domain>.md#requirement-outbox-soft-cap-pre-flight`.

## Spec impact ops

- `adds:` — the PR introduces a NEW requirement; the anchor must
  exist in the spec file after the change.
- `modifies:` — the PR changes an existing requirement; the anchor
  must still exist after the change.
- `removes:` — the PR deletes a requirement; the anchor must be
  gone after the change.
- `-` — a bare dash marks "no documented behavior changed" (the
  literal used by `controller-manager/harness.md`; `- none` and
  `n/a` spellings also parse in `tools/check_spec_anchors.sh`, but
  the canonical spelling is `-`). A noop marker must not be
  combined with anchor entries.

Note: spec requirements were written ahead of the M1-M5
implementation milestones, so implementation PRs typically
`modifies:` (or `removes:`) existing anchors rather than `adds:` —
`adds:` is only for a genuinely new requirement.

Spec files group requirements by domain (`line-manager.md`,
`media.md`, ...). Keep the requirement heading text stable once a
fragment has referenced it — renaming a heading silently breaks the
anchor of every fragment that cites it.
