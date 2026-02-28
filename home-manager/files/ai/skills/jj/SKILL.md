---
name: jj
description: Use when working with Jujutsu (jj) version control - commits, rebases, conflicts, splits, and push workflows.
---

# /jj - Jujutsu VCS Operations

## Quick Start

```bash
jj new -m "start new work"    # create commit
jj describe -m "update msg"   # set message
jj squash -m "message"        # squash into parent
```

## Non-Interactive Commands

Always use flags to avoid hanging on interactive editors:

| Command | Non-interactive form |
|---------|---------------------|
| `jj split` | `jj split -r <rev> -m "message" <files>` |
| `jj describe` | `jj describe -m "message"` |
| `jj commit` | `jj commit -m "message"` |
| `jj squash` | `jj squash -m "message"` |
| `jj new` | `jj new -m "message"` (optional) |

## Conflict Resolution

**Never** use sed, awk, or Edit on conflict markers. Always use `jj resolve`:
1. `jj resolve --tool mergiraf` - First choice (syntax-aware)
2. `jj resolve --tool :ours` / `:theirs` - Take one side
3. `jj resolve --list` - List conflicted files

## Key Rules

- Use `jj push` (not `jj git push`) — runs pre-commit hooks automatically
- Recovery: `jj op log --limit 10` then `jj op restore <id>`
- Multiple "and"s in commit message? Consider `jj split`

## Workspaces

If you're inside a `.workspace/<name>/` directory, you're in a jj workspace.

**Critical rules:**
- Run ALL jj commands from your workspace directory — never use `-R`, `--repository`, or `cd ..` to point elsewhere
- `jj status` from your workspace shows YOUR working copy. Running it from the parent or with `-R ../..` shows the `default` workspace's state — this WILL cause you to act on the wrong changes
- `jj workspace update-stale` — run this if jj says your workspace is stale
- `jj workspace list` — see all workspaces (informational only, don't operate on others)

**Why this matters:** Each workspace has its own `@` revision and working copy. The repo history is shared, but in-progress changes are isolated. Using `-R` to point at the root repo puts you in the `default` workspace's context, where you'll see unrelated changes and potentially corrupt another workspace's state.

See [references/commands.md](references/commands.md) and [references/conflicts.md](references/conflicts.md).
