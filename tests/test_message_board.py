"""Unit tests for the message-board storage core, the HTTP server and the `board` client."""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "message-board"
sys.path.insert(0, str(_APP))  # board_core / board_server import by name
import board_core as core  # noqa: E402
import board_server  # noqa: E402
import board_sign  # noqa: E402

_spec = importlib.util.spec_from_file_location("board", _APP / "board.py")
board = importlib.util.module_from_spec(_spec)  # the agent-facing client
sys.modules["board"] = board
_spec.loader.exec_module(board)


@pytest.fixture
def db(tmp_path):
    core.init_db(str(tmp_path / "board.db"))
    return core


@pytest.fixture(autouse=True)
def no_signing_key(tmp_path, monkeypatch):
    """`board` signs with the nearest scratch/board_ed25519 above the cwd, so
    a key in the checkout running the tests must not leak into them."""
    monkeypatch.delenv("BOARD_KEY", raising=False)
    monkeypatch.chdir(tmp_path)


def test_post_returns_monotonic_ids(db):
    a = db.post("reversing", "ghidra", "g", "one")
    b = db.post("reversing", "ghidra", "g", "two")
    assert (a, b) == (2, 3)  # id 1 is the _system announcement for the new area


def test_new_area_is_announced_on_system(db):
    db.post("reversing", "ghidra", "g", "hi")
    ann = db.query_since(0, core.SYSTEM_AREA, "areas")
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
    handler = board_server.Handler.__new__(board_server.Handler)  # no socket setup needed
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
                        lambda p: json.dumps({"head": core.head_id()}))
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
        archived = q.get("archived") == "1"
        if urlparse(path).path == "/areas":
            return json.dumps(core.list_areas(include_archived=archived))
        if urlparse(path).path == "/pins":
            return json.dumps(core.pins_for(q.get("area", ""), q.get("topic", ""),
                                             include_archived=archived))
        rows = core.poll(int(q.get("since", 0)), q.get("area", ""),
                          q.get("topic", ""), float(q.get("timeout", 0)),
                          include_archived=archived)
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


def test_pin_versions_and_announces_as_a_doorbell_not_a_copy(db):
    assert db.set_pin("reversing", "", "stefan", "manifesto v1") == 1
    assert db.set_pin("reversing", "", "stefan", "manifesto v2\nline two") == 2
    pin = db.get_pin("reversing")
    assert (pin["body"], pin["version"]) == ("manifesto v2\nline two", 2)
    # every save lands on the log: watchers wake, and history is kept -- but
    # the log carries a pointer, not the pin's own text (which may be
    # unrelated or sensitive-sounding out of the context a human reading the
    # pin box gets; see `pins_for`'s no-area case for the same principle)
    log = db.query_since(0, "reversing", core.PIN_TOPIC)
    assert [m["body"] for m in log] == [
        "pin updated: reversing v1 — see `board pin reversing`",
        "pin updated: reversing v2 — see `board pin reversing`"]
    assert "manifesto" not in log[-1]["body"]


def test_topic_pin_is_announced_in_its_topic(db):
    db.set_pin("reversing", "ghidra", "s", "ghidra rules")
    [m] = db.query_since(0, "reversing", "ghidra")
    assert m["body"].startswith("pin updated: reversing/ghidra v1")


def test_stale_pin_edit_is_rejected(db):
    db.set_pin("reversing", "", "a", "v1")
    db.set_pin("reversing", "", "b", "v2", base_version=1)
    with pytest.raises(core.StalePin):
        db.set_pin("reversing", "", "a", "lost update", base_version=1)
    assert db.get_pin("reversing")["body"] == "v2"


def test_empty_body_removes_pin(db):
    db.set_pin("reversing", "", "s", "v1")
    db.set_pin("reversing", "", "s", "   ")
    assert db.get_pin("reversing") is None
    assert db.query_since(0, "reversing", core.PIN_TOPIC)[-1]["body"] == \
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
    # no area: nothing, not "every area" -- area is the isolation boundary
    assert db.pins_for() == []


def test_list_topics_includes_pinned_only_topics(db):
    db.post("reversing", "fuzz", "f", "x")
    db.set_pin("reversing", "", "s", "area pin")
    db.create_area("reversing", "")
    db.set_pin("reversing", "ghidra", "s", "topic pin")
    assert db.list_topics("reversing") == [core.PIN_TOPIC, "fuzz", "ghidra"]


# --- HTTP layer: a real server on an ephemeral port --------------------------

@pytest.fixture
def server(db, monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer

    board_server._web_init()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), board_server.Handler)
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
    ghidra = [m["body"] for m in db.poll(0, "reversing", "ghidra", 0)]
    assert ghidra == ["pin updated: reversing v1 — see `board pin reversing`"]


# --- archiving ----------------------------------------------------------------

def test_archive_hides_area_from_listings_and_unscoped_reads(db):
    db.post("reversing", "ghidra", "g", "old run")
    db.set_pin("reversing", "", "s", "old manifesto")
    db.post("openwpm", "crawler", "c", "live")
    assert db.set_archived("reversing", True) is True
    assert [a["name"] for a in db.list_areas()] == [core.SYSTEM_AREA, "openwpm"]
    assert "reversing" in [a["name"] for a in db.list_areas(include_archived=True)]
    live = [m["body"] for m in db.poll(0, "", "", 0, include_archived=False)
            if m["area"] != core.SYSTEM_AREA]
    assert live == ["live"]
    assert db.pins_for("reversing", include_archived=False) == []
    assert db.pins_for("reversing", include_archived=True) != []
    # nothing is deleted: the archive is still readable when asked for
    assert "old run" in [m["body"] for m in db.poll(0, "reversing", "ghidra", 0)]


def test_archive_is_announced_and_idempotent(db):
    db.post("reversing", "ghidra", "g", "x")
    assert db.set_archived("reversing", True) is True
    assert db.set_archived("reversing", True) is False  # no second announcement
    ann = [m["body"] for m in db.query_since(0, core.SYSTEM_AREA, "areas")]
    assert ann == ["new area: **reversing**", "area archived: **reversing**"]


def test_activity_unarchives(db):
    db.post("reversing", "ghidra", "g", "x")
    db.set_archived("reversing", True)
    db.post("reversing", "ghidra", "late-agent", "still here")
    assert "reversing" in [a["name"] for a in db.list_areas()]
    db.set_archived("reversing", True)
    db.set_pin("reversing", "", "s", "pin edit counts too")
    assert "reversing" in [a["name"] for a in db.list_areas()]
    ann = [m["body"] for m in db.query_since(0, core.SYSTEM_AREA, "areas")]
    assert ann.count("area unarchived: **reversing**") == 2


def test_archive_rejects_system_and_unknown_areas(db):
    with pytest.raises(ValueError):
        db.set_archived(core.SYSTEM_AREA, True)
    with pytest.raises(KeyError):
        db.set_archived("nowhere", True)


def test_old_board_is_migrated(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE areas(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
                           created_ts TEXT NOT NULL);
        INSERT INTO areas VALUES('reversing', '', '2026-09-01T00:00:00Z');""")
    old.commit()
    old.close()
    core.init_db(path)
    assert core.set_archived("reversing", True) is True
    core.init_db(path)  # second open must not try to add the column again
    assert core.list_areas() == []


def test_cli_archive_unarchive_and_areas_flag(server, db, capsys):
    db.post("reversing", "ghidra", "g", "x")
    board.main(["archive", "reversing"])
    assert "reversing archived" in capsys.readouterr().err
    board.main(["archive", "reversing"])
    assert "already archived" in capsys.readouterr().err
    board.main(["areas"])
    assert "reversing" not in capsys.readouterr().out
    board.main(["areas", "--archived"])
    assert "reversing" in capsys.readouterr().out.split("[archived")[0]
    board.main(["unarchive", "reversing"])
    board.main(["areas"])
    assert "reversing" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        board.main(["archive", "nowhere"])
    assert e.value.code == "board: no such area: nowhere"
    with pytest.raises(SystemExit) as e:
        board.main(["archive", core.SYSTEM_AREA])
    assert "cannot be archived" in e.value.code


def test_cli_read_requires_an_area(server, db):
    # No silent "every area merged": that was exactly how an unrelated
    # agent's first move pulled in another workstream's pinned instructions.
    with pytest.raises(SystemExit) as e:
        board.main(["read"])
    assert "no --area given" in e.value.code
    with pytest.raises(SystemExit) as e:
        board.main(["read", "--archived"])  # --archived alone still needs a scope
    assert "no --area given" in e.value.code


def test_cli_read_hides_archived_unless_asked(server, db, capsys):
    db.post("reversing", "ghidra", "g", "OLD RUN")
    db.set_pin("reversing", "", "s", "OLD MANIFESTO")
    db.set_archived("reversing", True)
    with pytest.raises(SystemExit) as e:  # by name: say why, don't print nothing
        board.main(["read", "--area", "reversing"])
    assert e.value.code == "board: area reversing is archived; add --archived to read it"
    board.main(["read", "--area", "reversing", "--archived"])
    out = capsys.readouterr().out
    assert "OLD MANIFESTO" in out and "OLD RUN" in out


def test_web_archive_button_roundtrip(server, db):
    import urllib.request
    db.post("reversing", "ghidra", "g", "x")
    _, page = _get(f"{server}/?area=reversing")
    assert "archive area" in page
    _, page = _get(f"{server}/?area={core.SYSTEM_AREA}")
    assert "archive area" not in page  # _system cannot be archived

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)

    def form_post(data):
        req = urllib.request.Request(f"{server}/ui/archive", method="POST",
                                     data=urlencode_(data).encode())
        try:
            opener.open(req, timeout=5)
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location")

    from urllib.parse import urlencode as urlencode_
    assert form_post({"area": "reversing", "archived": "1"}) == (303, "/")
    _, home = _get(f"{server}/")
    assert 'href="/?area=reversing"' not in home.split("archived (")[0]
    assert "archived (1)" in home
    _, listing = _get(f"{server}/?show=archived")
    assert 'href="/?area=reversing"' in listing
    _, page = _get(f"{server}/?area=reversing")
    assert "Archived since" in page and ">unarchive<" in page
    assert form_post({"area": "reversing", "archived": "0"}) == (303, "/?area=reversing")
    _, home = _get(f"{server}/")
    assert "archived (" not in home


def test_agent_cli_cannot_start_a_server(capsys):
    """Starting the server is board-server's job; `board` is a pure client."""
    with pytest.raises(SystemExit) as exc:
        board.main(["serve"])
    assert exc.value.code == 2
    assert "serve" in capsys.readouterr().err
    assert not hasattr(board, "ThreadingHTTPServer")
    assert not hasattr(board, "init_db")


# --- idempotent posts ---------------------------------------------------------

def test_post_key_makes_resending_harmless(db):
    first = db.post("x", "t", "agent", "once", key="k1")
    assert db.post("x", "t", "agent", "once", key="k1") == first
    assert [m["body"] for m in db.query_since(0, "x", "")] == ["once"]
    with pytest.raises(core.Conflict):
        db.post("x", "t", "agent", "something else", key="k1")
    assert db.post("x", "t", "agent", "once", key="k2") != first  # a new key is a new post


def test_http_post_key_dedupes_and_refuses_reuse(server, db):
    msg = {"area": "x", "topic": "t", "from": "a", "body": "once", "key": "k1"}
    status, out = _post(f"{server}/post", msg)
    assert status == 200
    assert _post(f"{server}/post", msg) == (200, out)
    status, out = _post(f"{server}/post", {**msg, "body": "other"})
    assert status == 409 and "already used" in json.loads(out)["error"]
    assert _post(f"{server}/post", {**msg, "key": None, "body": "keyless"})[0] == 200
    assert [m["body"] for m in db.query_since(0, "x", "")] == ["once", "keyless"]


def test_cli_post_retry_reuses_the_key_and_does_not_double_post(server, db, monkeypatch,
                                                                capsys):
    real = board._client_post
    calls = []

    def lost_response(path, data, timeout=10):
        calls.append((data["key"], timeout))
        out = real(path, data, timeout)
        if len(calls) == 1:
            raise TimeoutError("timed out")  # landed, but the answer never arrived
        return out

    monkeypatch.setattr(board, "_client_post", lost_response)
    monkeypatch.setattr(board.time, "sleep", lambda _s: None)
    board.main(["post", "rev", "t", "agent", "exactly once"])
    out, err = capsys.readouterr()
    assert "retrying" in err
    assert len(calls) == 2 and calls[0] == calls[1] and calls[0][1] == 30
    [m] = db.query_since(0, "rev", "t")
    assert json.loads(out) == {"id": m["id"]}


def test_cli_post_gives_up_after_three_retries(monkeypatch):
    import urllib.error
    attempts = []

    def down(path, data, timeout=10):
        attempts.append(data["key"])
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(board, "_client_post", down)
    monkeypatch.setattr(board.time, "sleep", lambda _s: None)
    with pytest.raises(SystemExit) as e:
        board.main(["post", "rev", "t", "agent", "x"])
    assert e.value.code.startswith("board: cannot reach ")
    assert len(attempts) == 4 and len(set(attempts)) == 1


# --- signing: canonical bytes and SSHSIG interop with ssh-keygen --------------

def _keyfile(directory, name="agent"):
    """A key on disk as `board keygen` writes it: (path, private key, fingerprint)."""
    private, public, fingerprint = board_sign.generate(f"board:{name}")
    path = directory / f"{name}_ed25519"
    path.write_bytes(private)
    path.chmod(0o600)  # ssh-keygen refuses to read a private key others can
    path.with_name(path.name + ".pub").write_text(public)
    return path, board_sign.load_private_key(path), fingerprint


def _ssh_keygen(*args, data=None, check=True):
    return subprocess.run(["ssh-keygen", *map(str, args)], input=data,
                          capture_output=True, check=check)


def _openssh_key(directory, name, *extra):
    """A key made by ssh-keygen itself; returns its path."""
    path = directory / name
    _ssh_keygen("-q", "-t", "ed25519", "-N", "", "-C", name, "-f", path, *extra)
    return path


def _ssh_verify(directory, public_line, namespace, data, signature):
    """`ssh-keygen -Y verify` of a board signature; True if it accepts it."""
    allowed = directory / "allowed_signers"
    allowed.write_text(f'signer namespaces="{namespace}" {public_line}\n')
    body = "".join(signature.split())
    sig = directory / "data.sig"
    sig.write_text("-----BEGIN SSH SIGNATURE-----\n"
                   + "\n".join(body[i:i + 70] for i in range(0, len(body), 70))
                   + "\n-----END SSH SIGNATURE-----\n")
    out = _ssh_keygen("-Y", "verify", "-f", allowed, "-I", "signer", "-n", namespace,
                      "-s", sig, data=data, check=False)
    return out.returncode == 0


def _fingerprint_of(pub_path):
    return _ssh_keygen("-l", "-f", pub_path).stdout.split()[1].decode()


MESSAGE_FIELDS = board_sign.MESSAGE[1]


def test_canonical_encoding_separates_what_key_value_lines_conflate():
    # body "a\nfrom=b" from "c", versus body "a" from "b\nfrom=c"
    one = {"area": "x", "topic": "t", "from": "c", "body": "a\nfrom=b", "key": "k", "nonce": "7"}
    two = {"area": "x", "topic": "t", "from": "b\nfrom=c", "body": "a", "key": "k", "nonce": "7"}
    sorted_kv = lambda v: "".join(f"{k}={v[k]}\n" for k in sorted(v))  # noqa: E731
    assert sorted_kv(one) == sorted_kv(two)  # the ambiguity being avoided
    assert board_sign.canonical(MESSAGE_FIELDS, one) != board_sign.canonical(MESSAGE_FIELDS, two)
    assert board_sign.canonical(MESSAGE_FIELDS, one) == (
        b"area:1:x\ntopic:1:t\nfrom:1:c\nbody:8:a\nfrom=b\nkey:1:k\nnonce:1:7\n")
    # the length counts UTF-8 bytes, not characters
    assert board_sign.canonical(("body",), {"body": "ü"}) == "body:2:ü\n".encode()


def test_python_signatures_verify_with_ssh_keygen(tmp_path):
    path = _openssh_key(tmp_path, "id")  # a key ssh-keygen made, loaded by Python
    key = board_sign.load_private_key(path)
    data = board_sign.canonical(MESSAGE_FIELDS, {
        "area": "a", "topic": "t", "from": "me", "body": "hi\nthere 🙂", "key": "k1",
        "nonce": "42"})
    sig = board_sign.sign_data(key, "board-message", data)
    public_line = (tmp_path / "id.pub").read_text()
    assert _ssh_verify(tmp_path, public_line, "board-message", data, sig)
    assert not _ssh_verify(tmp_path, public_line, "board-message", data + b"x", sig)
    assert not _ssh_verify(tmp_path, public_line, "board-pin", data, sig)


def test_ssh_keygen_signatures_verify_in_python(tmp_path):
    path = _openssh_key(tmp_path, "id")
    data = tmp_path / "data"
    data.write_bytes(b"area:1:a\n")
    _ssh_keygen("-Y", "sign", "-f", path, "-n", "board-message", data)
    sig = "".join(line for line in (tmp_path / "data.sig").read_text().splitlines()
                  if not line.startswith("-----"))
    public_line, fingerprint = board_sign.verify_data("board-message", b"area:1:a\n", sig)
    assert fingerprint == _fingerprint_of(tmp_path / "id.pub")
    assert public_line == " ".join((tmp_path / "id.pub").read_text().split()[:2])
    with pytest.raises(board_sign.SignatureError, match="does not match"):
        board_sign.verify_data("board-message", b"area:1:b\n", sig)
    with pytest.raises(board_sign.SignatureError, match="namespace"):
        board_sign.verify_data("board-pin", b"area:1:a\n", sig)
    (tmp_path / "data.sig").unlink()  # ssh-keygen will not overwrite it
    _ssh_keygen("-Y", "sign", "-f", path, "-n", "board-message", "-O", "hashalg=sha256", data)
    sha256 = "".join(line for line in (tmp_path / "data.sig").read_text().splitlines()
                     if not line.startswith("-----"))
    with pytest.raises(board_sign.SignatureError, match="only sha512"):
        board_sign.verify_data("board-message", b"area:1:a\n", sha256)


def test_generated_keys_are_ordinary_openssh_keys(tmp_path):
    path, _, fingerprint = _keyfile(tmp_path)
    pub = path.with_name(path.name + ".pub")
    derived = _ssh_keygen("-y", "-f", path).stdout.decode().split()
    assert derived[:2] == pub.read_text().split()[:2]
    assert pub.read_text().split()[2] == "board:agent"
    assert _fingerprint_of(pub) == fingerprint


def test_other_key_types_and_encrypted_keys_are_refused(tmp_path):
    ecdsa = tmp_path / "ec"
    _ssh_keygen("-q", "-t", "ecdsa", "-N", "", "-f", ecdsa)
    with pytest.raises(board_sign.SignatureError, match="only ed25519"):
        board_sign.load_private_key(ecdsa)
    with pytest.raises(board_sign.SignatureError, match="only ed25519"):
        board_sign.load_public_key((tmp_path / "ec.pub").read_text())
    data = tmp_path / "data"
    data.write_bytes(b"x")
    _ssh_keygen("-Y", "sign", "-f", ecdsa, "-n", "board-message", data)
    sig = "".join(line for line in (tmp_path / "data.sig").read_text().splitlines()
                  if not line.startswith("-----"))
    with pytest.raises(board_sign.SignatureError, match="only ed25519"):
        board_sign.verify_data("board-message", b"x", sig)
    locked = _openssh_key(tmp_path, "locked", "-N", "secret")
    with pytest.raises(board_sign.SignatureError, match="passphrase-protected"):
        board_sign.load_private_key(locked)


# --- identities: first signed use claims a name -------------------------------

def _signer(fingerprint):
    """A key as the server's signature check hands it to the core. Call it for
    a write made now: its nonce is the current head."""
    return lambda: core.Signer(f"ssh-ed25519 KEY-{fingerprint}", fingerprint,
                               f"sig-{fingerprint}", str(core.head_id()))


A, B, OWNER = _signer("SHA256:a"), _signer("SHA256:b"), _signer("SHA256:owner")


def _make_owner(db, name="Stefan"):
    db.set_owner(name, OWNER().pubkey, OWNER().fingerprint)


def test_first_signed_post_claims_the_name_then_locks_it(db):
    db.post("x", "t", "agent", "unsigned while the name is free")
    db.post("x", "t", "agent", "this claims it", signer=A())
    ident = db.get_identity("agent")
    assert (ident["fingerprint"], ident["source"]) == ("SHA256:a", "tofu")
    with pytest.raises(core.Forbidden, match="'agent' is claimed; sign the post"):
        db.post("x", "t", "agent", "impostor, unsigned")
    with pytest.raises(core.Forbidden, match="claimed by key SHA256:a"):
        db.post("x", "t", "agent", "impostor, other key", signer=B())
    db.post("x", "t", "agent", "the real one again", signer=A())
    assert [(m["body"], m["verified"], m["signed_by"]) for m in db.query_since(0, "x", "")] == [
        ("unsigned while the name is free", False, None),
        ("this claims it", True, "SHA256:a"),
        ("the real one again", True, "SHA256:a")]


def test_refused_post_leaves_no_trace(db):
    db.post("x", "t", "agent", "claim", signer=A())
    head = db.head_id()
    with pytest.raises(core.Forbidden):
        db.post("brand-new-area", "t", "agent", "unsigned")
    assert db.head_id() == head  # no message, no _system announcement
    assert "brand-new-area" not in [a["name"] for a in db.list_areas()]
    with pytest.raises(core.Forbidden):
        db.post("x", "t", "agent", "other key", signer=B())
    assert db.get_identity("agent")["fingerprint"] == "SHA256:a"


def test_owner_overrides_a_claim_and_there_is_one_owner(db):
    db.post("x", "t", "Stefan", "an agent got here first", signer=A())
    _make_owner(db)
    assert db.get_identity("Stefan")["source"] == "owner"
    with pytest.raises(core.Forbidden):
        db.post("x", "t", "Stefan", "the early claimer", signer=A())
    db.post("x", "t", "Stefan", "the owner", signer=OWNER())
    since = db.get_identity("Stefan")["registered_ts"]
    _make_owner(db)  # every server start
    assert db.get_identity("Stefan")["registered_ts"] == since
    db.set_owner("stefan2", B().pubkey, B().fingerprint)
    assert db.get_identity("Stefan")["source"] == "tofu"  # still claimed, not owner


def test_signed_pin_answers_to_its_key_or_the_owners(db):
    _make_owner(db)
    assert db.set_pin("rev", "", "author", "v1", signer=A()) == 1
    for editor, signer in (("intruder", None), ("intruder", B()), ("anon", None)):
        with pytest.raises(core.Forbidden, match=r"signed by key SHA256:a \(as author\)"):
            db.set_pin("rev", "", editor, "takeover", signer=signer)
    with pytest.raises(core.Forbidden):
        db.set_pin("rev", "", "intruder", "", signer=B())  # removing it counts too
    assert db.set_pin("rev", "", "author", "v2", base_version=1, signer=A()) == 2
    # a name is an alias: the same key may save it under another of its names
    assert db.set_pin("rev", "", "pin-writer", "v3", signer=A()) == 3
    assert db.set_pin("rev", "", "Stefan", "v4", signer=OWNER()) == 4
    pin = db.get_pin("rev")
    assert (pin["editor"], pin["verified"], pin["signed_by"]) == ("Stefan", True, "SHA256:owner")
    versions = core._db.execute(
        "SELECT version, editor, base_version, signed_by, signature FROM pin_versions"
        " WHERE area='rev' ORDER BY id").fetchall()
    assert versions == [(1, "author", None, "SHA256:a", "sig-SHA256:a"),
                        (2, "author", 1, "SHA256:a", "sig-SHA256:a"),
                        (3, "pin-writer", None, "SHA256:a", "sig-SHA256:a"),
                        (4, "Stefan", None, "SHA256:owner", "sig-SHA256:owner")]
    # the doorbells name a verified editor too, but carry no signature of their own
    bells = core._db.execute("SELECT sender, signed_by, signature FROM messages"
                             " WHERE topic=?", (core.PIN_TOPIC,)).fetchall()
    assert bells[-1] == ("Stefan", "SHA256:owner", None)


def test_a_reclaimed_name_does_not_carry_the_pin_with_it(db):
    """Key A signs a pin as X, X is released and key B claims it: the pin
    still answers to A's key (and the owner's), not to whoever is X now."""
    _make_owner(db)
    db.set_pin("rev", "", "X", "A's rules", signer=A())
    assert db.delete_identity("X")  # what board-admin's release does
    db.post("rev", "t", "X", "B claims the name", signer=B())
    assert db.get_identity("X")["fingerprint"] == "SHA256:b"
    for body in ("B's rules", ""):  # neither change nor remove it
        with pytest.raises(core.Forbidden, match="signed by key SHA256:a"):
            db.set_pin("rev", "", "X", body, signer=B())
    assert db.get_pin("rev")["body"] == "A's rules"
    assert db.set_pin("rev", "", "A-again", "A's key, another name", signer=A()) == 2
    assert db.set_pin("rev", "", "Stefan", "the owner's rules", signer=OWNER()) == 3


def test_unsigned_pin_stays_open_to_anyone(db):
    db.set_pin("rev", "", "alice", "v1")
    db.set_pin("rev", "", "bob", "v2")
    assert db.set_pin("rev", "", "author", "now signed", signer=A()) == 3
    with pytest.raises(core.Forbidden, match="signed by key"):
        db.set_pin("rev", "", "bob", "v4")


def test_watch_lines_mark_verified_senders(db):
    db.post("x", "t", "agent", "signed\nsecond line", signer=A())
    db.post("x", "t", "other", "unsigned")
    assert core.format_lines(db.query_since(0, "x", "")).splitlines() == [
        "2\t[x/t] agent ✓: signed", "3\t[x/t] other: unsigned"]


# --- sender names: the charset, and distance from every claimed name ----------

GOOD_NAMES = ["Stefan", "board", "me", "anon", "ghidra-agent", "psp_db", "007", "x" * 64]
BAD_NAMES = ["", "Stefan ", " Stefan", "Stefan\n", "Ste fan", "stefan.z", "Stef\u0430n",
             "stéfan", "a/b", "x" * 65]
RULE = "names are 1-64 ASCII letters, digits, '_' and '-'"


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_names_pass_server_and_client(name):
    # includes what the board writes as itself (board) and the web form's default (me)
    core.check_name(name)
    board._check_name(name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_names_fail_server_and_client_alike(name):
    with pytest.raises(core.BadName) as e:
        core.check_name(name)
    assert str(e.value) == f"sender name {ascii(name)} is not allowed: {RULE}"
    with pytest.raises(SystemExit) as e:
        board._check_name(name)
    assert e.value.code == f"board: sender name {ascii(name)} is not allowed: {RULE}"


def test_server_refuses_a_bad_name_on_every_write(server, db, tmp_path):
    import html
    _, key, _ = _keyfile(tmp_path)
    status, out = _post(f"{server}/post", {"area": "x", "topic": "t", "from": "Stefan ",
                                           "body": "hi"})
    assert (status, json.loads(out)["error"]) == (
        400, f"sender name 'Stefan ' is not allowed: {RULE}")
    status, out = _post(f"{server}/post", _message(key, **{"from": "agent.x"}))
    assert status == 400 and RULE in json.loads(out)["error"]  # signing does not help
    status, out = _post(f"{server}/pin", {"area": "x", "from": "Stef\u0430n", "body": "rules"})
    assert status == 400 and "'Stef\\u0430n' is not allowed" in json.loads(out)["error"]
    status, text = _post(f"{server}/ui/post", {"area": "x", "topic": "t", "from": "a b",
                                               "body": "hi"}, form=True)
    assert (status, text) == (400, f"sender name 'a b' is not allowed: {RULE}")
    status, form = _post(f"{server}/ui/pin", {"area": "x", "topic": "", "from": "a b",
                                              "body": "my rules", "base_version": "0"}, form=True)
    assert status == 400 and RULE in html.unescape(form) and "my rules" in form  # text kept
    assert db.head_id() == 0  # nothing landed, not even a new area


def test_names_from_before_the_rule_stay_as_history(db):
    core._db.execute("INSERT INTO messages(ts, area, topic, sender, body)"
                     " VALUES('2026-09-01T00:00:00Z', 'old', 't', 'Stefan ', 'old post')")
    core._db.execute("INSERT INTO pins VALUES('old', '', 'old pin', 1, 'Stef an',"
                     " '2026-09-01T00:00:00Z', NULL)")
    assert [m["from"] for m in db.query_since(0, "old", "")] == ["Stefan "]
    assert db.get_pin("old")["editor"] == "Stef an"
    assert db.set_pin("old", "", "Stefan", "new pin", base_version=1) == 2


PRODUCTION_NAMES = [
    "Stefan", "aspfuzz", "board", "claude-for-stefan", "final-reviewer", "firefox-src-agent",
    "libafl", "librarian", "openwpm-orchestrator", "openwpm-signoff", "orchestrator",
    "psp-db", "psp-etl", "psptool", "psptool-rs", "psptool-rs-orchestrator", "qemu-psp",
    "qemu-tools", "re-reuse", "vetinari", "vetinari-design"]


def test_the_names_in_use_on_the_board_coexist_as_claims(db):
    # all but the board's own sender, which no client can claim
    agents = [n for n in PRODUCTION_NAMES if n != core.SERVER_NAME]
    for i, name in enumerate(agents):
        db.post("x", "t", name, "claim", signer=_signer(f"SHA256:{i}")())
    assert [c["name"] for c in db.list_identities()] == sorted(agents)
    # the closest pairs are exactly far enough apart
    assert {core._distance(a, b) for a, b in (
        ("psp-db", "psp-etl"), ("psp-etl", "psptool"), ("psptool", "psptool-rs"))} == {3}


@pytest.mark.parametrize("near", ["stefan", "STEFAN", "Stefan_", "Stefn", "Stephan"])
def test_a_claim_keeps_names_that_look_like_it_out(db, near):
    db.post("x", "t", "Stefan", "the claim", signer=OWNER())
    refused = f"name '{near}' is too close to the claimed name 'Stefan'"
    with pytest.raises(core.Forbidden, match=refused):
        db.post("x", "t", near, "claiming a look-alike", signer=B())
    with pytest.raises(core.Forbidden, match=refused):
        db.post("x", "t", near, "an unsigned look-alike")
    with pytest.raises(core.Forbidden, match=refused):
        db.set_pin("x", "", near, "a pin too")
    assert db.get_identity(near) is None
    db.post("x", "t", "Stefan-db", "three edits away is fine", signer=B())


def test_the_claimed_name_itself_follows_the_claim_rule(db):
    db.post("x", "t", "Stefan", "the claim", signer=OWNER())
    with pytest.raises(core.Forbidden, match="'Stefan' is claimed; sign the post"):
        db.post("x", "t", "Stefan", "unsigned")
    with pytest.raises(core.Forbidden, match="claimed by key SHA256:owner"):
        db.post("x", "t", "Stefan", "another key", signer=B())
    db.post("x", "t", "Stefan", "its own key", signer=OWNER())


@pytest.mark.parametrize("name", ["board", "Board", "BOARD", "boards", "boar", "b0ard"])
def test_nobody_writes_as_the_board_itself(db, name):
    reason = ("is the board itself" if name == core.SERVER_NAME
              else "is too close to the reserved name 'board'")
    for signer in (None, A()):
        with pytest.raises(core.Forbidden, match=reason):
            db.post("x", "t", name, "pretending to be the server", signer=signer)
        with pytest.raises(core.Forbidden, match=reason):
            db.set_pin("x", "", name, "a pin as the server", signer=signer)
    with pytest.raises(core.Forbidden, match=reason):
        db.set_owner(name, OWNER().pubkey, OWNER().fingerprint)
    assert db.list_identities() == [] and db.head_id() == 0


def test_the_boards_own_notes_still_come_from_board(db):
    db.create_area("rev", "")
    db.set_archived("rev", True)
    db.set_archived("rev", False)
    notes = db.query_since(0, core.SYSTEM_AREA, "areas")
    assert [(m["from"], m["verified"]) for m in notes] == [(core.SERVER_NAME, False)] * 3
    assert [m["body"] for m in notes] == [
        "new area: **rev**", "area archived: **rev**", "area unarchived: **rev**"]


def test_http_writes_as_the_board_itself_get_403(server, db, tmp_path):
    _, key, _ = _keyfile(tmp_path)
    for msg in ({"area": "x", "topic": "t", "from": "board", "body": "unsigned"},
                _message(key, **{"from": "board"}), _message(key, **{"from": "Board"})):
        status, out = _post(f"{server}/post", msg)
        assert status == 403 and "board" in json.loads(out)["error"]
    status, text = _post(f"{server}/ui/post", {"area": "x", "topic": "t", "from": "board",
                                               "body": "hi"}, form=True)
    assert (status, text) == (403, "name 'board' is the board itself; write as your own name")
    status, form = _post(f"{server}/ui/pin", {"area": "x", "topic": "", "from": "Board",
                                              "body": "rules", "base_version": "0"}, form=True)
    assert status == 403 and "too close to the reserved name" in form
    assert db.head_id() == 0


def test_http_unsigned_post_as_a_near_name_is_refused(server, db):
    db.post("x", "t", "Stefan", "the claim", signer=OWNER())
    status, out = _post(f"{server}/post", {"area": "x", "topic": "t", "from": "stefan",
                                           "body": "hi"})
    assert status == 403 and json.loads(out)["error"].startswith(
        "name 'stefan' is too close to the claimed name 'Stefan'")


# --- nonces: a signature is good for one write --------------------------------

def test_a_signature_is_good_for_one_write(db):
    signer = A()
    db.post("x", "t", "agent", "first", signer=signer)
    with pytest.raises(core.StaleNonce) as e:
        db.post("x", "t", "agent", "same signature again", signer=signer)
    assert e.value.head == db.head_id()
    with pytest.raises(core.StaleNonce):
        db.set_pin("x", "", "agent", "as a pin save", signer=signer)
    db.post("x", "t", "someone", "unsigned writes carry no nonce")
    db.set_pin("x", "", "someone", "nor do unsigned pins")


def test_deleting_the_newest_message_does_not_revive_its_nonce(db):
    db.post("x", "t", "a", "one")
    signer = A()  # signed against the head before its own post
    mid = db.post("x", "t", "agent", "signed", signer=signer)
    assert db.delete_message(mid)  # board-admin
    assert db.head_id() == mid  # the highest id ever issued, not the highest left
    with pytest.raises(core.StaleNonce):
        db.post("x", "t", "agent", "signed", signer=signer)


def test_old_board_gains_signing_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                              area TEXT NOT NULL, topic TEXT NOT NULL,
                              sender TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE pins(area TEXT NOT NULL, topic TEXT NOT NULL, body TEXT NOT NULL,
                          version INTEGER NOT NULL, editor TEXT NOT NULL, ts TEXT NOT NULL,
                          PRIMARY KEY(area, topic));
        INSERT INTO messages(ts, area, topic, sender, body)
            VALUES('2026-09-01T00:00:00Z', 'old', 't', 'Stefan', 'from before signing');
        INSERT INTO pins VALUES('old', '', 'old pin', 1, 'Stefan', '2026-09-01T00:00:00Z');""")
    old.commit()
    old.close()
    core.init_db(path)
    [m] = core.query_since(0, "old", "")
    assert (m["verified"], m["signed_by"]) == (False, None)
    assert core.get_pin("old")["verified"] is False
    core.post("old", "t", "a", "x", key="k")
    core.init_db(path)  # a second open must not add the columns again
    with pytest.raises(core.Conflict):
        core.post("old", "t", "a", "y", key="k")


# --- signing over HTTP --------------------------------------------------------

def _message(signing_key, **fields):
    """A signed post, its nonce the current head."""
    values = {"area": "x", "topic": "t", "from": "agent", "body": "hello",
              "key": "k1", "nonce": str(core.head_id()), **fields}
    values["signature"] = board_sign.sign(signing_key, board_sign.MESSAGE, values)
    return values


def _pin_save(signing_key, **fields):
    """A signed JSON pin save, its nonce the current head. base_version is
    signed as text and sent as a number, or not at all when empty."""
    values = {"area": "rev", "topic": "", "from": "agent", "body": "rules",
              "base_version": "", "nonce": str(core.head_id()), **fields}
    signature = board_sign.sign(signing_key, board_sign.PIN, values)
    base = values["base_version"]
    return {**values, "base_version": int(base) if base else None, "signature": signature}


def _post_headers(url, data):
    """POST JSON; (status, headers, body) whatever the status."""
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(data).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def test_http_signed_post_claims_and_is_verified(server, db, tmp_path):
    _, key, fingerprint = _keyfile(tmp_path)
    _, other, _ = _keyfile(tmp_path, "other")
    status, out = _post(f"{server}/post", _message(key))
    assert status == 200
    mid = json.loads(out)["id"]
    _, got = _get(f"{server}/msg/{mid}")
    assert json.loads(got)["verified"] is True and json.loads(got)["signed_by"] == fingerprint
    _, polled = _get(f"{server}/poll?area=x&timeout=0")
    assert [m["verified"] for m in json.loads(polled)] == [True]
    # a resend of the same post is the same post; reusing its key is not
    assert _post(f"{server}/post", _message(key)) == (200, json.dumps({"id": mid}))
    status, out = _post(f"{server}/post", _message(key, body="changed"))
    assert status == 409
    # the name is claimed now
    status, out = _post(f"{server}/post", {"area": "x", "topic": "t", "from": "agent",
                                           "body": "unsigned", "key": "k9"})
    assert (status, json.loads(out)["error"]) == (403, "name 'agent' is claimed; sign the post")
    assert _post(f"{server}/post", _message(other, key="k2"))[0] == 403
    # a signature only covers what it signed
    tampered = {**_message(key, key="k3"), "body": "not what was signed"}
    status, out = _post(f"{server}/post", tampered)
    assert status == 400 and "does not match" in json.loads(out)["error"]
    assert [m["body"] for m in db.query_since(0, "x", "")] == ["hello"]


def test_http_signed_pin_rules(server, db, tmp_path):
    _, key, fingerprint = _keyfile(tmp_path)
    status, _ = _post(f"{server}/pin", _pin_save(key))
    assert status == 200
    _, pin = _get(f"{server}/pin?area=rev")
    assert json.loads(pin)["signed_by"] == fingerprint
    refusal = f"signed by key {fingerprint} (as agent)"
    status, out = _post(f"{server}/pin", {"area": "rev", "from": "anon", "body": "mine now"})
    assert status == 403 and refusal in json.loads(out)["error"]
    status, form = _post(f"{server}/ui/pin", {"area": "rev", "topic": "", "from": "me",
                         "body": "web edit", "base_version": "1"}, form=True)
    assert status == 403 and refusal in form and "web edit" in form  # text kept
    # a signed edit through the web form: its values plus a signature
    edit = {"area": "rev", "topic": "", "from": "agent", "body": "web edit",
            "base_version": "1", "nonce": str(db.head_id())}
    status, box = _post(f"{server}/ui/pin", {**edit, "signature": board_sign.sign(
        key, board_sign.PIN, edit)}, form=True)
    assert status == 200 and "web edit" in box and "✓" in box
    assert db.get_pin("rev")["version"] == 2


def test_web_post_form_signed_like_the_browser_and_refused_unsigned(server, db, tmp_path):
    _, key, _ = _keyfile(tmp_path)
    status, rows = _post(f"{server}/ui/post", _message(key, area="web"), form=True)
    assert status == 200 and "✓" in rows
    import urllib.request
    req = urllib.request.Request(
        f"{server}/ui/post", method="POST",
        data=b"area=web&topic=t&from=agent&body=unsigned",
        headers={"content-type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    assert e.value.headers["content-type"].startswith("text/plain")
    assert e.value.read().decode() == "name 'agent' is claimed; sign the post"


def test_owner_is_preseeded_from_its_key_file(db, tmp_path, capsys):
    _keyfile(tmp_path, "owner")
    pub = tmp_path / "owner_ed25519.pub"
    env = {"BOARD_OWNER_NAME": "Stefan", "BOARD_OWNER_PUBKEY": str(tmp_path / "missing.pub")}
    board_server.preseed_owner(env)
    assert capsys.readouterr().out == (
        f"board: no owner key at {tmp_path / 'missing.pub'}; Stefan is not preseeded\n")
    assert db.get_identity("Stefan") is None
    db.post("x", "t", "Stefan", "claimed before the owner key existed", signer=A())
    board_server.preseed_owner({**env, "BOARD_OWNER_PUBKEY": str(pub)})
    ident = db.get_identity("Stefan")
    assert ident["source"] == "owner" and ident["fingerprint"] == _fingerprint_of(pub)
    _ssh_keygen("-q", "-t", "ecdsa", "-N", "", "-f", tmp_path / "ec")
    capsys.readouterr()
    board_server.preseed_owner({**env, "BOARD_OWNER_PUBKEY": str(tmp_path / "ec.pub")})
    assert "only ed25519" in capsys.readouterr().out
    board_server.preseed_owner({})  # unset: nothing happens
    assert db.get_identity("Stefan")["source"] == "owner"


def test_owner_preseed_keeps_look_alike_claims_but_warns(db, tmp_path, capsys):
    _keyfile(tmp_path, "owner")
    env = {"BOARD_OWNER_NAME": "Stefan", "BOARD_OWNER_PUBKEY": str(tmp_path / "owner_ed25519.pub")}
    db.post("x", "t", "stefan", "an early look-alike", signer=A())
    db.post("x", "t", "Stefan-db", "three edits away", signer=B())
    board_server.preseed_owner(env)
    out = capsys.readouterr().out
    assert ("board: warning: the claimed name 'stefan' is fewer than 3 edits from the"
            " owner's 'Stefan'") in out
    assert "Stefan-db" not in out
    assert db.get_identity("Stefan")["source"] == "owner"
    db.post("x", "t", "stefan", "the look-alike keeps its claim", signer=A())
    board_server.preseed_owner({**env, "BOARD_OWNER_NAME": "Stefan Z"})
    assert capsys.readouterr().out == (
        f"board: BOARD_OWNER_NAME: sender name 'Stefan Z' is not allowed: {RULE};"
        " no owner is preseeded\n")
    assert db.get_identity("Stefan Z") is None
    board_server.preseed_owner({**env, "BOARD_OWNER_NAME": "board"})
    assert capsys.readouterr().out == (
        "board: BOARD_OWNER_NAME: name 'board' is the board itself; write as your own"
        " name; no owner is preseeded\n")
    assert db.get_identity("board") is None


def test_http_replayed_signed_writes_get_stale_nonce(server, db, tmp_path):
    _, key, _ = _keyfile(tmp_path)
    signed = _message(key)
    status, out = _post(f"{server}/post", signed)
    assert status == 200
    assert _post(f"{server}/post", signed) == (200, out)  # its key: the same post again
    # ...until an admin deletes it: replaying it then must not bring it back
    assert db.delete_message(json.loads(out)["id"])
    status, headers, out = _post_headers(f"{server}/post", signed)
    assert (status, json.loads(out)) == (409, {"error": "stale-nonce", "head": db.head_id()})
    assert headers["board-error"] == "stale-nonce"
    keyless = _message(key, key="", body="no key, so nothing to dedupe on")
    assert _post(f"{server}/post", keyless)[0] == 200
    assert json.loads(_post(f"{server}/post", keyless)[1])["error"] == "stale-nonce"
    # an old pin save replayed to revert the pin
    first = _pin_save(key, body="v1")
    assert _post(f"{server}/pin", first)[0] == 200
    assert _post(f"{server}/pin", _pin_save(key, body="v2", base_version="1"))[0] == 200
    status, headers, out = _post_headers(f"{server}/pin", first)
    assert (status, json.loads(out)["error"], headers["board-error"]) == (
        409, "stale-nonce", "stale-nonce")
    assert db.get_pin("rev")["body"] == "v2"
    assert [m["body"] for m in db.query_since(0, "x", "")] == ["no key, so nothing to dedupe on"]


def test_http_stale_base_version_is_its_own_409(server, db, tmp_path):
    _, key, _ = _keyfile(tmp_path)
    _post(f"{server}/pin", _pin_save(key, body="v1"))
    _post(f"{server}/pin", _pin_save(key, body="v2", base_version="1"))
    status, headers, out = _post_headers(f"{server}/pin",
                                         _pin_save(key, body="late edit", base_version="1"))
    assert (status, json.loads(out)) == (409, {"error": "pin changed since base_version",
                                               "version": 2})
    assert "board-error" not in headers  # an edit conflict, not a nonce to refresh


# --- the CLI: keygen and signing ----------------------------------------------

def test_cli_keygen_writes_an_ignored_key_at_the_repo_root(tmp_path, monkeypatch, capsys):
    import stat
    repo = tmp_path / "repo"
    (repo / ".jj").mkdir(parents=True)
    (repo / "sub" / "dir").mkdir(parents=True)
    monkeypatch.chdir(repo / "sub" / "dir")
    board.main(["keygen", "--from", "agent"])
    key = repo / "scratch" / "board_ed25519"
    path, fingerprint = capsys.readouterr().out.splitlines()
    assert path == str(key) and fingerprint == _fingerprint_of(key.with_name(key.name + ".pub"))
    assert (repo / "scratch" / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.with_name(key.name + ".pub").read_text().split()[2] == "board:agent"
    with pytest.raises(SystemExit) as e:
        board.main(["keygen", "--from", "agent"])
    assert "already exists" in e.value.code
    # an existing scratch/.gitignore is the repo's own business
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / "scratch").mkdir()
    (other / "scratch" / ".gitignore").write_text("custom\n")
    monkeypatch.chdir(other)
    board.main(["keygen", "--from", "x"])
    assert (other / "scratch" / ".gitignore").read_text() == "custom\n"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        board.main(["keygen", "--from", "x"])
    assert "not inside a repository" in e.value.code


def test_cli_signs_with_the_nearest_key_and_marks_verified(server, db, tmp_path,
                                                         monkeypatch, capsys):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "deep").mkdir()
    monkeypatch.chdir(repo)
    board.main(["keygen", "--from", "agent"])
    capsys.readouterr()
    monkeypatch.chdir(repo / "deep")
    board.main(["post", "rev", "t", "agent", "signed", "post"])
    board.main(["read", "--area", "rev"])
    assert "[rev/t] agent ✓\nsigned post" in capsys.readouterr().out
    _, lines = _get(f"{server}/poll?area=rev&format=lines&timeout=0")
    assert lines.split("\t", 1)[1] == "[rev/t] agent ✓: signed post\n"
    monkeypatch.chdir(tmp_path)  # no key up here: unsigned, and the name is taken
    with pytest.raises(SystemExit) as e:
        board.main(["post", "rev", "t", "agent", "impostor"])
    assert e.value.code == "board: 403: name 'agent' is claimed; sign the post"
    monkeypatch.setenv("BOARD_KEY", str(repo / "scratch" / "board_ed25519"))
    board.main(["post", "rev", "t", "agent", "via BOARD_KEY"])
    monkeypatch.setenv("BOARD_KEY", str(tmp_path / "nope"))
    with pytest.raises(SystemExit) as e:
        board.main(["post", "rev", "t", "agent", "x"])
    assert e.value.code.startswith("board: cannot read key")
    assert [m["verified"] for m in db.query_since(0, "rev", "t")] == [True, True]


def test_cli_pin_set_is_signed(server, db, tmp_path, monkeypatch, capsys):
    import io
    _keyfile(tmp_path, "agent")
    monkeypatch.setenv("BOARD_KEY", str(tmp_path / "agent_ed25519"))
    monkeypatch.setattr("sys.stdin", io.StringIO("rules\n"))
    board.main(["pin", "rev", "--set", "--from", "agent"])
    board.main(["pin", "rev"])
    assert capsys.readouterr().out.startswith("=== pinned to rev · v1 · agent ✓ ·")


def test_cli_refuses_a_bad_name_before_signing_or_sending(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(board, "_client_get", lambda *a: pytest.fail("sent a request"))
    monkeypatch.setattr(board, "_client_post", lambda *a, **k: pytest.fail("sent a request"))
    for argv in (["keygen", "--from", "Stefan "], ["post", "rev", "t", "a.b", "hi"],
                 ["pin", "rev", "--set", "--from", "Stefаn"]):
        with pytest.raises(SystemExit) as e:
            board.main(argv)
        assert e.value.code.endswith(f"is not allowed: {RULE}")
    assert not (repo / "scratch").exists()  # keygen wrote nothing


def _signing_cli(tmp_path, monkeypatch):
    """Give the CLI a key, and record what it POSTs."""
    _keyfile(tmp_path, "agent")
    monkeypatch.setenv("BOARD_KEY", str(tmp_path / "agent_ed25519"))
    real, sent = board._client_post, []

    def record(path, data, timeout=10):
        sent.append(data)
        return real(path, data, timeout)

    monkeypatch.setattr(board, "_client_post", record)
    return sent


def test_cli_signs_again_once_when_another_write_lands_first(server, db, tmp_path,
                                                             monkeypatch, capsys):
    import io
    sent = _signing_cli(tmp_path, monkeypatch)
    real_head, heads = board._head, []

    def racing_head():
        heads.append(real_head())
        if len(heads) % 2:  # every other read, someone else writes right after it
            db.post("rev", "t", "other", f"concurrent write {len(heads)}")
        return heads[-1]

    monkeypatch.setattr(board, "_head", racing_head)
    board.main(["post", "rev", "t", "agent", "signed post"])
    monkeypatch.setattr("sys.stdin", io.StringIO("rules\n"))
    board.main(["pin", "rev", "--set", "--from", "agent"])
    out, err = capsys.readouterr()
    assert err == "board: pin saved as v1\n"  # transparent: nothing about the retry
    assert len(sent) == 4 and [d["nonce"] for d in sent] == [str(h) for h in heads]
    assert sent[0]["key"] == sent[1]["key"]  # the same post, re-signed
    [mine] = [m for m in db.query_since(0, "rev", "t") if (m["from"], m["topic"]) == ("agent", "t")]
    assert json.loads(out) == {"id": mine["id"]} and mine["verified"]
    assert db.get_pin("rev")["verified"]


def test_cli_gives_up_when_every_signature_goes_stale(server, db, tmp_path, monkeypatch):
    sent = _signing_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(board, "_head", lambda: db.post("rev", "t", "other", "busy") - 1)
    with pytest.raises(SystemExit) as e:
        board.main(["post", "rev", "t", "agent", "never lands"])
    assert e.value.code.startswith(f"board: gave up after {board.NONCE_TRIES} tries")
    assert len(sent) == board.NONCE_TRIES
    assert "never lands" not in [m["body"] for m in db.query_since(0, "rev", "")]


def test_cli_signed_post_retry_after_a_lost_answer_still_lands_once(server, db, tmp_path,
                                                                     monkeypatch, capsys):
    sent = _signing_cli(tmp_path, monkeypatch)
    recording = board._client_post

    def lost_response(path, data, timeout=10):
        out = recording(path, data, timeout)
        if len(sent) == 1:
            raise TimeoutError("timed out")  # landed, but the answer never arrived
        return out

    monkeypatch.setattr(board, "_client_post", lost_response)
    monkeypatch.setattr(board.time, "sleep", lambda _s: None)
    board.main(["post", "rev", "t", "agent", "exactly once"])
    out, err = capsys.readouterr()
    assert "retrying" in err
    # re-signed against the head its own landing moved, and deduped on the key
    assert sent[0]["key"] == sent[1]["key"] and sent[0]["nonce"] != sent[1]["nonce"]
    [m] = db.query_since(0, "rev", "t")
    assert json.loads(out) == {"id": m["id"]} and m["verified"]
