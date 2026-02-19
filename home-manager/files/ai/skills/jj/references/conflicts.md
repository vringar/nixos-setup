# Conflict Resolution

## Non-Interactive Built-in Tools

```bash
# Take "our" side (current branch)
jj resolve --tool :ours <file>

# Take "their" side (incoming changes)
jj resolve --tool :theirs <file>
```

## Automatic Resolution with Mergiraf

[Mergiraf](https://mergiraf.org/) is a syntax-aware merge tool (installed via home-manager) that can automatically resolve many conflicts by understanding the AST of 33+ languages:

```bash
# Try mergiraf first - it handles most syntax-aware conflicts
jj resolve --tool mergiraf <file>

# Or resolve all conflicted files at once
jj resolve --tool mergiraf
```

If mergiraf can't fully resolve, it exits non-zero and asks the user for manual resolution.

## List Conflicts

```bash
jj resolve --list
```
