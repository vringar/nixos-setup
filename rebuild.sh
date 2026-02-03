#!/usr/bin/env bash

cd "$(dirname "$0")" || exit

if [ $# -ne 0 ]; then
  echo "Ignoring args:" "$@"
fi

nixpkgs_pin=$(nix eval --raw -f npins/default.nix nixpkgs)
nix_path="nixpkgs=${nixpkgs_pin}"

# Use GUI askpass when running non-interactively (e.g., from Claude Code)
if [[ ! -t 0 ]] && command -v ksshaskpass &>/dev/null; then
  SUDO_ASKPASS=$(command -v ksshaskpass)
  export SUDO_ASKPASS
  NIX_PATH="${nix_path}" sudo -A colmena apply-local --show-trace
else
  NIX_PATH="${nix_path}" colmena apply-local --show-trace --sudo
fi
