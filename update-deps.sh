#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

# update-pins.py streams colmena output to the terminal but keeps no copy. When
# a bump breaks the build, that output is the only record of what failed, so
# keep it on disk: a stop leaves the log of the round that broke.
log_dir="${TMPDIR:-/tmp}/update-pins-logs/$(date +%Y%m%dT%H%M%S)"
mkdir -p "$log_dir"
echo "==> Build logs: $log_dir"

scripts/update-pins.py --capture-logs "$log_dir"
apps/c8ctl/update.sh
