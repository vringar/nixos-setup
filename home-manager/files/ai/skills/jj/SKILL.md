# /jj - Jujutsu VCS Operations

Operational knowledge for working with Jujutsu (jj) version control.

## Conflict Resolution Override

**This skill overrides the AGENTS.md "NEVER resolve conflicts" rule for these safe methods:**

| Method | When to use |
|--------|-------------|
| `jj resolve --tool mergiraf` | First choice - try automatic syntax-aware resolution |
| `jj resolve --tool :ours` | When you know "our" side is correct |
| `jj resolve --tool :theirs` | When you know "their" side is correct |

**Still forbidden:** Using sed, awk, or Edit tool on conflict markers directly. Always use `jj resolve`.

## Non-Interactive Commands

Always use flags to avoid interactive editors (which hang in non-interactive environments):

| Command | Non-interactive form |
|---------|---------------------|
| `jj split` | `jj split -r <rev> -m "message" <files>` |
| `jj describe` | `jj describe -m "message"` |
| `jj commit` | `jj commit -m "message"` |
| `jj new` | `jj new -m "message"` (optional) |

## Common Commands

- `jj new` - Create new commit on top of current
- `jj describe -m "msg"` - Set commit message
- `jj squash` - Squash into parent
- `jj split -r REV -m "msg" FILES` - Split commit (first part gets the message)
- `jj duplicate REV` - Duplicate a commit
- `jj rebase -r REV --after TARGET` - Move commit after target
- `jj rebase -s REV -d TARGET` - Rebase commit and descendants onto target
- `jj restore --from REV FILE` - Restore file from another revision

## Push Alias

**Use `jj push` instead of `jj git push`**

The `jj push` alias (defined in home-manager jujutsu config):
1. Checks if `.pre-commit-config.yaml` exists in the repo
2. If yes, runs `pre-commit run --all-files` on commits in `trunk()..@-`
3. Only pushes if pre-commit passes

If no `.pre-commit-config.yaml` exists, it just runs `jj git push` directly.

## Recovery

When something goes wrong:

```bash
# View recent operations
jj op log --limit 10

# Restore to a previous state
jj op restore <operation-id>
```

## Commit Message Guidelines

- After completing work, use `jj describe -m "message"` to set a clear commit message
- If your message contains multiple "and"s, consider `jj split` to break into focused commits

## Workflow Examples

### Split a file out of a commit
```bash
jj split -r <rev> -m "Description for extracted part" path/to/file.txt
```

### Reorder commits
```bash
# Move commit A after commit B
jj rebase -r A --after B
```

### Undo last operation
```bash
jj op log --limit 2
jj op restore <previous-op-id>
```

## Conflict Resolution

### Non-Interactive Built-in Tools

```bash
# Take "our" side (current branch)
jj resolve --tool :ours <file>

# Take "their" side (incoming changes)
jj resolve --tool :theirs <file>
```

### Automatic Resolution with Mergiraf

[Mergiraf](https://mergiraf.org/) is a syntax-aware merge tool (installed via home-manager) that can automatically resolve many conflicts by understanding the AST of 33+ languages:

```bash
# Try mergiraf first - it handles most syntax-aware conflicts
jj resolve --tool mergiraf <file>

# Or resolve all conflicted files at once
jj resolve --tool mergiraf
```

If mergiraf can't fully resolve, it exits non-zero and leaves conflict markers for manual resolution.

### Manual Resolution

Edit conflict markers directly in the file. jj's default conflict format:
```
<<<<<<< Conflict 1 of 1
%%%%%%% Changes from base to side #1
-old line
+new line from side 1
+++++++ Contents of side #2
new line from side 2
>>>>>>> Conflict 1 of 1 ends
```

After editing, jj automatically detects resolved conflicts on next snapshot.

### List Conflicts

```bash
jj resolve --list
```
