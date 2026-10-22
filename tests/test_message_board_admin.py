"""board-admin v2 (the deleting TUI) and board-server share one SQLite file
and must not disagree about it. These tests run the two programs against
the same database: the TUI driven through a pty, its handling of the user
service through a stand-in systemctl that runs a real board-server, and
admin deletions between two server runs.

The v2 TUI is specified by what it does (browse the tree, delete a branch,
delete a message nothing points to, keep an audit log, refuse a file that
is not a v2 board), not by its screens. What this suite assumes of the
screens is in the constants and helpers right below, so that they can be
matched to the program in one place:

- every list is drawn one row per entry, a row naming the node's path (or
  its last segment) or, in a thread, the message's `#id`; a list starts
  on its first row, under a title row; j/k move, Enter opens, q goes back
  and quits at the top;
- `d` on a selected node asks for its full path to be typed, then deletes
  it and everything under it;
- Enter on a message shows it with the v1 prompt `Delete permanently?
  [y/N]` (or says why it cannot be deleted), and `y` deletes it;
- the audit log has v1's shape: `<UTC stamp> delete_message #<id> <path>
  from=<name>`, and `<UTC stamp> delete_branch <path> ...`.
"""

import json
import os
import re
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    Board, Do, FakeSystemd, Kill, Pick, Select, Server, Wait, admin, digest, drive_tui, isolated,
    keyring, make_db, make_v1_db, owner_env, request, running, sql,
)

pytestmark = pytest.mark.usefixtures("isolated")

DB = "board-v2.db"
# Always on the first screen: the board's own container is in every v2 file.
TOP = "_board"
DELETE_ONE = "Delete permanently? [y/N]"
STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def back_out(n=5):
    """Enough q's to leave whatever screen a session is on, and quit."""
    return [b"q"] * n


def delete_branch(path, typed=None):
    """Select `path` in the list on screen, ask to delete it, type the
    confirmation."""
    return [Select(path.rsplit("/", 1)[-1]), b"d", f"{typed or path}\r".encode()]


def _env(tmp_path, **extra):
    """board-admin on the test's database, with no service to manage."""
    return {**os.environ, "BOARD_DB": str(tmp_path / DB), "BOARD_ADMIN_SERVICE": "",
            "BOARD_ADMIN_LOG": str(tmp_path / "audit.log"), **extra}


def _audit(tmp_path):
    log = tmp_path / "audit.log"
    return log.read_text() if log.exists() else ""


def _events(db, *ids):
    """Which of these event ids are still in the file."""
    marks = ",".join("?" * len(ids))
    return sorted(i for (i,) in sql(db, f"SELECT id FROM events WHERE id IN ({marks})", *ids))


def _populate(b, keyring):
    """Two branches; in proj, a message a note's `through` points to, one an
    answer's `ref` points to, and one nothing points to."""
    alice = b.trusted(keyring["alice"], "alice")
    bob = b.trusted(keyring["bob"], "bob")
    b.owner.create("proj", "container")
    b.owner.create("other", "container")
    alice.create("proj/design", "thread")
    alice.create("proj/chatter", "thread")
    alice.create("other/general", "thread")
    through = alice.post("proj/design", "design one")
    ref = alice.post("proj/design", "design two")
    free = alice.post("proj/chatter", "noise")
    elsewhere = alice.post("other/general", "elsewhere")
    alice.create("proj/summary", "note", body="v1")
    alice.set_note("proj/summary", "the design so far", base_version=1, through=through)
    alice.create("proj/asks", "queue")
    alice.answer(bob.ask("proj/asks", "where is the design?"), "see there", ref=ref)
    return SimpleNamespace(through=through, ref=ref, free=free, elsewhere=elsewhere,
                           head=b.head_id())


@pytest.fixture
def populated(tmp_path, keyring):
    """A board database with two branches in it, its server stopped again:
    an admin session happens between two server runs."""
    with running(tmp_path / DB, tmp_path / "logs", keyring["owner"]) as b:
        board = _populate(b, keyring)
    board.db = tmp_path / DB
    return board


# --- what the TUI shows, and what its deletions do --------------------------------------

def test_the_tree_can_be_browsed(populated, tmp_path):
    run = drive_tui(_env(tmp_path), [Wait(TOP), Pick("proj"), Wait("design"), *back_out()])
    assert run.code == 0
    for shown in ("other", "chatter", "summary", "asks"):
        assert run.seen(shown), shown
    assert not run.term.alt  # the terminal is given back


def test_deleting_a_branch_deletes_everything_under_it_and_nothing_else(populated, tmp_path,
                                                                         keyring):
    run = drive_tui(_env(tmp_path), [Wait(TOP), *delete_branch("proj"), Wait(TOP), *back_out()])
    assert run.code == 0
    db = populated.db
    assert sql(db, "SELECT path FROM nodes WHERE path = 'proj' OR path LIKE 'proj/%'") == []
    assert _events(db, populated.through, populated.ref, populated.free, populated.elsewhere) == \
        [populated.elsewhere]
    assert re.search(rf"^{STAMP} delete_branch proj\b", _audit(tmp_path), re.M)
    with running(db, tmp_path / "logs", keyring["owner"]) as b:
        assert b.get("/n/proj").status == 404
        assert [m["body"] for m in b.messages("other/general")] == ["elsewhere"]
        assert b.head_id() >= populated.head  # ids never go back
        b.owner.create("proj", "container")
        again = b.as_key(keyring["alice"], "alice")
        again.create("proj/design", "thread")
        assert again.post("proj/design", "after the purge") > populated.head


def test_a_wrong_confirmation_deletes_nothing(populated, tmp_path):
    before = digest(populated.db)
    run = drive_tui(_env(tmp_path), [Wait(TOP), *delete_branch("proj", typed="yes"), Wait(TOP),
                                     *back_out()])
    assert run.code == 0
    assert sql(populated.db, "SELECT COUNT(*) FROM nodes WHERE path LIKE 'proj%'") != [(0,)]
    assert _events(populated.db, populated.free) == [populated.free]
    assert digest(populated.db) == before and _audit(tmp_path) == ""


def test_a_message_nothing_points_to_can_be_deleted(populated, tmp_path):
    run = drive_tui(_env(tmp_path), [Wait(TOP), Pick("proj"), Pick("chatter"),
                                     Pick(f"#{populated.free}"), b"d", Wait(DELETE_ONE), b"y",
                                     *back_out()])
    assert run.code == 0
    assert _events(populated.db, populated.free, populated.through, populated.ref) == \
        [populated.through, populated.ref]
    assert re.search(rf"^{STAMP} delete_message #{populated.free} proj/chatter from=alice$",
                     _audit(tmp_path), re.M)


@pytest.mark.parametrize("which", ["through", "ref"])
def test_a_message_a_note_or_an_answer_points_to_is_kept(populated, tmp_path, which):
    """A note's `through` and an answer's `ref` must keep pointing at
    something: such a message is not deleted, whatever is pressed."""
    mid = getattr(populated, which)
    before = digest(populated.db)
    run = drive_tui(_env(tmp_path), [Wait(TOP), Pick("proj"), Pick("design"), Pick(f"#{mid}"),
                                     b"d", b"y", *back_out()])
    assert run.code == 0
    assert _events(populated.db, mid) == [mid]
    assert digest(populated.db) == before
    assert "delete_message" not in _audit(tmp_path)


def test_the_audit_log_records_every_deletion(populated, tmp_path):
    log = tmp_path / "state" / "admin-deletions.log"  # its directory does not exist yet
    env = _env(tmp_path, BOARD_ADMIN_LOG=str(log))
    for steps in ([Pick("proj"), Pick("chatter"), Pick(f"#{populated.free}"), b"d", Wait(DELETE_ONE),
                   b"y"],
                  delete_branch("other")):
        assert drive_tui(env, [Wait(TOP), *steps, *back_out()]).code == 0
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert re.fullmatch(rf"{STAMP} delete_message #{populated.free} proj/chatter from=alice",
                        lines[0])
    assert re.match(rf"{STAMP} delete_branch other\b", lines[1])


def test_a_deletion_the_audit_log_cannot_take_is_named(populated, tmp_path):
    """The deletion is done before its entry is written. If that write
    fails, the error must name what went unrecorded."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        run = drive_tui(_env(tmp_path, BOARD_ADMIN_LOG=str(locked / "audit.log")), [
            Wait(TOP), Pick("proj"), Pick("chatter"), Pick(f"#{populated.free}"),
            b"d", Wait(DELETE_ONE), b"y", *back_out()], cols=400)
    finally:
        locked.chmod(0o700)
    assert run.code == 1
    assert _events(populated.db, populated.free) == []
    assert run.seen(f'"delete_message #{populated.free} proj/chatter from=alice" is done but not'
                    " in the audit log")


def test_a_watcher_cursor_from_before_a_deletion_gets_exactly_the_next_post(tmp_path, keyring):
    db = tmp_path / DB
    with running(db, tmp_path / "logs", keyring["owner"]) as b:
        alice = b.trusted(keyring["alice"], "alice")
        b.owner.create("proj", "container")
        alice.create("proj/chatter", "thread")
        alice.post("proj/chatter", "one")
        cursor = alice.post("proj/chatter", "two")  # a watcher has seen up to here
        b.server.stop()
        run = drive_tui(_env(tmp_path), [Wait(TOP), Pick("proj"), Pick("chatter"),
                                         Pick(f"#{cursor}"), b"d", Wait(DELETE_ONE), b"y", *back_out()])
        assert run.code == 0 and _events(db, cursor) == []
        b.server.start(**owner_env(keyring["owner"]))
        assert b.head_id() >= cursor  # `brief` and `watch --since head` assume ids only grow
        nxt = alice.post("proj/chatter", "three")
        assert [e["id"] for e in b.poll(cursor, path="proj/chatter")["events"]] == [nxt]


# --- which file it opens --------------------------------------------------------------------

def test_a_v1_database_is_refused_before_anything_touches_it(tmp_path):
    """v1's board-admin would have migrated v1 tables into whatever it
    opened; v2's reads `meta` first, and refuses before it stops the
    board."""
    db = make_v1_db(tmp_path / DB)
    before = digest(db)
    systemd = FakeSystemd(tmp_path, active=True)
    r = admin(_env(tmp_path, PATH=systemd.path(), BOARD_ADMIN_SERVICE="message-board"))
    assert r.code == 1 and "v1" in r.err
    assert digest(db) == before
    assert systemd.calls() == []


def test_a_file_that_is_no_board_is_refused_unchanged(tmp_path):
    db = tmp_path / DB
    make_db(db, "CREATE TABLE notes(x TEXT); INSERT INTO notes VALUES('someone else''s');")
    before = digest(db)
    r = admin(_env(tmp_path))
    assert r.code == 1 and r.err
    assert digest(db) == before
    assert sql(db, "SELECT name FROM sqlite_master WHERE type='table'") == [("notes",)]


def test_a_missing_database_is_refused_not_created(tmp_path):
    target = tmp_path / "nope.db"
    systemd = FakeSystemd(tmp_path, active=True)
    r = admin(_env(tmp_path, BOARD_DB=str(target), PATH=systemd.path(),
                   BOARD_ADMIN_SERVICE="message-board"))
    assert r.code == 1 and "no database" in r.err
    assert not target.exists()  # it never opened one
    assert systemd.calls() == []  # and the service was never touched


def _refused_db_path(tmp_path, **env):
    """The database path board-admin looked for and did not find."""
    systemd = FakeSystemd(tmp_path, active=True)
    base = {k: v for k, v in os.environ.items()
            if k not in ("BOARD_DB", "XDG_STATE_HOME", "HOME")}
    r = admin({**base, "PATH": systemd.path(), "BOARD_ADMIN_SERVICE": "message-board", **env})
    assert r.code == 1 and systemd.calls() == []  # refused before touching the service
    return re.search(r"no database at (.*) \(set BOARD_DB to point elsewhere\)", r.err)[1]


def test_the_database_path_defaults_to_where_the_service_keeps_it(tmp_path):
    # Every case names a directory that does not exist, so none can reach a
    # real board's database.
    assert _refused_db_path(tmp_path, BOARD_DB="/nonexistent/y.db") == "/nonexistent/y.db"
    assert _refused_db_path(tmp_path, XDG_STATE_HOME="/nonexistent/st") == \
        f"/nonexistent/st/message-board/{DB}"
    assert _refused_db_path(tmp_path, HOME="/nonexistent/h") == \
        f"/nonexistent/h/.local/state/message-board/{DB}"
    assert _refused_db_path(tmp_path, BOARD_DB="", XDG_STATE_HOME="", HOME="/nonexistent/h") == \
        f"/nonexistent/h/.local/state/message-board/{DB}"  # empty is unset


def test_the_default_database_path_matches_the_deployed_service(tmp_path):
    """Drift guard: if the unit's state dir or BOARD_DB moves, board-admin
    must follow -- otherwise it opens the wrong file (or the v1 one)."""
    ai = (Path(__file__).parent.parent / "home-manager" / "ai.nix").read_text()
    assert 'StateDirectory = "message-board"' in ai
    assert f"BOARD_DB=%S/message-board/{DB}" in ai


# --- taking the service down and bringing it back -----------------------------------------

@pytest.fixture
def service(tmp_path, keyring):
    """The board as a user service: a real board-server on the test's
    database. FakeSystemd(server=...) makes `systemctl stop` stop it."""
    server = Server(tmp_path / DB, tmp_path / "logs").start(**owner_env(keyring["owner"]))
    b = Board(server, keyring["owner"])
    b.owner.create("proj", "container")
    b.owner.create("proj/log", "thread")
    b.owner.post("proj/log", "live data")
    yield server
    server.stop()


def _service_env(tmp_path, systemd, server):
    return _env(tmp_path, BOARD_ADMIN_SERVICE="message-board", PATH=systemd.path(),
                BOARD_URL=server.url)


def test_a_running_service_is_stopped_for_the_session_then_restarted(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    during = []
    run = drive_tui(_service_env(tmp_path, systemd, service), [
        Wait(TOP), Do(lambda: during.extend(systemd.calls())), *back_out()])
    assert run.code == 0
    assert during == ["is-active", "stop"]  # the session runs with the board down
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert run.seen("stopping message-board") and run.seen("restarting message-board")


def test_the_service_comes_back_even_if_the_tui_crashes(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    run = drive_tui(_service_env(tmp_path, systemd, service), [
        Wait(TOP),
        Do(lambda: sql(tmp_path / DB, "DROP TABLE nodes")),  # the next query fails
        Pick("proj")])
    assert run.code != 0
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert run.seen("no such table")  # the cause is shown, not swallowed
    assert not run.term.alt  # and the terminal is given back


def test_the_service_comes_back_on_ctrl_c(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    run = drive_tui(_service_env(tmp_path, systemd, service), [Wait(TOP), b"\x03"])
    assert run.code != 0
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert not run.term.alt


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_the_service_comes_back_when_the_session_is_killed(tmp_path, service, signum):
    systemd = FakeSystemd(tmp_path, active=True, server=service)
    run = drive_tui(_service_env(tmp_path, systemd, service), [Wait(TOP), Kill(signum)])
    assert run.code != 0
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert not run.term.alt


def test_a_service_that_was_not_running_is_left_alone(populated, tmp_path):
    systemd = FakeSystemd(tmp_path, active=False)  # and nothing answers on BOARD_URL
    run = drive_tui(_env(tmp_path, BOARD_ADMIN_SERVICE="message-board", PATH=systemd.path()),
                    [Wait(TOP), *back_out()])
    assert run.code == 0
    assert systemd.calls() == ["is-active"]  # no stop, and crucially no start afterwards


def test_an_unmanaged_server_is_refused_without_touching_anything(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=False, server=service)  # systemd says down...
    r = admin(_service_env(tmp_path, systemd, service))  # ...but something answers the port
    assert r.code == 1 and "unmanaged" in r.err
    assert systemd.calls() == ["is-active"]
    answer = request(service.url + "/n/proj/log").json()
    assert [m["body"] for m in answer["messages"]] == ["live data"]


def test_a_port_still_answering_after_stop_restarts_the_service_it_stopped(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service, stop_works=False)
    r = admin(_service_env(tmp_path, systemd, service))
    assert r.code == 1 and "still answering" in r.err
    assert systemd.calls() == ["is-active", "stop", "start"]


def test_a_stop_that_reports_failure_still_restarts_the_service(tmp_path, service):
    """systemctl can fail after systemd has begun the stop (a Ctrl-C, its
    wait timing out), and systemd finishes it anyway."""
    systemd = FakeSystemd(tmp_path, active=True, server=service, stop_fails=True)
    r = admin(_service_env(tmp_path, systemd, service))
    assert r.code == 1 and "canceled" in r.err
    assert systemd.calls() == ["is-active", "stop", "start"]
    assert json.loads(systemd.state.read_text())["active"] is True


def test_a_failed_restart_warns_instead_of_raising(tmp_path, service):
    systemd = FakeSystemd(tmp_path, active=True, server=service, start_fails=True)
    run = drive_tui(_service_env(tmp_path, systemd, service), [Wait(TOP), *back_out()],
                    cols=400)  # the warning on one line
    assert run.code == 0
    assert run.seen("systemctl --user start message-board")


def test_service_handling_can_be_disabled(populated, tmp_path):
    systemd = FakeSystemd(tmp_path, active=True)
    run = drive_tui(_env(tmp_path, BOARD_ADMIN_SERVICE="", PATH=systemd.path()),
                    [Wait(TOP), *back_out()])
    assert run.code == 0
    assert systemd.calls() == []


def test_admin_refuses_while_the_server_is_up_and_does_not_disturb_it(tmp_path, service):
    r = admin(_env(tmp_path, BOARD_URL=service.url))  # no service: only the port probe applies
    assert r.code == 1 and "unmanaged" in r.err
    assert service.proc.poll() is None
    assert request(service.url + "/healthz").status == 200
