#!/usr/bin/env bash
# PreToolUse/Bash hook: deny `gh issue|pr create|comment|edit --body ...`,
# nudging the agent to write the body to a file with the Write tool and
# pass `--body-file <path>` instead. Avoids backtick / heredoc escaping bugs
# when the body contains code blocks or markdown.

if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0

# All four conditions must hold:
#   1. `gh` invoked as a command (start, after whitespace, or after env-var prefix)
#   2. subcommand group `issue` or `pr` somewhere on the line
#   3. action `create`, `comment`, or `edit` somewhere on the line
#   4. `--body` followed by space, `=`, or end-of-string (NOT `--body-file`)
if [[ "$CMD" =~ (^|[^[:alnum:]_-])gh[[:space:]] ]] && \
   [[ "$CMD" =~ [[:space:]](issue|pr)[[:space:]] ]] && \
   [[ "$CMD" =~ [[:space:]](create|comment|edit)([[:space:]]|$) ]] && \
   [[ "$CMD" =~ --body([[:space:]=]|$) ]]; then

  # shellcheck disable=SC2016  # backticks here are literal markdown, not command substitution
  REASON='Use `--body-file <path>` instead of `--body "..."` for `gh issue|pr create|comment|edit`. Write the body to a temp file with the Write tool first — this avoids shell-escaping bugs with backticks, code blocks, and heredoc terminators. Example: Write tool → /tmp/body.md, then `gh issue create --title "..." --body-file /tmp/body.md`.'

  jq -n --arg reason "$REASON" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
fi

exit 0
