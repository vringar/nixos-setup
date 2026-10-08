#!/usr/bin/env bash
# Extract every top-level command name from a built crosslink binary's own
# --help output, so gate.py can tell "a real command the gate hasn't been
# reviewed against yet" apart from "not a real command at all" (a typo or a
# hallucinated subcommand). One name per line. Same parsing as
# generate-completions.sh, by design -- same source of truth.
# Usage: extract-known-commands.sh /path/to/crosslink-real
set -euo pipefail

CROSSLINK="$1"

"$CROSSLINK" help 2>&1 \
  | sed -n '/^Commands:/,/^Options:/{/^  [a-zA-Z]/p}' \
  | awk '{print $1}'
