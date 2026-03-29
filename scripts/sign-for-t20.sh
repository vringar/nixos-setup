#!/usr/bin/env bash
# Sign the t20 NixOS closure with sz1's key so colmena can copy it.
# Run this outside the Claude sandbox (needs sudo for the key file).
set -euo pipefail

KEY=/etc/nix/signing-key.sec
HIVE="$(cd "$(dirname "$0")/.." && pwd)/hive.nix"

echo "Evaluating t20 toplevel..."
drv=$(colmena --config "$HIVE" eval --instantiate -E \
  '{ nodes, ... }: nodes.t20.config.system.build.toplevel' 2>/dev/null \
  | tail -1)

echo "Building $drv ..."
out=$(nix-store --realise "$drv" | tail -1)

echo "Signing closure $out ..."
nix path-info --recursive "$out" \
  | sudo xargs nix store sign --no-use-registries --key-file "$KEY"

echo "Done. You can now run: colmena apply --on t20"
