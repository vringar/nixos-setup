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


def _serve_polls_from_core(monkeypatch, page=500):
    """Route the client's GET /poll straight into the server core, capping
    each response at `page` rows the way the server caps at 500."""
    from urllib.parse import parse_qs, urlparse

    def fake_get(path):
        q = {k: v[0] for k, v in parse_qs(urlparse(path).query).items()}
        rows = board.poll(int(q.get("since", 0)), q.get("area", ""),
                          q.get("topic", ""), float(q.get("timeout", 0)))
        return json.dumps(rows[:page])

    monkeypatch.setattr(board, "_client_get", fake_get)


def test_read_all_pages_past_the_poll_cap(db, monkeypatch):
    _serve_polls_from_core(monkeypatch, page=3)
    for i in range(10):  # several full pages plus a partial one
        db.post("bulk", "t", "b", f"m{i}")
    msgs = board.read_all(0, "bulk", "")
    assert [m["body"] for m in msgs] == [f"m{i}" for i in range(10)]


def test_read_all_respects_scope_and_since(db, monkeypatch):
    _serve_polls_from_core(monkeypatch)
    db.post("reversing", "ghidra", "g", "old")
    mark = db.head_id()
    db.post("reversing", "ghidra", "g", "new")
    db.post("reversing", "fuzz", "f", "other topic")
    db.post("openwpm", "crawler", "c", "other area")
    assert [m["body"] for m in board.read_all(mark, "reversing", "ghidra")] == ["new"]
    assert board.read_all(db.head_id(), "", "") == []


def test_unreachable_board_is_one_line_not_a_traceback(monkeypatch, capsys):
    import urllib.error

    def down(_path):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(board, "_client_get", down)
    with pytest.raises(SystemExit) as exit_info:
        board.main(["read", "--area", "x"])
    assert exit_info.value.code.startswith("board: cannot reach ")
    assert "Traceback" not in capsys.readouterr().err


def test_read_cursor_covers_posts_racing_the_read(db, monkeypatch, capsys):
    """Nothing posted after `read` starts may fall between read and watch."""
    _serve_polls_from_core(monkeypatch)
    real_get = board._client_get
    db.post("rev", "t", "a", "before")

    def get(path):
        if path == "/healthz":
            out = json.dumps({"head": db.head_id()})
            db.post("rev", "t", "a", "raced the read")  # lands after head is taken
            return out
        return real_get(path)

    monkeypatch.setattr(board, "_client_get", get)
    board.main(["read", "--area", "rev"])
    out, err = capsys.readouterr()
    cursor = int(err.split("read through id ")[1].split(";")[0])
    db.post("rev", "t", "a", "after")
    shown = [b for b in ("before", "raced the read", "after") if b in out]
    followed = [m["body"] for m in db.query_since(cursor, "rev", "")]
    # every message is delivered exactly once, across read + watch
    assert sorted(shown + followed) == sorted(["before", "raced the read", "after"])
