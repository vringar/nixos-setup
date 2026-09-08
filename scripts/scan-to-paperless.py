#!/usr/bin/env python3
"""Scan a document over the network straight into Paperless' consume directory.

Discovers eSCL/WSD scanners via SANE (sane-airscan), scans the flatbed (single
page by default, or an interactive multi-page loop), and wraps the result into
one PDF with img2pdf. Paperless does its own OCR, so a plain image-only PDF is
all it needs; dropping it under `consume/scanner/` auto-tags it `scanner`
(PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS).

The Epson XP-970 is flatbed-only, so the default is a single flatbed page; use
`-m/--multipage` to scan several glass pages into one PDF, or `--adf` on a
scanner that has a feeder.

Binaries resolve from PATH; override with $SCANIMAGE / $IMG2PDF. The output
directory must be writable by the invoking user, so run this as the `paperless`
user (or point `--out-dir` somewhere you can write and let Paperless pick it up):

    sudo -u paperless scan-to-paperless.py            # single flatbed page
    sudo -u paperless scan-to-paperless.py -m         # multi-page flatbed
    scan-to-paperless.py --list                       # just discover scanners
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCANIMAGE = os.environ.get("SCANIMAGE", "scanimage")
IMG2PDF = os.environ.get("IMG2PDF", "img2pdf")

# The Paperless consume dir; a `scanner/` subdir turns into the `scanner` tag.
DEFAULT_OUT_DIR = "/var/lib/paperless/consume/scanner"

# `scanimage -L` prints one line per device:
#   device `airscan:e0:Epson XP-970 Series' is a eSCL Epson XP-970 Series flatbed scanner
_DEVICE_RE = re.compile(r"device `([^']+)' is a (.+?)\s*$")


@dataclass(frozen=True)
class Device:
    id: str
    description: str

    @property
    def is_network(self) -> bool:
        # The airscan backend labels eSCL/WSD devices `escl:`/`wsd:` (and
        # sometimes `airscan:`); `net:` is saned. Anything carrying a URL is
        # remote too. Local backends (v4l, genesys, ...) match none of these.
        return self.id.startswith(("airscan:", "escl:", "wsd:", "net:")) or (
            "://" in self.id
        )


def parse_devices(text: str) -> list[Device]:
    """Parse the output of `scanimage -L` into a list of Devices."""
    out = []
    for line in text.splitlines():
        m = _DEVICE_RE.match(line.strip())
        if m:
            out.append(Device(id=m.group(1), description=m.group(2)))
    return out


def pick_device(
    devices: list[Device], match: str | None = None, prefer_network: bool = True
) -> Device:
    """Select one device, or raise LookupError with an actionable message.

    With `match`, keep devices whose id or description contains it. Otherwise,
    prefer network scanners when any exist. Ambiguity is an error, never a guess.
    """
    if not devices:
        raise LookupError(
            "no scanners found. Is the printer on and on the LAN, and is "
            "sane-airscan installed with avahi/mDNS running?"
        )

    candidates = devices
    if match:
        needle = match.lower()
        candidates = [
            d
            for d in devices
            if needle in d.id.lower() or needle in d.description.lower()
        ]
        if not candidates:
            listing = "\n".join(f"  {d.id}  ({d.description})" for d in devices)
            raise LookupError(f"no device matches {match!r}. Found:\n{listing}")
    elif prefer_network:
        network = [d for d in devices if d.is_network]
        if network:
            candidates = network

    if len(candidates) == 1:
        return candidates[0]

    listing = "\n".join(f"  {d.id}  ({d.description})" for d in candidates)
    raise LookupError(
        "multiple scanners match; pass --device <id> or narrow with "
        f"--match <text>:\n{listing}"
    )


def discover() -> list[Device]:
    proc = subprocess.run(
        [SCANIMAGE, "-L"], capture_output=True, text=True, check=True
    )
    return parse_devices(proc.stdout)


def _scan_one(device: str, dest: Path, resolution: int, mode: str, source: str) -> None:
    # TIFF carries the resolution tags scanimage writes, so img2pdf lays the
    # page out at the right physical size without us passing --pagesize.
    cmd = [
        SCANIMAGE,
        "-d", device,
        "--format=tiff",
        "--resolution", str(resolution),
        "--mode", mode,
        "--source", source,
    ]
    with dest.open("wb") as fh:
        subprocess.run(cmd, stdout=fh, check=True)


def scan_flatbed(
    device: str,
    work: Path,
    resolution: int,
    mode: str,
    source: str,
    multipage: bool,
) -> list[Path]:
    pages: list[Path] = []
    while True:
        idx = len(pages) + 1
        if multipage:
            input(f"Place page {idx} on the glass and press Enter (Ctrl-C to abort)…")
        dest = work / f"page-{idx:03d}.tiff"
        _scan_one(device, dest, resolution, mode, source)
        pages.append(dest)
        if not multipage:
            break
        if input("Scan another page? [y/N] ").strip().lower() not in ("y", "yes"):
            break
    return pages


def scan_adf(
    device: str, work: Path, resolution: int, mode: str, source: str
) -> list[Path]:
    pattern = str(work / "page-%03d.tiff")
    cmd = [
        SCANIMAGE,
        "-d", device,
        "--format=tiff",
        f"--batch={pattern}",
        "--resolution", str(resolution),
        "--mode", mode,
        "--source", source,
    ]
    subprocess.run(cmd, check=True)
    return sorted(work.glob("page-*.tiff"))


def build_pdf(pages: list[Path], out_pdf: Path) -> None:
    subprocess.run([IMG2PDF, *map(str, pages), "-o", str(out_pdf)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--list", action="store_true", help="discover scanners and exit"
    )
    parser.add_argument("-d", "--device", help="explicit SANE device id")
    parser.add_argument("--match", help="substring to select among discovered devices")
    parser.add_argument(
        "-o", "--out-dir", default=DEFAULT_OUT_DIR, help="where the PDF is written"
    )
    parser.add_argument(
        "-n", "--name", help="base filename (default scan-<timestamp>)"
    )
    parser.add_argument(
        "-m", "--multipage", action="store_true",
        help="flatbed: prompt for each additional page, combine into one PDF",
    )
    parser.add_argument(
        "--adf", action="store_true", help="use the ADF feeder (batch scan)"
    )
    parser.add_argument(
        "--source", help="override the SANE source (default Flatbed, or ADF with --adf)"
    )
    parser.add_argument("-r", "--resolution", type=int, default=300)
    parser.add_argument("--mode", default="Color", help="Color, Gray, or Lineart")
    args = parser.parse_args(argv)

    if args.list:
        try:
            devices = discover()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"scanner discovery failed: {exc}", file=sys.stderr)
            return 1
        if not devices:
            print("no scanners found.")
            return 1
        for d in devices:
            tag = "network" if d.is_network else "local"
            print(f"{d.id}\t[{tag}]\t{d.description}")
        return 0

    # An explicit --device skips discovery entirely: no `scanimage -L` latency,
    # and no race with a sleepy printer that has dropped off mDNS (connecting
    # to it wakes it). Discovery only runs for --list or auto-selection.
    if args.device:
        device_id = args.device
    else:
        try:
            devices = discover()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"scanner discovery failed: {exc}", file=sys.stderr)
            return 1
        try:
            device_id = pick_device(devices, match=args.match).id
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1

    source = args.source or ("ADF" if args.adf else "Flatbed")
    out_dir = Path(args.out_dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        print(
            f"cannot write to {out_dir} — run as the paperless user or pass "
            "--out-dir somewhere writable.",
            file=sys.stderr,
        )
        return 1

    base = args.name or f"scan-{datetime.now():%Y%m%d-%H%M%S}"
    out_pdf = out_dir / f"{base}.pdf"

    with tempfile.TemporaryDirectory(prefix="scan-to-paperless-") as tmp:
        work = Path(tmp)
        try:
            if args.adf:
                pages = scan_adf(device_id, work, args.resolution, args.mode, source)
            else:
                pages = scan_flatbed(
                    device_id, work, args.resolution, args.mode, source,
                    args.multipage,
                )
        except subprocess.CalledProcessError as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
            return 1

        if not pages:
            print("no pages scanned.", file=sys.stderr)
            return 1

        try:
            build_pdf(pages, out_pdf)
        except subprocess.CalledProcessError as exc:
            print(f"PDF assembly failed: {exc}", file=sys.stderr)
            return 1

    print(f"wrote {out_pdf} ({len(pages)} page(s)) — Paperless will pick it up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
