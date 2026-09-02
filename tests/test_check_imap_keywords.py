"""Unit tests for check-imap-keywords.py logic functions."""

import sqlite3
import sys
from pathlib import Path

import pytest

# Allow importing the script as a module despite the hyphen in the filename
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "check_imap_keywords",
    Path(__file__).parent.parent / "scripts" / "check-imap-keywords.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_imap_keywords"] = _mod
_spec.loader.exec_module(_mod)

assess_permanentflags = _mod.assess_permanentflags
default_port = _mod.default_port
load_tag_rules = _mod.load_tag_rules
parse_flag_lines = _mod.parse_flag_lines
processed_for = _mod.processed_for
text = _mod.text
usable_uid = _mod.usable_uid


# ── assess_permanentflags ─────────────────────────────────────────────────────


def test_permanentflags_wildcard_means_keywords_allowed():
    supported, why = assess_permanentflags("\\Answered \\Flagged \\Seen \\*")
    assert supported is True
    assert "\\*" in why


def test_permanentflags_without_wildcard_means_keywords_dropped():
    supported, why = assess_permanentflags("\\Answered \\Flagged \\Seen")
    assert supported is False
    assert "dropped" in why


def test_permanentflags_absent_is_unknown_not_no():
    """A silent server is not a refusal; the round trip has to settle it."""
    supported, _ = assess_permanentflags("")
    assert supported is None


def test_permanentflags_wildcard_not_confused_with_literal_star():
    supported, _ = assess_permanentflags("\\Seen $label1 star")
    assert supported is False


# ── default_port ──────────────────────────────────────────────────────────────


def test_configured_port_is_used_without_warning():
    port, warning = default_port({"imap_port": 1143, "imap_security": _mod.SECURITY_SSL})
    assert (port, warning) == (1143, None)


@pytest.mark.parametrize(
    ("security", "expected"),
    [
        (_mod.SECURITY_SSL, 993),
        (_mod.SECURITY_STARTTLS, 143),
        (_mod.SECURITY_NONE, 143),
    ],
)
def test_missing_port_falls_back_by_security(security, expected):
    port, warning = default_port({"imap_port": None, "imap_security": security})
    assert port == expected
    assert "unset" in warning


# ── text / parse_flag_lines ───────────────────────────────────────────────────


def test_text_flattens_imaplib_response_shapes():
    assert text(None) == ""
    assert text(b"OK") == "OK"
    assert text([b"a", b"b"]) == "a b"


def test_parse_flag_lines_keys_by_uid():
    data = [
        b"1 (UID 41 FLAGS (\\Seen processed))",
        b"2 (UID 42 FLAGS (\\Seen))",
    ]
    flags = parse_flag_lines(data)
    assert set(flags) == {"41", "42"}
    assert "processed" in flags["41"]
    assert "processed" not in flags["42"]


def test_parse_flag_lines_ignores_lines_without_uid():
    assert parse_flag_lines([b"OK", b"1 (FLAGS (\\Seen))"]) == {}


# ── usable_uid ────────────────────────────────────────────────────────────────


def test_uid_is_stale_after_uidvalidity_change():
    assert not usable_uid({"uid_validity": "12"}, "13")
    assert usable_uid({"uid_validity": "13"}, "13")


def test_uid_without_recorded_validity_is_taken_at_face_value():
    """Rows predate the uid_validity column, so a null must not drop them."""
    assert usable_uid({"uid_validity": None}, "13")


# ── database layer ────────────────────────────────────────────────────────────


def make_db(path):
    """A stand-in with the column names the script actually reads."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE paperless_mail_mailaccount (
            id INTEGER PRIMARY KEY, name TEXT, imap_server TEXT, imap_port INTEGER,
            imap_security INTEGER, username TEXT, password TEXT, is_token INTEGER
        );
        CREATE TABLE paperless_mail_mailrule (
            id INTEGER PRIMARY KEY, name TEXT, folder TEXT, action INTEGER,
            action_parameter TEXT, enabled INTEGER, account_id INTEGER
        );
        CREATE TABLE paperless_mail_processedmail (
            id INTEGER PRIMARY KEY, rule_id INTEGER, folder TEXT, uid TEXT,
            uid_validity TEXT, subject TEXT, processed TEXT, status TEXT
        );
        INSERT INTO paperless_mail_mailaccount
            VALUES (1, 'main', 'imap.example.net', 993, 2, 'me', 'hunter2', 0);
        INSERT INTO paperless_mail_mailrule
            VALUES (1, 'tagger', 'INBOX', 5, 'processed', 1, 1),
                   (2, 'mover', 'INBOX', 2, 'Archive', 1, 1);
        INSERT INTO paperless_mail_processedmail
            VALUES (1, 1, 'INBOX', '41', '13', 'invoice', '2026-01-02', 'SUCCESS'),
                   (2, 1, 'INBOX', '42', '13', 'receipt', '2026-01-03', 'SUCCESS'),
                   (3, 1, 'INBOX', '43', '13', 'broken',  '2026-01-04', 'FAILED'),
                   (4, 2, 'INBOX', '44', '13', 'other',   '2026-01-05', 'SUCCESS');
        """,
    )
    db.commit()
    return db


def test_load_tag_rules_selects_only_the_tag_action(tmp_path):
    db = make_db(tmp_path / "db.sqlite3")
    rules = load_tag_rules(db)
    assert [r["name"] for r in rules] == ["tagger"]
    assert rules[0]["action_parameter"] == "processed"


def test_load_tag_rules_inlines_the_account(tmp_path):
    db = make_db(tmp_path / "db.sqlite3")
    account = load_tag_rules(db)[0]["account"]
    assert account["imap_server"] == "imap.example.net"
    assert account["username"] == "me"


def test_processed_for_takes_successes_newest_first(tmp_path):
    """Failures never had the action applied, so they prove nothing."""
    db = make_db(tmp_path / "db.sqlite3")
    mails = processed_for(db, 1)
    assert [m["uid"] for m in mails] == ["42", "41"]


def test_missing_table_names_what_was_found(tmp_path):
    db = sqlite3.connect(tmp_path / "db.sqlite3")
    db.execute("CREATE TABLE paperless_mail_other (id INTEGER)")
    with pytest.raises(SystemExit) as excinfo:
        load_tag_rules(db)
    assert "paperless_mail_other" in str(excinfo.value)
