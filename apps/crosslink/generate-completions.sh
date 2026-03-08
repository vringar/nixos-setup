#!/usr/bin/env bash
# Generate a zsh completion file for crosslink from its --help output.
# Usage: generate-completions.sh /path/to/crosslink > _crosslink
set -euo pipefail

CROSSLINK="$1"

# Parse subcommands: "  name   Description text" -> "'name:Description text'"
subcommands=$("$CROSSLINK" help 2>/dev/null | \
  sed -n '/^Commands:/,/^Options:/{/^  [a-z]/p}' | \
  while IFS= read -r line; do
    cmd=$(echo "$line" | awk '{print $1}')
    desc=$(echo "$line" | awk '{$1=""; sub(/^[[:space:]]+/, ""); print}')
    # Escape single quotes in descriptions
    desc="${desc//\'/\\\'}"
    printf "    '%s:%s'\n" "$cmd" "$desc"
  done)

cat <<EOF
#compdef crosslink

_crosslink() {
  local -a commands global_opts

  global_opts=(
    '(-q --quiet)'{-q,--quiet}'[Quiet mode: only output essential data]'
    '--json[Output as JSON]'
    '(-h --help)'{-h,--help}'[Print help]'
    '(-V --version)'{-V,--version}'[Print version]'
  )

  commands=(
${subcommands}
  )

  _arguments -s \$global_opts \\
    '1:command:->command' \\
    '*::arg:->args'

  case \$state in
    command)
      _describe -t commands 'crosslink command' commands
      ;;
    args)
      _default
      ;;
  esac
}

_crosslink "\$@"
EOF
