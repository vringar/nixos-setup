#!/usr/bin/env bash

cd $(dirname $0)
# assume that if there are no args, you want to switch to the configuration
cmd=${1:-switch}
shift

if [ $# -ne 0 ]
  then
    echo "Ignoring args:" $@
fi

nixpkgs_pin=$(nix eval --raw -f npins/default.nix nixpkgs)
nix_path="nixpkgs=${nixpkgs_pin}:nixos-config=${PWD}/configuration.nix"

NIX_PATH="${nix_path}" colmena apply-local --show-trace --sudo 
