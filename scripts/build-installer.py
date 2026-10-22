#!/usr/bin/env python3
"""Build installer-iso.nix and print the path of the ISO.

The ISO authorizes the hosts' login keys for root (the list in
home-manager/files/ssh/login-keys.nix), so once it is on the LAN or the
tailnet the install can be driven over SSH. It deliberately takes no keys from
~/.ssh: that also holds keys that must never log in anywhere, like the forge
key every sandbox can use.

    scripts/build-installer.py [--out-link PATH]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def find_iso(out_link: Path) -> Path:
    isos = sorted((out_link / "iso").glob("*.iso"))
    if len(isos) != 1:
        raise SystemExit(f"expected one ISO under {out_link}/iso, found {isos}")
    return isos[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-link", type=Path, default=REPO / "result-installer")
    args = parser.parse_args()

    subprocess.run(
        [
            "nix-build",
            str(REPO / "installer-iso.nix"),
            "--out-link",
            str(args.out_link),
        ],
        check=True,
    )
    print(find_iso(args.out_link))
    return 0


if __name__ == "__main__":
    sys.exit(main())
