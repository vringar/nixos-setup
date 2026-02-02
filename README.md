# My Nix/Homemanager setup

For my NixOS machines, this contains a colmena setup, that can hopefully describe a unified config for all machines.

However, for my non-nixos systems, I still want to have a ~ equivalent home setup, so I'm moving as much as possible
into a home-manager configuration.

## Known Issues

### `'system' has been renamed to 'stdenv.hostPlatform.system'` warning
This harmless warning appears during `colmena build/apply`. It's caused by colmena 0.4.0 using deprecated nixpkgs API in its bundled `eval.nix` (line 144: `inherit (npkgs) system;`).

**Status**: [Fixed in colmena main](https://github.com/zhaofengli/colmena/commit/bcda96150473a11cedb9ca649c57072d462adb61), waiting for 0.5.0 release to land in nixpkgs.

**Source**: `lixPackageSets.git.colmena` in `modules/baseline.nix`
