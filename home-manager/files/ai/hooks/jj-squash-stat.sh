#!/usr/bin/env bash
# PreToolUse/Bash hook: before jj squash, inject a stat of the files about
# to be moved so Claude can abort or undo if unrelated changes are included.

set -uo pipefail

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$CMD" ] && exit 0

echo "$CMD" | grep -q 'jj squash' || exit 0

command -v jj >/dev/null 2>&1 || exit 0
jj root >/dev/null 2>&1 || exit 0

WARN=""
SOURCES=()   # --from / -f values (repeatable)
SOURCE_R=""  # -r / --revision value
FILESETS=()
SKIP_NEXT=false

# Static failure: quotes mean naive word-split will misparse quoted tokens
# (e.g. -m "two words" spills "words" into filesets, or 'glob("*.rs")' splits)
if [[ "$CMD" =~ [\"\'] ]]; then
  WARN="command contains quoted strings — fileset/source detection may be inaccurate"
fi

read -ra TOKENS <<< "$CMD"

for ((i = 0; i < ${#TOKENS[@]}; i++)); do
  tok="${TOKENS[$i]}"

  if $SKIP_NEXT; then
    SKIP_NEXT=false
    continue
  fi

  case "$tok" in
    jj | squash | -k | --keep-emptied | -i | --interactive | -u | --use-destination-message \
      | --editor | --ignore-working-copy | --no-integrate-operation | --ignore-immutable \
      | --quiet | --no-pager | --debug | -h | --help) ;;

    # Flags that consume a value but don't change source/fileset semantics
    --tool | -R | --repository)
      SKIP_NEXT=true
      ;;
    --tool=* | -R=* | --repository=*)
      ;;

    # Source: --from / -f (repeatable)
    --from | -f)
      [[ $((i + 1)) -lt ${#TOKENS[@]} ]] && { SOURCES+=("${TOKENS[$((i + 1))]}"); SKIP_NEXT=true; }
      ;;
    --from=* | -f=*)
      SOURCES+=("${tok#*=}")
      ;;

    # Source: -r / --revision (squash into its parent)
    -r | --revision)
      [[ $((i + 1)) -lt ${#TOKENS[@]} ]] && { SOURCE_R="${TOKENS[$((i + 1))]}"; SKIP_NEXT=true; }
      ;;
    -r=* | --revision=*)
      SOURCE_R="${tok#*=}"
      ;;

    # Dest / message / global flags that consume a value: skip value
    --into | --to | -t | -m | --message | --color | --config | --config-file | --at-op | --at-operation)
      SKIP_NEXT=true
      ;;
    --into=* | --to=* | -t=* | -m=* | --message=* | --color=* | --config=* | --config-file=* | --at-op=* | --at-operation=*)
      ;;

    # Experimental flags (-o/-A/-B create a new commit; source semantics differ)
    -o | --onto | -d | --destination | -A | --insert-after | --after | -B | --insert-before | --before)
      [[ -z "$WARN" ]] && WARN="experimental squash mode (${tok}) — source detection may be incorrect"
      SKIP_NEXT=true
      ;;
    -o=* | --onto=* | -d=* | --destination=* | -A=* | --insert-after=* | --after=* | -B=* | --insert-before=* | --before=*)
      [[ -z "$WARN" ]] && WARN="experimental squash mode — source detection may be incorrect"
      ;;

    -*)
      # Unknown flag — we may have misread the source or lost a fileset
      [[ -z "$WARN" ]] && WARN="unrecognized flag '${tok}' — source/fileset detection may be incorrect"
      ;;

    *)
      FILESETS+=("$tok")
      ;;
  esac
done

# Resolve effective source(s): -r wins, then --from list, then default @
declare -a EFFECTIVE_SOURCES
if [[ -n "$SOURCE_R" ]]; then
  EFFECTIVE_SOURCES=("$SOURCE_R")
elif [[ ${#SOURCES[@]} -gt 0 ]]; then
  EFFECTIVE_SOURCES=("${SOURCES[@]}")
else
  EFFECTIVE_SOURCES=("@")
fi

# When parsing is uncertain (quoted strings), pass no filesets to jj diff —
# spurious tokens from mis-tokenized quotes would cause jj diff to error
# silently, swallowing the warning along with the stat.
declare -a DIFF_FILESETS
[[ -z "$WARN" ]] && DIFF_FILESETS=("${FILESETS[@]+"${FILESETS[@]}"}") || DIFF_FILESETS=()

# Collect stat output (one block per source for multi-from squash)
STAT_OUT=""
for src in "${EFFECTIVE_SOURCES[@]}"; do
  STAT=$(jj diff -r "$src" "${DIFF_FILESETS[@]+"${DIFF_FILESETS[@]}"}" --stat 2>/dev/null)
  [[ -z "$STAT" ]] && continue
  if [[ ${#EFFECTIVE_SOURCES[@]} -gt 1 ]]; then
    STAT_OUT+="From ${src}:"$'\n'"${STAT}"$'\n'
  else
    STAT_OUT="${STAT}"
  fi
done

[[ -z "$STAT_OUT" ]] && exit 0

if [[ ${#DIFF_FILESETS[@]} -gt 0 ]]; then
  HEADER="Files about to be squashed from ${EFFECTIVE_SOURCES[*]} (fileset: ${DIFF_FILESETS[*]}):"
elif [[ ${#FILESETS[@]} -gt 0 ]]; then
  HEADER="Files about to be squashed from ${EFFECTIVE_SOURCES[*]} (fileset unparseable — showing all):"
else
  HEADER="Files about to be squashed from ${EFFECTIVE_SOURCES[*]}:"
fi

MSG="${HEADER}"$'\n'"${STAT_OUT}"
[[ -n "$WARN" ]] && MSG="${MSG}"$'\n'"WARNING: ${WARN}"

jq -n --arg msg "$MSG" '{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": $msg
  }
}'
