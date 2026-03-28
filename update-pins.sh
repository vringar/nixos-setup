#!/usr/bin/env bash
set -euo pipefail

# Update npins and fix any resulting FOD hash mismatches (cargoHash, vendorHash, etc.)
# Updates one pin at a time so at most one cargoHash needs fixing per build cycle.
#
# Usage: ./update-pins.sh [pin...]   # omit to update all pins

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAX_ROUNDS=5

if [ "$#" -gt 0 ]; then
    pins=("$@")
else
    mapfile -t pins < <(jq -r 'keys[]' "$SCRIPT_DIR/npins/sources.json")
fi

for pin in "${pins[@]}"; do
    echo "==> Updating pin: $pin"
    npins update "$pin"

    round=1
    while [ "$round" -le "$MAX_ROUNDS" ]; do
        echo "==> Build attempt $round/$MAX_ROUNDS"
        build_log=$(mktemp)
        # shellcheck disable=SC2064
        trap "rm -f '$build_log'" EXIT

        # Stream build output to terminal while capturing it for parsing
        if colmena build --on "$(hostname)" 2>&1 | tee "$build_log"; then
            echo "==> Build succeeded!"
            rm -f "$build_log"
            break
        fi

        # Handle cargoHash out-of-date: Cargo.lock changed but cargoHash hasn't been updated.
        # Reset to "" (nixpkgs treats this as lib.fakeHash) so the next round produces a
        # standard FOD mismatch with the correct hash we can extract and write back.
        if rg -q 'cargoHash or cargoSha256 is out of date' "$build_log"; then
            echo "==> cargoHash out of date; resetting to \"\" for next round"
            rm -f "$build_log"
            files=$(rg -l --glob='*.nix' --glob='!.workspace' 'cargoHash' "$SCRIPT_DIR") || true
            if [ -z "$files" ]; then
                echo "==> Could not find any .nix file with cargoHash"
                exit 1
            fi
            fixed=0
            while IFS= read -r f; do
                if rg -q 'cargoHash = "sha256-' "$f"; then
                    echo "  Resetting cargoHash in ${f#"$SCRIPT_DIR"/}"
                    sed -i 's|cargoHash = "sha256-[^"]*"|cargoHash = ""|g' "$f"
                    fixed=1
                    break
                fi
            done <<< "$files"
            if [ "$fixed" -eq 0 ]; then
                echo "==> Could not find cargoHash entry to reset"
                exit 1
            fi
            round=$((round + 1))
            continue
        fi

        # Extract unique (old_hash, new_hash) pairs from "specified:/got:" lines
        mismatches=$(rg -A 1 --no-context-separator --no-filename 'specified:.*sha256' "$build_log" \
            | paste - - \
            | sed -n 's/.*specified:[[:space:]]*\(sha256-[^[:space:]]*\).*got:[[:space:]]*\(sha256-[^[:space:]]*\).*/\1 \2/p' \
            | sort -u)
        rm -f "$build_log"

        if [ -z "$mismatches" ]; then
            echo "==> Build failed but no hash mismatches found."
            exit 1
        fi

        echo "==> Fixing $(echo "$mismatches" | wc -l) hash mismatch(es)"
        while IFS=' ' read -r old_hash new_hash; do
            files=$(rg -lF --glob='*.nix' --glob='!.workspace' -- "$old_hash" "$SCRIPT_DIR") || true
            if [ -n "$files" ]; then
                while IFS= read -r f; do
                    echo "  Fixing ${f#"$SCRIPT_DIR"/}: $old_hash -> $new_hash"
                    sed -i "s|$old_hash|$new_hash|g" "$f"
                done <<< "$files"
            else
                # cargoHash was reset to ""; find by empty string and fill in the real hash
                files=$(rg -l --glob='*.nix' --glob='!.workspace' 'cargoHash = ""' "$SCRIPT_DIR") || true
                if [ -z "$files" ]; then
                    echo "  WARNING: Could not find file containing $old_hash"
                    continue
                fi
                while IFS= read -r f; do
                    echo "  Fixing ${f#"$SCRIPT_DIR"/}: cargoHash=\"\" -> $new_hash"
                    sed -i "s|cargoHash = \"\"|cargoHash = \"$new_hash\"|g" "$f"
                done <<< "$files"
            fi
        done <<< "$mismatches"

        round=$((round + 1))
    done

    if [ "$round" -gt "$MAX_ROUNDS" ]; then
        echo "==> Failed to converge after $MAX_ROUNDS rounds for pin: $pin"
        exit 1
    fi
done

echo "==> All pins updated successfully"
