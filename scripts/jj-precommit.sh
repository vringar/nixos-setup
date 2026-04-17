#!/usr/bin/env bash
# jj-precommit: Run pre-commit on files changed in a jj workspace.
#
# Problem: pre-commit traverses up looking for .git, which in a jj workspace
# points to the REPO root rather than the workspace. Hooks then run against the
# repo root's working copy, not the workspace's files.
#
# Solution: override GIT_WORK_TREE to point at the workspace, and pass the
# changed files explicitly via --files so pre-commit never touches the index.
# For pure-jj repos (no git backend), a temporary bare git dir is created just
# to satisfy pre-commit's requirement for a git root.
#
# Usage:
#   jj-precommit              # diff vs trunk (all branch changes + working copy)
#   jj-precommit --working    # working copy changes only
#   jj-precommit -- <hook>    # run a specific hook
set -euo pipefail

WORKING_ONLY=0
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --working) WORKING_ONLY=1; shift ;;
    --) shift; EXTRA_ARGS+=("$@"); break ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# Workspace root (where .jj lives for this workspace)
WORKSPACE_ROOT=$(jj root 2>/dev/null) || { echo "Not in a jj repo" >&2; exit 1; }

# Changed files: branch diff vs trunk, or working copy only
if [[ $WORKING_ONLY -eq 1 ]]; then
  REVSET="@"
else
  # trunk()..@ = all commits from trunk to @ (inclusive of working copy)
  REVSET="trunk()..@"
fi

mapfile -t CHANGED < <(
  jj diff --summary --no-pager -r "$REVSET" 2>/dev/null \
    | awk '{print $2}' \
    | sort -u
)

if [[ ${#CHANGED[@]} -eq 0 ]]; then
  echo "jj-precommit: no changed files in ${REVSET}" >&2
  exit 0
fi

echo "jj-precommit: checking ${#CHANGED[@]} file(s) from ${REVSET}" >&2

cd "$WORKSPACE_ROOT"

# Find the git dir (may be in a parent when running from a workspace subdir).
# Falls back to a temp bare repo for pure-jj repos with no git backend.
GIT_DIR=$(git -C "$WORKSPACE_ROOT" rev-parse --absolute-git-dir 2>/dev/null || true)
if [[ -z "$GIT_DIR" ]]; then
  _tmpdir=$(mktemp -d)
  trap 'rm -rf "$_tmpdir"' EXIT
  git init -q "$_tmpdir"
  GIT_DIR="$_tmpdir/.git"
fi

GIT_DIR="$GIT_DIR" GIT_WORK_TREE="$WORKSPACE_ROOT" \
  pre-commit run --files "${CHANGED[@]}" "${EXTRA_ARGS[@]}"
