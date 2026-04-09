# AGENTS.md - nixos-setup

This file supplements the global AGENTS.md. Nix-specific guidance follows.

## Repository Overview

NixOS/Home-Manager configuration for multiple machines using Colmena.

```
nixos-setup/
├── hive.nix              # Colmena entry point (hosts, defaults, meta)
├── home.nix              # Standalone home-manager entry point (non-NixOS)
├── hm-switch.sh          # Non-NixOS deployment wrapper (home-manager switch)
├── rebuild.sh            # NixOS deployment wrapper (colmena apply-local)
├── hardware/             # Host-specific hardware configs (sz1, sz3, pi)
├── modules/              # System-level NixOS modules
├── home-manager/         # User environment (shell, editors, packages)
├── user/                 # User definition (groups, shell, packages)
├── apps/                 # Custom Nix packages (claude-code, crosslink, rtk, etc.)
├── secrets/              # agenix-encrypted secrets (WireGuard, etc.)
├── scripts/              # Utility scripts (e.g. sign-for-t20.sh)
├── tests/                # Python tests for update-pins.py
└── npins/                # Pinned dependencies (DO NOT EDIT DIRECTLY)
```

## Workflow

This is a single-branch repo. Push working changes directly to `main`:

```bash
jj bookmark set main -r @
jj push
```

## Verification

First, detect whether you are on a NixOS system:

```bash
[ -f /etc/NIXOS ] && echo "NixOS" || echo "non-NixOS"
```

- **On NixOS (sz1, sz3):** Run `colmena build` to verify changes compile before committing.
- **On non-NixOS (standalone home-manager):** Use `hm-switch.sh` to build and apply:

```bash
bash hm-switch.sh
```

`hm-switch.sh` resolves the pinned nixpkgs from `npins`, then invokes `home-manager switch` against `home.nix` with the correct `NIX_PATH`. Always use this script — do not invoke `home-manager` directly.

## npins

**Never edit `npins/sources.json` directly.** Use the npins CLI:
- `npins add` - Add new dependency
- `npins update` - Update pins
- `npins show` - List current pins

### Rebasing commits that add npins sources

When rebasing a commit that added a source via `npins add`, do not attempt to resolve conflicts in `npins/sources.json` manually. Instead:

1. Restore the `npins/` directory to the state of the rebase destination (base branch):
   ```bash
   jj restore --from <base-revision> npins/
   ```
2. Re-run the original `npins add` command to re-add the source.

## Module Organization

- Ask before creating new modules - discuss organization first
- Use `lowercase-hyphenated.nix` naming for new files
- Default changes to all hosts via `modules/` or the `defaults` section in `hive.nix`
- Hardware-specific settings (boot, filesystems, kernel modules) go in `hardware/`.
  Host-specific software configuration belongs in the host's block in `hive.nix`.

### Home-manager tiers

Home-manager modules are split into tiers based on what kind of machine they target:

| Module | Hosts |
|--------|-------|
| `home-manager/baseline.nix` | All hosts (headless and graphical) |
| `home-manager/graphical.nix` | Any machine with a GUI: sz1, sz3, non-NixOS work laptop |
| `home-manager/workstation.nix` | NixOS personal machines only: sz1, sz3 |
| `home-manager/ai.nix` | Any graphical machine (same as graphical tier) |

On NixOS hosts, `modules/desktop.nix` imports `graphical.nix`, `workstation.nix`, and `ai.nix` together.
On non-NixOS (`home.nix`), `graphical.nix` and `ai.nix` are imported directly.

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
# home-manager/baseline.nix or appropriate tier module
home.packages = with pkgs; [
  new-package
];
```

### Custom options
Custom options use the `my.*` namespace, defined in `home-manager/baseline.nix`.

## Hosts

| Host | Hardware | Filesystem | Use |
|------|----------|------------|-----|
| sz1  | AMD      | ZFS        | Desktop workstation |
| sz3  | Intel    | Btrfs+LUKS | Laptop |
| t20  | Raspberry Pi 3 | ext4 | Headless server (Ghidra) |

sz1 and sz3 have tag `@personal` and allow local deployment. t20 has tag `@personal` but is remote-only.
