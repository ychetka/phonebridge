#!/usr/bin/env bash
# tools/release_notes.sh - aggregate .changes/*.md into release notes.
#
# Usage:
#   tools/release_notes.sh                      print both sections
#   tools/release_notes.sh --version X.Y.Z      print with a
#                                               "## [X.Y.Z] - <date>" header
#   tools/release_notes.sh --version X.Y.Z --apply
#                                               insert the section into
#                                               CHANGELOG.md (after the
#                                               title) and delete the
#                                               consumed .changes files
#
# Zero dependencies beyond POSIX bash + python3 (repo standard).
# .changes/ holds only unreleased entries by convention, so "since the
# last tag" is simply "all files present". See .changes/README.md.
set -euo pipefail

TOP=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo" >&2; exit 1; }
CHANGES="$TOP/.changes"
CHANGELOG="$TOP/CHANGELOG.md"

VERSION=""
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="${2:?--version needs a version}"; shift 2 ;;
    --apply)   APPLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
if [ -n "$VERSION" ] && ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "--version must be X.Y.Z, got '$VERSION'" >&2; exit 2
fi
if [ "$APPLY" = 1 ] && [ -z "$VERSION" ]; then
  echo "--apply requires --version" >&2; exit 2
fi
[ -d "$CHANGES" ] || { echo "no .changes/ directory" >&2; exit 1; }

GENERATED=$(CHANGES_DIR="$CHANGES" VERSION="$VERSION" python3 - <<'PYEOF'
import os, re, sys, datetime

changes_dir = os.environ["CHANGES_DIR"]
version = os.environ.get("VERSION", "")

FIELDS = ("type", "scope", "user_visible", "breaking",
          "user_summary", "dev_summary", "why", "refs")
TYPES = ("feat", "fix", "refactor", "docs", "chore")

def _close_quote(s, q):
    """Index of the closing quote in s (s[0] is the open quote), or
    -1. Single quotes escape by doubling (''), double quotes by
    backslash."""
    i = 1
    while i < len(s):
        c = s[i]
        if q == '"' and c == "\\":
            i += 2
            continue
        if c == q:
            if q == "'" and s[i:i + 2] == "''":
                i += 2
                continue
            return i
        i += 1
    return -1

def unquote_scalar(s):
    """Unquote a COMPLETE quoted scalar: strip the matching quotes
    and decode YAML escapes ('' inside single quotes, backslash
    escapes inside double quotes). Unquoted input is returned
    verbatim - matching-quote stripping only (round 31, swe-2 item
    40; escape decoding added round 32, swe-2 item 27)."""
    if len(s) >= 2 and s[0] in ("\"", "'"):
        idx = _close_quote(s, s[0])
        if idx == len(s) - 1:
            body = s[1:-1]
            if s[0] == "'":
                return body.replace("''", "'")
            return re.sub(
                r"\\(.)",
                lambda m: {
                    "n": "\n", "t": "\t", "r": "\r",
                    "\\": "\\", '"': '"', "'": "'",
                }.get(m.group(1), m.group(1)),
                body,
            )
    return s

def parse(path):
    name = os.path.basename(path)
    lines = open(path, encoding="utf-8").read().splitlines()
    if not lines or lines[0].strip() != "---":
        raise SystemExit(f"{name}: missing --- frontmatter")
    end = next(
        (i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"),
        None,
    )
    if end is None:
        raise SystemExit(f"{name}: unterminated --- frontmatter")
    fm, last = {}, None
    fm_lines = lines[1:end]
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        i += 1
        s = line.strip()
        if s.startswith("#") or not s:
            continue
        km = re.match(r"^([A-Za-z_][\w]*)\s*:(.*)$", line)
        if km and not line[0].isspace():
            last = km.group(1)
            rest = km.group(2).strip()
            if rest in (">", ">-", ">+", "|", "|-", "|+"):
                # YAML block scalar: consume the indented continuation
                # lines (plus blank lines between them); a margin-level
                # comment line is skipped without ending the block.
                folded = rest.startswith(">")
                block = []
                while i < len(fm_lines):
                    cont = fm_lines[i]
                    cs = cont.strip()
                    if not cs:
                        block.append("")
                        i += 1
                    elif not cont[0].isspace():
                        if cs.startswith("#"):
                            i += 1
                            continue
                        break
                    else:
                        block.append(cs)
                        i += 1
                while block and not block[-1]:
                    block.pop()
                if folded:
                    # '>' folds lines of a paragraph into spaces;
                    # blank lines separate paragraphs with a newline.
                    paras, cur = [], []
                    for b in block:
                        if b:
                            cur.append(b)
                        elif cur:
                            paras.append(" ".join(cur))
                            cur = []
                    if cur:
                        paras.append(" ".join(cur))
                    fm[last] = "\n".join(paras)
                else:
                    # '|' keeps one line of content per source line.
                    fm[last] = "\n".join(block)
            else:
                # A QUOTED scalar may span frontmatter lines (YAML
                # folds its embedded newlines to single spaces):
                # if `rest` opens a quote that does not close on
                # this line, keep consuming lines until the closing
                # quote lands, then unquote properly - stripping
                # first/last chars is not unquoting (round 32,
                # swe-2 item 27): a folded double-quoted scalar must
                # have its backslash escapes decoded and a folded
                # single-quoted scalar its '' escapes, not just
                # lose its outer quote pair.
                if (
                    rest[:1] in ("\"", "'")
                    and _close_quote(rest, rest[0]) < 0
                ):
                    q = rest[0]
                    while i < len(fm_lines):
                        rest += " " + fm_lines[i].strip()
                        i += 1
                        if _close_quote(rest, q) >= 0:
                            break
                # Drop any trailing whitespace/comment after the
                # closing quote so unquote_scalar sees a complete
                # scalar; a trailing non-comment keeps the value
                # verbatim (malformed YAML would fail elsewhere).
                if rest[:1] in ("\"", "'"):
                    idx = _close_quote(rest, rest[0])
                    if idx >= 0:
                        tail = rest[idx + 1:].strip()
                        if not tail or tail.startswith("#"):
                            rest = rest[: idx + 1]
                fm[last] = unquote_scalar(rest)
        elif last:
            fm[last] = (fm[last] + " " + s).strip()
    missing = [f for f in FIELDS if f not in fm]
    if missing:
        raise SystemExit(f"{name}: missing fields: {', '.join(missing)}")
    if fm["type"] not in TYPES:
        raise SystemExit(f"{name}: bad type {fm['type']!r}")
    for b in ("user_visible", "breaking"):
        if fm[b] not in ("true", "false"):
            raise SystemExit(f"{name}: {b} must be true|false")
    if fm["user_visible"] == "true" and fm["user_summary"] in ("", "-"):
        raise SystemExit(f"{name}: user_visible entry needs user_summary")
    return fm

files = sorted(
    f for f in os.listdir(changes_dir)
    if f.endswith(".md") and not f.startswith("_") and f != "README.md"
)
entries = [parse(os.path.join(changes_dir, f)) for f in files]
if not entries:
    raise SystemExit(".changes/ has no pending entries")

out = []
if version:
    out.append(f"## [{version}] - {datetime.date.today().isoformat()}")
    out.append("")

# --- User-facing: only user_visible entries, grouped by type -------
visible = [e for e in entries if e["user_visible"] == "true"]
out.append("### User-facing")
out.append("")
if not visible:
    out.append("_No user-visible changes._")
    out.append("")
for t in TYPES:
    group = [e for e in visible if e["type"] == t]
    if not group:
        continue
    out.append(f"#### {t}")
    for e in group:
        refs = f" ({e['refs']})" if e["refs"] not in ("", "-") else ""
        brk = " **[breaking]**" if e["breaking"] == "true" else ""
        out.append(f"- {e['user_summary']} `{e['scope']}`{brk}{refs}")
    out.append("")

# --- Codebase: every entry, grouped by scope ------------------------
out.append("### Codebase")
out.append("")
scopes = {}
for e in entries:
    scopes.setdefault(e["scope"], []).append(e)
for scope in sorted(scopes):
    out.append(f"#### {scope}")
    for e in scopes[scope]:
        refs = f" ({e['refs']})" if e["refs"] not in ("", "-") else ""
        out.append(f"- [{e['type']}] {e['dev_summary']} Why: {e['why']}{refs}")
    out.append("")

sys.stdout.write("\n".join(out).rstrip() + "\n")
PYEOF
) || exit 1

if [ "$APPLY" = 0 ]; then
  printf '%s\n' "$GENERATED"
  exit 0
fi

# --apply: fold into CHANGELOG.md above the first release section
# (the title and any preamble stay on top), then drop consumed files.
# The rewrite goes through a temp file + os.replace so a crash cannot
# leave a truncated CHANGELOG. The generated notes travel in a temp
# FILE, never the environment (round 32, swe-2 item 27): env vars
# share ARG_MAX/MAX_ARG_STRLEN with argv (~128 KiB per string), so a
# large enough notes payload would make the python child fail to
# exec at all.
[ -f "$CHANGELOG" ] || { printf '# Changelog\n' > "$CHANGELOG"; }
# Stale-coverage guard (round 33, iteration-1 audit item 32): the
# fold is idempotent - a CHANGELOG that already carries the target
# version section means these fragments were already folded (the
# release-prep ran before) or the version argument is wrong; either
# way re-appending would duplicate the section and double-delete
# nothing, so refuse loudly instead of corrupting the changelog.
if grep -qF "## [$VERSION]" "$CHANGELOG"; then
  echo "FAIL: CHANGELOG.md already has a '## [$VERSION]' section -" >&2
  echo "the fragments were already folded (stale leftovers) or the" >&2
  echo "--version argument is wrong. Nothing applied." >&2
  exit 1
fi
NOTES_FILE=$(mktemp) || { echo "mktemp failed" >&2; exit 1; }
trap 'rm -f "$NOTES_FILE"' EXIT
printf '%s\n' "$GENERATED" > "$NOTES_FILE"
python3 - "$CHANGELOG" "$NOTES_FILE" <<'PYEOF'
import os, sys, re, tempfile
path, notes_path = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
new = open(notes_path, encoding="utf-8").read().rstrip() + "\n"
m = re.search(r"(?m)^## ", text)
if m:
    out = text[: m.start()] + new + "\n" + text[m.start():]
else:
    out = text.rstrip() + "\n\n" + new
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(out)
os.replace(tmp, path)
PYEOF

for f in "$CHANGES"/*.md; do
  base=$(basename "$f")
  case "$base" in _*|README.md) ;; *) rm -f "$f" ;; esac
done
echo "{\"status\":\"applied\",\"version\":\"$VERSION\",\"changelog\":\"$CHANGELOG\"}"
