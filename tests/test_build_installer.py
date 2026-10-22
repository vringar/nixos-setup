"""Unit tests for build-installer.py."""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_installer",
    Path(__file__).parent.parent / "scripts" / "build-installer.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["build_installer"] = _mod
_spec.loader.exec_module(_mod)

find_iso = _mod.find_iso


def test_find_iso_returns_the_single_iso(tmp_path):
    (tmp_path / "iso").mkdir()
    iso = tmp_path / "iso" / "nixos-minimal.iso"
    iso.touch()
    assert find_iso(tmp_path) == iso


def test_find_iso_rejects_missing_iso(tmp_path):
    (tmp_path / "iso").mkdir()
    with pytest.raises(SystemExit):
        find_iso(tmp_path)
