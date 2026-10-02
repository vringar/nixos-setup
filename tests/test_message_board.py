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
        if urlparse(path).path == "/pins":
            return json.dumps(board.pins_for(q.get("area", ""), q.get("topic", "")))
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


def test_pin_versions_and_announces_with_full_body(db):
    assert db.set_pin("reversing", "", "stefan", "manifesto v1") == 1
    assert db.set_pin("reversing", "", "stefan", "manifesto v2\nline two") == 2
    pin = db.get_pin("reversing")
    assert (pin["body"], pin["version"]) == ("manifesto v2\nline two", 2)
    # every save lands on the log: watchers wake, and history is kept
    log = db.query_since(0, "reversing", board.PIN_TOPIC)
    assert [m["body"].splitlines()[0] for m in log] == [
        "pin updated: reversing v1", "pin updated: reversing v2"]
    assert log[-1]["body"].endswith("manifesto v2\nline two")


def test_topic_pin_is_announced_in_its_topic(db):
    db.set_pin("reversing", "ghidra", "s", "ghidra rules")
    [m] = db.query_since(0, "reversing", "ghidra")
    assert m["body"].startswith("pin updated: reversing/ghidra v1")


def test_stale_pin_edit_is_rejected(db):
    db.set_pin("reversing", "", "a", "v1")
    db.set_pin("reversing", "", "b", "v2", base_version=1)
    with pytest.raises(board.StalePin):
        db.set_pin("reversing", "", "a", "lost update", base_version=1)
    assert db.get_pin("reversing")["body"] == "v2"


def test_empty_body_removes_pin(db):
    db.set_pin("reversing", "", "s", "v1")
    db.set_pin("reversing", "", "s", "   ")
    assert db.get_pin("reversing") is None
    assert db.query_since(0, "reversing", board.PIN_TOPIC)[-1]["body"] == \
        "pin removed: reversing"


def test_pins_for_scope(db):
    db.set_pin("reversing", "", "s", "area")
    db.set_pin("reversing", "ghidra", "s", "g")
    db.set_pin("reversing", "fuzz", "s", "f")
    db.set_pin("openwpm", "", "s", "other area")
    bodies = lambda ps: [p["body"] for p in ps]
    assert bodies(db.pins_for("reversing", "ghidra")) == ["area", "g"]
    assert bodies(db.pins_for("reversing")) == ["area", "f", "g"]
    assert db.pins_for("nowhere") == []


def test_list_topics_includes_pinned_only_topics(db):
    db.post("reversing", "fuzz", "f", "x")
    db.set_pin("reversing", "", "s", "area pin")
    db.create_area("reversing", "")
    db.set_pin("reversing", "ghidra", "s", "topic pin")
    assert db.list_topics("reversing") == [board.PIN_TOPIC, "fuzz", "ghidra"]


# --- HTTP layer: a real server on an ephemeral port --------------------------

@pytest.fixture
def server(db, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    board._web_init()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(board, "BASE_URL", url)  # point the CLI at it
    yield url
    srv.shutdown()
    srv.server_close()


def _get(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def _post(url, data, form=False):
    import urllib.request
    from urllib.parse import urlencode as enc
    body = enc(data).encode() if form else json.dumps(data).encode()
    ctype = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"content-type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_web_topic_nav_and_clickable_tags(server, db):
    db.post("reversing", "ghidra", "g", "x")
    db.post("reversing", "fuzz", "f", "y")
    _, page = _get(f"{server}/?area=reversing")
    assert 'href="/?area=reversing&topic=fuzz"' in page
    assert 'href="/?area=reversing&topic=ghidra"' in page
    _, page = _get(f"{server}/?area=reversing&topic=ghidra")
    assert ">x<" in page and ">y<" not in page  # topic view filters the log


def test_web_shows_area_pin_and_topic_pin_in_topic_view(server, db):
    db.set_pin("reversing", "", "s", "AREA MANIFESTO")
    db.set_pin("reversing", "ghidra", "s", "GHIDRA RULES")
    pins_of = lambda page: page.split('id="messages"')[0]  # above the log
    _, area_page = _get(f"{server}/?area=reversing")
    assert "AREA MANIFESTO" in pins_of(area_page)
    assert "GHIDRA RULES" not in pins_of(area_page)  # topic pins stay in their topic
    _, topic_page = _get(f"{server}/?area=reversing&topic=ghidra")
    top = pins_of(topic_page)
    assert top.index("AREA MANIFESTO") < top.index("GHIDRA RULES")


def test_web_pin_edit_roundtrip_and_stale_save(server, db):
    status, form = _get(f"{server}/ui/pin/edit?area=reversing&topic=")
    assert status == 200 and 'name="base_version" value="0"' in form
    status, box = _post(f"{server}/ui/pin", {"area": "reversing", "topic": "",
                        "from": "stefan", "body": "v1 text", "base_version": "0"}, form=True)
    assert status == 200 and "v1 text" in box and "v1 ·" in box
    db.set_pin("reversing", "", "agent", "agent edit")  # someone else saves v2
    status, again = _post(f"{server}/ui/pin", {"area": "reversing", "topic": "",
                          "from": "stefan", "body": "my edit", "base_version": "1"}, form=True)
    assert "Someone saved v2" in again and "my edit" in again  # text kept
    assert db.get_pin("reversing")["body"] == "agent edit"     # nothing lost
    assert 'name="base_version" value="2"' in again            # resave overwrites


def test_web_pin_body_is_escaped(server, db):
    db.set_pin("reversing", "", "s", "<script>alert(1)</script>")
    _, page = _get(f"{server}/?area=reversing")
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_http_pin_api(server, db):
    status, _ = _post(f"{server}/pin", {"area": "reversing", "body": "x", "from": "s"})
    assert status == 200
    status, body = _post(f"{server}/pin", {"area": "reversing", "body": "y",
                                           "base_version": 0})
    assert status == 409 and json.loads(body)["version"] == 1
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(f"{server}/pin?area=nowhere")
    assert e.value.code == 404


def test_cli_pin_set_show_and_read_prints_pins_first(server, db, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("# Manifesto\nbe kind to the firmware\n"))
    board.main(["pin", "reversing", "--set", "--from", "stefan"])
    assert "saved as v1" in capsys.readouterr().err
    board.main(["pin", "reversing"])
    out = capsys.readouterr().out
    assert out.startswith("=== pinned to reversing · v1 · stefan") and "be kind" in out
    db.post("reversing", "ghidra", "g", "log entry")
    board.main(["read", "--area", "reversing"])
    out = capsys.readouterr().out
    assert out.index("be kind to the firmware") < out.index("log entry")
    board.main(["read", "--area", "reversing", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert [p["body"] for p in data["pins"]] == ["# Manifesto\nbe kind to the firmware\n"]


def test_cli_pin_missing_is_one_line(server):
    with pytest.raises(SystemExit) as e:
        board.main(["pin", "nowhere"])
    assert e.value.code == "board: no pin on nowhere"


def test_topic_watchers_hear_area_pin_updates_but_not_other_topics(db):
    db.post("reversing", "fuzz", "f", "fuzz chatter")
    db.set_pin("reversing", "", "s", "manifesto")
    db.set_pin("reversing", "fuzz", "s", "fuzz-only pin")
    ghidra = [m["body"].splitlines()[0] for m in db.poll(0, "reversing", "ghidra", 0)]
    assert ghidra == ["pin updated: reversing v1"]
