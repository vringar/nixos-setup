#!/usr/bin/env bash
# jj-check-wip: Block push if any commit in trunk()..@ has a WIP description.
#
# WIP commits are a "floating commit" pattern — a commit kept on top of the
# local stack to hold debug scripts, credentials, or exploratory code that
# must never leave the machine. Descriptions starting with "WIP" (case-insensitive)
# are treated as WIP commits.
#
# Optional argument: revset to check (default: trunk()..@).
# Pass the output of jj-push-revset to check only the commits being pushed.
set -euo pipefail

REVSET="${1:-trunk()..@}"
found=0

while IFS= read -r line; do
  change_id="${line%% *}"
  description="${line#* }"
  echo "error: commit $change_id is a WIP commit and must not be pushed: $description" >&2
  found=1
done < <(
  jj log -r "$REVSET" --no-graph \
    -T 'change_id ++ " " ++ description.first_line() ++ "\n"' 2>/dev/null \
    | awk 'tolower($2) ~ /^wip/' \
    || true
)

if [[ $found -ne 0 ]]; then
  echo >&2
  echo "Push blocked: WIP commits must stay local. Move changes below the WIP commit or drop them." >&2
  exit 1
fi
