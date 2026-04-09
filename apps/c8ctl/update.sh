#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
app_dir="$repo_root/apps/c8ctl"
default_nix="$app_dir/default.nix"
lockfile="$app_dir/package-lock.json"

cd "$repo_root"

current_version=$(python3 - "$default_nix" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
match = re.search(r'version = "([^"]+)";', text)
if not match:
    raise SystemExit(1)
print(match.group(1))
PY
)

version=$(npm view @camunda8/cli versions --json | jq -r 'map(select(test("alpha"))) | last')
if [[ "$version" == "$current_version" ]]; then
  printf 'c8ctl is already up to date at %s\n' "$current_version"
  exit 0
fi

printf 'Updating c8ctl: %s -> %s\n' "$current_version" "$version"

tarball=$(npm view "@camunda8/cli@$version" dist.tarball --json | jq -r '.')
src_hash=$(nix store prefetch-file --json "$tarball" | jq -r '.hash')

tmpdir=$(mktemp -d)
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

pushd "$tmpdir" >/dev/null
npm pack "@camunda8/cli@$version" >/dev/null
tarball_file=$(printf 'camunda8-cli-%s.tgz' "$version")
tar xzf "$tarball_file"
chmod -R u+w package

python3 - <<'PY'
import json
from pathlib import Path

p = Path("package/package.json")
data = json.loads(p.read_text())
data.pop("devDependencies", None)
p.write_text(json.dumps(data, indent=2) + "\n")
PY

pushd package >/dev/null
npm install --package-lock-only --ignore-scripts >/dev/null
popd >/dev/null

cp "$tmpdir/package/package-lock.json" "$lockfile"
popd >/dev/null

python3 - "$default_nix" "$version" "$tarball" "$src_hash" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
version = sys.argv[2]
tarball = sys.argv[3]
src_hash = sys.argv[4]

text = path.read_text()
text = re.sub(r'version = ".*?";', f'version = "{version}";', text, count=1)
text = re.sub(r'url = ".*?";', f'url = "{tarball}";', text, count=1)
text = re.sub(r'hash = ".*?";', f'hash = "{src_hash}";', text, count=1)
text = re.sub(r'npmDepsHash = ".*?";', 'npmDepsHash = pkgs.lib.fakeHash;', text, count=1)
path.write_text(text)
PY

build_output=$(mktemp)
if nix build --impure --expr 'let pkgs = import (import ./npins).nixpkgs {}; in import ./apps/c8ctl { inherit pkgs; }' > /dev/null 2>"$build_output"; then
  rm -f "$build_output"
else
  npm_deps_hash=$(python3 - "$build_output" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text()
matches = re.findall(r'got:\s+(sha256-[A-Za-z0-9+/=]+)', text)
if not matches:
    sys.exit(1)
print(matches[-1])
PY
)

  python3 - "$default_nix" "$npm_deps_hash" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
npm_deps_hash = sys.argv[2]
text = path.read_text()
text = re.sub(r'npmDepsHash = .*?;', f'npmDepsHash = "{npm_deps_hash}";', text, count=1)
path.write_text(text)
PY

  nix build --impure --expr 'let pkgs = import (import ./npins).nixpkgs {}; in import ./apps/c8ctl { inherit pkgs; }' > /dev/null
  rm -f "$build_output"
fi

cat <<EOF
Updated c8ctl: $current_version -> $version
Refreshed hashes successfully.
You can now run either:
- bash hm-switch.sh
- ./rebuild.sh
EOF
