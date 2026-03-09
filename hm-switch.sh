#!/usr/bin/env bash

set -euo pipefail
cd "$(dirname "$0")" || exit

nixpkgs="$(nix eval --raw --impure --expr '(import ./npins).nixpkgs')"

exec nix-shell \
  -I "nixpkgs=$nixpkgs" \
  -p home-manager \
  --run "home-manager -f '$(pwd)/home.nix' -I 'nixpkgs=$nixpkgs' switch --show-trace"
