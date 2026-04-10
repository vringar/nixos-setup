# My Nix/Homemanager setup

For my NixOS machines, this contains a colmena setup, that can hopefully describe a unified config for all machines.

However, for my non-nixos systems, I still want to have a ~ equivalent home setup, so I'm moving as much as possible
into a home-manager configuration.

## TODO

- [ ] Add standalone home-manager deployment for non-NixOS machines

## Private GitHub packages

Some npins sources (e.g. `camunda-ai-dev-kit`) are private GitHub repos. Nix fetches them using credentials from `~/.config/nix/github-netrc`.

### First-time setup

1. Add yourself to trusted users in `/etc/nix/nix.conf`:
   ```
   trusted-users = root stefan
   ```
2. Restart the Nix daemon.
3. Create the netrc file:
   ```bash
   mkdir -p ~/.config/nix
   cat > ~/.config/nix/github-netrc << EOF
   machine github.com
   login stefan
   password $(gh auth token)

   machine api.github.com
   login stefan
   password $(gh auth token)
   EOF
   chmod 600 ~/.config/nix/github-netrc
   echo "netrc-file = /home/stefan/.config/nix/github-netrc" >> ~/.config/nix/nix.conf
   ```

### Rotating the token

When your GitHub token rotates, refresh the netrc file:

```bash
sed -i "s/password .*/password $(gh auth token)/" ~/.config/nix/github-netrc
```

Note: `access-tokens` in `nix.conf` does not work with Lix 2.94 — `netrc-file` is required.

## Known Issues

### `'system' has been renamed to 'stdenv.hostPlatform.system'` warning
This harmless warning appears during `colmena build/apply`. It's caused by colmena 0.4.0 using deprecated nixpkgs API in its bundled `eval.nix` (line 144: `inherit (npkgs) system;`).

**Status**: [Fixed in colmena main](https://github.com/zhaofengli/colmena/commit/bcda96150473a11cedb9ca649c57072d462adb61), waiting for 0.5.0 release to land in nixpkgs.

**Source**: `lixPackageSets.git.colmena` in `modules/baseline.nix`
