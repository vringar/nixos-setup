#!/usr/bin/env python3
"""Build installer-iso.nix with every SSH public key from ~/.ssh.

The keys are authorized for root on the live system, so once it is on the LAN
or the tailnet the install can be driven over SSH. Only *.pub files are read:
private keys never go near the Nix store, which is world-readable.

    scripts/build-installer.py [--ssh-dir DIR] [--out-link PATH]

Prints the path of the ISO on success.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Key types sshd accepts in authorized_keys. A .pub file whose first field is
# none of these is not an SSH key (~/.ssh/SZ1.pub is a bare WireGuard key).
KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


def parse_public_keys(text: str) -> list[str]:
    """The authorized_keys lines in a .pub file, dropping anything else."""
    keys = []
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in KEY_TYPES:
            keys.append(line.strip())
    return keys


def collect_keys(ssh_dir: Path) -> tuple[list[str], list[Path]]:
    """Keys from every *.pub in ssh_dir, plus the files that held none."""
    keys: list[str] = []
    skipped: list[Path] = []
    for path in sorted(ssh_dir.glob("*.pub")):
        found = parse_public_keys(path.read_text(encoding="utf-8"))
        if found:
            keys.extend(found)
        else:
            skipped.append(path)
    return keys, skipped


def find_iso(out_link: Path) -> Path:
    isos = sorted((out_link / "iso").glob("*.iso"))
    if len(isos) != 1:
        raise SystemExit(f"expected one ISO under {out_link}/iso, found {isos}")
    return isos[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ssh-dir", type=Path, default=Path.home() / ".ssh")
    parser.add_argument("--out-link", type=Path, default=REPO / "result-installer")
    args = parser.parse_args()

    keys, skipped = collect_keys(args.ssh_dir)
    for path in skipped:
        print(f"skipping {path}: no SSH public key in it", file=sys.stderr)
    if not keys:
        raise SystemExit(f"no SSH public keys in {args.ssh_dir}")
    for key in keys:
        key_type, _blob, *comment = key.split()
        print(f"authorizing {key_type} {' '.join(comment)}", file=sys.stderr)

    subprocess.run(
        [
            "nix-build",
            str(REPO / "installer-iso.nix"),
            "--argstr",
            "authorizedKeys",
            "\n".join(keys),
            "--out-link",
            str(args.out_link),
        ],
        check=True,
    )
    print(find_iso(args.out_link))
    return 0


if __name__ == "__main__":
    sys.exit(main())
