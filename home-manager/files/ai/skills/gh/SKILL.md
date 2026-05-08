---
name: gh
description: Use when invoking the gh CLI — creating, commenting on, or editing GitHub issues and pull requests. Covers two patterns that agents reliably get wrong: deriving the repo target from jj inside a workspace, and avoiding shell-escaping bugs in issue/PR bodies by using --body-file instead of --body.
---

# /gh — GitHub CLI patterns

Two rules when running `gh` from an agent context. Both bypass classes of bugs that the harness can no longer catch via PreToolUse hooks once `--dangerously-skip-permissions` is in effect.

## 1. Derive `-R OWNER/REPO` from jj, pass it explicitly

`gh` auto-detects the repo by walking up to find `.git/config`. Inside a jj workspace (`.workspace/<name>/` contains only `.jj/`, no `.git/`), the walk hits the parent project's `.git/` — which may or may not be the right repo, and the implicit detection makes the tool call hard to audit. Query jj instead and pass it explicitly:

```bash
NWO=$(jj git remote list | awk '$1=="origin"{print $2}' | sed -E 's#.*[:/]([^/]+/[^/]+)\.git$#\1#')
gh issue create -R "$NWO" --title '...' --body-file /tmp/body.md
```

The sed strips both URL forms:
- `git@github.com:owner/repo.git` → `owner/repo`
- `https://github.com/owner/repo.git` → `owner/repo`

If the workspace tracks a non-`origin` remote, swap `origin` for the relevant remote name.

## 2. Use `--body-file <path>` instead of `--body "..."`

For `gh (issue|pr) (create|comment|edit)`, write the body to a file with the Write tool first, then pass `--body-file`:

```bash
# Step 1: Use the Write tool with file_path=/tmp/body.md and content=<your markdown>.
#         The Write tool takes content as a JSON string parameter — backticks, $VAR,
#         heredoc terminators all pass through as literal bytes, no shell escaping.
# Step 2:
gh issue create -R "$NWO" --title 'fix: ...' --body-file /tmp/body.md
```

Why this matters: `--body "..."` routes the body through shell quoting. Inside a double-quoted string, backticks become command substitution and `$VAR` expands; inside a heredoc, the terminator must match exactly. Bodies containing code blocks, shell snippets, or markdown reliably trip these layers. The Write-then-flag pattern sidesteps every layer.

Applies to: `gh issue create`, `gh pr create`, `gh issue comment`, `gh pr comment`, `gh issue edit`, `gh pr edit`.
