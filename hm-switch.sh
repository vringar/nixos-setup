#!/usr/bin/env bash

set -euo pipefail

if [ -f /etc/NIXOS ]; then
  echo "error: hm-switch.sh is for non-NixOS only; use 'colmena apply-local' here" >&2
  exit 1
fi

cd "$(dirname "$0")" || exit

nixpkgs="$(nix eval --raw --impure --expr '(import ./npins).nixpkgs')"
home_manager="$(nix eval --raw --impure --expr '(import ./npins).home-manager')"

exec nix-shell \
  -I "nixpkgs=$nixpkgs" \
  -p home-manager \
  --run "home-manager -f '$(pwd)/home.nix' -I 'nixpkgs=$nixpkgs' -I 'home-manager=$home_manager' switch --show-trace"
