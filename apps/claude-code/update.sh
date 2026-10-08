#!/usr/bin/env bash
# Refresh manifest.zst.json from Anthropic's release bucket (the same source
# nixpkgs' claude-code package consumes), so we aren't gated on nixpkgs merges
# and channel bumps. Pass a version to pin; default is the `latest` pointer.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

base_url="https://downloads.claude.ai/claude-code-releases"
version="${1:-$(curl -fsSL "$base_url/latest")}"

curl -fsSL "$base_url/$version/manifest.zst.json" --output manifest.zst.json
echo "claude-code manifest: $version"
