"""An agent's (re)start on message board v2: `board brief` for the notes
on its paths and its open items, then `board watch` from brief's cursor for
doorbells, filtered on the server; `board wait` for one item's outcome.
See board_harness."""

import itertools
import time

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    IDENTITY_EXIT, Process, board, cli, isolated, keyring, owner_env, proxy, refusal, run_command_line,
    sql_many, team, wait_until, watching,
)

pytestmark = pytest.mark.usefixtures("isolated")

# What every doorbell carries.
EVENT = ["type", "id", "ts", "event", "path", "from", "signed_by", "revoked_at", "item", "state",
         "version", "why", "summary"]


def bells(events):
    return [(e["event"], e["path"], sorted(e["why"])) for e in events]


@pytest.fixture
def swarm(board, team, keyring):
    """team, plus two agents of the reversing swarm and their home:
    rev/emu, with a log and aspfuzz's queue."""
    team.aspfuzz = board.trusted(keyring["aspfuzz"], "aspfuzz")
    team.qemu = board.trusted(keyring["qemu-psp"], "qemu-psp")
    team.alice.create("rev/emu", "container")
    team.alice.create("rev/emu/log", "thread")
    team.aspfuzz.create("rev/emu/aspfuzz", "queue")
    return team


# --- brief ---------------------------------------------------------------------------

def test_brief_loads_the_notes_on_the_paths_and_the_open_items_then_a_cursor(board, swarm):
    """The startup read: what the agent's paths say (their containers'
    listings and notes, root first), what it owes, what it waits for, and
    a watch ready to arm. Not threads, not notes off its path."""
    o, a, b, asp, qemu = swarm.owner, swarm.alice, swarm.bob, swarm.aspfuzz, swarm.qemu
    o.create("rev/charter", "note", body="CHARTER")
    a.create("rev/state", "note", body="STATE")
    a.create("rev/emu/state", "note", body="EMU STATE")
    a.post("rev/emu/log", "THREAD BODY")
    a.create("rev/kb", "container")
    a.create("rev/kb/notes", "note", body="KB NOTES")
    o.create("other", "container")
    qemu.create("other/qemu", "queue")
    asp.create("other/aspfuzz", "queue")
    owned = {qemu.ask("rev/emu/aspfuzz", "OWNED HERE"), b.ask("other/aspfuzz", "OWNED ELSEWHERE")}
    asked = {asp.ask("other/qemu", "ASKED")}
    asp.answer(qemu.ask("rev/emu/aspfuzz", "ANSWERED ALREADY"))
    head = board.head_id()

    run = cli("brief", "rev/emu/log", "rev/emu/aspfuzz", key=asp.key).ok()

    def group(line):
        if line["type"] in ("child", "note"):
            return line["type"], line["path"].rsplit("/", 1)[0]
        return (line["type"], line.get("role"))

    groups = [(kind, {line.get("path") if kind[0] in ("child", "note") else line.get("id")
                      for line in lines})
              for kind, lines in ((k, list(g)) for k, g in itertools.groupby(run.lines, group))]
    assert groups[:-1] == [
        (("child", "rev"), {"rev/log", "rev/charter", "rev/state", "rev/emu", "rev/kb"}),
        (("note", "rev"), {"rev/charter", "rev/state"}),
        (("child", "rev/emu"), {"rev/emu/log", "rev/emu/aspfuzz", "rev/emu/state"}),
        (("note", "rev/emu"), {"rev/emu/state"}),
        (("item", "owner"), owned),
        (("item", "asker"), asked),
    ]
    notes = {n["path"]: n["body"] for n in run.of("note")}
    assert notes == {"rev/charter": "CHARTER", "rev/state": "STATE", "rev/emu/state": "EMU STATE"}
    assert {i["body"] for i in run.of("item")} == {"OWNED HERE", "OWNED ELSEWHERE", "ASKED"}
    for absent in ("KB NOTES", "THREAD BODY", "ANSWERED ALREADY"):
        assert absent not in run.out
    assert run.lines[-1] == {
        "type": "cursor", "since": head,
        "watch": f"board watch rev rev/emu rev/emu/log rev/emu/aspfuzz --me --since {head}"}
    assert len(run.out) < 8 * 1024  # a few KB, not the backlog
    # the API's brief: the same, structured
    api = board.brief("rev/emu/log", "rev/emu/aspfuzz", as_="aspfuzz")
    # the API hands out the paths to watch; the CLI builds the command line from them
    assert (api["head"], api["watch"]) == (head, ["rev", "rev/emu", "rev/emu/log", "rev/emu/aspfuzz"])
    assert [c["path"] for c in api["containers"]] == ["rev", "rev/emu"]
    assert {n["path"] for n in api["containers"][0]["notes"]} == {"rev/charter", "rev/state"}
    assert {c["path"] for c in api["containers"][1]["children"]} == \
        {"rev/emu/log", "rev/emu/aspfuzz", "rev/emu/state"}
    assert ({i["id"] for i in api["owned"]}, {i["id"] for i in api["asked"]}) == (owned, asked)
    # --as names the agent without its key
    # (the watch it hands out spells --me out as the name, which no key backs)
    named = cli("brief", "rev/emu/log", "rev/emu/aspfuzz", "--as", "aspfuzz").ok().lines
    assert named[:-1] == run.lines[:-1] and named[-1]["since"] == head
    assert named[-1]["watch"] == (
        f"board watch rev rev/emu rev/emu/log rev/emu/aspfuzz --mention aspfuzz --asked-by aspfuzz"
        f" --owner aspfuzz --not-from aspfuzz --since {head}")


def test_brief_loads_a_note_given_as_a_path_once(board, swarm):
    swarm.alice.create("rev/kb", "container")
    swarm.alice.create("rev/kb/notes", "note", body="KB NOTES")
    run = cli("brief", "rev/emu/log", "rev/kb/notes", key=swarm.aspfuzz.key).ok()
    assert [n["body"] for n in run.of("note") if n["path"] == "rev/kb/notes"] == ["KB NOTES"]


def test_brief_for_a_key_without_a_name_has_no_items(board, swarm, keyring):
    """A new agent can read meanwhile: the notes and listings, nothing of
    an identity it does not have yet, and one line saying why."""
    swarm.alice.create("rev/emu/state", "note", body="EMU STATE")
    swarm.qemu.ask("rev/emu/aspfuzz", "for aspfuzz, not for a stranger")
    for key in (keyring["stranger"], keyring["newbie"]):
        if key is keyring["newbie"]:
            board.register(key, "newbie")
        run = cli("brief", "rev/emu/log", key=key)
        assert run.code == 0 and run.err.count("\n") == 1, run
        assert run.of("item") == [] and run.lines[-1]["type"] == "cursor"
        assert [n["body"] for n in run.of("note")] == ["EMU STATE"]


def test_an_event_between_brief_and_watch_arrives_exactly_once(board, swarm):
    asp = swarm.aspfuzz
    cursor = cli("brief", "rev/emu/log", "rev/emu/aspfuzz", key=asp.key).ok().lines[-1]
    between = swarm.alice.post("rev/emu/log", "posted between brief and watch")
    watch = run_command_line(cursor["watch"], key=asp.key)
    try:
        bell = watch.event()
        assert (bell["id"], bell["why"]) == (between, ["path"])
        after = swarm.alice.post("rev/emu/log", "after")
        assert watch.event()["id"] == after
    finally:
        watch.stop()


# --- watch: what wakes it -----------------------------------------------------------------

def test_a_leaf_watch_hears_every_event_on_the_leaf(board, team):
    a, b = team.alice, team.bob
    b.create("rev/bob", "queue")
    with watching("rev/log", "rev/bob", "--since", board.head_id(), key=team.carol.key) as w:
        mid = a.post("rev/log", "hello\nmore")
        item = a.ask("rev/bob", "a question")
        answer = b.answer(item, "an answer")
        events = w.events(3)
    assert bells(events) == [("message", "rev/log", ["path"]), ("item_filed", "rev/bob", ["path"]),
                             ("item_answered", "rev/bob", ["path"])]
    message, filed, answered = events
    assert (message["id"], message["from"], message["summary"]) == (mid, "alice", "hello")
    assert (filed["id"], filed["item"], filed["state"]) == (item, item, "open")
    assert (answered["id"], answered["item"], answered["state"], answered["from"]) == \
        (answer["id"], item, "answered", "bob")


def test_a_doorbell_line_and_a_note_update(board, team):
    a = team.alice
    a.create("rev/state", "note", body="v1")
    with watching("rev/log", "rev/state", "--since", board.head_id(), key=team.bob.key) as w:
        mid = a.post("rev/log", "é" * 300)
        saved = a.set_note("rev/state", "v2", base_version=1)
        message, note = w.events(2)
    assert set(EVENT) <= set(message) and "body" not in message
    assert {k: message[k] for k in EVENT if k not in ("ts", "summary")} == {
        "type": "event", "id": mid, "event": "message", "path": "rev/log", "from": "alice",
        "signed_by": a.fingerprint, "revoked_at": None, "item": None, "state": None,
        "version": None, "why": ["path"]}
    assert message["summary"].startswith("é" * 190) and len(message["summary"]) <= 201
    assert (note["event"], note["id"], note["version"], note["summary"]) == \
        ("note_version", saved["id"], 2, "note updated: rev/state v2")


def test_a_container_watch_hears_only_its_direct_children_come_and_change(board, team):
    """Watching a container is structure, not content: children created
    and notes updated; to hear content, watch the leaf; to hear a
    sub-container's children, watch it too."""
    o, a = team.owner, team.alice
    o.create("rev/charter", "note", body="v1")
    a.create("rev/emu", "container")
    with watching("rev", "--since", board.head_id(), key=team.bob.key) as w:
        a.post("rev/log", "content is not structure")
        a.create("rev/new", "thread")
        a.create("rev/emu/deep", "thread")  # a grandchild
        saved = o.set_note("rev/charter", "v2", base_version=1)
        a.create("rev/emu/notes", "note", body="a grandchild's note")
        a.create("rev/last", "thread")
        events = w.events(3)
        assert w.nothing_for(1)
    assert bells(events) == [("node_created", "rev/new", ["child"]),
                             ("note_version", "rev/charter", ["child"]),
                             ("node_created", "rev/last", ["child"])]
    assert (events[1]["id"], events[1]["summary"]) == (saved["id"], "note updated: rev/charter v2")


def test_a_root_watch_hears_only_top_level_nodes_come(board, team):
    """`/` is the root container: watching it alone is a watch of a path,
    and it hears a new top-level container -- nothing posted anywhere, and
    nothing made further down."""
    o, a = team.owner, team.alice
    with watching("/", "--since", board.head_id(), key=team.bob.key) as w:
        a.post("rev/log", "content anywhere is not for the root")
        a.create("rev/emu", "container")  # not top level
        o.create("newtop", "container")
        o.create("newtop/log", "thread")
        o.create("last", "container")
        events = w.events(2)
        assert w.nothing_for(1)
    assert bells(events) == [("node_created", "newtop", ["child"]),
                             ("node_created", "last", ["child"])]


def test_a_mention_is_the_whole_name_anywhere_but_in_a_note(board, team):
    a, b = team.alice, team.bob
    b.create("rev/bob", "queue")
    a.create("rev/state", "note", body="v1")
    with watching("--mention", "carol", "--since", board.head_id(), key=b.key) as w:
        first = a.post("rev/log", "hi @carol, look")
        a.post("rev/log", "@carolyn is someone else")
        a.post("rev/log", "@Carol is not carol: names are case-sensitive")
        a.post("rev/log", "carol without the at")
        a.set_note("rev/state", "@carol in a note wakes nobody", base_version=1)
        item = a.ask("rev/bob", "@carol, an item")
        last = a.post("rev/log", "first line\n\nand then @carol.")
        events = w.events(3)
    assert [(e["id"], e["why"]) for e in events] == \
        [(first, ["mention"]), (item, ["mention"]), (last, ["mention"])]
    assert events[2]["summary"] == "first line"


def test_asked_by_owner_and_item_follow_items(board, team):
    a, b, c = team.alice, team.bob, team.carol
    b.create("rev/bob", "queue")
    c.create("rev/carol", "queue")
    with watching("--asked-by", "alice", "--since", board.head_id(), key=c.key) as w:
        mine = a.ask("rev/bob", "asked by alice")
        b.ask("rev/carol", "asked by bob")
        b.answer(mine, "for alice")
        events = w.events(2)
    assert [(e["event"], e["item"], e["why"]) for e in events] == \
        [("item_filed", mine, ["asked"]), ("item_answered", mine, ["asked"])]
    with watching("--owner", "carol", "--since", board.head_id(), key=b.key) as w:
        a.ask("rev/bob", "bob's queue")
        theirs = a.ask("rev/carol", "carol's queue")
        a.cancel(theirs)
        events = w.events(2)
    assert [(e["event"], e["item"], e["why"]) for e in events] == \
        [("item_filed", theirs, ["owner"]), ("item_cancelled", theirs, ["owner"])]
    watched, other = a.ask("rev/bob", "watched"), a.ask("rev/bob", "other")
    with watching("--item", watched, "--since", board.head_id(), key=c.key) as w:
        b.answer(other)
        b.decline(watched, "no")
        [event] = w.events(1)
    assert (event["event"], event["item"], event["state"], event["why"]) == \
        ("item_declined", watched, "declined", ["item"])


def test_not_from_subtracts_and_me_is_the_keys_own_name(board, team):
    a, b, c = team.alice, team.bob, team.carol
    b.create("rev/bob", "queue")
    c.create("rev/carol", "queue")
    with watching("rev/log", "--not-from", "alice", "--since", board.head_id(), key=c.key) as w:
        a.post("rev/log", "alice's own")
        bobs = b.post("rev/log", "bob's")
        assert w.event()["id"] == bobs
    with watching("--me", "--since", board.head_id(), key=b.key) as w:
        b.post("rev/log", "@bob talking to himself")  # --me skips bob's own
        for_bob = a.ask("rev/bob", "for bob")         # in bob's queue
        hey = a.post("rev/log", "hey @bob")           # mentions bob
        asked = b.ask("rev/carol", "bob asks carol")  # bob's own event
        answered = c.answer(asked, "carol answers")   # on bob's item
        events = w.events(3)
    assert [(e["id"], e["why"]) for e in events] == \
        [(for_bob, ["owner"]), (hey, ["mention"]), (answered["id"], ["asked"])]


def test_me_needs_a_bound_name(board, team, keyring):
    run = cli("watch", "--me", "--since", "0", key=keyring["stranger"], timeout=30)
    assert (run.code, run.out) == (IDENTITY_EXIT, "") and "not-registered" in run.err


def test_why_names_every_rule_that_matched(board, team):
    c = team.carol
    c.create("rev/carol", "queue")
    with watching("rev/carol", "--mention", "carol", "--owner", "carol", "--since",
                  board.head_id(), key=team.bob.key) as w:
        team.alice.ask("rev/carol", "@carol please")
        [event] = w.events(1)
    assert sorted(event["why"]) == ["mention", "owner", "path"]


# --- watch: the cursor ---------------------------------------------------------------------

def test_the_cursor_moves_past_what_the_watch_does_not_want(board, team, proxy):
    """The server filters: unwanted events never travel, and the next poll
    starts after them, so a busy board does not keep a quiet watch busy."""
    a = team.alice
    a.create("rev/noise", "thread")
    with watching("rev/log", "--since", board.head_id(), "--timeout", "1", key=team.carol.key) as w:
        noise = {a.post("rev/noise", f"noise {i}") for i in range(3)}
        last = board.head_id()
        wait_until(lambda: any(ex.route == "/poll" and ex.query.get("since") == str(last)
                               for ex in proxy.seen), "a poll from past the noise")
        signal = a.post("rev/log", "signal")
        assert w.event()["id"] == signal
    carried = {e["id"] for ex in proxy.seen if ex.route == "/poll" and ex.status == 200
               for e in ex.answer_json()["events"]}
    assert carried == {signal} and not noise & carried


def test_a_poll_answers_its_events_and_a_cursor(board, team):
    a = team.alice
    a.create("rev/noise", "thread")
    a.post("rev/noise", "unwanted")
    # a leaf's watch gets all its events, its creation included; the noise elsewhere never
    page = board.poll(0, path="rev/log")
    assert [(e["event"], e["path"]) for e in page["events"]] == [("node_created", "rev/log")]
    assert page["cursor"] == board.head_id()
    mid = a.post("rev/log", "wanted")
    page = board.poll(page["cursor"], path="rev/log")
    assert [e["id"] for e in page["events"]] == [mid] and page["cursor"] == board.head_id()


def test_a_full_poll_page_ends_its_cursor_at_its_last_event(board, team):
    """Past the page cap (500 events) the cursor is the last event sent,
    not the head, so the next page starts right after it."""
    a = team.alice
    a.create("rev/bulk", "thread")
    node = board.node_id("rev/bulk")
    sql_many(board.db, "INSERT INTO events(ts, type, node, actor, actor_fp, body)"
                       " VALUES('2026-10-22T00:00:00Z', 'message', ?, 'alice', ?, ?)",
             [(node, a.fingerprint, f"m{i}") for i in range(1000)])
    a.post("rev/log", "the head moves on")
    first = board.poll(0, path="rev/bulk")
    assert len(first["events"]) == 500 and first["cursor"] == first["events"][-1]["id"]
    second = board.poll(first["cursor"], path="rev/bulk")
    assert len(second["events"]) == 500 and second["cursor"] == second["events"][-1]["id"]
    # 1000 messages and the node's creation: one event is left for a third page
    third = board.poll(second["cursor"], path="rev/bulk")
    assert len(third["events"]) == 1 and third["cursor"] == board.head_id()
    assert [m["body"] for m in cli("read", "rev/bulk", "--all").ok().of("message")] == \
        [f"m{i}" for i in range(1000)]


def test_re_arming_from_the_last_printed_id_loses_nothing_and_repeats_nothing(board, team):
    a = team.alice
    a.create("rev/other", "thread")
    a.post("rev/other", "before the re-arm point, on the leaf added later")
    with watching("rev/log", "--since", board.head_id(), key=team.carol.key) as w:
        seen = a.post("rev/log", "one")
        last = w.event()["id"]
    assert last == seen
    missed = a.post("rev/log", "while no watch ran")
    newly = a.post("rev/other", "after the re-arm point, on the new leaf")
    with watching("rev/log", "rev/other", "--since", last, key=team.carol.key) as w:
        assert [e["id"] for e in w.events(2)] == [missed, newly]
        assert w.nothing_for(2)


def test_watch_starts_at_the_head_by_default(board, team, proxy):
    team.alice.post("rev/log", "already here")
    head = board.head_id()
    with watching("rev/log", key=team.carol.key) as w:
        wait_until(lambda: "/poll" in proxy.routes(), "a poll")
        new = team.alice.post("rev/log", "new")
        assert w.event()["id"] == new
    assert [ex.query["since"] for ex in proxy.seen if ex.route == "/poll"][0] == str(head)


def test_watch_says_when_the_board_is_down_and_resumes_after_a_restart(board, team, keyring):
    with watching("rev/log", "--since", board.head_id(), "--timeout", "1", key=team.carol.key) as w:
        before = team.alice.post("rev/log", "before")
        assert w.event()["id"] == before
        board.server.stop()
        down = w.event()
        assert down["type"] == "unreachable" and down["error"]
        board.server.start(**owner_env(keyring["owner"]))
        after = team.alice.post("rev/log", "after")
        while (event := w.event())["type"] == "unreachable":
            pass  # silence never looks like "no news"
        assert (event["type"], event["id"]) == ("event", after)


@pytest.mark.parametrize("argv", [["--since", "0"], ["--not-from", "alice", "--since", "0"]])
def test_a_watch_of_everything_is_refused(board, team, proxy, argv):
    """There is no global stream: a watch names a path or a filter."""
    run = cli("watch", *argv, key=team.carol.key, timeout=30)
    assert run.code in (1, 2) and run.out == "" and run.err
    assert "/poll" not in proxy.routes()
    for query in ({}, {"not_from": "alice"}):
        with refusal(400, "bad-request"):
            board.poll(0, **query)


def test_a_watch_of_a_missing_path_fails_at_start(board, team):
    run = cli("watch", "rev/nope", "--since", "0", timeout=30)
    assert (run.code, run.out) == (1, "") and "not-found" in run.err
    with refusal(404, "not-found"):
        board.poll(0, path="rev/nope")


@pytest.mark.parametrize("timeout", ["0", "-5"])
def test_watch_refuses_a_timeout_that_would_spin(board, team, proxy, timeout):
    run = cli("watch", "rev/log", "--since", "0", f"--timeout={timeout}")
    assert run.code == 2 and "--timeout" in run.err
    assert proxy.seen == []


@pytest.mark.parametrize("option", ["--not-from", "--mention", "--asked-by", "--owner"])
def test_watch_refuses_a_name_that_is_no_name(board, team, proxy, option):
    run = cli("watch", "rev/log", option, "my agent", "--since", "0")
    assert run.code != 0 and "my agent" in run.err
    assert proxy.seen == []


# --- wait ----------------------------------------------------------------------------------

def test_wait_prints_the_resolution(board, team):
    team.bob.create("rev/bob", "queue")
    item = team.alice.ask("rev/bob", "a question")
    waiter = Process("wait", item)
    try:
        assert waiter.nothing_for(1)
        answer = team.bob.answer(item, "the answer")
        [line] = waiter.rest()
        assert waiter.wait()[0] == 0
    finally:
        waiter.stop()
    assert line == {"type": "resolved", "item": item, "state": "answered", "id": answer["id"]}
    again = cli("wait", item).ok()  # already resolved: at once
    assert again.result == line


def test_wait_exits_124_on_its_timeout(board, team):
    team.bob.create("rev/bob", "queue")
    item = team.alice.ask("rev/bob", "nobody answers")
    started = time.monotonic()
    run = cli("wait", item, "--timeout", "2")
    assert (run.code, run.out) == (124, "")
    assert time.monotonic() - started >= 2


def test_wait_for_no_such_item_fails(board, team):
    run = cli("wait", board.head_id() + 1000, "--timeout", "2")
    assert (run.code, run.out) == (1, "")
