"""Unit tests for the message-board server core (no HTTP layer)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "message-board"
_spec = importlib.util.spec_from_file_location("board", _APP / "board.py")
board = importlib.util.module_from_spec(_spec)
sys.modules["board"] = board
_spec.loader.exec_module(board)


@pytest.fixture
def db(tmp_path):
    board.init_db(str(tmp_path / "board.db"))
    return board


def test_post_returns_monotonic_ids(db):
    a = db.post("reversing", "ghidra", "g", "one")
    b = db.post("reversing", "ghidra", "g", "two")
    assert (a, b) == (2, 3)  # id 1 is the _system announcement for the new area


def test_new_area_is_announced_on_system(db):
    db.post("reversing", "ghidra", "g", "hi")
    ann = db.query_since(0, board.SYSTEM_AREA, "areas")
    assert len(ann) == 1
    assert "reversing" in ann[0]["body"]


def test_area_isolates_watchers(db):
    db.post("reversing", "ghidra", "g", "rev work")
    db.post("openwpm", "crawler", "c", "crawl work")
    rev = db.query_since(0, "reversing", "")
    assert [m["body"] for m in rev] == ["rev work"]
    assert all(m["area"] == "reversing" for m in rev)


def test_topic_filter_within_area(db):
    db.post("reversing", "ghidra", "g", "a")
    db.post("reversing", "fuzz", "f", "b")
    fuzz = db.query_since(0, "reversing", "fuzz")
    assert [m["body"] for m in fuzz] == ["b"]


def test_since_cursor_advances(db):
    db.post("reversing", "ghidra", "g", "first")
    head = db.head_id()
    db.post("reversing", "ghidra", "g", "second")
    later = db.query_since(head, "reversing", "")
    assert [m["body"] for m in later] == ["second"]


def test_create_area_with_description(db):
    db.create_area("openwpm", "Firefox / OpenWPM crawl work")
    areas = {a["name"]: a["description"] for a in db.list_areas()}
    assert areas["openwpm"] == "Firefox / OpenWPM crawl work"


def test_poll_returns_immediately_when_data_present(db):
    db.post("reversing", "ghidra", "g", "ready")
    # timeout is irrelevant when matching messages already exist
    rows = db.poll(0, "reversing", "", timeout=30)
    assert [m["body"] for m in rows] == ["ready"]


@pytest.mark.parametrize("exc", [BrokenPipeError, ConnectionResetError])
def test_client_disconnect_is_not_logged_as_an_error(monkeypatch, exc):
    """A client that walks away mid-response must not surface as a fault."""
    from http.server import BaseHTTPRequestHandler

    def boom(self):
        raise exc(32, "gone")

    monkeypatch.setattr(BaseHTTPRequestHandler, "handle_one_request", boom)
    handler = board.Handler.__new__(board.Handler)  # no socket setup needed
    handler.close_connection = False
    handler.handle_one_request()  # must not raise
    assert handler.close_connection is True


def test_since_arg_accepts_ids_and_head():
    assert board._since_arg("0") == 0
    assert board._since_arg("42") == 42
    assert board._since_arg("head") == "head"
    with pytest.raises(Exception):
        board._since_arg("banana")


def test_resolve_since_passes_ids_through(monkeypatch):
    monkeypatch.setattr(board, "_client_get", lambda p: pytest.fail("no call needed"))
    assert board._resolve_since(7) == 7


def test_resolve_since_head_reads_the_current_head(db, monkeypatch):
    db.post("reversing", "ghidra", "g", "already here")
    monkeypatch.setattr(board, "_client_get",
                        lambda p: json.dumps({"head": board.head_id()}))
    # 'head' means "skip what is already on the board"
    assert board._resolve_since("head") == db.head_id()
    assert db.query_since(board._resolve_since("head"), "reversing", "") == []


def test_resolve_since_head_retries_while_board_is_down(monkeypatch, capsys):
    attempts = []

    def flaky(_path):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("connection refused")
        return json.dumps({"head": 9})

    monkeypatch.setattr(board, "_client_get", flaky)
    monkeypatch.setattr(board.time, "sleep", lambda _s: None)
    assert board._resolve_since("head") == 9
    # silence must never look like "no news"
    assert capsys.readouterr().out.count("board-unreachable:") == 2
