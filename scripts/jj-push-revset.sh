#!/usr/bin/env bash
# jj-push-revset: Compute the jj revset that would actually be pushed
# by `jj git push <args>`, so pre-push checks can validate only those commits.
#
# Outputs a single revset expression to stdout.
# Exits non-zero only on unexpected errors (not on empty range).
set -euo pipefail

bookmarks=()
all=0
tracked=0
remote="origin"

args=("$@")
i=0
while [[ $i -lt ${#args[@]} ]]; do
  case "${args[$i]}" in
    -b | --bookmark)
      i=$((i + 1))
      bookmarks+=("${args[$i]}")
      ;;
    --bookmark=*)
      bookmarks+=("${args[$i]#*=}")
      ;;
    --all) all=1 ;;
    --tracked) tracked=1 ;;
    --remote)
      i=$((i + 1))
      remote="${args[$i]}"
      ;;
    --remote=*)
      remote="${args[$i]#*=}"
      ;;
  esac
  i=$((i + 1))
done

if [[ $all -eq 1 || $tracked -eq 1 ]]; then
  echo "trunk()..bookmarks()"
  exit 0
fi

if [[ ${#bookmarks[@]} -gt 0 ]]; then
  parts=()
  for bm in "${bookmarks[@]}"; do
    if jj log -r "${bm}@${remote}" --no-graph -T '""' 2>/dev/null | grep -q .; then
      parts+=("(${bm}@${remote}..${bm})")
    else
      parts+=("(trunk()..${bm})")
    fi
  done
  (
    IFS='|'
    echo "${parts[*]}"
  )
  exit 0
fi

# Default: no explicit bookmark flags. jj would push tracking bookmarks.
# Use the topmost bookmark in trunk()..@ as the upper bound, which excludes
# any floating commit sitting above the bookmarks (e.g. a WIP commit at @).
bm_tip=$(jj log -r 'latest(trunk()..@ & bookmarks())' --no-graph \
  -T 'change_id' 2>/dev/null || true)
if [[ -n "$bm_tip" ]]; then
  echo "trunk()..$bm_tip"
else
  echo "trunk()..@"
fi
