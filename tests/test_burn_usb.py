"""Unit tests for burn-usb.py target selection."""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "burn_usb",
    Path(__file__).parent.parent / "scripts" / "burn-usb.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["burn_usb"] = _mod
_spec.loader.exec_module(_mod)

check_target = _mod.check_target
listing = _mod.listing
mountpoints = _mod.mountpoints
usb_disks = _mod.usb_disks

GiB = 2**30

# Shape of `lsblk --json --bytes --output PATH,SIZE,MODEL,TRAN,TYPE,MOUNTPOINTS`
# from util-linux 2.42, which reports nothing mounted as [].
LSBLK = {
    "blockdevices": [
        {
            "path": "/dev/nvme0n1",
            "size": 1000 * GiB,
            "model": "Samsung SSD 980",
            "tran": "nvme",
            "type": "disk",
            "mountpoints": [],
            "children": [
                {
                    "path": "/dev/nvme0n1p1",
                    "size": GiB,
                    "model": None,
                    "tran": "nvme",
                    "type": "part",
                    "mountpoints": ["/boot"],
                }
            ],
        },
        {
            "path": "/dev/sda",
            "size": 32 * GiB,
            "model": "Ultra           ",
            "tran": "usb",
            "type": "disk",
            "mountpoints": [],
            "children": [
                {
                    "path": "/dev/sda1",
                    "size": 32 * GiB,
                    "model": None,
                    "tran": "usb",
                    "type": "part",
                    "mountpoints": [],
                }
            ],
        },
        {
            "path": "/dev/sdb",
            "size": 16 * GiB,
            "model": "Cruzer",
            "tran": "usb",
            "type": "disk",
            "mountpoints": [],
            "children": [
                {
                    "path": "/dev/sdb1",
                    "size": 16 * GiB,
                    "model": None,
                    "tran": "usb",
                    "type": "part",
                    "mountpoints": ["/run/media/vringar/STICK"],
                }
            ],
        },
    ]
}


def test_usb_disks_excludes_internal_disks():
    assert [d["path"] for d in usb_disks(LSBLK)] == ["/dev/sda", "/dev/sdb"]


def test_mountpoints_include_partitions():
    sdb = usb_disks(LSBLK)[1]
    assert mountpoints(sdb) == ["/run/media/vringar/STICK"]


def test_mountpoints_accept_pre_2_42_null_entries():
    assert (
        mountpoints({"mountpoints": [None], "children": [{"mountpoints": [None]}]})
        == []
    )


def test_listing_says_none_without_usb_disks():
    assert listing([]) == "  (none)"


def test_check_target_accepts_unmounted_usb_disk():
    assert check_target(usb_disks(LSBLK), "/dev/sda", GiB)["path"] == "/dev/sda"


@pytest.mark.parametrize(
    "device",
    [
        "/dev/nvme0n1",  # internal disk
        "/dev/sda1",  # partition, not a whole disk
        "/dev/sdz",  # absent
    ],
)
def test_check_target_rejects_non_usb_disks(device):
    with pytest.raises(SystemExit, match="not a whole USB disk"):
        check_target(usb_disks(LSBLK), device, GiB)


def test_check_target_rejects_mounted_disk():
    with pytest.raises(SystemExit, match="mounted"):
        check_target(usb_disks(LSBLK), "/dev/sdb", GiB)


def test_check_target_rejects_too_small_disk():
    with pytest.raises(SystemExit, match="smaller"):
        check_target(usb_disks(LSBLK), "/dev/sda", 64 * GiB)
