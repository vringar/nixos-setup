#!/usr/bin/env python3
"""
Update npins pins and fix any resulting FOD hash mismatches.
Updates one pin at a time so at most one cargoHash needs fixing per build cycle.

Usage:
    ./update-pins.py [--capture-logs DIR] [--max-rounds N] [pin ...]
    ./update-pins.py --replay-log FILE         # test hash-fixing against a captured log
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).parent
DEFAULT_MAX_ROUNDS = 5


# ── Pure logic (no I/O, fully unit-testable) ─────────────────────────────────


@dataclass(frozen=True)
class HashMismatch:
    old: str
    new: str


def strip_colmena_prefixes(output: str) -> str:
    """
    Remove per-host prefixes that colmena adds to every build line.

    Colmena formats build output as:
        "  sz1 | <nix output line>"
    and error context as:
        "[ERROR]   stderr) > <nix output line>"

    Stripping these lets the downstream parsers work on plain Nix output.
    """
    # "  sz1 | " style  (main build log)
    output = re.sub(r"^\s*\w[\w-]*\s+\|\s+", "", output, flags=re.MULTILINE)
    # "[ERROR]   stderr)        > " style  (colmena error-context section)
    output = re.sub(r"^\[ERROR\]\s+stderr\)\s+>\s+", "", output, flags=re.MULTILINE)
    return output


def is_cargo_hash_outdated(output: str) -> bool:
    """Return True when Nix reports cargoHash/cargoSha256 is out of date."""
    return "cargoHash or cargoSha256 is out of date" in output


def parse_fod_mismatches(output: str) -> list[HashMismatch]:
    """
    Parse FOD hash-mismatch pairs from Nix build output.

    Nix emits two consecutive lines like:
         specified: sha256-XXXX
            got:    sha256-YYYY
    """
    # Match "specified: <hash>" then whitespace/newline then "got: <hash>".
    # The two lines always appear back-to-back in Nix output.
    pattern = re.compile(
        r"specified:\s*(sha256-\S+)\s+got:\s*(sha256-\S+)"
    )
    seen: set[HashMismatch] = set()
    for m in pattern.finditer(output):
        old, new = m.group(1).rstrip(","), m.group(2).rstrip(",")
        if old != new:
            seen.add(HashMismatch(old, new))
    return list(seen)


def reset_cargo_hash_content(content: str) -> tuple[str, bool]:
    """
    Replace `cargoHash = "sha256-..."` with `cargoHash = ""` in *content*.
    Returns (new_content, changed).
    """
    new, n = re.subn(r'(cargoHash\s*=\s*)"sha256-[^"]*"', r'\1""', content)
    return new, n > 0


def apply_hash_replacement(content: str, old: str, new: str) -> tuple[str, bool]:
    """Replace *old* hash string with *new* in *content*. Returns (new_content, changed)."""
    if old not in content:
        return content, False
    return content.replace(old, new), True


def fill_empty_cargo_hash(content: str, new_hash: str) -> tuple[str, bool]:
    """Replace `cargoHash = ""` with the real hash. Returns (new_content, changed)."""
    new, n = re.subn(r'(cargoHash\s*=\s*)""', rf'\1"{new_hash}"', content)
    return new, n > 0


# ── File I/O layer ────────────────────────────────────────────────────────────


def find_nix_files(root: Path) -> list[Path]:
    """Return all *.nix files under *root*, skipping .workspace trees."""
    return sorted(
        p
        for p in root.rglob("*.nix")
        if ".workspace" not in p.parts
    )


def _files_for_pin(nix_files: list[Path], pin: Optional[str]) -> list[Path]:
    """
    Return the subset of *nix_files* whose path contains *pin* as a component.
    Falls back to all files if no match (e.g. pin lives only in nixpkgs, not locally).
    """
    if not pin:
        return nix_files
    candidates = [f for f in nix_files if pin in f.parts]
    return candidates if candidates else nix_files


def reset_cargo_hash_in_files(
    nix_files: list[Path], root: Path, pin: Optional[str] = None
) -> bool:
    """
    Reset the first cargoHash = "sha256-..." in pin-relevant files to "".
    Falls back to searching all *nix_files* if no pin-scoped match is found.
    Returns True if a change was made.
    """
    for f in _files_for_pin(nix_files, pin):
        content = f.read_text()
        new_content, changed = reset_cargo_hash_content(content)
        if changed:
            print(f"  Resetting cargoHash in {f.relative_to(root)}")
            f.write_text(new_content)
            return True
    # Fallback: no pin-scoped file had a sha256 cargoHash; try all files
    if pin is not None:
        for f in nix_files:
            content = f.read_text()
            new_content, changed = reset_cargo_hash_content(content)
            if changed:
                print(f"  Resetting cargoHash in {f.relative_to(root)}")
                f.write_text(new_content)
                return True
    print("  ERROR: No cargoHash = \"sha256-...\" found to reset")
    return False


def fix_mismatch_in_files(
    nix_files: list[Path], mismatch: HashMismatch, root: Path, pin: Optional[str] = None
) -> bool:
    """
    Replace *mismatch.old* with *mismatch.new* across *nix_files*.
    Falls back to filling in `cargoHash = ""` when the old hash isn't in any file
    (which happens after a cargoHash reset: lib.fakeHash never appears verbatim).
    The fill-fallback is scoped to pin-relevant files to avoid cross-package contamination.
    Returns True if any change was made.
    """
    changed_any = False
    for f in nix_files:
        content = f.read_text()
        new_content, changed = apply_hash_replacement(content, mismatch.old, mismatch.new)
        if changed:
            print(f"  Fixing {f.relative_to(root)}: {mismatch.old} -> {mismatch.new}")
            f.write_text(new_content)
            changed_any = True

    if changed_any:
        return True

    # Fallback: fill empty cargoHash (set in previous round), preferring pin-scoped files
    for f in _files_for_pin(nix_files, pin):
        content = f.read_text()
        new_content, changed = fill_empty_cargo_hash(content, mismatch.new)
        if changed:
            print(f'  Fixing {f.relative_to(root)}: cargoHash="" -> {mismatch.new}')
            f.write_text(new_content)
            return True

    print(f"  WARNING: Could not find {mismatch.old} in any .nix file")
    return False


# ── Subprocess layer ──────────────────────────────────────────────────────────


def run_npins_update(pin: str) -> None:
    subprocess.run(["npins", "update", pin], check=True)


def run_colmena_build(hostname: str) -> tuple[bool, str]:
    """
    Run `colmena build --on <hostname>`, streaming output to the terminal while
    also capturing it for analysis.
    Returns (success, combined_stdout_stderr).
    """
    proc = subprocess.Popen(
        ["colmena", "build", "--on", hostname],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        chunks.append(line)
    proc.wait()
    output = "".join(chunks)
    return proc.returncode == 0, output


# ── Orchestration ─────────────────────────────────────────────────────────────


def handle_build_failure(
    output: str,
    nix_files: list[Path],
    root: Path,
    pin: Optional[str] = None,
) -> bool:
    """
    Inspect *output* and apply the appropriate hash fix to *nix_files*.
    *pin* scopes cargoHash resets and empty-hash fills to pin-relevant files,
    preventing cross-package contamination when multiple packages have cargoHashes.
    Returns True if a fix was applied (caller should retry the build).
    Returns False if we don't know how to fix it (caller should abort).
    """
    normalized = strip_colmena_prefixes(output)

    if is_cargo_hash_outdated(normalized):
        print("==> cargoHash out of date; resetting to \"\" for next round")
        return reset_cargo_hash_in_files(nix_files, root, pin=pin)

    mismatches = parse_fod_mismatches(normalized)
    if not mismatches:
        print("==> Build failed but no hash mismatches found.")
        return False

    print(f"==> Fixing {len(mismatches)} hash mismatch(es)")
    all_fixed = True
    for mm in mismatches:
        if not fix_mismatch_in_files(nix_files, mm, root, pin=pin):
            all_fixed = False
    return all_fixed


def update_pin(
    pin: str,
    hostname: str,
    root: Path,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    capture_dir: Optional[Path] = None,
) -> bool:
    """
    Update *pin* and converge the build by fixing hash mismatches.
    Returns True on success.
    """
    print(f"==> Updating pin: {pin}")
    run_npins_update(pin)

    nix_files = find_nix_files(root)

    for round_num in range(1, max_rounds + 1):
        print(f"==> Build attempt {round_num}/{max_rounds}")
        success, output = run_colmena_build(hostname)

        if capture_dir is not None:
            log_path = capture_dir / f"{pin}-round{round_num}.log"
            log_path.write_text(output)
            print(f"==> Captured build log: {log_path}")

        if success:
            print("==> Build succeeded!")
            return True

        fixed = handle_build_failure(output, nix_files, root, pin=pin)
        if not fixed:
            print(f"==> Cannot fix build failure for pin: {pin}")
            return False

    print(f"==> Failed to converge after {max_rounds} rounds for pin: {pin}")
    return False


def list_pins(sources_json: Path) -> list[str]:
    data = json.loads(sources_json.read_text())
    return sorted(data["pins"].keys())


# ── Entry point ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Update npins pins and fix FOD hash mismatches"
    )
    p.add_argument(
        "pins",
        nargs="*",
        metavar="PIN",
        help="Pins to update (default: all)",
    )
    p.add_argument(
        "--max-rounds",
        type=int,
        default=DEFAULT_MAX_ROUNDS,
        metavar="N",
        help="Max build-fix iterations per pin (default: %(default)s)",
    )
    p.add_argument(
        "--capture-logs",
        metavar="DIR",
        help="Directory to write captured build logs (for test fixtures)",
    )
    p.add_argument(
        "--replay-log",
        metavar="FILE",
        help="Replay a captured build log through the hash-fixing logic (dry-run, no build)",
    )
    p.add_argument(
        "--hostname",
        default=socket.gethostname(),
        help="Colmena target host (default: %(default)s)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.replay_log:
        log_path = Path(args.replay_log)
        output = log_path.read_text()
        nix_files = find_nix_files(SCRIPT_DIR)
        print(f"==> Replaying log: {log_path}")
        result = handle_build_failure(output, nix_files, SCRIPT_DIR)
        return 0 if result else 1

    capture_dir: Optional[Path] = None
    if args.capture_logs:
        capture_dir = Path(args.capture_logs)
        capture_dir.mkdir(parents=True, exist_ok=True)

    pins = args.pins or list_pins(SCRIPT_DIR / "npins" / "sources.json")

    failed: list[str] = []
    for pin in pins:
        ok = update_pin(
            pin,
            hostname=args.hostname,
            root=SCRIPT_DIR,
            max_rounds=args.max_rounds,
            capture_dir=capture_dir,
        )
        if not ok:
            failed.append(pin)

    if failed:
        print(f"==> FAILED pins: {', '.join(failed)}")
        return 1

    print("==> All pins updated successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
