#!/usr/bin/env python3
"""Write a disk image to a USB stick.

    scripts/burn-usb.py                  # list the USB disks
    scripts/burn-usb.py IMAGE DEVICE     # write IMAGE to DEVICE

Only a whole USB disk with nothing mounted is accepted, and its size has to be
typed back before anything is written. The disk is checked again after that,
since the stick can be swapped while the prompt waits, and opened exclusively,
which fails if the kernel holds it for anything lsblk does not show as a mount
(LUKS, LVM, md, ZFS, swap). dd and the read-back comparison run under sudo;
everything else runs as the invoking user. Run it outside the Claude sandbox:
it needs sudo and the real /dev.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

LSBLK_COLUMNS = "PATH,SIZE,MODEL,TRAN,TYPE,MOUNTPOINTS"

# Run as root: O_EXCL on a block device fails with EBUSY while any partition
# of it is mounted, used as swap, or claimed by device-mapper, md or ZFS.
EXCLUSIVE_OPEN = (
    "import os, sys; os.close(os.open(sys.argv[1], os.O_RDONLY | os.O_EXCL))"
)


def usb_disks(lsblk: dict) -> list[dict]:
    """Whole disks attached over USB, from `lsblk --json` output."""
    return [
        dev
        for dev in lsblk.get("blockdevices", [])
        if dev.get("type") == "disk" and dev.get("tran") == "usb"
    ]


def mountpoints(dev: dict) -> list[str]:
    """Everything mounted from dev or any of its partitions."""
    # util-linux before 2.42 reports an unmounted device as [null], 2.42 as [].
    found = [m for m in dev.get("mountpoints") or [] if m]
    for child in dev.get("children") or []:
        found.extend(mountpoints(child))
    return found


def size_gib(dev: dict) -> str:
    return f"{int(dev['size']) / 2**30:.1f}"


def describe(dev: dict) -> str:
    return f"{dev['path']}  {size_gib(dev)} GiB  {(dev.get('model') or '').strip()}"


def listing(disks: list[dict]) -> str:
    return "\n".join(f"  {describe(d)}" for d in disks) or "  (none)"


def check_target(disks: list[dict], device: str, image_size: int) -> dict:
    """The lsblk entry for device, if it is safe to overwrite with the image."""
    by_path = {dev["path"]: dev for dev in disks}
    dev = by_path.get(device)
    if dev is None:
        raise SystemExit(
            f"{device} is not a whole USB disk. USB disks:\n{listing(disks)}"
        )
    mounted = mountpoints(dev)
    if mounted:
        raise SystemExit(f"{device} has mounted filesystems: {', '.join(mounted)}")
    if int(dev["size"]) < image_size:
        raise SystemExit(f"{device} is smaller than the image ({image_size} bytes)")
    return dev


def read_lsblk() -> dict:
    result = subprocess.run(
        ["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", type=Path, nargs="?")
    parser.add_argument("device", nargs="?")
    args = parser.parse_args()

    if args.device is None:
        print("USB disks:")
        print(listing(usb_disks(read_lsblk())))
        return 0

    image_size = args.image.stat().st_size
    # Accept /dev/disk/by-id/... and friends; lsblk reports the kernel name.
    device = os.path.realpath(args.device)
    dev = check_target(usb_disks(read_lsblk()), device, image_size)

    # Asking for something only shown here, not the name already typed on the
    # command line, is what makes the user actually read which disk this is.
    print(f"About to ERASE {describe(dev)}")
    if input("Type its size in GiB, as shown, to continue: ").strip() != size_gib(dev):
        print("Aborted.")
        return 1

    # Take the sudo password now, so the recheck below is not followed by
    # another open-ended wait.
    subprocess.run(["sudo", "--validate"], check=True)
    recheck = check_target(usb_disks(read_lsblk()), device, image_size)
    if describe(recheck) != describe(dev):
        raise SystemExit(
            f"{device} changed since it was confirmed: {describe(recheck)}"
        )
    probe = ["sudo", sys.executable, "-c", EXCLUSIVE_OPEN, device]
    if subprocess.run(probe, check=False).returncode != 0:
        raise SystemExit(f"{device} is in use")

    subprocess.run(
        [
            "sudo",
            "dd",
            f"if={args.image}",
            f"of={device}",
            "bs=4M",
            # nocreat: if the stick vanishes, fail instead of creating a
            # regular file named after it in /dev.
            "conv=fsync,nocreat",
            "oflag=direct",
            "status=progress",
        ],
        check=True,
    )
    # dd only reports what it handed to the device. Reading it back catches
    # sticks that silently drop writes (counterfeit capacity, worn flash).
    # oflag=direct left no page cache behind, so cmp reads the stick itself.
    print("Verifying...")
    verify = subprocess.run(
        ["sudo", "cmp", "--bytes", str(image_size), str(args.image), device],
        check=False,
    )
    if verify.returncode == 1:
        raise SystemExit(f"{device} does not match {args.image}")
    if verify.returncode != 0:
        raise SystemExit(f"could not read {device} back to verify it")
    print(f"Done: {args.image} written to {device} and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
