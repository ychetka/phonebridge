#!/usr/bin/env bash
# tools/check_spec_anchors.sh - validate Spec impact anchors.
#
# Every .changes/<slug>.md must carry a "## Spec impact" section after
# its frontmatter, listing requirement anchors it touches:
#
#   ## Spec impact
#   - adds:     docs/specs/<domain>.md#<requirement-anchor>
#   - modifies: docs/specs/<domain>.md#<requirement-anchor>
#   - removes:  docs/specs/<domain>.md#<requirement-anchor>
#
# or a bare "-" / "- none" when the change touches no documented
# behavior. Post-change invariant: adds/modifies anchors MUST exist
# in the referenced spec file, removes anchors MUST NOT exist (the
# PR applies its own spec delta - the check reads the working tree,
# which already carries the delta).
#
# Anchors follow GitHub's markdown rule for "### Requirement: <name>"
# headings: the WHOLE heading text is slugified (the "Requirement:"
# prefix included), so the anchor is "#requirement-<name-slug>" -
# lowercase, punctuation dropped, spaces -> hyphens, hyphen/underscore
# runs kept; duplicate headings get -1/-2 suffixes.
#
# Usage: tools/check_spec_anchors.sh [--base <ref>]
#   (exit 0 = clean, 1 = violations)
#
# --base <ref> (round 27, review-17 item 42): when a merge-base ref is
#   given, fragments that are themselves added or modified in
#   `git diff --name-only <ref>...HEAD` get an extra check - every
#   adds/modifies/removes anchor must point at a spec file that also
#   changed in the same diff, so a "modifies" entry can no longer
#   point at an untouched requirement. Pending fragments NOT part of
#   the diff keep the plain existence checks (round 27 fixup): they
#   live between releases and are folded by a separate release-prep
#   PR, so an old fragment must not fail a PR that never touched its
#   spec file. A fragment inside the diff that declares a bare "-"
#   noop while the same diff touches docs/specs/*.md is REJECTED
#   (round 31, swe-2 item 40): the spec edit is documented behavior
#   changing, so it must be anchored, not noop'd away. Without
#   --base the existence checks above still apply
#   (a bare "-" noop remains undeclarable - a script cannot prove no
#   behavior changed).
set -euo pipefail

BASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="${2:?--base needs a ref}"; shift 2 ;;
    *) echo "usage: check_spec_anchors.sh [--base <ref>]" >&2; exit 2 ;;
  esac
done

TOP=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo" >&2; exit 1; }
CHANGES="$TOP/.changes"

[ -d "$CHANGES" ] || { echo "no .changes/ directory - nothing to check"; exit 0; }

CHANGED_PATHS=""
SPEC_BASE_SET=0
if [ -n "$BASE" ]; then
  SPEC_BASE_SET=1
  if git merge-base "$BASE" HEAD >/dev/null 2>&1; then
    CHANGED_PATHS=$(git diff --name-only "$BASE...HEAD") \
      || { echo "cannot diff $BASE...HEAD" >&2; exit 1; }
  else
    CHANGED_PATHS=$(git diff --name-only "$BASE" HEAD) \
      || { echo "cannot diff $BASE HEAD" >&2; exit 1; }
  fi
fi
export CHANGED_PATHS SPEC_BASE_SET

CHANGES_DIR="$CHANGES" TOP_DIR="$TOP" python3 - <<'PYEOF'
import os, re, sys

changes_dir = os.environ["CHANGES_DIR"]
top = os.environ["TOP_DIR"]
# None when --base was not given (existence-only mode); otherwise a
# possibly EMPTY set of repo-relative paths changed in the diff.
# The diff is split in two: spec files touched by the PR (the
# enforced anchor-target set) and fragments themselves added or
# modified by the PR. The "spec must have changed" rule applies
# ONLY to fragments inside the diff - pending fragments from
# earlier rounds live between releases and must not fail a PR that
# never touched their spec file (round 27 fixup).
_raw = os.environ.get("CHANGED_PATHS", "")
changed_paths = (
    set(_raw.splitlines())
    if os.environ.get("SPEC_BASE_SET", "0") == "1"
    else None
)
changed_specs = changed_fragments = None
if changed_paths is not None:
    changed_specs = {
        p for p in changed_paths
        if p.startswith("docs/specs/") and p.endswith(".md")
    }
    changed_fragments = {
        p for p in changed_paths
        if p.startswith(".changes/") and p.endswith(".md")
    }

ANCHOR_OPS = ("adds", "modifies", "removes")
NOOP = {"-", "- none", "none", "- n/a", "n/a"}

def slugify(title):
    """GitHub's heading anchor rule: lowercase, drop punctuation
    (anything not letter/number/space/hyphen/underscore), spaces ->
    hyphens. Hyphen and underscore runs are preserved as-is."""
    s = re.sub(r"[^\w\- ]", "", title.lower())
    return s.replace(" ", "-")

def requirement_anchors(path):
    """All requirement anchors present in a spec file, including
    GitHub's -1/-2 dedup suffixes for repeated heading names."""
    anchors = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^###\s+(Requirement:\s*.+?)\s*$", line)
                if not m:
                    continue
                # GitHub slugs the whole heading text - the
                # "Requirement:" prefix is part of the anchor.
                base = slugify(m.group(1))
                anchor, n = base, 0
                while anchor in anchors:
                    n += 1
                    anchor = f"{base}-{n}"
                anchors.add(anchor)
    except FileNotFoundError:
        return None
    return anchors

def parse_impact(path):
    """Extract Spec impact entries from a fragment, or None."""
    lines = open(path, encoding="utf-8").read().splitlines()
    # frontmatter occupies [0 .. second '---']; the section lives after
    end = next(
        (i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"),
        None,
    ) if lines and lines[0].strip() == "---" else None
    body = lines[end + 1:] if end is not None else lines
    start = next(
        (i for i, l in enumerate(body) if re.match(r"^##\s+Spec impact\s*$", l)),
        None,
    )
    if start is None:
        return None
    entries = []
    for line in body[start + 1:]:
        if re.match(r"^##\s", line):
            if re.match(r"^##\s+Spec impact\s*$", line):
                entries.append(("malformed", "duplicate '## Spec impact' section"))
            break
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^-\s*(\w+):\s*(\S+)\s*$", s)
        if m:
            entries.append((m.group(1).lower(), m.group(2)))
        elif s in NOOP:
            entries.append(("noop", s))
        else:
            entries.append(("malformed", s))
    return entries

files = sorted(
    f for f in os.listdir(changes_dir)
    if f.endswith(".md") and not f.startswith("_") and f != "README.md"
)
if not files:
    print("no pending .changes entries - nothing to check")
    sys.exit(0)

problems = []
anchor_cache = {}
for name in files:
    # repo-relative fragment path, matched against the --base diff set
    frag_rel = os.path.relpath(os.path.join(changes_dir, name), top)
    entries = parse_impact(os.path.join(changes_dir, name))
    if entries is None:
        problems.append(f"{name}: missing '## Spec impact' section")
        continue
    if not entries:
        problems.append(f"{name}: empty '## Spec impact' section - use '-' when no behavior changes")
        continue
    kinds = {k for k, _ in entries}
    if "noop" in kinds and kinds - {"noop", "malformed"}:
        problems.append(f"{name}: '-' cannot be combined with anchor entries")
        continue
    # round 31, swe-2 item 40: in --base mode a fragment that is
    # itself part of the diff cannot declare a noop while the same
    # diff touches docs/specs/*.md - the spec edit IS documented
    # behavior changing, so it must be anchored, not noop'd away.
    # Fragments outside the diff keep the old semantics (pending
    # entries awaiting the release-prep fold).
    if (
        "noop" in kinds
        and changed_specs
        and frag_rel in changed_fragments
    ):
        problems.append(
            f"{name}: '-' noop declared but the diff touches "
            f"{sorted(changed_specs)} - a spec edit must be anchored"
        )
        continue
    for op, ref in entries:
        if op == "noop":
            continue
        if op == "malformed":
            problems.append(f"{name}: unparsable Spec impact line: {ref!r}")
            continue
        if op not in ANCHOR_OPS:
            problems.append(f"{name}: unknown Spec impact op '{op}' (use adds/modifies/removes or '-')")
            continue
        spec_rel, _, anchor = ref.partition("#")
        if not anchor or not spec_rel.endswith(".md") or "/" not in spec_rel:
            problems.append(f"{name}: bad anchor ref '{ref}' - expected docs/specs/<domain>.md#<anchor>")
            continue
        norm = os.path.normpath(spec_rel)
        if norm != spec_rel or not norm.startswith("docs/specs/"):
            problems.append(f"{name}: Spec impact must target docs/specs/, got '{spec_rel}'")
            continue
        spec_abs = os.path.join(top, spec_rel)
        if spec_rel not in anchor_cache:
            anchor_cache[spec_rel] = requirement_anchors(spec_abs)
        anchors = anchor_cache[spec_rel]
        if anchors is None:
            if op == "removes":
                continue  # the whole file is gone - the anchor with it
            problems.append(f"{name}: '{spec_rel}' does not exist (needed by {op}: {anchor})")
            continue
        if op in ("adds", "modifies") and anchor not in anchors:
            problems.append(
                f"{name}: {op} anchor '#{anchor}' not found in {spec_rel}"
            )
        if op == "removes" and anchor in anchors:
            problems.append(
                f"{name}: removes anchor '#{anchor}' still present in {spec_rel}"
            )
        # --base mode (round 27, review-17 item 42 + fixup): for a
        # fragment that is itself added or touched by this diff, the
        # anchored spec file MUST also be part of the PR's spec
        # delta - an adds/modifies/removes entry pointing at an
        # untouched spec file is a stale or vacuous declaration.
        # Fragments outside the diff skip this check entirely: they
        # are pending entries awaiting the release-prep fold, not
        # claims this PR makes.
        if (
            changed_specs is not None
            and frag_rel in changed_fragments
            and spec_rel not in changed_specs
        ):
            problems.append(
                f"{name}: {op} anchor '#{anchor}' targets {spec_rel}, "
                "which is unchanged in this diff - update the spec or "
                "drop the anchor"
            )

for p in problems:
    print(f"FAIL: {p}", file=sys.stderr)
if problems:
    sys.exit(1)
print(f"spec anchors OK ({len(files)} entries)")
PYEOF
