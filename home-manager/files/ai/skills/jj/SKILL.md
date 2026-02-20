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

See [references/commands.md](references/commands.md) and [references/conflicts.md](references/conflicts.md).
