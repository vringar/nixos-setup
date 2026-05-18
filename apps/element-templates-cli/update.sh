#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
default_nix="$repo_root/apps/element-templates-cli/default.nix"

OWNER="vringar"
REPO="element-templates-cli"
BRANCH="feat/query-set-subcommands"

rev=$(curl -fsSL "https://api.github.com/repos/$OWNER/$REPO/commits/$BRANCH" | jq -r '.sha')

current_rev=$(python3 - "$default_nix" <<'PY'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
m = re.search(r'etCliRev = "([^"]+)"', text)
if not m:
    raise SystemExit("etCliRev not found")
print(m.group(1))
PY
)

if [[ "$rev" == "$current_rev" ]]; then
  printf 'element-templates-cli is already at %.8s\n' "$rev"
  exit 0
fi

printf 'Updating element-templates-cli: %.8s -> %.8s\n' "$current_rev" "$rev"

tarball_url="https://github.com/$OWNER/$REPO/archive/$rev.tar.gz"
src_hash=$(nix store prefetch-file --unpack --json "$tarball_url" | jq -r '.hash')

python3 - "$default_nix" "$rev" "$src_hash" <<'PY'
import re, sys
from pathlib import Path
path, rev, src_hash = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
text = re.sub(r'etCliRev = "[^"]*"', f'etCliRev = "{rev}"', text)
text = re.sub(r'etCliHash = "[^"]*"', f'etCliHash = "{src_hash}"', text)
text = re.sub(r'etCliNpmDepsHash = "[^"]*"', 'etCliNpmDepsHash = pkgs.lib.fakeHash', text)
path.write_text(text)
PY

build_expr='let pkgs = import (import ./npins).nixpkgs {}; in import ./apps/element-templates-cli { inherit pkgs; }'
build_output=$(mktemp)

if nix build --impure --expr "$build_expr" >/dev/null 2>"$build_output"; then
  rm -f "$build_output"
else
  npm_deps_hash=$(python3 - "$build_output" <<'PY'
import re, sys
from pathlib import Path
text = Path(sys.argv[1]).read_text()
matches = re.findall(r'got:\s+(sha256-[A-Za-z0-9+/=]+)', text)
if not matches:
    sys.exit(1)
print(matches[-1])
PY
)

  python3 - "$default_nix" "$npm_deps_hash" <<'PY'
import re, sys
from pathlib import Path
path, npm_deps_hash = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
text = re.sub(r'etCliNpmDepsHash = [^;]+', f'etCliNpmDepsHash = "{npm_deps_hash}"', text)
path.write_text(text)
PY

  nix build --impure --expr "$build_expr" >/dev/null
  rm -f "$build_output"
fi

printf 'Updated element-templates-cli to %.8s\nHashes refreshed. Run hm-switch.sh or rebuild.sh to apply.\n' "$rev"
