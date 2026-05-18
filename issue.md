# jj-check-wip: false positive when pushing a branch whose ancestry includes already-remote WIP commits

## Summary

`jj-check-wip` blocks a push when the push range `trunk()..@` includes WIP commits
that are already on the remote — i.e. commits that live on a different remote branch
(e.g. `stage@origin`) and are ancestors of the branch being pushed, but are not new
work being introduced by this push.

## Steps to reproduce

1. Remote branch `stage` exists and contains two commits with descriptions starting
   with `wip` (e.g. committed by a colleague doing exploratory work directly on stage).
2. Locally, create a new branch based on `main` that contains one normal commit:
   ```
   jj new -r main -m "chore: some legitimate change"
   # make changes
   jj bookmark set my-feature -r @
   jj push --bookmark my-feature --allow-new
   ```
3. Push is blocked:
   ```
   error: commit <id> is a WIP commit and must not be pushed: wip
   error: commit <id> is a WIP commit and must not be pushed: wip
   Push blocked: WIP commits must stay local.
   ```

The flagged commits are the WIP commits on `stage@origin`. They appear in
`trunk()..my-feature` because `my-feature` was derived from `main`, and `main`
shares a common ancestor with `stage` — so traversing from `trunk()` to `my-feature`
can pass through `stage`'s ancestry depending on the revset evaluation.

**Note:** This also triggers when using `jj new -r stage -r main` (a merge commit)
for the same reason — stage's WIP commits are reachable.

## Expected behaviour

Only commits that are genuinely new (not yet on any remote) should be checked.
Commits already reachable from `remote_bookmarks()` are already pushed and should
not block unrelated work.

## Suggested fix

Change the checked revset from:

```
trunk()..@
```

to:

```
trunk()..@ ~ remote_bookmarks()
```

or equivalently, filter out commits already reachable from any remote bookmark
before running the WIP description check.

## Environment

- Repo: `camunda/lamppost`
- Triggered while pushing `chore/renovate-stage-sync` (based on `main`) — blocked
  by `wip` commits on `stage@origin` belonging to a different developer
- Worked around by using `jj git push` directly (bypasses `jj-precommit`)
