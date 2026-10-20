"""board-admin (the deleting TUI) and board (the server) share one SQLite
file and must not disagree about it. These tests run the two against the
same database: pure admin operations, main()'s service handling, a real
`board-server` process with admin deletions between two server runs, and the
curses TUI itself driven through a pty.
"""

import importlib.util
import json
import os
import pty
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import fcntl
import urllib.request
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "message-board"
_spec = importlib.util.spec_from_file_location("board_admin", _APP / "board-admin.py")
admin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(admin)
board = admin.core  # the very module instance admin operates on

# The integration tests below launch the two programs as processes. By
# default that's the source files; point these at the installed wrappers to
# test what actually gets deployed (the Nix package's bin/board and
# bin/board-admin), e.g.
#   BOARD_SERVER_BIN=$out/bin/board-server BOARD_ADMIN_BIN=$out/bin/board-admin pytest ...
BOARD_CMD = shlex.split(os.environ.get("BOARD_SERVER_BIN", "")) or [sys.executable, str(_APP / "board_server.py")]
ADMIN_CMD = shlex.split(os.environ.get("BOARD_ADMIN_BIN", "")) or [sys.executable, str(_APP / "board-admin.py")]

# How long a server start, a request or the TUI's exit may take: a bound on a
# hang, not a speed check. On a loaded box a single SQLite commit (an fsync)
# has stalled for half a minute.
HANG_TIMEOUT = 120


@pytest.fixture
def db(tmp_path):
    board.init_db(str(tmp_path / "board.db"))
    return board


def _populate(db):
    db.post("proj", "design", "alice", "design one")
    db.post("proj", "design", "bob", "design two")
    db.post("proj", "chatter", "alice", "noise")
    db.post("other", "general", "carol", "elsewhere")
    db.set_pin("proj", "design", "stefan", "design pin")


# --- pure operations ---------------------------------------------------------


def test_counts_cover_archived_and_empty_areas(db):
    _populate(db)
    db.create_area("empty", "")
    db.set_archived("other", True)
    counts = {a: (arch, n) for a, arch, n in admin.area_counts()}
    assert counts["proj"] == (False, 3 + 1)  # +1: the pin announcement on proj/design
    assert counts["other"] == (True, 1)  # archived areas are exactly what cleanup targets
    assert counts["empty"] == (False, 0)
    assert dict(admin.topic_counts("proj")) == {"chatter": 1, "design": 3}


def test_delete_message_deletes_only_that_message(db):
    _populate(db)
    target = admin.messages_in("proj", "design")[0]
    assert admin.delete_message(target["id"]) is True
    assert [m["body"] for m in admin.messages_in("proj", "design")][0] == "design two"
    assert admin.delete_message(target["id"]) is False  # already gone: reported, not an error
    assert admin.messages_in("proj", "chatter")  # neighbours untouched


def test_delete_topic_removes_messages_but_not_pins_or_other_topics(db):
    _populate(db)
    assert admin.delete_topic("proj", "design") == 3
    assert admin.messages_in("proj", "design") == []
    assert admin.messages_in("proj", "chatter")
    assert admin.messages_in("other", "general")
    # a pin is its own row; this tool's scope is messages
    assert db.get_pin("proj", "design")["body"] == "design pin"


def test_audit_log_records_every_deletion(db, tmp_path, monkeypatch):
    log = tmp_path / "state" / "admin-deletions.log"
    monkeypatch.setattr(admin, "AUDIT_LOG", log)
    admin.log_deletion("delete_topic", "proj/design (3 messages)")
    admin.log_deletion("delete_message", "#7 proj/chatter from=alice")
    lines = log.read_text().splitlines()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ delete_topic proj/design \(3 messages\)", lines[0])
    assert lines[1].endswith("delete_message #7 proj/chatter from=alice")


def test_pre_archive_database_is_migrated_not_rejected(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE areas(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
                           created_ts TEXT NOT NULL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                              area TEXT NOT NULL, topic TEXT NOT NULL,
                              sender TEXT NOT NULL, body TEXT NOT NULL);
        INSERT INTO areas VALUES('legacy', '', '2026-09-01T00:00:00Z');
        INSERT INTO messages(ts, area, topic, sender, body)
            VALUES('2026-09-01T00:00:00Z', 'legacy', 't', 's', 'old message');
        """
    )
    old.commit()
    old.close()
    board.init_db(str(path))  # what main() does before the TUI
    assert admin.area_counts() == [("legacy", False, 1)]
    assert admin.delete_topic("legacy", "t") == 1


# --- locating the database ---------------------------------------------------


def test_db_path_defaults_to_where_the_service_keeps_it():
    assert admin.resolve_db_path({"BOARD_DB": "/x/y.db"}) == Path("/x/y.db")
    assert admin.resolve_db_path({"XDG_STATE_HOME": "/st"}) == Path("/st/message-board/board.db")
    assert admin.resolve_db_path({"HOME": "/h"}).name == "board.db"
    assert admin.resolve_db_path({}).parts[-2:] == ("message-board", "board.db")


def test_default_db_path_matches_the_deployed_service():
    """Drift guard: if the unit's state dir or BOARD_DB moves, board-admin
    must follow -- otherwise it opens the wrong (or a fresh, empty) file."""
    ai = (Path(__file__).parent.parent / "home-manager" / "ai.nix").read_text()
    assert 'StateDirectory = "message-board"' in ai
    assert 'BOARD_DB=%S/message-board/board.db' in ai
    assert admin.resolve_db_path({"XDG_STATE_HOME": "/s"}) == Path("/s/message-board/board.db")


def test_missing_database_is_refused_not_created(tmp_path, monkeypatch):
    target = tmp_path / "nope.db"
    monkeypatch.setenv("BOARD_DB", str(target))
    calls = []
    monkeypatch.setattr(admin, "service_is_active", lambda: calls.append("active?") or True)
    with pytest.raises(SystemExit) as e:
        admin.main()
    assert "no database" in str(e.value)
    assert not target.exists()  # init_db never ran
    assert calls == []  # and the service was never touched


# --- main(): taking the service down and bringing it back ---------------------


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """main() with systemctl, the port probe and the TUI all faked."""
    path = tmp_path / "board.db"
    board.init_db(str(path))
    monkeypatch.setenv("BOARD_DB", str(path))
    state = {"active": True, "live": True, "events": [], "tui": lambda s: None}

    monkeypatch.setattr(admin, "service_is_active", lambda: state["active"])

    def stop():
        state["events"].append("stop")
        state["active"] = False
        state["live"] = False

    def start():
        state["events"].append("start")
        state["active"] = True

    monkeypatch.setattr(admin, "service_stop", stop)
    monkeypatch.setattr(admin, "service_start", start)
    monkeypatch.setattr(admin, "service_is_live", lambda url: state["live"])
    monkeypatch.setattr(admin.curses, "wrapper", lambda fn: (state["events"].append("tui"), state["tui"](None)))
    monkeypatch.setattr(admin.time, "sleep", lambda s: None)
    return state


def test_running_service_is_stopped_for_the_session_then_restarted(svc):
    assert admin.main() == 0
    assert svc["events"] == ["stop", "tui", "start"]


def test_service_comes_back_even_if_the_tui_crashes(svc):
    def boom(_):
        raise RuntimeError("tui exploded")

    svc["tui"] = boom
    with pytest.raises(RuntimeError):
        admin.main()
    assert svc["events"] == ["stop", "tui", "start"]


def test_service_comes_back_on_ctrl_c(svc):
    def interrupt(_):
        raise KeyboardInterrupt

    svc["tui"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        admin.main()
    assert svc["events"][-1] == "start"


def test_service_that_was_not_running_is_left_alone(svc):
    svc["active"] = False
    svc["live"] = False
    assert admin.main() == 0
    assert svc["events"] == ["tui"]  # no stop, and crucially no start afterwards


def test_unmanaged_server_is_refused_without_touching_anything(svc):
    svc["active"] = False  # systemd says down...
    svc["live"] = True  # ...but something answers on the port
    with pytest.raises(SystemExit) as e:
        admin.main()
    assert "unmanaged" in str(e.value)
    assert svc["events"] == []


def test_port_still_answering_after_stop_restarts_the_service_we_stopped(svc, monkeypatch):
    # stop "succeeds" but a second server keeps the port busy
    monkeypatch.setattr(admin, "service_stop", lambda: svc["events"].append("stop"))
    svc["live"] = True
    with pytest.raises(SystemExit) as e:
        admin.main()
    assert "still answering" in str(e.value)
    assert svc["events"] == ["stop", "start"]  # we took it down, so we bring it back
    assert "tui" not in svc["events"]


def test_failed_restart_warns_instead_of_raising(svc, monkeypatch, capsys):
    def fail():
        raise subprocess.CalledProcessError(1, ["systemctl"])

    monkeypatch.setattr(admin, "service_start", fail)
    assert admin.main() == 0
    assert "systemctl --user start" in capsys.readouterr().err


def test_service_handling_can_be_disabled(monkeypatch):
    monkeypatch.setattr(admin, "SERVICE", "")
    assert admin.service_is_active() is False


# --- the two real binaries on one database -------------------------------------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """A real `board-server` process on a private port and database."""

    def __init__(self, dbpath):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ, BOARD_DB=str(dbpath), BOARD_PORT=str(self.port), BOARD_URL=self.url)
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            BOARD_CMD,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + HANG_TIMEOUT
        while time.time() < deadline:
            try:
                self.get("/healthz")
                return self
            except OSError:
                time.sleep(0.1)
        raise AssertionError("board-server never came up")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=HANG_TIMEOUT) as r:
            return json.loads(r.read())

    def post(self, area, topic, sender, body):
        req = urllib.request.Request(
            self.url + "/post",
            data=json.dumps({"area": area, "topic": topic, "from": sender, "body": body}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HANG_TIMEOUT) as r:
            return json.loads(r.read())["id"]

    def poll(self, since=0, area="", topic="", archived=True):
        q = f"/poll?since={since}&area={area}&topic={topic}&timeout=0" + ("&archived=1" if archived else "")
        return self.get(q)


@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path / "board.db")
    try:  # a start that times out has a process too, and nothing else stops it
        yield srv.start()
    finally:
        srv.stop()


def _admin_session(dbpath):
    """What board-admin does to the file between two server runs."""
    board.init_db(str(dbpath))


def test_admin_deletions_are_visible_to_the_restarted_server_and_ids_never_return(server, tmp_path):
    ids = [server.post("proj", "design", "a", f"m{i}") for i in range(3)]
    keep = server.post("proj", "chatter", "a", "keep me")
    server.stop()

    _admin_session(tmp_path / "board.db")
    assert admin.delete_topic("proj", "design") == 3
    server.start()

    left = [(m["topic"], m["body"]) for m in server.poll(area="proj")]
    assert left == [("chatter", "keep me")]
    new = server.post("proj", "design", "a", "after the purge")
    assert new > max(ids + [keep]), "a deleted id was handed out again"


def test_head_never_moves_backwards_after_the_newest_message_is_deleted(server, tmp_path):
    server.post("proj", "t", "a", "one")
    newest = server.post("proj", "t", "a", "two")
    before = server.get("/healthz")["head"]
    server.stop()

    _admin_session(tmp_path / "board.db")
    assert admin.delete_message(newest) is True
    server.start()

    # `board read` hands out a resume cursor from this, and `watch --since head`
    # starts from it: both assume ids only ever go up.
    assert server.get("/healthz")["head"] >= before


def test_a_watcher_cursor_from_before_the_purge_still_gets_exactly_the_next_post(server, tmp_path):
    server.post("proj", "t", "a", "one")
    cursor = server.post("proj", "t", "a", "two")  # a watcher has seen up to here
    server.stop()

    _admin_session(tmp_path / "board.db")
    admin.delete_message(cursor)
    server.start()
    nxt = server.post("proj", "t", "a", "three")

    got = server.poll(since=cursor, area="proj")
    assert [m["id"] for m in got] == [nxt]


def test_archive_state_and_pins_survive_a_purge_of_another_topic(server, tmp_path):
    server.post("proj", "design", "a", "d")
    server.post("proj", "chatter", "a", "c")
    server.post("old", "x", "a", "y")
    for path, body in (("/pin", {"area": "proj", "topic": "", "from": "s", "body": "area pin"}),
                       ("/archive", {"area": "old", "archived": True})):
        req = urllib.request.Request(server.url + path, data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=HANG_TIMEOUT).read()
    server.stop()

    _admin_session(tmp_path / "board.db")
    admin.delete_topic("proj", "design")
    server.start()

    areas = {a["name"]: a["archived_ts"] for a in server.get("/areas?archived=1")}
    assert areas["old"] is not None and areas["proj"] is None
    assert server.get("/pin?area=proj")["body"] == "area pin"
    assert [m["body"] for m in server.poll(area="proj", topic="chatter") if m["topic"] == "chatter"] == ["c"]


def test_admin_refuses_while_the_server_is_up_and_does_not_disturb_it(server, tmp_path, monkeypatch):
    server.post("proj", "t", "a", "live data")
    monkeypatch.setenv("BOARD_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("BOARD_URL", server.url)
    monkeypatch.setattr(admin, "SERVICE", "")  # no systemd here: only the port probe applies
    with pytest.raises(SystemExit) as e:
        admin.main()
    assert "unmanaged" in str(e.value)
    assert [m["body"] for m in server.poll(area="proj")] == ["live data"]


# --- the TUI itself, through a real terminal -------------------------------------


def _drive_tui(dbpath, keys, tmp_path):
    """Run board-admin under a pty and feed it keystrokes. Returns (exit
    code, everything it drew)."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    env = dict(
        os.environ,
        TERM="xterm",
        BOARD_DB=str(dbpath),
        BOARD_ADMIN_SERVICE="",
        BOARD_URL=f"http://127.0.0.1:{_free_port()}",  # nothing listens here
        BOARD_ADMIN_LOG=str(tmp_path / "audit.log"),
    )
    proc = subprocess.Popen(
        ADMIN_CMD,
        stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True, start_new_session=True,
    )
    os.close(slave)
    drawn = b""

    def drain(wait):
        nonlocal drawn
        end = time.time() + wait
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    return
                if not chunk:
                    return
                drawn += chunk

    try:
        drain(0.6)  # let it start and draw the first screen
        for k in keys:
            os.write(master, k)
            drain(0.25)
        deadline = time.time() + HANG_TIMEOUT
        while proc.poll() is None and time.time() < deadline:
            drain(0.1)
        if proc.poll() is None:
            proc.kill()
            raise AssertionError("board-admin did not exit; screen so far: " + drawn.decode("utf-8", "replace")[-400:])
    finally:
        os.close(master)
    return proc.returncode, drawn.decode("utf-8", "replace")


def _nav(area_idx):
    return [b"j"] * area_idx + [b"\r"]


def test_tui_shows_the_real_areas_and_quits_cleanly(db, tmp_path):
    _populate(db)
    code, screen = _drive_tui(tmp_path / "board.db", [b"q"], tmp_path)
    assert code == 0
    assert "select an area" in screen and "proj" in screen and "other" in screen


def test_tui_wipes_a_topic_only_after_the_typed_confirmation(db, tmp_path):
    _populate(db)
    areas = [a for a, _, _ in admin.area_counts()]
    topics = [t for t, _ in admin.topic_counts("proj")]
    keys = (
        _nav(areas.index("proj"))
        + _nav(topics.index("design"))
        + [b"d", b"proj/design\r"]  # exact word required
        + [b"q", b"q"]
    )
    code, _ = _drive_tui(tmp_path / "board.db", keys, tmp_path)
    assert code == 0
    board.init_db(str(tmp_path / "board.db"))
    assert admin.messages_in("proj", "design") == []
    assert admin.messages_in("proj", "chatter")  # the neighbour topic is untouched
    log = (tmp_path / "audit.log").read_text()
    assert "delete_topic proj/design (3 messages)" in log


def test_tui_wrong_confirmation_deletes_nothing(db, tmp_path):
    _populate(db)
    areas = [a for a, _, _ in admin.area_counts()]
    topics = [t for t, _ in admin.topic_counts("proj")]
    keys = (
        _nav(areas.index("proj"))
        + _nav(topics.index("design"))
        + [b"d", b"yes\r"]  # not the required word
        + [b"q", b"q"]
    )
    code, _ = _drive_tui(tmp_path / "board.db", keys, tmp_path)
    assert code == 0
    board.init_db(str(tmp_path / "board.db"))
    assert len(admin.messages_in("proj", "design")) == 3
    assert not (tmp_path / "audit.log").exists()


def test_tui_deletes_one_message_on_y_and_keeps_it_on_anything_else(db, tmp_path):
    _populate(db)
    areas = [a for a, _, _ in admin.area_counts()]
    topics = [t for t, _ in admin.topic_counts("proj")]
    first = admin.messages_in("proj", "design")[0]["id"]
    keys = (
        _nav(areas.index("proj"))
        + _nav(topics.index("design"))
        + [b"b"]
        + [b"\r", b"n"]  # open message 0, decline
        + [b"\r", b"y"]  # open message 0 again, confirm
        + [b"q", b"q", b"q"]
    )
    code, _ = _drive_tui(tmp_path / "board.db", keys, tmp_path)
    assert code == 0
    board.init_db(str(tmp_path / "board.db"))
    assert first not in [m["id"] for m in admin.messages_in("proj", "design")]
    assert len(admin.messages_in("proj", "design")) == 2
    assert f"delete_message #{first} proj/design" in (tmp_path / "audit.log").read_text()


# --- name claims --------------------------------------------------------------


def _claim(db, name, fingerprint):
    signer = db.Signer(f"ssh-ed25519 KEY-{fingerprint}", fingerprint, "sig", str(db.head_id()))
    db.post("proj", "t", name, f"{name} signs", signer=signer)
    return signer


def test_release_frees_a_claimed_name(db):
    _claim(db, "agent", "SHA256:a")
    with pytest.raises(db.Forbidden):
        db.post("proj", "t", "agent", "unsigned")
    assert [c["name"] for c in admin.list_identities()] == ["agent"]
    assert admin.delete_identity("agent") is True
    assert admin.delete_identity("agent") is False  # already free: reported, not an error
    db.post("proj", "t", "agent", "unsigned works again")
    _claim(db, "agent", "SHA256:new")  # and a new key can claim it
    assert db.get_identity("agent")["fingerprint"] == "SHA256:new"


def test_release_frees_the_names_around_it_too(db):
    _claim(db, "Stefan", "SHA256:s")
    with pytest.raises(db.Forbidden, match="too close to the claimed name 'Stefan'"):
        db.post("proj", "t", "stefan", "a look-alike")
    assert admin.delete_identity("Stefan")
    db.post("proj", "t", "stefan", "free again")
    _claim(db, "Stefn", "SHA256:other")


def test_tui_releases_a_name_claim_only_on_y(db, tmp_path):
    _populate(db)
    _claim(db, "alice", "SHA256:alice")
    _claim(db, "bob", "SHA256:bob")
    claims_entry = len(admin.area_counts())  # listed after the areas
    keys = (
        _nav(claims_entry)
        + [b"\r", b"n"]  # alice, declined
        + [b"j", b"\r", b"y"]  # bob, released
        + [b"q", b"q"]
    )
    code, screen = _drive_tui(tmp_path / "board.db", keys, tmp_path)
    assert code == 0
    assert "name claims (2)" in screen
    board.init_db(str(tmp_path / "board.db"))
    assert [c["name"] for c in admin.list_identities()] == ["alice"]
    log = (tmp_path / "audit.log").read_text()
    assert log.endswith("release_claim bob SHA256:bob (tofu)\n")
