"""board-admin (the deleting TUI) and board-server share one SQLite file and
must not disagree about it. These tests run the two programs against the
same database: the TUI driven through a pty, its handling of the user
service through a stand-in systemctl that runs a real board-server, and
admin deletions between two server runs.
"""

import json
import os
import re
import signal
from pathlib import Path

import pytest

from board_harness import (
    Board, Do, FakeSystemd, Forbidden, Key, Kill, Pick, Server, Wait, admin, drive_tui, isolate,
    make_db, running, sql, sql_changes,
)

# The confirmation prompts, as board-admin shows them.
DELETE_ONE = "Delete permanently? [y/N]"
RELEASE = "Release this claim? [y/N]"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)


def _env(tmp_path, **extra):
    """board-admin on the test's database, with no service to manage."""
    return {**os.environ, "BOARD_DB": str(tmp_path / "board.db"), "BOARD_ADMIN_SERVICE": "",
            "BOARD_ADMIN_LOG": str(tmp_path / "audit.log"), **extra}


def _populate(b):
    b.post("proj", "design", "alice", "design one")
    b.post("proj", "design", "bob", "design two")
    b.post("proj", "chatter", "alice", "noise")
    b.post("other", "general", "carol", "elsewhere")
    b.set_pin("proj", "design", "stefan", "design pin")


@pytest.fixture
def populated(tmp_path):
    """A board database with two areas in it, its server stopped again: an
    admin session happens between two server runs."""
    with running(tmp_path / "board.db", tmp_path / "logs") as b:
        _populate(b)
    return tmp_path / "board.db"


def _ids(db, area, topic):
    return [i for (i,) in sql(db, "SELECT id FROM messages WHERE area=? AND topic=? ORDER BY id",
                              area, topic)]


def _bodies(db, area, topic):
    return [b for (b,) in sql(db, "SELECT body FROM messages WHERE area=? AND topic=?"
                                  " ORDER BY id", area, topic)]


def _audit(tmp_path):
    log = tmp_path / "audit.log"
    return log.read_text() if log.exists() else ""


# --- what the TUI shows, and what its deletions do --------------------------------


def test_counts_cover_archived_and_empty_areas(tmp_path):
    with running(tmp_path / "board.db", tmp_path / "logs") as b:
        _populate(b)
        b.create_area("empty", "")
        b.set_archived("other", True)
    run = drive_tui(_env(tmp_path), [Wait("select an area"), Pick("proj  ["),
                                     Wait("select a topic"), b"q", b"q"])
    assert run.code == 0
    assert run.seen("proj  [live]  (4 messages)")  # +1: the pin announcement on proj/design
    assert run.seen("other  [archived]  (1 messages)")  # archived areas are what cleanup targets
    assert run.seen("empty  [live]  (0 messages)")
    assert run.seen("chatter  (1 messages)") and run.seen("design  (3 messages)")


def test_delete_message_deletes_only_that_message(populated, tmp_path):
    first, second, _ = _ids(populated, "proj", "design")
    run = drive_tui(_env(tmp_path), [
        Pick("proj  ["), Pick("design  ("), b"b", Pick(f"#{first}  "), Wait(DELETE_ONE), b"y",
        Wait(f"#{second}  "), b"q", b"q", b"q"])
    assert run.code == 0
    assert _bodies(populated, "proj", "design")[0] == "design two"
    assert _bodies(populated, "proj", "chatter") == ["noise"]  # neighbours untouched


def test_a_message_already_deleted_meanwhile_is_not_an_error(populated, tmp_path):
    first, _, _ = _ids(populated, "proj", "design")
    run = drive_tui(_env(tmp_path), [
        Pick("proj  ["), Pick("design  ("), b"b", Pick(f"#{first}  "), Wait(DELETE_ONE),
        Do(lambda: sql_changes(populated, "DELETE FROM messages WHERE id=?", first)), b"y",
        Wait("view/delete one message"),
        b"q", b"q", b"q"])
    assert run.code == 0
    assert f"#{first} " not in _audit(tmp_path)  # nothing was deleted by this session
    assert len(_ids(populated, "proj", "design")) == 2


def test_delete_topic_removes_messages_but_not_pins_or_other_topics(populated, tmp_path):
    run = drive_tui(_env(tmp_path), [
        Pick("proj  ["), Pick("design  ("), b"d", b"proj/design\r", Wait("chatter  ("),
        b"q", b"q"])
    assert run.code == 0
    assert _ids(populated, "proj", "design") == []
    assert _bodies(populated, "proj", "chatter") == ["noise"]
    assert _bodies(populated, "other", "general") == ["elsewhere"]
    # a pin is its own row; this tool's scope is messages
    assert sql(populated, "SELECT body FROM pins WHERE area='proj' AND topic='design'") == [
        ("design pin",)]


def test_audit_log_records_every_deletion(populated, tmp_path):
    log = tmp_path / "state" / "admin-deletions.log"  # its directory does not exist yet
    [chatter] = _ids(populated, "proj", "chatter")
    run = drive_tui(_env(tmp_path, BOARD_ADMIN_LOG=str(log)), [
        Pick("proj  ["), Pick("design  ("), b"d", b"proj/design\r",
        Pick("chatter  ("), b"b", Pick(f"#{chatter}  "), Wait(DELETE_ONE), b"y",
        Wait("proj: no messages."), b"x", Wait("select an area"), b"q"])
    assert run.code == 0
    lines = log.read_text().splitlines()
    stamp = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
    assert re.fullmatch(rf"{stamp} delete_topic proj/design \(3 messages\)", lines[0])
    assert re.fullmatch(rf"{stamp} delete_message #{chatter} proj/chatter from=alice", lines[1])
    assert len(lines) == 2


def test_pre_archive_database_is_migrated_not_rejected(tmp_path):
    make_db(tmp_path / "board.db", """
        CREATE TABLE areas(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
                           created_ts TEXT NOT NULL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                              area TEXT NOT NULL, topic TEXT NOT NULL,
                              sender TEXT NOT NULL, body TEXT NOT NULL);
        INSERT INTO areas VALUES('legacy', '', '2026-09-01T00:00:00Z');
        INSERT INTO messages(ts, area, topic, sender, body)
            VALUES('2026-09-01T00:00:00Z', 'legacy', 't', 's', 'old message');
        """)
    run = drive_tui(_env(tmp_path), [
        Pick("legacy  [live]  (1 messages)"), Pick("t  (1 messages)"), b"d", b"legacy/t\r",
        Wait("legacy: no messages."), b"x", Wait("select an area"), b"q"])
    assert run.code == 0
    assert sql(tmp_path / "board.db", "SELECT COUNT(*) FROM messages") == [(0,)]
    columns = [c[1] for c in sql(tmp_path / "board.db", "PRAGMA table_info(areas)")]
    assert "archived_ts" in columns


# --- locating the database ---------------------------------------------------


def _refused_db_path(tmp_path, **env):
    """The database path board-admin looked for and did not find."""
    systemd = FakeSystemd(tmp_path, active=True)
    base = {k: v for k, v in os.environ.items()
            if k not in ("BOARD_DB", "XDG_STATE_HOME", "HOME")}
    r = admin({**base, "PATH": systemd.path(), "BOARD_ADMIN_SERVICE": "message-board", **env})
    assert r.code == 1 and systemd.calls() == []  # refused before touching the service
    return re.search(r"no database at (.*) \(set BOARD_DB to point elsewhere\)", r.err)[1]


def test_db_path_defaults_to_where_the_service_keeps_it(tmp_path):
    # Every case names a directory that does not exist, so none can reach a
    # real board's database.
    assert _refused_db_path(tmp_path, BOARD_DB="/nonexistent/y.db") == "/nonexistent/y.db"
    assert _refused_db_path(tmp_path, XDG_STATE_HOME="/nonexistent/st") == \
        "/nonexistent/st/message-board/board.db"
    assert _refused_db_path(tmp_path, HOME="/nonexistent/h") == \
        "/nonexistent/h/.local/state/message-board/board.db"
    assert _refused_db_path(tmp_path, BOARD_DB="", XDG_STATE_HOME="", HOME="/nonexistent/h") == \
        "/nonexistent/h/.local/state/message-board/board.db"  # empty is unset


def test_default_db_path_matches_the_deployed_service(tmp_path):
    """Drift guard: if the unit's state dir or BOARD_DB moves, board-admin
    must follow -- otherwise it opens the wrong (or a fresh, empty) file."""
    ai = (Path(__file__).parent.parent / "home-manager" / "ai.nix").read_text()
    assert 'StateDirectory = "message-board"' in ai
    assert 'BOARD_DB=%S/message-board/board.db' in ai
    assert _refused_db_path(tmp_path, XDG_STATE_HOME="/nonexistent/s") == \
        "/nonexistent/s/message-board/board.db"


def test_missing_database_is_refused_not_created(tmp_path):
    target = tmp_path / "nope.db"
    systemd = FakeSystemd(tmp_path, active=True)
    r = admin(_env(tmp_path, BOARD_DB=str(target), PATH=systemd.path(),
                   BOARD_ADMIN_SERVICE="message-board"))
    assert r.code == 1 and "no database" in r.err
    assert not target.exists()  # it never opened one
    assert systemd.calls() == []  # and the service was never touched


# --- taking the service down and bringing it back ------------------------------------


@pytest.fixture
def service(tmp_path):
    """The board as a user service: a real board-server on the test's
    database. FakeSystemd(server=...) makes `systemctl stop` stop it."""
    server = Server(tmp_path / "board.db", tmp_path / "logs").start()
    Board(server).post("proj", "t", "a", "live data")
    yield server
    server.stop()


def _service_env(tmp_path, systemd, server):
    return _env(tmp_path, BOARD_ADMIN_SERVICE="message-board", PATH=systemd.path(),
                BOARD_URL=server.url)


def test_running_service_is_stopped_for_the_session_then_restarted(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    during = []
    run = drive_tui(_service_env(tmp_path, systemd, service), [
        Wait("select an area"), Do(lambda: during.extend(systemd.calls())), b"q"])
    assert run.code == 0
    assert during == ["is-active", "stop"]  # the session runs with the board down
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert run.seen("stopping message-board") and run.seen("restarting message-board")


def test_service_comes_back_even_if_the_tui_crashes(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    run = drive_tui(_service_env(tmp_path, systemd, service), [
        Wait("select an area"),
        Do(lambda: sql(tmp_path / "board.db", "DROP TABLE messages")),  # the next query fails
        Pick("proj  [")])
    assert run.code != 0
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert run.seen("no such table: messages")  # the cause is shown, not swallowed
    assert not run.term.alt  # and the terminal is given back


def test_service_comes_back_on_ctrl_c(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    run = drive_tui(_service_env(tmp_path, systemd, service), [Wait("select an area"), b"\x03"])
    assert run.code != 0
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert not run.term.alt


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_service_comes_back_when_the_session_is_killed(tmp_path, service, signum):
    """A plain kill, or the terminal closing under it (SIGHUP), ends the
    session like a quit: the board is restarted and the terminal restored."""
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    run = drive_tui(_service_env(tmp_path, systemd, service),
                    [Wait("select an area"), Kill(signum)])
    assert run.code != 0
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert not run.term.alt


def test_service_that_was_not_running_is_left_alone(populated, tmp_path):
    systemd = FakeSystemd(tmp_path, active=False)  # and nothing answers on BOARD_URL
    run = drive_tui(_env(tmp_path, BOARD_ADMIN_SERVICE="message-board", PATH=systemd.path()),
                    [Wait("select an area"), b"q"])
    assert run.code == 0
    assert systemd.calls() == ["is-active"]  # no stop, and crucially no start afterwards


def test_unmanaged_server_is_refused_without_touching_anything(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=False, server=service)  # systemd says down...
    r = admin(_service_env(tmp_path, systemd, service))  # ...but something answers the port
    assert r.code == 1 and "unmanaged" in r.err
    assert systemd.calls() == ["is-active"]
    assert [m["body"] for m in Board(service).query_since(0, "proj")] == ["live data"]


def test_port_still_answering_after_stop_restarts_the_service_we_stopped(tmp_path, service):
    # stop "succeeds" but a second server keeps the port busy
    systemd = FakeSystemd(tmp_path, active=True, server=service, stop_works=False)
    r = admin(_service_env(tmp_path, systemd, service))
    assert r.code == 1 and "still answering" in r.err
    assert systemd.calls() == ["is-active", "stop", "start"]  # we took it down, so we bring it back


def test_a_stop_that_reports_failure_still_restarts_the_service(tmp_path, service):
    """systemctl can fail after systemd has begun the stop (a Ctrl-C, its
    wait timing out), and systemd finishes it anyway. The board used to be
    left down: the restart only followed a stop that had succeeded."""
    systemd = FakeSystemd(tmp_path, active=True, server=service, stop_fails=True)
    r = admin(_service_env(tmp_path, systemd, service))
    assert r.code == 1 and "canceled" in r.err
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert json.loads(systemd.state.read_text())["active"] is True


def test_failed_restart_warns_instead_of_raising(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service, start_fails=True)
    run = drive_tui(_service_env(tmp_path, systemd, service), [Wait("select an area"), b"q"],
                    cols=400)  # the warning on one line
    assert run.code == 0
    assert run.seen("systemctl --user start message-board")


def test_service_handling_can_be_disabled(populated, tmp_path):
    systemd = FakeSystemd(tmp_path, active=True)
    run = drive_tui(_env(tmp_path, BOARD_ADMIN_SERVICE="", PATH=systemd.path()),
                    [Wait("select an area"), b"q"])
    assert run.code == 0
    assert systemd.calls() == []


# --- the two real programs on one database -------------------------------------


@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path / "board.db", tmp_path / "logs")
    try:  # a start that times out has a process too, and nothing else stops it
        yield srv.start()
    finally:
        srv.stop()


def test_server_exits_cleanly_on_sigterm(server):
    # It has a handler, so it also stops as PID 1 of a PID namespace (the
    # user unit's PrivatePIDs=), where SIGTERM without one is dropped.
    server.stop()
    assert server.returncode == 0


def _purge_topic(tmp_path, area, topic, then):
    """An admin session that deletes area/topic, then waits for `then`."""
    run = drive_tui(_env(tmp_path), [
        Pick(f"{area}  ["), Pick(f"{topic}  ("), b"d", f"{area}/{topic}\r".encode(), Wait(then),
        b"q", b"q"])
    assert run.code == 0


def _delete_message(tmp_path, area, topic, mid):
    run = drive_tui(_env(tmp_path), [
        Pick(f"{area}  ["), Pick(f"{topic}  ("), b"b", Pick(f"#{mid}  "), Wait(DELETE_ONE), b"y",
        b"q", b"q", b"q"])
    assert run.code == 0


def test_admin_deletions_are_visible_to_the_restarted_server_and_ids_never_return(server,
                                                                                    tmp_path):
    b = Board(server)
    ids = [b.post("proj", "design", "a", f"m{i}") for i in range(3)]
    keep = b.post("proj", "chatter", "a", "keep me")
    server.stop()

    _purge_topic(tmp_path, "proj", "design", then="chatter  (")
    server.start()

    left = [(m["topic"], m["body"]) for m in b.poll(area="proj")]
    assert left == [("chatter", "keep me")]
    new = b.post("proj", "design", "a", "after the purge")
    assert new > max(ids + [keep]), "a deleted id was handed out again"


def test_head_never_moves_backwards_after_the_newest_message_is_deleted(server, tmp_path):
    b = Board(server)
    b.post("proj", "t", "a", "one")
    newest = b.post("proj", "t", "a", "two")
    before = b.head_id()
    server.stop()

    _delete_message(tmp_path, "proj", "t", newest)
    server.start()

    # `board read` hands out a resume cursor from this, and `watch --since head`
    # starts from it: both assume ids only ever go up.
    assert b.head_id() >= before
    assert [m["body"] for m in b.poll(area="proj")] == ["one"]


def test_a_watcher_cursor_from_before_the_purge_still_gets_exactly_the_next_post(server,
                                                                                tmp_path):
    b = Board(server)
    b.post("proj", "t", "a", "one")
    cursor = b.post("proj", "t", "a", "two")  # a watcher has seen up to here
    server.stop()

    _delete_message(tmp_path, "proj", "t", cursor)
    server.start()
    nxt = b.post("proj", "t", "a", "three")

    assert [m["id"] for m in b.poll(since=cursor, area="proj")] == [nxt]


def test_archive_state_and_pins_survive_a_purge_of_another_topic(server, tmp_path):
    b = Board(server)
    b.post("proj", "design", "a", "d")
    b.post("proj", "chatter", "a", "c")
    b.post("old", "x", "a", "y")
    b.set_pin("proj", "", "s", "area pin")
    b.set_archived("old", True)
    server.stop()

    _purge_topic(tmp_path, "proj", "design", then="chatter  (")
    server.start()

    areas = {a["name"]: a["archived_ts"] for a in b.list_areas(include_archived=True)}
    assert areas["old"] is not None and areas["proj"] is None
    assert b.get_pin("proj")["body"] == "area pin"
    assert [m["body"] for m in b.poll(area="proj", topic="chatter")
            if m["topic"] == "chatter"] == ["c"]


def test_admin_refuses_while_the_server_is_up_and_does_not_disturb_it(server, tmp_path):
    b = Board(server)
    b.post("proj", "t", "a", "live data")
    r = admin(_env(tmp_path, BOARD_URL=server.url))  # no service: only the port probe applies
    assert r.code == 1 and "unmanaged" in r.err
    assert [m["body"] for m in b.poll(area="proj")] == ["live data"]


# --- the TUI itself, through a real terminal -------------------------------------


def test_tui_shows_the_real_areas_and_quits_cleanly(populated, tmp_path):
    run = drive_tui(_env(tmp_path), [Wait("select an area"), b"q"])
    assert run.code == 0
    assert run.seen("select an area") and run.seen("proj  [") and run.seen("other  [")
    assert not run.term.alt


def test_tui_wipes_a_topic_only_after_the_typed_confirmation(populated, tmp_path):
    run = drive_tui(_env(tmp_path), [
        Pick("proj  ["), Pick("design  ("), b"d", b"proj/design\r",  # exact word required
        Wait("chatter  ("), b"q", b"q"])
    assert run.code == 0
    assert _ids(populated, "proj", "design") == []
    assert _bodies(populated, "proj", "chatter")  # the neighbour topic is untouched
    assert "delete_topic proj/design (3 messages)" in _audit(tmp_path)


def test_tui_wrong_confirmation_deletes_nothing(populated, tmp_path):
    run = drive_tui(_env(tmp_path), [
        Pick("proj  ["), Pick("design  ("), b"d", b"yes\r",  # not the required word
        Wait("design  ("), b"q", b"q"])
    assert run.code == 0
    assert len(_ids(populated, "proj", "design")) == 3
    assert not (tmp_path / "audit.log").exists()


def test_tui_deletes_one_message_on_y_and_keeps_it_on_anything_else(populated, tmp_path):
    first = _ids(populated, "proj", "design")[0]
    run = drive_tui(_env(tmp_path), [
        Pick("proj  ["), Pick("design  ("), b"b",
        Pick(f"#{first}  "), Wait(DELETE_ONE), b"n",  # open the message, decline
        Pick(f"#{first}  "), Wait(DELETE_ONE), b"y",  # open it again, confirm
        b"q", b"q", b"q"])
    assert run.code == 0
    assert first not in _ids(populated, "proj", "design")
    assert len(_ids(populated, "proj", "design")) == 2
    assert f"delete_message #{first} proj/design" in _audit(tmp_path)


def test_a_deletion_the_audit_log_cannot_take_is_named(populated, tmp_path):
    """The deletion is done before its entry is written. If that write
    fails, the error must name what went unrecorded: "cannot write the
    audit log" alone left no trace of which message was gone."""
    first = _ids(populated, "proj", "design")[0]
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        run = drive_tui(_env(tmp_path, BOARD_ADMIN_LOG=str(locked / "audit.log")), [
            Pick("proj  ["), Pick("design  ("), b"b", Pick(f"#{first}  "), Wait(DELETE_ONE), b"y"],
            cols=400)
    finally:
        locked.chmod(0o700)
    assert run.code == 1
    assert first not in _ids(populated, "proj", "design")
    assert run.seen(f'"delete_message #{first} proj/design from=alice" is done but not in the audit log')


# --- name claims --------------------------------------------------------------


def _claim(b, tmp_path, name):
    key = Key.make(tmp_path, f"{name}-key")
    b.post("proj", "t", name, f"{name} signs", signer=b.signer(key))
    return key


def _release(tmp_path, name, *before_yes):
    run = drive_tui(_env(tmp_path), [
        Pick("-- name claims"), Pick(f"{name}  ["), Wait(RELEASE), *before_yes, b"y",
        Wait("name claims (q back)"), b"q", b"q"])
    assert run.code == 0
    return run


def test_release_frees_a_claimed_name(tmp_path):
    with running(tmp_path / "board.db", tmp_path / "logs") as b:
        _claim(b, tmp_path, "agent")
        with pytest.raises(Forbidden):
            b.post("proj", "t", "agent", "unsigned")
        b.server.stop()
        _release(tmp_path, "agent")
        b.server.start()
        b.post("proj", "t", "agent", "unsigned works again")
        new = Key.make(tmp_path, "new-key")  # and a new key can claim it
        b.post("proj", "t", "agent", "a new key claims it", signer=b.signer(new))
        assert b.get_identity("agent")["fingerprint"] == new.fingerprint


def test_a_claim_already_released_meanwhile_is_not_an_error(tmp_path):
    with running(tmp_path / "board.db", tmp_path / "logs") as b:
        _claim(b, tmp_path, "agent")
    db = tmp_path / "board.db"
    _release(tmp_path, "agent", Do(lambda: sql_changes(db, "DELETE FROM identities")))
    assert "release_claim" not in _audit(tmp_path)  # this session released nothing


def test_release_frees_the_names_around_it_too(tmp_path):
    with running(tmp_path / "board.db", tmp_path / "logs") as b:
        _claim(b, tmp_path, "Stefan")
        with pytest.raises(Forbidden, match=r"too close to the claimed name \WStefan\W"):
            b.post("proj", "t", "stefan", "a look-alike")
        b.server.stop()
        _release(tmp_path, "Stefan")
        b.server.start()
        b.post("proj", "t", "stefan", "free again")
        _claim(b, tmp_path, "Stefn")


def test_tui_releases_a_name_claim_only_on_y(populated, tmp_path):
    with running(populated, tmp_path / "logs") as b:
        _claim(b, tmp_path, "alice")
        bob = _claim(b, tmp_path, "bob")
    run = drive_tui(_env(tmp_path), [
        Pick("-- name claims"),
        Pick("alice  ["), Wait(RELEASE), b"n",  # alice, declined
        Pick("bob  ["), Wait(RELEASE), b"y",  # bob, released
        Wait("name claims (q back)"), b"q", b"q"])
    assert run.code == 0
    assert run.seen("-- name claims (2)")
    assert [n for (n,) in sql(populated, "SELECT name FROM identities ORDER BY name")] == ["alice"]
    assert _audit(tmp_path).endswith(f"release_claim bob {bob.fingerprint} (tofu)\n")
