#!/usr/bin/env bash
# jj-check-ac: Block push if any AC-N (acceptance criteria) markers remain
# in file contents or commit descriptions across trunk()..@.
#
# Agents use acceptance criteria markers (AC- followed by a number) locally
# while iterating. These markers must be stripped before code leaves the
# machine because the associated context is lost once pushed.
set -euo pipefail

REVSET="trunk()..@"
AC_PATTERN='AC-[0-9]+'
found=0

# --- Check commit descriptions ---
while IFS= read -r line; do
  # Each line is: <change_id> <first matching line>
  change_id="${line%% *}"
  match="${line#* }"
  echo "error: commit $change_id description contains AC marker: $match" >&2
  found=1
done < <(
  jj log -r "$REVSET" --no-graph -T 'change_id ++ " " ++ description ++ "\n"' 2>/dev/null \
    | grep -En "$AC_PATTERN" \
    | sed 's/^[0-9]*://' \
    || true
)

# --- Check file contents ---
WORKSPACE_ROOT=$(jj root 2>/dev/null) || { echo "Not in a jj repo" >&2; exit 1; }

mapfile -t CHANGED < <(
  jj diff --summary --no-pager -r "$REVSET" 2>/dev/null \
    | awk '$1 != "D" {print $2}' \
    | sort -u
)

for file in "${CHANGED[@]}"; do
  filepath="$WORKSPACE_ROOT/$file"
  [[ -f "$filepath" ]] || continue
  # Skip binary files
  [[ "$(file -b --mime-encoding "$filepath")" == "binary" ]] && continue
  if grep -nE "$AC_PATTERN" "$filepath" > /dev/null 2>&1; then
    while IFS= read -r match; do
      echo "error: $file:$match" >&2
      found=1
    done < <(grep -nE "$AC_PATTERN" "$filepath")
  fi
done

if [[ $found -ne 0 ]]; then
  echo >&2
  echo "Push blocked: AC-N markers found. Strip acceptance criteria before pushing." >&2
  echo "These markers are for local agent iteration only and lose context once pushed." >&2
  exit 1
fi
