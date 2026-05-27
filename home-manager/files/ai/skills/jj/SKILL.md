---
name: jj
description: Use when working with Jujutsu (jj) version control - commits, rebases, conflicts, splits, and push workflows.
---

# /jj - Jujutsu VCS Operations

## Quick Start

```bash
jj new -m "start new work"    # create commit
jj describe -m "update msg"   # set message
jj squash --use-destination-message  # squash into parent (keep parent's msg)
jj squash -m "message"        # squash into parent with new combined msg
```

## Non-Interactive Commands

Always use flags to avoid hanging on interactive editors:

| Command | Non-interactive form |
|---------|---------------------|
| `jj split` | `jj split -r <rev> -m "message" <files>` |
| `jj describe` | `jj describe -m "message"` |
| `jj commit` | `jj commit -m "message"` |
| `jj squash` | `jj squash --use-destination-message` (or `-m "message"`) — without one of these flags jj opens `$EDITOR` to combine the source/destination descriptions and will hang under any non-TTY runner |
| `jj new` | `jj new -m "message"` (optional) |

## Critical Recovery — `jj op restore` is the panic button

If a `jj` command produced unexpected state (a wrong `jj restore` wiped your work, a rebase landed sideways, a snapshot ate uncommitted edits), do NOT chain more "fix" commands. Rewind:

```bash
jj op log --limit 10        # find the op id from before things broke
jj op restore <op-id>       # rewinds the entire repo, including the working copy
```

This is itself an op, so it's reversible. Reach for it the moment the working state stops matching expectation — every extra command after a bad op makes recovery harder.

## Conflict Resolution

**Never** use sed, awk, or Edit on conflict markers. Always use `jj resolve`:
1. `jj resolve --tool mergiraf` - First choice (syntax-aware)
2. `jj resolve --tool :ours` / `:theirs` - Take one side
3. `jj resolve --list` - List conflicted files
4. **Generated/lock files** (package-lock.json, Cargo.lock, etc.) - Restore base version then regenerate (see [references/conflicts.md](references/conflicts.md))

## Key Rules

- Use `jj push` (not `jj git push`) — runs pre-commit hooks automatically via `jj-precommit`; works in workspaces. Flags pass through, so `jj push --allow-new` and `jj push --bookmark <name>` work.
- Use `jj-precommit` instead of `pre-commit` directly — workspace-aware wrapper, handles jj workspaces correctly
- `jj edit <change_id>` (no `-r` needed) and `jj describe @-` work from any position — operating on revisions other than `@` is just normal usage, not a special case
- Multiple "and"s in commit message? Consider `jj split`

## Bookmark Gotchas

- **First push of a new bookmark:** `jj push --bookmark <name> --allow-new`. Plain `jj push` refuses with *Refusing to create new remote bookmark*.
- **Moving a bookmark backwards or sideways after a history rewrite:** `jj bookmark set <name> -r @ --allow-backwards`. Required when the new tip is not strictly ahead of the old.
- **Diff against the pushed state:** the revset `<bookmark>@origin` references the remote tip — `jj diff --from main@origin --to @` shows local-vs-pushed; `jj restore --from feat/x@origin <paths>` resets paths to whatever is on origin.
- **Tangled stack?** See the snapshot-rewrite workflow in [references/commands.md](references/commands.md) — often cleaner than chained interactive splits.

## WIP Floating Commits

A "WIP" commit sits on top of the local stack and holds material that must **never be pushed**: debug scripts, temporary credentials, exploratory code, local overrides. It acts as a floating scratchpad that travels with the branch but stays behind when pushing.

**Conventions:**
- Description starts with `WIP` (case-insensitive): `WIP: debug helpers`, `WIP credentials`, etc.
- Keep it as the topmost commit (`@`). Work goes in commits below it.
- `jj push` automatically blocks if any commit in `trunk()..@` is a WIP commit — this is enforced by `jj-check-wip`.

**Working with a WIP commit:**

When working with floating commits, keep the working copy on the floating commit and only split out the changes that you want to commit. If your current chunk and the previous chunk are related, use `jj squash` to fold them together and keep history compact.

```bash
# Create one
jj new -m "WIP: debug helpers"

# Edit on the WIP — reference artifacts, debug scratch, and the in-progress
# real work all live in your working copy together, so the WIP material stays
# directly readable while you write.

# When a coherent chunk is ready to land below the WIP, split it out:
jj split -r @ -m "real commit description" <files-belonging-to-release>
# `@` stays on the WIP; the named files move into a new commit below it.

# If that new chunk extends the commit already below WIP, squash them together.
# Always pass --use-destination-message (or -m "..."): otherwise jj opens
# $EDITOR to combine the descriptions and hangs in a non-TTY runner.
jj squash --from @- --into @-- --use-destination-message

# Inspect what's in the WIP commit vs real commits
jj log -r 'trunk()..@'
```

**Do NOT attempt to push WIP commits or work around the push guard.** If `jj push` is blocked, reorganize the stack so WIP content is in a WIP-prefixed commit above the range being pushed.

## Workspaces

If you're inside a `.workspace/<name>/` directory, you're in a jj workspace.

**Critical rules:**
- Run ALL jj commands from your workspace directory — never use `-R`, `--repository`, or `cd ..` to point elsewhere
- `jj status` from your workspace shows YOUR working copy. Running it from the parent or with `-R ../..` shows the `default` workspace's state — this WILL cause you to act on the wrong changes
- `jj workspace update-stale` — run this if jj says your workspace is stale
- `jj workspace list` — see all workspaces (informational only, don't operate on others)

**Why this matters:** Each workspace has its own `@` revision and working copy. The repo history is shared, but in-progress changes are isolated. Using `-R` to point at the root repo puts you in the `default` workspace's context, where you'll see unrelated changes and potentially corrupt another workspace's state.

### Sandbox sessions: prefer a fresh workspace

When you're invoked from inside `claude-sandbox` (or any agent context that shares a jj repo with the user's interactive editor), prefer creating a new workspace over mutating the default `@`:

```bash
jj workspace add .workspace/<task-name>
cd .workspace/<task-name>
```

This isolates your churn (rebases, squashes, snapshot commits, `jj op restore` rewinds) from whatever the user has open live in the default workspace. The user's uncommitted edits stay untouched, and you can rewrite history aggressively without fear of clobbering their state. Tear down with `jj workspace forget <name>` when done.

See [references/commands.md](references/commands.md) and [references/conflicts.md](references/conflicts.md).
