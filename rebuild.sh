#!/usr/bin/env bash

cd "$(dirname "$0")" || exit

if [ $# -eq 0 ]; then
  # No arguments: deploy locally
  # Use GUI askpass when running non-interactively (e.g., from Claude Code)
  if [[ ! -t 0 ]] && command -v ksshaskpass &>/dev/null; then
    SUDO_ASKPASS=$(command -v ksshaskpass)
    export SUDO_ASKPASS
    sudo -A colmena apply-local --show-trace
  else
    colmena apply-local --show-trace --sudo
  fi
else
  # Arguments provided: forward to colmena apply
  colmena apply --show-trace "$@"
fi
