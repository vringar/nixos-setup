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

### "Un-include" a file from a commit (reset file content)
`jj restore --from <rev> <paths>` resets the named paths in `@` (or `--to <rev>`) to that revision's content. Cleaner and safer than `jj split` when you just want one file to revert.

```bash
# Drop changes to foo.txt from the current commit, taking the parent's version
jj restore --from @- foo.txt

# Reset paths to whatever is on origin's main
jj restore --from main@origin path/to/file.txt
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

### History rewrite by snapshot

When a stack has gotten tangled (multiple rebases, mixed-up squashes, half-resolved conflicts), it's often faster to rebuild the history from scratch than to keep splitting and rebasing. jj auto-snapshots the working copy on the next command, so the loop is just *write files → describe → new*.

```bash
# 1. Make sure the working copy holds the FINAL state you want.
# 2. Stash final contents of every file you'll touch:
mkdir /tmp/snap && cp -r path/to/files /tmp/snap/

# 3. Start fresh on top of trunk:
jj new -d 'trunk()'

# 4. For each logical commit:
cp /tmp/snap/<files-for-this-commit> <repo paths>
jj describe -m "first logical chunk"
jj new                                  # snapshot lands in the previous @, new @ ready

# ...repeat for next chunk...

# 5. Move the bookmark to the new tip (likely behind/sideways from the old one):
jj bookmark set <name> -r @ --allow-backwards

# 6. Force-push:
jj push --bookmark <name>               # add --allow-new on first push of a new bookmark
```

This avoids the editor entirely and keeps commit boundaries aligned with file boundaries instead of with whatever hunks `jj split` happens to surface.

### Remote-tracking revset

`<bookmark>@origin` resolves to the remote tip of a tracked branch:

```bash
jj log -r 'main@origin..@'              # what's on top of pushed main
jj diff --from main@origin --to @       # local vs. pushed
jj restore --from feat/x@origin path/   # reset paths to origin's version
```

## Push Alias Details

The `jj push` alias (defined in home-manager jujutsu config):
1. Checks if `.pre-commit-config.yaml` exists in the repo
2. If yes, runs `jj-precommit` on commits in `trunk()..@-`
3. Only pushes if pre-commit passes

If no `.pre-commit-config.yaml` exists, it just runs `jj git push` directly.

`jj push` works correctly from inside jj workspaces — `jj-precommit` handles workspace path resolution.

## jj-precommit

`jj-precommit` is a workspace-aware wrapper around `pre-commit`. Use it instead of `pre-commit` directly whenever running hooks manually:

```bash
jj-precommit run --all-files   # instead of: pre-commit run --all-files
jj-precommit run               # instead of: pre-commit run
```

It resolves the correct repo root even from inside a `.workspace/<name>/` directory, where plain `pre-commit` would walk up to the wrong `.git`.
