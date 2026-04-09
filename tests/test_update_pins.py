"""Unit tests for update-pins.py logic functions."""

import sys
import textwrap
from pathlib import Path
import pytest

# Allow importing the script as a module despite the hyphen in the filename
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "update_pins",
    Path(__file__).parent.parent / "scripts" / "update-pins.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["update_pins"] = _mod  # required before exec for @dataclass
_spec.loader.exec_module(_mod)

HashMismatch = _mod.HashMismatch
apply_hash_replacement = _mod.apply_hash_replacement
fill_empty_cargo_hash = _mod.fill_empty_cargo_hash
handle_build_failure = _mod.handle_build_failure
is_cargo_hash_outdated = _mod.is_cargo_hash_outdated
parse_fod_mismatches = _mod.parse_fod_mismatches
reset_cargo_hash_content = _mod.reset_cargo_hash_content
strip_colmena_prefixes = _mod.strip_colmena_prefixes
_files_for_pin = _mod._files_for_pin
reset_cargo_hash_in_files = _mod.reset_cargo_hash_in_files
fix_mismatch_in_files = _mod.fix_mismatch_in_files

FIXTURES = Path(__file__).parent / "fixtures"


# ── is_cargo_hash_outdated ────────────────────────────────────────────────────


def test_cargo_hash_outdated_detected():
    log = (FIXTURES / "cargo_hash_outdated.log").read_text()
    assert is_cargo_hash_outdated(log)


def test_cargo_hash_outdated_not_triggered_on_fod():
    log = (FIXTURES / "fod_mismatch.log").read_text()
    assert not is_cargo_hash_outdated(log)


def test_cargo_hash_outdated_not_triggered_on_success():
    log = (FIXTURES / "success.log").read_text()
    assert not is_cargo_hash_outdated(log)


# ── parse_fod_mismatches ──────────────────────────────────────────────────────


def test_parse_single_mismatch():
    log = (FIXTURES / "fod_mismatch.log").read_text()
    mismatches = parse_fod_mismatches(log)
    assert len(mismatches) == 1
    mm = mismatches[0]
    assert mm.old == "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert mm.new == "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="


def test_parse_multiple_mismatches():
    log = (FIXTURES / "multi_mismatch.log").read_text()
    mismatches = parse_fod_mismatches(log)
    assert len(mismatches) == 2
    olds = {mm.old for mm in mismatches}
    news = {mm.new for mm in mismatches}
    assert "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" in olds
    assert "sha256-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=" in olds
    assert "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=" in news
    assert "sha256-DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD=" in news


def test_parse_no_mismatches_on_success():
    log = (FIXTURES / "success.log").read_text()
    assert parse_fod_mismatches(log) == []


def test_parse_deduplicates_identical_pairs():
    log = textwrap.dedent("""\
        specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
           got:    sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
        specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
           got:    sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=
    """)
    assert len(parse_fod_mismatches(log)) == 1


def test_parse_ignores_same_hash():
    log = textwrap.dedent("""\
        specified: sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
           got:    sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
    """)
    assert parse_fod_mismatches(log) == []


# ── reset_cargo_hash_content ──────────────────────────────────────────────────


def test_reset_cargo_hash_replaces_sha256():
    content = 'cargoHash = "sha256-abc123XYZ=";'
    new_content, changed = reset_cargo_hash_content(content)
    assert changed
    assert 'cargoHash = ""' in new_content
    assert "sha256-abc123XYZ=" not in new_content


def test_reset_cargo_hash_no_change_when_already_empty():
    content = 'cargoHash = "";'
    new_content, changed = reset_cargo_hash_content(content)
    assert not changed
    assert new_content == content


def test_reset_cargo_hash_no_change_when_absent():
    content = 'src = fetchFromGitHub { ... };'
    _, changed = reset_cargo_hash_content(content)
    assert not changed


def test_reset_cargo_hash_preserves_surrounding_content():
    content = textwrap.dedent("""\
        stdenv.mkDerivation {
          pname = "myapp";
          cargoHash = "sha256-wf2RxYd8xc6stHHrEGbL+1SkzHj5643c94WUP59m/8M=";
          version = "1.0";
        }
    """)
    new_content, changed = reset_cargo_hash_content(content)
    assert changed
    assert 'cargoHash = ""' in new_content
    assert 'pname = "myapp"' in new_content
    assert 'version = "1.0"' in new_content


# ── apply_hash_replacement ────────────────────────────────────────────────────


def test_apply_hash_replacement_found():
    content = 'hash = "sha256-AAAA=";'
    new_content, changed = apply_hash_replacement(content, "sha256-AAAA=", "sha256-BBBB=")
    assert changed
    assert 'sha256-BBBB=' in new_content
    assert 'sha256-AAAA=' not in new_content


def test_apply_hash_replacement_not_found():
    content = 'hash = "sha256-XXXX=";'
    new_content, changed = apply_hash_replacement(content, "sha256-NOTHERE=", "sha256-NEW=")
    assert not changed
    assert new_content == content


# ── fill_empty_cargo_hash ─────────────────────────────────────────────────────


def test_fill_empty_cargo_hash():
    content = 'cargoHash = "";'
    new_content, changed = fill_empty_cargo_hash(content, "sha256-NEWHASH=")
    assert changed
    assert 'cargoHash = "sha256-NEWHASH="' in new_content


def test_fill_empty_cargo_hash_no_op_when_not_empty():
    content = 'cargoHash = "sha256-EXISTING=";'
    new_content, changed = fill_empty_cargo_hash(content, "sha256-NEWHASH=")
    assert not changed
    assert new_content == content


# ── handle_build_failure (integration of logic + file I/O) ───────────────────


def test_handle_build_failure_cargo_hash_outdated(tmp_path):
    nix_file = tmp_path / "pkg.nix"
    nix_file.write_text('cargoHash = "sha256-OLDHASH=";')

    log = (FIXTURES / "cargo_hash_outdated.log").read_text()
    result = handle_build_failure(log, [nix_file], tmp_path)

    assert result
    assert nix_file.read_text() == 'cargoHash = "";'


def test_handle_build_failure_fod_mismatch(tmp_path):
    old = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    new = "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    nix_file = tmp_path / "pkg.nix"
    nix_file.write_text(f'hash = "{old}";')

    log = (FIXTURES / "fod_mismatch.log").read_text()
    result = handle_build_failure(log, [nix_file], tmp_path)

    assert result
    assert nix_file.read_text() == f'hash = "{new}";'


def test_handle_build_failure_fod_via_empty_cargo_hash(tmp_path):
    """After cargoHash reset to "", the fakeHash mismatch fills it back in."""
    new = "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    nix_file = tmp_path / "pkg.nix"
    nix_file.write_text('cargoHash = "";')  # hash was reset in prior round

    log = (FIXTURES / "fod_mismatch.log").read_text()
    result = handle_build_failure(log, [nix_file], tmp_path)

    assert result
    assert f'cargoHash = "{new}"' in nix_file.read_text()


def test_handle_build_failure_unknown_returns_false(tmp_path):
    result = handle_build_failure("some random build failure", [], tmp_path)
    assert not result


# ── strip_colmena_prefixes ────────────────────────────────────────────────────


def test_strip_colmena_sz1_prefix():
    raw = "  sz1 | error: hash mismatch\n  sz1 |    specified: sha256-AAA=\n"
    stripped = strip_colmena_prefixes(raw)
    assert "sz1 |" not in stripped
    assert "error: hash mismatch" in stripped
    assert "specified: sha256-AAA=" in stripped


def test_strip_colmena_arbitrary_hostname():
    raw = "  myhost-1 | some nix output\n"
    stripped = strip_colmena_prefixes(raw)
    assert "myhost-1 |" not in stripped
    assert "some nix output" in stripped


def test_strip_colmena_error_context():
    raw = "[ERROR]   stderr) > specified: sha256-AAA=\n"
    stripped = strip_colmena_prefixes(raw)
    assert "[ERROR]" not in stripped
    assert "specified: sha256-AAA=" in stripped


def test_strip_colmena_is_noop_on_plain_output():
    raw = "error: hash mismatch\n   specified: sha256-AAA=\n      got:    sha256-BBB=\n"
    assert strip_colmena_prefixes(raw) == raw


# ── Colmena-format end-to-end (handle_build_failure with real prefix format) ──


def test_handle_build_failure_colmena_fod_mismatch(tmp_path):
    old = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    new = "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="
    nix_file = tmp_path / "pkg.nix"
    nix_file.write_text(f'hash = "{old}";')

    log = (FIXTURES / "colmena_fod_mismatch.log").read_text()
    result = handle_build_failure(log, [nix_file], tmp_path)

    assert result
    assert nix_file.read_text() == f'hash = "{new}";'


def test_handle_build_failure_colmena_cargo_hash_outdated(tmp_path):
    nix_file = tmp_path / "pkg.nix"
    nix_file.write_text('cargoHash = "sha256-OLDHASH=";')

    log = (FIXTURES / "colmena_cargo_hash_outdated.log").read_text()
    result = handle_build_failure(log, [nix_file], tmp_path)

    assert result
    assert nix_file.read_text() == 'cargoHash = "";'


# ── _files_for_pin ────────────────────────────────────────────────────────────


def test_files_for_pin_filters_by_path_component(tmp_path):
    rtk = tmp_path / "apps" / "rtk" / "default.nix"
    crosslink = tmp_path / "apps" / "crosslink" / "default.nix"
    rtk.parent.mkdir(parents=True)
    crosslink.parent.mkdir(parents=True)
    rtk.touch()
    crosslink.touch()

    result = _files_for_pin([rtk, crosslink], "rtk")
    assert result == [rtk]

    result = _files_for_pin([rtk, crosslink], "crosslink")
    assert result == [crosslink]


def test_files_for_pin_falls_back_when_no_match(tmp_path):
    f = tmp_path / "modules" / "something.nix"
    f.parent.mkdir(parents=True)
    f.touch()
    # "rtk" not in path → returns all files
    result = _files_for_pin([f], "rtk")
    assert result == [f]


def test_files_for_pin_none_returns_all(tmp_path):
    f = tmp_path / "pkg.nix"
    f.touch()
    assert _files_for_pin([f], None) == [f]


# ── Pin-scoped reset/fill prevents cross-package contamination ────────────────


def test_reset_cargo_hash_scoped_to_pin(tmp_path):
    """Should reset only the pin-matching file, not the alphabetically first one."""
    crosslink = tmp_path / "apps" / "crosslink" / "default.nix"
    rtk = tmp_path / "apps" / "rtk" / "default.nix"
    crosslink.parent.mkdir(parents=True)
    rtk.parent.mkdir(parents=True)
    crosslink.write_text('cargoHash = "sha256-CROSSLINK=";')
    rtk.write_text('cargoHash = "sha256-RTK=";')

    changed = reset_cargo_hash_in_files([crosslink, rtk], tmp_path, pin="rtk")

    assert changed
    assert crosslink.read_text() == 'cargoHash = "sha256-CROSSLINK=";'  # untouched
    assert rtk.read_text() == 'cargoHash = "";'  # only rtk reset


def test_fill_empty_cargo_hash_scoped_to_pin(tmp_path):
    """fill_empty_cargo_hash fallback should target the pin-matching file."""
    crosslink = tmp_path / "apps" / "crosslink" / "default.nix"
    rtk = tmp_path / "apps" / "rtk" / "default.nix"
    crosslink.parent.mkdir(parents=True)
    rtk.parent.mkdir(parents=True)
    crosslink.write_text('cargoHash = "";')  # crosslink also empty (shouldn't be touched)
    rtk.write_text('cargoHash = "";')
    mm = HashMismatch(old="sha256-FAKEHASH=", new="sha256-REALHASH=")

    changed = fix_mismatch_in_files([crosslink, rtk], mm, tmp_path, pin="rtk")

    assert changed
    assert 'sha256-REALHASH=' in rtk.read_text()
    assert crosslink.read_text() == 'cargoHash = "";'  # crosslink untouched


# ── Real log fixtures from actual builds (add as you capture them) ────────────
# To capture a real log:
#   ./scripts/update-pins.py --capture-logs tests/fixtures/captured <pin>
# Then write a test here that exercises the parsing on that log.
