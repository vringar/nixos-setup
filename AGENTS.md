# AGENTS.md - nixos-setup

This file supplements the global AGENTS.md. Nix-specific guidance follows.

## Repository Overview

NixOS/Home-Manager configuration for multiple machines using Colmena.

```
nixos-setup/
├── hive.nix              # Colmena entry point (hosts, defaults, meta)
├── rebuild.sh            # Deployment wrapper (colmena apply-local)
├── hardware/             # Host-specific hardware configs (sz1, sz3)
├── modules/              # System-level NixOS modules
├── home-manager/         # User environment (shell, editors, packages)
├── user/                 # User definition (groups, shell, packages)
└── npins/                # Pinned dependencies (DO NOT EDIT DIRECTLY)
```

## Workflow

This is a single-branch repo. Push working changes directly to `main`:

```bash
jj bookmark set main -r @
jj push
```

## Verification

Run `colmena build` to verify changes compile before committing.

## npins

**Never edit `npins/sources.json` directly.** Use the npins CLI:
- `npins add` - Add new dependency
- `npins update` - Update pins
- `npins show` - List current pins

## Module Organization

- Ask before creating new modules - discuss organization first
- Use `lowercase-hyphenated.nix` naming for new files
- Default changes to all hosts via `modules/` or the `defaults` section in `hive.nix`
- Host-specific changes go in `hardware/sz1.nix` or `hardware/sz3.nix`

## Patterns

### Adding system packages
```nix
# modules/baseline.nix or appropriate module
environment.systemPackages = with pkgs; [
  new-package
];
```

### Adding user packages
```nix
# home-manager/baseline.nix or user/default.nix
home.packages = with pkgs; [
  new-package
];
```

### Custom options
Custom options use the `my.*` namespace (see `home-manager/config.nix`).

## Hosts

| Host | Hardware | Filesystem | Use |
|------|----------|------------|-----|
| sz1  | AMD      | ZFS        | Desktop workstation |
| sz3  | Intel    | Btrfs+LUKS | Laptop |

Both hosts have tag `@personal` and allow local deployment.
