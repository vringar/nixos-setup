# PostToolUse hook: show `jj diff --stat` after `jj squash`

## Problem

When Claude runs `jj squash` it has no visibility into what was actually moved.
The command's own output contains only the new commit IDs — no file list, no
stat, nothing about what changed hands. In a workspace where the working-copy
commit (`@`) holds both WIP scratch material and real in-progress work, Claude
has squashed the wrong files into an underlying commit more than once because it
didn't inspect the full contents of `@` before acting.

A pre-squash check (`jj diff -r @ --stat`) would fix this but requires blocking
the tool call unconditionally, which creates a clunky acknowledge-and-retry loop
and bloats context on every squash. A lighter reactive approach is preferred.

## Proposed solution

Add a `PostToolUse` hook on the `Bash` tool that:

1. Checks whether the executed command contained `jj squash`
2. If so, runs `jj diff -r @- --stat` and injects the output into Claude's context

Claude then immediately sees what landed in the destination commit. If something
looks wrong, `jj undo` is a clean escape hatch before any push happens.

## Why PostToolUse rather than PreToolUse

- **PreToolUse** can block the command (non-zero exit), but there is no
  "acknowledged — let me through" signal. The hook would block every squash
  unconditionally, forcing a workaround convention (e.g. a `# reviewed` comment
  in the command string) that is fragile and adds noise.
- **PostToolUse** is reactive: one stat block appears in context only when a
  squash occurs, no blocking, no extra round-trips. The `jj undo` escape hatch
  covers the case where the squash was wrong.

## Expected hook shape (pseudocode)

```bash
# PostToolUse — Bash
if echo "$CLAUDE_TOOL_INPUT_COMMAND" | grep -q "jj squash"; then
  jj diff -r @- --stat
fi
```

The hook should run from the correct workspace directory (the repo may be a jj
workspace inside `.workspace/<name>/`).

## Acceptance criteria

- After any Bash call containing `jj squash`, the stat of the destination commit
  (`@-`) is automatically injected into Claude's context.
- No effect on Bash calls that do not contain `jj squash`.
- Works correctly inside jj workspaces (`.workspace/` subdirectories).
