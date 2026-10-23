"""Archiving a thread message or a queue item on message board v2: the
owner key's, reversible, hidden from reads, `items`, `brief` and watches
unless asked for. See board_harness."""

import os

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    Pick, Wait, board, cli, drive_tui, isolated, keyring, refusal, running, sql, team, watching,
)

pytestmark = pytest.mark.usefixtures("isolated")


@pytest.fixture
def desk(board, team):
    """team, plus a queue rev/q that alice owns, and the owner's key."""
    team.alice.create("rev/q", "queue")
    team.key = team.owner.key
    return team


def archive(key, *args):
    return cli("archive", *args, key=key)


def bodies(run):
    return [line["body"] for line in run.lines if "body" in line]


# --- the commands ---------------------------------------------------------------------------

def test_archive_hides_a_message_from_reads_and_unarchive_brings_it_back(board, desk):
    a = desk.alice
    keep, noise, last = a.post("rev/log", "keep"), a.post("rev/log", "noise"), a.post("rev/log", "last")
    done = archive(desk.key, noise, "--reason", "off topic").ok().result
    assert list(done) == ["type", "id", "event", "archived"]
    assert (done["type"], done["event"], done["archived"]) == ("archived", noise, True)
    assert done["id"] > last

    default = cli("read", "rev/log", key=desk.key).ok()
    assert [m["id"] for m in default.of("message")] == [keep, last]
    assert all("archived" not in m for m in default.of("message"))
    assert default.of("cursor")[0]["earlier"] == 0
    everything = cli("read", "rev/log", "--all", "--archived", key=desk.key).ok()
    assert [(m["id"], m.get("archived")) for m in everything.of("message")] == \
        [(keep, None), (noise, True), (last, None)]
    # the listing counts what shows
    [thread] = [c for c in board.children("rev") if c["path"] == "rev/log"]
    assert (thread["messages"], thread["last_id"]) == (2, last)

    # by id it is still there, marked; the archive event says what it did
    shown = cli("show", noise, key=desk.key).ok().result
    assert (shown["type"], shown["body"], shown["archived"]) == ("message", "noise", True)
    event = cli("show", done["id"], key=desk.key).ok().result
    assert (event["type"], event["event"], event["ref"], event["body"]) == \
        ("archive", "node_archived", noise, "off topic")
    assert cli("show", keep).ok().result.get("archived") is None

    undone = cli("unarchive", noise, key=desk.key).ok().result
    assert (undone["type"], undone["event"], undone["archived"]) == ("archived", noise, False)
    assert undone["id"] > done["id"], "event ids are never reused"
    assert [m["id"] for m in cli("read", "rev/log").ok().of("message")] == [keep, noise, last]
    assert "archived" not in cli("show", noise).ok().result


def test_archiving_is_idempotent_and_writes_nothing_when_nothing_changes(board, desk):
    m = desk.alice.post("rev/log", "hi")
    head = board.head_id()
    nothing = cli("unarchive", m, key=desk.key).ok().result
    assert (nothing["id"], nothing["event"], nothing["archived"]) == (None, m, False)
    assert board.head_id() == head
    first = archive(desk.key, m).ok().result
    head = board.head_id()
    assert archive(desk.key, m).ok().result == first
    assert board.head_id() == head
    off = cli("unarchive", m, key=desk.key).ok().result
    head = board.head_id()
    assert cli("unarchive", m, key=desk.key).ok().result == off
    assert board.head_id() == head


def test_only_the_owner_key_archives_and_what_cannot_be_archived_is_refused(board, desk, keyring):
    a = desk.alice
    m = a.post("rev/log", "hi")
    item = desk.bob.ask("rev/q", "q")
    answer = a.answer(item)["id"]
    a.create("rev/state", "note", body="v1")
    version = a.set_note("rev/state", "v2", base_version=1)["id"]
    head = board.head_id()
    for who in (a, desk.bob):
        run = archive(who.key, m)
        assert (run.code, run.out) == (1, "") and run.last_err.startswith("board: forbidden: "), run
        assert cli("unarchive", m, key=who.key).code == 1
        assert archive(who.key, 99999).last_err.startswith("board: forbidden: ")  # no existence leak
    with refusal(403, "forbidden"):
        a.write("archive", event=m, archived="1", reason="")
    assert board.head_id() == head and "archived" not in board.event(m)

    assert archive(desk.key, 99999).last_err.startswith("board: not-found: ")
    for target, why in ((version, "only messages and queue items"), (answer, f"archive the item (#{item})"),
                        (board.node("rev/log")["node"]["created"], "only messages and queue items")):
        run = archive(desk.key, target)
        assert run.last_err.startswith("board: bad-request: ") and why in run.last_err, run
    registration = board.register(keyring["newbie"], "newbie")["item"]
    head = board.head_id()
    run = archive(desk.key, registration)
    assert run.last_err.startswith("board: bad-request: ") and "registration" in run.last_err, run
    assert archive(desk.key, m, "--reason", "x" * 201).last_err.startswith("board: bad-request: ")
    assert board.head_id() == head
    # no key at all
    assert archive(None, m).code == 3


def test_the_endpoint_and_the_signed_field_must_agree(board, desk):
    m = desk.alice.post("rev/log", "hi")
    for route, archived in (("archive", "0"), ("unarchive", "1"), ("archive", "yes")):
        body = board.signed("archive", desk.key, {"event": m, "archived": archived, "key": "k-" + route + archived})
        with refusal(400, "bad-request"):
            board._ok(board.send(route, body))
    assert "archived" not in board.event(m)
    assert board.owner.archive(m, "a")["archived"] is True
    assert board.event(m)["archived"] is True
    assert board.owner.unarchive(m)["archived"] is False


# --- items --------------------------------------------------------------------------------

def test_archiving_an_item_hides_the_filing_and_its_resolution(board, desk):
    a, b = desk.alice, desk.bob
    open_ = b.ask("rev/q", "still open")
    done = b.ask("rev/q", "answered")
    answer = a.answer(done, "the answer")["id"]
    assert archive(desk.key, open_, "--reason", "dup").ok().result["archived"] is True
    assert archive(desk.key, done).ok().result["archived"] is True

    assert cli("read", "rev/q", "--state", "all").ok().of("item") == []
    assert cli("items", "--state", "all", "--owner", "alice").ok().lines == []
    assert cli("items", "--asked-by", "bob").ok().lines == []
    assert board.queue_items("rev/q", state="all") == []
    [queue] = [c for c in board.children("rev") if c["path"] == "rev/q"]
    assert queue["open"] == 0
    assert cli("brief", "rev/q", key=a.key).ok().of("item") == []
    assert cli("brief", "rev/q", key=b.key).ok().of("item") == []

    shown = cli("read", "rev/q", "--state", "all", "--archived").ok().of("item")
    assert [(i["id"], i["archived"]) for i in shown] == [(open_, True), (done, True)]
    assert shown[1]["resolution"]["id"] == answer
    listed = cli("items", "--state", "all", "--owner", "alice", "--archived").ok().of("item")
    assert [i["id"] for i in listed] == [open_, done]

    # by id: the item, and its resolution, marked
    assert cli("show", done).ok().result["archived"] is True
    resolution = cli("show", answer).ok().result
    assert (resolution["type"], resolution["archived"]) == ("resolution", True)

    assert cli("unarchive", done, key=desk.key).ok().result["archived"] is False
    back = cli("read", "rev/q", "--state", "all").ok().of("item")
    assert [(i["id"], i.get("archived")) for i in back] == [(done, None)]
    assert "archived" not in cli("show", answer).ok().result
    assert cli("items", "--asked-by", "bob").ok().lines == []  # the open one is still archived
    cli("unarchive", open_, key=desk.key).ok()
    assert [i["id"] for i in cli("items", "--asked-by", "bob").ok().of("item")] == [open_]


def test_an_archived_item_or_message_refuses_writes(board, desk):
    a, b = desk.alice, desk.bob
    item = b.ask("rev/q", "q")
    archive(desk.key, item).ok()
    for run in (cli("answer", item, "x", key=a.key), cli("decline", item, "x", key=desk.key),
                cli("cancel", item, key=b.key)):
        assert run.code == 1 and run.last_err.startswith(f"board: archived: #{item} is archived"), run
        assert f"board unarchive {item}" in run.last_err
    with refusal(409, "archived") as r:
        a.answer(item)
    assert r.value.body["id"] == item
    assert board.item(item)["item"]["state"] == "open"
    cli("unarchive", item, key=desk.key).ok()
    cli("answer", item, "now", key=a.key).ok()

    # an answer points at a message that shows
    second = b.ask("rev/q", "again")
    m = a.post("rev/log", "details")
    archive(desk.key, m).ok()
    with refusal(409, "archived"):
        a.answer(second, ref=m)


# --- watching -------------------------------------------------------------------------------

def test_a_watch_does_not_announce_the_archived_and_hears_the_archive(board, desk):
    a, b = desk.alice, desk.bob
    since = board.head_id()
    seen = a.post("rev/log", "seen")
    noise = a.post("rev/log", "never announced")
    archived_before = archive(desk.key, noise).ok().result["id"]
    item = b.ask("rev/q", "q")
    a.answer(item)
    archive(desk.key, item).ok()

    with watching("rev/log", "--since", since, key=b.key) as w:
        first, second = w.events(2)
        assert (first["event"], first["id"]) == ("message", seen)
        assert (second["event"], second["id"], second["ref"]) == ("node_archived", archived_before, noise)
        assert second["summary"] == f"archived message #{noise} in rev/log"
        assert second["why"] == ["path"]
        assert w.nothing_for(0.5)
        # unarchiving is a doorbell of its own, and the message is not announced again
        back = cli("unarchive", noise, key=desk.key).ok().result["id"]
        bell = w.event()
        assert (bell["event"], bell["id"], bell["ref"]) == ("node_unarchived", back, noise)
        assert w.nothing_for(0.5)

    # the queue's watchers hear the item go, not its filing or answer
    with watching("rev/q", "--since", since, key=b.key) as w:
        bell = w.event()
        assert (bell["event"], bell["ref"], bell["item"]) == ("node_archived", item, item)
        assert bell["summary"] == f"archived item #{item} in rev/q"
        assert w.nothing_for(0.5)
    with watching("--item", item, "--since", since, key=b.key) as w:
        assert w.event()["event"] == "node_archived" and w.nothing_for(0.5)

    # a container's watcher heard of no message, so is not told one went
    page = board.poll(since=since, path="rev")
    assert [e["event"] for e in page["events"] if e["event"] in ("node_archived", "node_unarchived")] == []
    assert board.poll(since=since, path="rev/log")["events"][-1]["ref"] == noise


def test_an_archived_message_that_was_never_announced_stays_quiet_to_mentions(board, desk):
    since = board.head_id()
    m = desk.alice.post("rev/log", "hey @bob")
    archive(desk.key, m).ok()
    assert board.poll(since=since, mention="bob")["events"] == []


def test_event_ids_are_never_reused_by_an_archive_or_an_undo(board, desk):
    m = desk.alice.post("rev/log", "one")
    ids = [m]
    for _ in range(2):
        ids.append(archive(desk.key, m).ok().result["id"])
        ids.append(cli("unarchive", m, key=desk.key).ok().result["id"])
    assert ids == sorted(set(ids))
    assert desk.alice.post("rev/log", "two") > ids[-1]


# --- the operator's tool -----------------------------------------------------------------------

def test_board_admin_still_lists_an_archived_message_and_deleting_it_takes_its_archive_events(tmp_path, keyring):
    with running(tmp_path / "board-v2.db", tmp_path / "logs", keyring["owner"]) as b:
        alice = b.trusted(keyring["alice"], "alice")
        b.owner.create("proj", "container")
        alice.create("proj/chatter", "thread")
        kept = alice.post("proj/chatter", "kept")
        noise = alice.post("proj/chatter", "noise")
        archive_event = b.owner.archive(noise)["id"]
    env = {**os.environ, "BOARD_DB": str(tmp_path / "board-v2.db"), "BOARD_ADMIN_SERVICE": "",
           "BOARD_ADMIN_LOG": str(tmp_path / "audit.log")}
    run = drive_tui(env, [Wait("_board"), Pick("proj"), Pick("chatter"), Wait("[archived]"),
                          Pick(f"#{noise}"), b"d", Wait("Delete permanently? [y/N]"), b"y",
                          b"q", b"q", b"q", b"q"])
    assert run.code == 0 and run.seen("[archived]")
    left = {i for (i,) in sql(tmp_path / "board-v2.db", "SELECT id FROM events WHERE id IN (?,?,?)",
                              kept, noise, archive_event)}
    assert left == {kept}
