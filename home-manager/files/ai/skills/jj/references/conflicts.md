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

## Generated / Lock File Conflicts

Generated files (e.g., `package-lock.json`, `Cargo.lock`, `flake.lock`, `yarn.lock`) often conflict because they change frequently and aren't human-editable. The best strategy is to restore the base/older version and regenerate:

```bash
# 1. Restore the file to the version from the parent commit (or another rev)
jj restore --from <base-rev> <file>

# 2. Regenerate the lock file with the appropriate tool
npm install            # package-lock.json
cargo generate-lockfile # Cargo.lock
nix flake update       # flake.lock
yarn install           # yarn.lock
```

This avoids broken merges of machine-generated content. When in doubt about which revision to restore from, prefer the older/base version so the regeneration step picks up all intended dependency changes.

## List Conflicts

```bash
jj resolve --list
```
