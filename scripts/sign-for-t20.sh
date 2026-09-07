#!/usr/bin/env bash
# Sign the t20 NixOS closure with sz1's key so colmena can copy it.
# Run this outside the Claude sandbox (needs sudo for the key file).
set -euo pipefail

KEY=/etc/nix/signing-key.sec
HIVE="$(cd "$(dirname "$0")/.." && pwd)/hive.nix"
# Keep the built closure alive. nix.gc runs weekly and deletes unrooted paths
# outright — --delete-older-than only spares profile generations — so without a
# root here the closure is reaped and the next run rebuilds it from scratch.
ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/nixos-setup/t20-system"

echo "Evaluating t20 toplevel..."
drv=$(colmena --config "$HIVE" eval --instantiate -E \
  '{ nodes, ... }: nodes.t20.config.system.build.toplevel' 2>/dev/null \
  | tail -1)

echo "Building $drv ..."
mkdir -p "$(dirname "$ROOT")"
out=$(nix-store --realise "$drv" --add-root "$ROOT" --indirect | tail -1)

echo "Signing closure $out ..."
# Collect the paths *before* sudo runs. Both halves of a pipeline start at
# once, and path-info draws a progress display on the tty: its \r + ESC[K
# erases sudo's "[sudo] password for ..." prompt, leaving a blank line that
# looks like a hang rather than a password request.
paths=$(nix path-info --recursive "$out")
printf '%s\n' "$paths" \
  | sudo xargs nix store sign --no-use-registries --key-file "$KEY"

echo "Done. You can now run: colmena apply --on t20"
