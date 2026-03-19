#!/usr/bin/env bash
set -euo pipefail

# Update npins and fix any resulting FOD hash mismatches (cargoHash, vendorHash, etc.)
# Mimics the yazi/fishnet pattern from nixpkgs: build, extract correct hash, sed replace.
#
# Usage: ./update-pins.sh [npins update args...]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAX_ROUNDS=5

echo "==> Running npins update $*"
npins update "$@"

round=1
while [ "$round" -le "$MAX_ROUNDS" ]; do
    echo "==> Build attempt $round/$MAX_ROUNDS"
    build_log=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$build_log'" EXIT

    # Stream build output to terminal while capturing it for parsing
    if colmena build 2>&1 | tee "$build_log"; then
        echo "==> Build succeeded!"
        rm -f "$build_log"
        exit 0
    fi

    # Extract unique (old_hash, new_hash) pairs from "specified:/got:" lines
    mismatches=$(grep -A1 'specified:.*sha256' "$build_log" \
        | paste - - \
        | sed -n 's/.*specified: *\(sha256-[^ ]*\).*got: *\(sha256-[^ ]*\).*/\1 \2/p' \
        | sort -u)
    rm -f "$build_log"

    if [ -z "$mismatches" ]; then
        echo "==> Build failed but no hash mismatches found."
        exit 1
    fi

    echo "==> Fixing $(echo "$mismatches" | wc -l) hash mismatch(es)"
    while IFS=' ' read -r old_hash new_hash; do
        files=$(grep -rl --include='*.nix' "$old_hash" "$SCRIPT_DIR") || true
        if [ -z "$files" ]; then
            echo "  WARNING: Could not find file containing $old_hash"
            continue
        fi
        while read -r f; do
            echo "  Fixing ${f#"$SCRIPT_DIR"/}: $old_hash -> $new_hash"
            sed -i "s|$old_hash|$new_hash|g" "$f"
        done <<< "$files"
    done <<< "$mismatches"

    round=$((round + 1))
done

echo "==> Failed after $MAX_ROUNDS rounds"
exit 1
