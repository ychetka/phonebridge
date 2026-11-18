#!/usr/bin/env bash
# check_version_tag.sh - assert the current [project].version has a
# git tag ON THE REMOTE that marks the commit this run checks out, and
# that the pyproject.toml AT THE TAGGED COMMIT carries the same
# version. Runs after merges to the base branch so a version bump
# merged without a release cannot stay invisible (the 2026-11-12
# version-tag-drift incident).
#
# Round 32, swe-2 item 27: the check is remote-truth based, never
# local-ancestry based - a tag that exists only in the local clone
# (or was force-deleted upstream) must not pass, and a merge that
# forgot the version bump must not pass against the PREVIOUS tag.
#
# Round 33, iteration-1 audit finding 20: the comparison is pinned to
# the tagged commit itself, NEVER to the moving remote main tip. The
# old form asserted "tag's peeled commit == remote main tip", so a
# later merge landing while an earlier release run was still polling
# invalidated that run retroactively. The pinned rule: the remote tag
# must mark THIS run's commit (HEAD, or VERSION_TAG_EXPECT_SHA when
# the workflow passes the triggering sha explicitly), and the
# pyproject.toml read back FROM the tagged commit on the remote must
# carry the tag's own version - what a later main-tip merge does is
# irrelevant to an already-triggered run.
#
# Polls the remote for up to VERSION_TAG_TIMEOUT seconds (default
# 600) because the release step lands a few seconds after the merge
# push. A timeout of 0 means "check once immediately, no polling"
# (round 31, swe-2 item 40 - previously it made the poll loop exit
# without a single check).
#
# Env overrides: VERSION_TAG_REMOTE (default: the repo's first
# remote, usually 'origin' or 'github'), VERSION_TAG_EXPECT_SHA
# (default: HEAD - CI passes the triggering commit explicitly).
# Usage: check_version_tag.sh [timeout_seconds]
set -uo pipefail

TIMEOUT="${1:-${VERSION_TAG_TIMEOUT:-600}}"
case "$TIMEOUT" in
  ''|*[!0-9]*)
    echo "FAIL: timeout must be a non-negative integer, got '$TIMEOUT'" >&2
    exit 2 ;;
esac

version_from_stream() {
  awk -F= '
    /^\[project\][[:space:]]*$/ {inp=1; next}
    /^\[/ {inp=0}
    inp && /^[[:space:]]*version[[:space:]]*=/ {
      v=$2; gsub(/["'"'"'[:space:]]/, "", v); print v; exit }'
}

VER=$(version_from_stream < pyproject.toml 2>/dev/null)

if [[ ! "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "FAIL: cannot parse [project].version from pyproject.toml"
  exit 1
fi

EXPECT_SHA="${VERSION_TAG_EXPECT_SHA:-}"
if [ -z "$EXPECT_SHA" ]; then
  EXPECT_SHA=$(git rev-parse HEAD 2>/dev/null || true)
fi
if [[ ! "$EXPECT_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "FAIL: cannot resolve the commit this run must check (HEAD)"
  exit 1
fi

REMOTE="${VERSION_TAG_REMOTE:-}"
if [ -z "$REMOTE" ]; then
  REMOTE=$(git remote | head -n 1)
fi
if [ -z "$REMOTE" ]; then
  echo "FAIL: no git remote configured - cannot verify a remote tag"
  exit 1
fi
TAG="v$VER"
echo "checking remote $REMOTE for tag $TAG on commit ${EXPECT_SHA:0:8} (timeout ${TIMEOUT}s)"

# Peeled commit sha of the remote tag: ls-remote lists an annotated
# tag twice - the tag object and its '^{}' peeled commit; a
# lightweight tag lists once. Prefer the peeled line, else take the
# ref line itself.
remote_tag_sha() {
  local out peeled plain
  out=$(git ls-remote "$REMOTE" \
        "refs/tags/$TAG^{}" "refs/tags/$TAG" 2>/dev/null) || return 1
  peeled=$(printf '%s\n' "$out" | awk -v r="refs/tags/$TAG^{}" '$2==r {print $1}')
  if [ -n "$peeled" ]; then printf '%s' "$peeled"; return 0; fi
  plain=$(printf '%s\n' "$out" | awk -v r="refs/tags/$TAG" '$2==r {print $1}')
  [ -n "$plain" ] && printf '%s' "$plain"
  # ls-remote succeeded even when the tag is absent - an empty
  # printout is "tag missing", distinct from the transport failure
  # above which returns 1.
  return 0
}

elapsed=0
LAST_DIAG="remote never answered"
# Run the check at least once regardless of TIMEOUT (round 31,
# swe-2 item 40): with TIMEOUT=0 this loop performs a single
# immediate check and falls through to FAIL - the old `-lt` guard
# skipped the body entirely, so a 0 timeout false-failed without
# ever looking at the remote.
while true; do
  if tag_sha=$(remote_tag_sha); then
    tag_ok=1
  else
    tag_sha=""
    tag_ok=0
  fi
  if [ "$tag_ok" = 0 ]; then
    LAST_DIAG="cannot ls-remote $REMOTE - remote tag check impossible"
  elif [ -z "$tag_sha" ]; then
    LAST_DIAG="remote has no tag $TAG"
  elif git fetch --quiet "$REMOTE" "refs/tags/$TAG" 2>/dev/null; then
    # The tag exists remotely: fetch it and read pyproject.toml AT
    # THE TAGGED COMMIT (round 33 - never from the moving main tip,
    # so a later merge cannot invalidate an earlier release run).
    tagged_ver=$(git show "FETCH_HEAD^{}:pyproject.toml" 2>/dev/null \
                 | version_from_stream || true)
    if [ -z "$tagged_ver" ]; then
      LAST_DIAG="cannot read pyproject.toml at tag $TAG's commit ${tag_sha:0:8}"
    elif [ "$tagged_ver" != "$VER" ]; then
      LAST_DIAG="tag $TAG marks a commit whose pyproject reads '$tagged_ver', not '$VER'"
    elif [ "$tag_sha" != "$EXPECT_SHA" ]; then
      LAST_DIAG="tag $TAG marks ${tag_sha:0:8}, but this run's commit is ${EXPECT_SHA:0:8} - the release must tag the merged commit"
    else
      echo "PASS: remote tag $TAG marks this run's commit ${EXPECT_SHA:0:8} (version $VER at the tagged commit)"
      exit 0
    fi
  else
    LAST_DIAG="cannot fetch tag $TAG from $REMOTE - remote check impossible"
  fi
  [ "$elapsed" -ge "$TIMEOUT" ] && break
  remaining=$((TIMEOUT - elapsed))
  [ "$remaining" -gt 30 ] && remaining=30
  sleep "$remaining"
  elapsed=$((elapsed + remaining))
done

echo "FAIL: $LAST_DIAG (after ${elapsed}s)"
echo "A merged version bump without a release is the version-tag drift"
echo "incident - tag $TAG on the merge commit and push it (repo_release.sh)."
exit 1
