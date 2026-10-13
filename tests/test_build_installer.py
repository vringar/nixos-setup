"""Unit tests for build-installer.py key collection."""

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

collect_keys = _mod.collect_keys
find_iso = _mod.find_iso
parse_public_keys = _mod.parse_public_keys

ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample me@host"
SK = "sk-ecdsa-sha2-nistp256@openssh.com AAAAInNrLWVjZHNhExample git@host"


def test_parse_keeps_known_key_types():
    assert parse_public_keys(f"{ED25519}\n{SK}\n") == [ED25519, SK]


def test_parse_drops_wireguard_key():
    # ~/.ssh/SZ1.pub on sz1 holds a bare base64 WireGuard key.
    assert parse_public_keys("+wGwN6M6RNApY/K02lgsRSeySlgqdZL+kabnkjvHuDw=\n") == []


def test_parse_ignores_blank_lines_and_whitespace():
    assert parse_public_keys(f"\n  {ED25519}  \n\n") == [ED25519]


def test_collect_reads_only_pub_files(tmp_path):
    (tmp_path / "id_ed25519.pub").write_text(ED25519 + "\n")
    (tmp_path / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (tmp_path / "wg.pub").write_text("+wGwN6M6RNApY/K02lgsRSeySlgqdZL+kabnkjvHuDw=\n")

    keys, skipped = collect_keys(tmp_path)

    assert keys == [ED25519]
    assert skipped == [tmp_path / "wg.pub"]


def test_find_iso_returns_the_single_iso(tmp_path):
    (tmp_path / "iso").mkdir()
    iso = tmp_path / "iso" / "nixos-minimal.iso"
    iso.touch()
    assert find_iso(tmp_path) == iso


def test_find_iso_rejects_missing_iso(tmp_path):
    (tmp_path / "iso").mkdir()
    with pytest.raises(SystemExit):
        find_iso(tmp_path)
