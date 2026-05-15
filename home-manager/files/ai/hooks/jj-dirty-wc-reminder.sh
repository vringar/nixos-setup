#!/usr/bin/env bash
# SessionStart hook: warn Claude if the jj working copy already has changes
# at session start (startup or clear), so it can suggest `jj new` before
# starting new work. Splitting a dirty working copy later can be quite tricky.

set -euo pipefail

cat > /dev/null

command -v jj >/dev/null 2>&1 || exit 0
jj root >/dev/null 2>&1 || exit 0

CHANGED=$(jj diff --summary 2>/dev/null)
[ -z "$CHANGED" ] && exit 0

COUNT=$(echo "$CHANGED" | wc -l | tr -d ' ')
jq -n --arg count "$COUNT" --arg files "$CHANGED" '
  {
    "hookSpecificOutput": {
      "hookEventName": "SessionStart",
      "additionalContext": (
        "The jj working copy already has " + $count + " changed file(s):\n"
        + $files
        + "\nConsider suggesting `jj new` before starting new work — splitting a dirty working copy later can be quite tricky."
      )
    }
  }
'
