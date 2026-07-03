#!/usr/bin/env bash
# Stop hook: block the session from ending if the current jj change has
# uncommitted work but no description.
#
# Uses the Stop hook JSON API:
#   {"decision": "block", "reason": "<message injected back to assistant>"}

set -euo pipefail

# Consume stdin (required by the advanced Stop hook API even if unused)
cat > /dev/null

# Not inside a jj repo — stay silent. Nagging here would push the agent to
# `jj git init` a repo deliberately kept on plain git (e.g. for git-lfs).
if ! jj root >/dev/null 2>&1; then
  exit 0
fi

# If @ already has a description, nothing to do
if jj log -r @ --no-graph -T 'description' 2>/dev/null | grep -q .; then
  exit 0
fi

# If @ has no changes either, nothing to do.
# Use the `empty` template, not `jj diff --stat`: the latter always prints a
# "0 files changed" summary line, which a content check would misread as work.
if [ "$(jj log -r @ --no-graph -T 'empty' 2>/dev/null)" = "true" ]; then
  exit 0
fi

# Change with no description — block stop and remind
jq -n '{
  "decision": "block",
  "reason": "The current jj change (@) has modifications but no description. Act without asking the user — check the parent (`jj log -r @- --no-graph -T '\''description'\''`) and either squash into it if the changes are a natural continuation, or describe with an appropriate message if they are standalone."
}'
