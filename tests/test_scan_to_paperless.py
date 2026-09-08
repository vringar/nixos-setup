"""Unit tests for scan-to-paperless.py's device discovery parsing/selection."""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "scan_to_paperless",
    Path(__file__).parent.parent / "scripts" / "scan-to-paperless.py",
)
scan = importlib.util.module_from_spec(_spec)
sys.modules["scan_to_paperless"] = scan
_spec.loader.exec_module(scan)


# A realistic `scanimage -L` dump: the Epson over eSCL, a WSD Brother, and a
# local USB device, so parsing and the network-preference both get exercised.
SAMPLE = """\
device `airscan:e0:Epson XP-970 Series' is a eSCL Epson XP-970 Series flatbed scanner
device `airscan:w1:Brother MFC-L2710DW' is a WSD Brother MFC-L2710DW scanner
device `genesys:libusb:001:004' is a Canon LiDE 210 flatbed scanner
"""


def test_parse_extracts_id_and_description():
    devices = scan.parse_devices(SAMPLE)
    assert [d.id for d in devices] == [
        "airscan:e0:Epson XP-970 Series",
        "airscan:w1:Brother MFC-L2710DW",
        "genesys:libusb:001:004",
    ]
    assert devices[0].description == "eSCL Epson XP-970 Series flatbed scanner"


def test_parse_marks_network_devices():
    devices = scan.parse_devices(SAMPLE)
    assert [d.is_network for d in devices] == [True, True, False]


def test_parse_ignores_noise_lines():
    noisy = "searching for devices...\n" + SAMPLE + "\n"
    assert len(scan.parse_devices(noisy)) == 3


def test_parse_empty():
    assert scan.parse_devices("") == []


def test_pick_prefers_network_but_still_ambiguous():
    # Two network devices -> can't guess, must raise with both listed.
    devices = scan.parse_devices(SAMPLE)
    with pytest.raises(LookupError) as exc:
        scan.pick_device(devices)
    assert "airscan:e0" in str(exc.value) and "airscan:w1" in str(exc.value)
    # The local Canon must not appear once network devices exist.
    assert "genesys" not in str(exc.value)


def test_pick_match_selects_single():
    devices = scan.parse_devices(SAMPLE)
    assert scan.pick_device(devices, match="XP-970").id == "airscan:e0:Epson XP-970 Series"


def test_pick_match_is_case_insensitive_and_matches_description():
    devices = scan.parse_devices(SAMPLE)
    assert scan.pick_device(devices, match="canon").id == "genesys:libusb:001:004"


def test_pick_single_network_device_is_chosen():
    devices = [
        scan.Device("airscan:e0:Epson XP-970 Series", "eSCL Epson flatbed scanner"),
        scan.Device("genesys:libusb:001:004", "Canon LiDE 210 flatbed scanner"),
    ]
    assert scan.pick_device(devices).id == "airscan:e0:Epson XP-970 Series"


def test_pick_no_devices_raises():
    with pytest.raises(LookupError, match="no scanners found"):
        scan.pick_device([])


def test_pick_no_match_raises_with_listing():
    devices = scan.parse_devices(SAMPLE)
    with pytest.raises(LookupError, match="no device matches"):
        scan.pick_device(devices, match="nonexistent")


# The real sz1 run: airscan labels the Epson with an `escl:` id (not `airscan:`),
# and a v4l webcam shares the bus. This is what broke auto-selection before.
REAL = """\
device `v4l:/dev/video0' is a Noname Integrated Camera: Integrated C virtual device
device `escl:https://192.168.178.56:443' is a Epson XP-970 Series platen scanner
"""


def test_escl_device_is_classified_network():
    devices = scan.parse_devices(REAL)
    by_id = {d.id: d for d in devices}
    assert by_id["escl:https://192.168.178.56:443"].is_network is True
    assert by_id["v4l:/dev/video0"].is_network is False


def test_pick_selects_escl_printer_over_local_webcam():
    devices = scan.parse_devices(REAL)
    assert scan.pick_device(devices).id == "escl:https://192.168.178.56:443"
