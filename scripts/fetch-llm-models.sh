#!/usr/bin/env bash
# Download the shortlisted GGUF models (docs/local-llm-service.md) into
# /var/lib/llm/models. Idempotent: existing complete files are skipped,
# interrupted downloads resume. Run with sudo (the directory is root-owned).
set -euo pipefail

DEST=/var/lib/llm/models

declare -A MODELS=(
  ["MN-12B-Mag-Mell-Q4_K_M.gguf"]="https://huggingface.co/inflatebot/MN-12B-Mag-Mell-R1-GGUF/resolve/main/MN-12B-Mag-Mell-Q4_K_M.gguf"
  ["Rocinante-12B-v1.1-Q4_K_M.gguf"]="https://huggingface.co/TheDrummer/Rocinante-12B-v1.1-GGUF/resolve/main/Rocinante-12B-v1.1-Q4_K_M.gguf"
  ["Ayla-Light-12B-v2.Q4_K_M.gguf"]="https://huggingface.co/mradermacher/Ayla-Light-12B-v2-GGUF/resolve/main/Ayla-Light-12B-v2.Q4_K_M.gguf"
)

for name in "${!MODELS[@]}"; do
  if [[ -f "$DEST/$name" ]]; then
    echo "skip (exists): $name"
    continue
  fi
  echo "fetching: $name"
  curl --fail --location --continue-at - --output "$DEST/$name.part" "${MODELS[$name]}"
  mv "$DEST/$name.part" "$DEST/$name"
  chmod 0644 "$DEST/$name"
done

ls -lh "$DEST"
