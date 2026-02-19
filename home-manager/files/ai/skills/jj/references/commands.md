# Common Commands

- `jj new` - Create new commit on top of current
- `jj describe -m "msg"` - Set commit message
- `jj squash` - Squash into parent
- `jj split -r REV -m "msg" FILES` - Split commit (first part gets the message)
- `jj duplicate REV` - Duplicate a commit
- `jj rebase -r REV --after TARGET` - Move commit after target
- `jj rebase -s REV -d TARGET` - Rebase commit and descendants onto target
- `jj restore --from REV FILE` - Restore file from another revision

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

## Push Alias Details

The `jj push` alias (defined in home-manager jujutsu config):
1. Checks if `.pre-commit-config.yaml` exists in the repo
2. If yes, runs `pre-commit run --all-files` on commits in `trunk()..@-`
3. Only pushes if pre-commit passes

If no `.pre-commit-config.yaml` exists, it just runs `jj git push` directly.
