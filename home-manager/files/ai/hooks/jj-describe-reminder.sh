#!/usr/bin/env bash
# Stop hook: block the session from ending if the current jj change has
# uncommitted work but no description.
#
# Uses the Stop hook JSON API:
#   {"decision": "block", "reason": "<message injected back to assistant>"}

set -euo pipefail

# Consume stdin (required by the advanced Stop hook API even if unused)
cat > /dev/null

# If @ already has a description, nothing to do
if jj log -r @ --no-graph -T 'description' 2>/dev/null | grep -q .; then
  exit 0
fi

# If @ has no changes either, nothing to do
if ! jj diff --stat -r @ 2>/dev/null | grep -q .; then
  exit 0
fi

# Change with no description — block stop and remind
jq -n '{
  "decision": "block",
  "reason": "The current jj change (@) has uncommitted modifications but no description. Please:\n1. Run `jj describe -m \"<message>\"` to describe the change\n2. Run `jj new` to start a fresh change\nThen you may stop."
}'
