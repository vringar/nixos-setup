"""Who may write on message board v2: a name is bound by the owner
approving one specific key. Registration, its refusals and limits, approval
(over HTTP, signed as the owner, and with `board approve`), replacement,
revocation, the owner key itself, and the CLI's keys. See board_harness."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    IDENTITY_EXIT, OWNER, Board, Key, Process, Server, board, canonical, cli, distance,
    fingerprint_of, isolated, keyring, new_client_key, owner_env, refusal, request, ssh_keygen,
    team, watching,
)

pytestmark = pytest.mark.usefixtures("isolated")

COMMAND = 'board register --name <name> --note "<repo, purpose>"'


def _refused_by_identity(run, code):
    """A CLI write refused for who it is: exit 3, the code on stderr."""
    assert (run.code, run.out) == (IDENTITY_EXIT, ""), run
    assert code in run.err, run.err


def test_an_unknown_key_is_told_the_command_that_registers_it(board, team, keyring):
    stranger = keyring["stranger"]
    head = board.head_id()
    with refusal(403, "not-registered") as r:
        board.as_key(stranger).post("rev/log", "hi")
    assert (r.value["fingerprint"], r.value["command"]) == (stranger.fingerprint, COMMAND)
    run = cli("post", "rev/log", "hi", key=stranger)
    _refused_by_identity(run, "not-registered")
    assert "board register --name" in run.err
    assert board.head_id() == head


def test_from_keygen_to_a_first_post(board, team, tmp_path, monkeypatch):
    """The whole way in: a new repo key is refused, registers, waits for
    the owner, who approves it signed with the owner key over HTTP, and
    then posts as the name it asked for."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)
    key = cli("keygen", "--name", "newbie").ok().result
    _refused_by_identity(cli("post", "rev/log", "hello"), "not-registered")
    registered = cli("register", "--name", "newbie", "--note", "repo, testing").ok().result
    assert {k: registered[k] for k in ("type", "name", "fingerprint")} == \
        {"type": "registered", "name": "newbie", "fingerprint": key["fingerprint"]}
    item = registered["item"]
    assert cli("whoami").ok().result == {"type": "whoami", "fingerprint": key["fingerprint"],
                                       "name": "newbie", "state": "pending"}
    _refused_by_identity(cli("post", "rev/log", "hello"), "registration-pending")
    waiter = Process("wait", item)
    try:
        assert waiter.nothing_for(1)  # no outcome yet: it waits
        board.approve(item, "newbie", key["fingerprint"])
        [resolved] = waiter.rest()
        assert waiter.wait()[0] == 0
    finally:
        waiter.stop()
    assert resolved == {"type": "resolved", "item": item, "state": "answered", "id": resolved["id"]}
    posted = cli("post", "rev/log", "hello").ok().result
    [message] = cli("read", "rev/log").ok().of("message")
    assert (message["id"], message["from"], message["signed_by"]) == \
        (posted["id"], "newbie", key["fingerprint"])
    info = board.key_info(key["fingerprint"])
    assert (info["name"], info["state"], info["note"]) == ("newbie", "trusted", "repo, testing")
    assert info["repo"] == str(repo.resolve())  # where the key was found, informational
    assert cli("whoami").ok().result["state"] == "trusted"


def test_each_identity_refusal_has_its_own_code(board, team, keyring):
    """A launcher acts differently on each: register, wait for the owner,
    give up on the name, or make a new key."""
    head = board.head_id()
    unsigned = request(board.url + "/post", {"path": "rev/log", "body": "x",
                                             "key": new_client_key(), "nonce": str(head)})
    assert (unsigned.status, unsigned.json()["error"]) == (401, "unsigned")

    pending = keyring["pending-agent"]
    pending_item = board.register(pending, "pending-agent")["item"]
    declined = keyring["declined-agent"]
    declined_item = board.register(declined, "declined-agent")["item"]
    board.owner.decline(declined_item, "not now")
    revoked = board.trusted(keyring["revoked-agent"], "revoked-agent")
    board.owner.revoke(revoked.fingerprint, "gone")
    head = board.head_id()

    with refusal(403, "registration-pending") as r:
        board.as_key(pending).post("rev/log", "x")
    assert r.value["item"] == pending_item
    with refusal(403, "registration-declined") as r:
        board.as_key(declined).post("rev/log", "x")
    assert r.value["item"] == declined_item
    with refusal(403, "key-revoked") as r:
        revoked.post("rev/log", "x")
    assert isinstance(r.value["revoked_at"], int) and r.value["revoked_at"] <= head
    for key, code in ((pending, "registration-pending"), (declined, "registration-declined"),
                      (revoked.key, "key-revoked")):
        _refused_by_identity(cli("post", "rev/log", "x", key=key), code)
    assert board.head_id() == head  # a refusal writes nothing


def test_a_key_without_a_key_file_writes_nothing(board, team):
    head = board.head_id()
    run = cli("post", "rev/log", "unsigned")  # no BOARD_KEY, no scratch/board_ed25519
    assert run.code != 0 and run.out == ""
    assert board.head_id() == head


def test_registering_again_while_pending_returns_the_same_item_for_the_same_data(board, keyring):
    key = keyring["newbie"]
    first = board.register(key, "newbie", note="first")
    head = board.head_id()
    assert board.register(key, "newbie", note="first")["item"] == first["item"]
    for other in ({"name": "newbie-renamed", "note": "first"}, {"name": "newbie", "note": "second"},
                  {"name": "newbie", "note": "first", "repo": "/elsewhere"}):
        with refusal(409, "conflict"):  # one open per key, and it says what it was
            board.register(key, **other)
    assert board.head_id() == head
    assert [i["id"] for i in board.queue_items("_board/register")] == [first["item"]]
    cli_first = cli("register", "--name", "cliagent", "--note", "n", key=keyring["dora"]).ok().result
    again = cli("register", "--name", "cliagent", "--note", "n", key=keyring["dora"]).ok().result
    assert again["item"] == cli_first["item"]
    changed = cli("register", "--name", "cliagent", "--note", "m", key=keyring["dora"])
    assert (changed.code, changed.out) == (1, "") and "conflict" in changed.err


def test_what_registering_does_for_a_trusted_revoked_or_declined_key(board, keyring):
    alice = board.trusted(keyring["alice"], "alice")
    with refusal(409, "already-registered") as r:
        board.register(alice.key, "alice")
    assert r.value["name"] == "alice"
    run = cli("register", "--name", "alice", key=alice.key)
    assert (run.code, run.out) == (1, "") and "already-registered" in run.err
    board.owner.revoke(alice.fingerprint, "compromised")
    with refusal(403, "key-revoked"):  # a revoked key stays revoked: tell the human
        board.register(alice.key, "alice")
    dora = keyring["dora"]
    item = board.register(dora, "dora")["item"]
    board.owner.decline(item, "who are you")
    with refusal(403, "registration-declined") as declined:  # but it is told not to, unprompted
        board.as_key(dora).post("rev/log", "hi")
    assert "do not register again" in declined.value["message"]
    again = board.register(dora, "dora")["item"]  # the store lets it, once the owner asked
    assert again != item
    assert [i["id"] for i in board.queue_items("_board/register")] == [again]


def test_a_look_alike_of_a_name_bound_since_cannot_be_approved(board, keyring):
    squatter = keyring["dora"]
    item = board.register(squatter, "ghidr4")["item"]
    board.trusted(keyring["alice"], "ghidra")
    with refusal(403, "forbidden") as r:
        board.approve(item, "ghidr4", squatter.fingerprint)
    assert "too close" in r.value["message"]
    run = cli("approve", item, key=board.owner.key)
    assert run.code == 1 and "forbidden" in run.err and "too close" in run.err
    assert board.key_info(squatter.fingerprint)["state"] == "pending"
    board.owner.decline(item, "close to ghidra")


NATO = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
        "juliet", "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo",
        "sierra", "tango", "uniform"]


def test_twenty_open_registrations_fill_the_board(board, keyring):
    """Anything on the host or the tailnet can register; the open ones are
    bounded so a flood cannot bury the owner's inbox."""
    keys = [keyring[f"cap-{name}"] for name in NATO]
    items = [board.register(key, name)["item"] for key, name in zip(keys[:20], NATO[:20], strict=True)]
    with refusal(429, "register-full"):
        board.register(keys[20], NATO[20])
    run = cli("register", "--name", NATO[20], key=keys[20])
    assert (run.code, run.out) == (1, "") and "register-full" in run.err
    board.owner.decline(items[0])
    board.register(keys[20], NATO[20])


def test_a_registration_note_is_at_most_1kb(board, keyring):
    board.register(keyring["alpha"], "alpha", note="n" * 1024)
    head = board.head_id()
    with refusal(400, "bad-request"):
        board.register(keyring["bravo"], "bravo", note="n" * 1025)
    assert board.head_id() == head


BAD_NAMES = ["", "Stefan ", " Stefan", "Ste fan", "stefan.z", "Stefаn", "stéfan", "a/b", "x" * 65,
             "agent\n"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_a_name_outside_the_rule_is_refused(board, keyring, name):
    head = board.head_id()
    with refusal(400, "bad-request"):
        board.register(keyring["newbie"], name)
    assert board.head_id() == head and board.queue_items("_board/register") == []


@pytest.mark.parametrize("name", ["board", "Board", "BOARD", "boards", "boar", "b0ard"])
def test_the_board_and_names_like_it_are_reserved(board, keyring, name):
    head = board.head_id()
    with refusal(403, "forbidden"):
        board.register(keyring["newbie"], name)
    assert board.head_id() == head


@pytest.mark.parametrize("near", ["stefan", "STEFAN", "Stefan_", "Stefn", "Stephan", "Stefan12"])
def test_a_look_alike_of_a_bound_name_never_reaches_the_owner(board, keyring, near):
    """Refused at registration, against every bound name: the owner
    should not have to spot `Stefn` among twenty requests."""
    assert distance(near.lower(), OWNER.lower()) < 3
    head = board.head_id()
    with refusal(403, "forbidden"):
        board.register(keyring["squatter"], near)
    run = cli("register", "--name", near, key=keyring["squatter"])
    assert (run.code, run.out) == (1, "")
    assert board.head_id() == head and board.queue_items("_board/register") == []
    board.trusted(keyring["fine"], "Stefan-db")  # three edits away


@pytest.mark.parametrize("near", ["alicia", "Alice", "alice_"])
def test_a_look_alike_of_a_bound_agent_is_refused(board, keyring, near):
    board.trusted(keyring["alice"], "alice")
    with refusal(403, "forbidden"):
        board.register(keyring["squatter"], near)
    board.trusted(keyring["fine"], "alice-db")


PRODUCTION_NAMES = [
    "Stefan", "aspfuzz", "board", "claude-for-stefan", "final-reviewer", "firefox-src-agent",
    "libafl", "librarian", "openwpm-orchestrator", "openwpm-signoff", "orchestrator",
    "psp-db", "psp-etl", "psptool", "psptool-rs", "psptool-rs-orchestrator", "qemu-psp",
    "qemu-tools", "re-reuse", "vetinari", "vetinari-design"]


def test_the_names_in_use_today_can_all_be_bound(board, keyring):
    agents = [n for n in PRODUCTION_NAMES if n not in ("board", OWNER)]
    for name in agents:
        board.trusted(keyring[f"prod-{name}"], name)
    trusted = {k["name"] for k in board.keys() if k["state"] == "trusted"}
    assert trusted == set(agents)
    # the closest pairs are exactly far enough apart
    assert {distance(a, b) for a, b in (
        ("psp-db", "psp-etl"), ("psp-etl", "psptool"), ("psptool", "psptool-rs"))} == {3}


# --- approving --------------------------------------------------------------------

def test_approving_a_new_key_for_a_bound_name_replaces_the_old_one(board, team, keyring):
    """Session keys (claude-for-stefan) register their name every session:
    approving the new key revokes the old one, and what the name owns
    stays with the name."""
    old = team.alice
    by_old = old.post("rev/log", "by the old key")
    old.create("rev/alice", "queue")
    item = team.bob.ask("rev/alice", "for alice")
    new_key = keyring["alice-2"]
    registration = board.register(new_key, "alice")["item"]  # the exact bound name: allowed
    old.post("rev/log", "the old key still writes meanwhile")
    board.approve(registration, "alice", new_key.fingerprint)
    with refusal(403, "key-revoked") as r:
        old.post("rev/log", "the old key after")
    revoked_at = r.value["revoked_at"]
    assert revoked_at == board.trust_note()["id"]  # the trust list's version that revoked it
    assert board.key_info(old.fingerprint)["state"] == "revoked"
    info = board.key_info(new_key.fingerprint)
    assert (info["name"], info["state"]) == ("alice", "trusted")
    [message] = [m for m in cli("read", "rev/log").ok().of("message") if m["id"] == by_old]
    assert (message["from"], message["signed_by"], message["revoked_at"]) == \
        ("alice", old.fingerprint, revoked_at)
    new = board.as_key(new_key, "alice")
    new.answer(item, "the new key answers the name's queue")
    by_new = new.post("rev/log", "by the new key")
    [later] = [m for m in cli("read", "rev/log").ok().of("message") if m["id"] == by_new]
    assert (later["signed_by"], later["revoked_at"]) == (new_key.fingerprint, None)


def test_approving_one_key_declines_the_others_asking_for_that_name(board, team, keyring):
    first, second = keyring["twin-1"], keyring["twin-2"]
    chosen = board.register(first, "newbie")["item"]
    other = board.register(second, "newbie")["item"]
    waiter = Process("wait", other)
    try:
        board.approve(chosen, "newbie", first.fingerprint)
        [resolved] = waiter.rest()
        assert waiter.wait()[0] == 0  # wait exits 0 whatever the outcome
    finally:
        waiter.stop()
    assert (resolved["type"], resolved["item"], resolved["state"]) == ("resolved", other, "declined")
    with refusal(403, "registration-declined") as r:
        board.as_key(second).post("rev/log", "me too")
    assert r.value["item"] == other
    board.as_key(first).post("rev/log", "I am newbie")
    assert board.queue_items("_board/register") == []


def test_an_approval_signs_the_key_not_just_the_name(board, keyring):
    """What the owner signs covers the fingerprint: an approval for the
    right item and name but another key binds nothing."""
    asking, other = keyring["newbie"], keyring["other"]
    item = board.register(asking, "newbie")["item"]
    head = board.head_id()
    try:
        board.approve(item, "newbie", other.fingerprint)
    except Exception as e:  # noqa: BLE001
        assert 400 <= getattr(e, "status", 0) < 500, e
    else:
        raise AssertionError("an approval naming another key was accepted")
    assert board.head_id() == head
    assert board.key_info(asking.fingerprint)["state"] == "pending"


def test_only_the_human_approves_and_revokes(board, team, keyring):
    key = keyring["newbie"]
    item = board.register(key, "newbie")["item"]
    with refusal(403, "forbidden"):
        team.alice.approve(item, "newbie", key.fingerprint)
    with refusal(403, "forbidden"):
        team.alice.revoke(team.bob.fingerprint, "I don't like bob")
    run = cli("approve", item, key=team.alice.key)
    assert (run.code, run.out) == (1, "") and "forbidden" in run.err
    assert board.key_info(key.fingerprint)["state"] == "pending"
    assert board.key_info(team.bob.fingerprint)["state"] == "trusted"


def test_cli_approve_and_revoke_with_the_owner_key(board, team, keyring):
    key = keyring["newbie"]
    item = board.register(key, "newbie")["item"]
    approved = cli("approve", item, key=board.owner.key).ok().result
    assert approved == {"type": "approved", "item": item, "name": "newbie",
                        "fingerprint": key.fingerprint, "id": approved["id"]}
    assert board.key_info(key.fingerprint)["state"] == "trusted"
    revoked = cli("revoke", key.fingerprint, "--reason", "done with it", key=board.owner.key).ok().result
    assert revoked == {"type": "revoked", "fingerprint": key.fingerprint, "name": "newbie",
                       "id": revoked["id"]}
    _refused_by_identity(cli("post", "rev/log", "after", key=key), "key-revoked")
    with refusal(403, "key-revoked") as r:
        board.as_key(key).post("rev/log", "after")
    assert r.value["revoked_at"] == revoked["id"]


def test_registrations_lists_the_pending_ones_and_approve_takes_a_name(board, team, keyring):
    first, second, third = keyring["newbie"], keyring["dora"], keyring["erin"]
    one = board.register(first, "newbie", note="line one\nline two")["item"]
    two = board.register(second, "twin")["item"]
    three = board.register(third, "twin")["item"]
    owner = board.owner.key
    listed = cli("registrations", key=owner).ok().lines
    assert [(r["type"], r["item"], r["name"], r["fingerprint"]) for r in listed] == [
        ("registration", one, "newbie", first.fingerprint),
        ("registration", two, "twin", second.fingerprint),
        ("registration", three, "twin", third.fingerprint)]
    assert {"repo", "note", "look_alikes"} <= set(listed[0]) and listed[0]["note"].startswith("line one")
    # two pending keys with the name: refused, and the candidates are listed
    run = cli("approve", "twin", key=owner)
    assert (run.code, run.out) == (1, "") and "2 pending keys are named twin" in run.err
    assert second.fingerprint in run.err and third.fingerprint in run.err
    assert first.fingerprint not in run.err
    # no match: refused, every pending one listed
    run = cli("approve", "nobody", key=owner)
    assert run.code == 1 and all(k.fingerprint in run.err for k in (first, second, third))
    # a fingerprint or an item number must be exact, and pending
    assert cli("approve", first.fingerprint[:-1], key=owner).code == 1
    assert cli("approve", str(one + 5000), key=owner).code == 1
    assert cli("approve", "newbi", key=owner).code == 1
    assert board.key_info(first.fingerprint)["state"] == "pending"
    # a lone name approves; so does a fingerprint
    approved = cli("approve", "newbie", key=owner).ok().result
    assert (approved["type"], approved["item"], approved["name"], approved["fingerprint"]) == \
        ("approved", one, "newbie", first.fingerprint)
    assert board.key_info(first.fingerprint)["state"] == "trusted"
    cli("approve", second.fingerprint, key=owner).ok()
    assert board.key_info(second.fingerprint)["state"] == "trusted"
    # approving `second` settled the other registration for the name too
    assert cli("registrations", key=owner).ok().lines == []
    assert board.key_info(third.fingerprint)["state"] != "pending"


def test_revoke_takes_a_fingerprint_or_an_unambiguous_name(board, team):
    owner = board.owner.key
    run = cli("revoke", "nobody", key=owner)
    assert run.code == 1 and "no key is bound to the name nobody" in run.err
    revoked = cli("revoke", "bob", "--reason", "by name", key=owner).ok().result
    assert (revoked["type"], revoked["fingerprint"], revoked["name"]) == \
        ("revoked", team.bob.fingerprint, "bob")
    assert board.key_info(team.bob.fingerprint)["state"] == "revoked"
    assert board.key_info(team.alice.fingerprint)["state"] == "trusted"


def test_revoking_a_key_marks_what_it_signed_before(board, team):
    """Posts signed before the revoke stay verified; they say since when
    their key is no longer trusted."""
    a = team.alice
    mid = a.post("rev/log", "before the revoke")
    a.create("rev/state", "note", body="state by alice")
    board.owner.revoke(a.fingerprint, "compromised")
    with refusal(403, "key-revoked") as r:
        a.post("rev/log", "after the revoke")
    revoked_at = r.value["revoked_at"]
    assert revoked_at == board.trust_note()["id"]  # the trust list's version that revoked it
    [message] = [m for m in cli("read", "rev/log").ok().of("message") if m["id"] == mid]
    assert (message["from"], message["signed_by"], message["revoked_at"]) == \
        ("alice", a.fingerprint, revoked_at)
    assert cli("show", mid).ok().result["revoked_at"] == revoked_at
    [note] = cli("read", "rev/state").ok().of("note")
    assert (note["signed_by"], note["revoked_at"]) == (a.fingerprint, revoked_at)
    with watching("rev/log", "--since", mid - 1, key=team.bob.key) as watch:
        bell = watch.event()
    assert (bell["id"], bell["signed_by"], bell["revoked_at"]) == (mid, a.fingerprint, revoked_at)
    assert board.key_info(a.fingerprint)["state"] == "revoked"


def test_the_owner_key_cannot_be_revoked(board, team):
    head = board.head_id()
    with refusal(403):
        board.owner.revoke(board.owner.fingerprint, "oops")
    assert board.head_id() == head
    assert board.healthz()["owner"]["fingerprint"] == board.owner.fingerprint
    board.owner.create("still-mine", "container")


# --- the owner key ------------------------------------------------------------------

def test_a_new_owner_key_in_the_config_revokes_the_old_one(board, team, keyring):
    old = board.owner
    by_old = old.post("rev/log", "from the old owner key")
    new_key = keyring["owner-2"]
    board.restart(**owner_env(new_key))
    assert board.healthz()["owner"] == {"name": OWNER, "fingerprint": new_key.fingerprint}
    with refusal(403, "key-revoked") as r:
        old.post("rev/log", "the old owner key")
    [message] = [m for m in cli("read", "rev/log").ok().of("message") if m["id"] == by_old]
    assert (message["from"], message["revoked_at"]) == (OWNER, r.value["revoked_at"])
    new = board.as_key(new_key, OWNER)
    new.create("second", "container")  # the human now
    head = board.head_id()
    board.restart(**owner_env(new_key))  # bound at every start; the same key changes nothing
    assert board.head_id() == head
    assert board.key_info(new_key.fingerprint)["state"] == "owner"


def test_a_refused_owner_setting_says_which_owner_it_keeps(board, keyring, tmp_path):
    """The log must not claim the board has no owner when an earlier start's
    owner is still bound."""
    first = board.owner.fingerprint
    board.restart(BOARD_OWNER_NAME=OWNER, BOARD_OWNER_PUBKEY=str(tmp_path / "gone.pub"))
    log = board.server.log()
    assert f"keeping the current owner {OWNER} ({first})" in log and "the board has no owner" not in log
    assert board.healthz()["owner"]["fingerprint"] == first
    # a key revoked by an earlier rotation cannot come back as the owner
    new_key = keyring["owner-2"]
    board.restart(**owner_env(new_key))
    board.restart(**owner_env(keyring["owner"]))
    log = board.server.log()
    assert "was revoked at" in log and f"keeping the current owner {OWNER} ({new_key.fingerprint})" in log
    assert board.healthz()["owner"]["fingerprint"] == new_key.fingerprint


def test_a_board_without_an_owner_key_starts_and_approves_nothing(tmp_path, keyring):
    server = Server(tmp_path / "board-v2.db", tmp_path / "logs").start()  # no owner configured
    try:
        b = Board(server)
        assert b.healthz()["owner"] is None
        item = b.register(keyring["alice"], "alice")["item"]
        with refusal(403):
            b.as_key(keyring["owner"]).approve(item, "alice", keyring["alice"].fingerprint)
        assert b.key_info(keyring["alice"].fingerprint)["state"] == "pending"
    finally:
        server.stop()


@pytest.mark.parametrize("setting", ["missing key file", "ecdsa key", "the board's name",
                                     "a name with a space"])
def test_an_unusable_owner_setting_leaves_the_board_ownerless(tmp_path, keyring, setting):
    missing = tmp_path / "missing.pub"
    env = {
        "missing key file": {"BOARD_OWNER_NAME": OWNER, "BOARD_OWNER_PUBKEY": str(missing)},
        "ecdsa key": owner_env(Key.make(tmp_path, "ec", kind="ecdsa")),
        "the board's name": owner_env(keyring["owner"], "board"),
        "a name with a space": owner_env(keyring["owner"], "Stefan Z"),
    }[setting]
    server = Server(tmp_path / "board-v2.db", tmp_path / "logs").start(**env)
    try:
        assert Board(server).healthz()["owner"] is None
        log = server.log()
        assert {"missing key file": str(missing), "ecdsa key": "only ed25519",
                "the board's name": "BOARD_OWNER_NAME",
                "a name with a space": "BOARD_OWNER_NAME"}[setting] in log
    finally:
        server.stop()


# --- what a key can ask about itself -------------------------------------------------

def test_whoami_says_what_the_board_knows_of_the_key(board, keyring):
    stranger, newbie = keyring["stranger"], keyring["newbie"]
    assert cli("whoami", key=stranger).ok().result == {
        "type": "whoami", "fingerprint": stranger.fingerprint, "name": None, "state": "unregistered"}
    item = board.register(newbie, "newbie")["item"]
    assert cli("whoami", key=newbie).ok().result["state"] == "pending"
    board.approve(item, "newbie", newbie.fingerprint)
    assert cli("whoami", key=newbie).ok().result == {
        "type": "whoami", "fingerprint": newbie.fingerprint, "name": "newbie", "state": "trusted"}
    assert cli("whoami", key=board.owner.key).ok().result == {
        "type": "whoami", "fingerprint": board.owner.fingerprint, "name": OWNER, "state": "owner"}


def test_the_keys_endpoints(board, team, keyring):
    with refusal(404, "not-found"):
        board.key_info(keyring["stranger"].fingerprint)
    listed = {k["fingerprint"]: k for k in board.keys()}
    assert (listed[board.owner.fingerprint]["name"], listed[board.owner.fingerprint]["state"]) == \
        (OWNER, "owner")
    assert (listed[team.alice.fingerprint]["name"], listed[team.alice.fingerprint]["state"]) == \
        ("alice", "trusted")
    info = board.key_info(team.alice.fingerprint)
    assert (info["fingerprint"], info["name"], info["state"]) == \
        (team.alice.fingerprint, "alice", "trusted")


# --- the CLI's keys -------------------------------------------------------------------

def test_keygen_writes_an_ignored_key_at_the_repo_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".jj").mkdir(parents=True)
    (repo / "sub" / "dir").mkdir(parents=True)
    monkeypatch.chdir(repo / "sub" / "dir")
    r = cli("keygen", "--name", "agent")
    key = repo / "scratch" / "board_ed25519"
    assert r.result == {"type": "key", "path": str(key), "name": "agent",
                        "fingerprint": fingerprint_of(key.with_name(key.name + ".pub"))}
    assert (repo / "scratch" / ".gitignore").read_text() == "*\n"
    assert "added" not in r.err
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert key.with_name(key.name + ".pub").read_text().split()[2] == "board:agent"
    again = cli("keygen", "--name", "agent")
    assert again.code == 1 and "already exists" in again.err
    other = tmp_path / "other"
    # not the repository the hook runs in: GIT_DIR and friends would point git there
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(other)], env=clean, check=True)
    (other / "scratch").mkdir()
    (other / "scratch" / ".gitignore").write_text("custom\n")
    monkeypatch.chdir(other)
    run = cli("keygen", "--name", "x")
    assert run.code == 0
    # a .gitignore that did not cover the key gets a line that does, and says so
    assert (other / "scratch" / ".gitignore").read_text() == "custom\nboard_ed25519*\n"
    assert "board_ed25519*" in run.err and "did not keep the key out of a commit" in run.err
    assert subprocess.run(["git", "check-ignore", "-q", "scratch/board_ed25519"], cwd=other, env=clean).returncode == 0
    covered = tmp_path / "covered"
    (covered / ".git").mkdir(parents=True)
    (covered / "scratch").mkdir()
    (covered / "scratch" / ".gitignore").write_text("*.log\n*")  # covers it, with no final newline
    monkeypatch.chdir(covered)
    run = cli("keygen", "--name", "y")
    assert (run.code, run.err) == (0, "")
    assert (covered / "scratch" / ".gitignore").read_text() == "*.log\n*"
    taken_back = tmp_path / "taken-back"
    (taken_back / ".git").mkdir(parents=True)
    (taken_back / "scratch").mkdir()
    (taken_back / "scratch" / ".gitignore").write_text("*\n!board_ed25519\n")
    monkeypatch.chdir(taken_back)
    assert cli("keygen", "--name", "z").code == 0
    assert (taken_back / "scratch" / ".gitignore").read_text() == "*\n!board_ed25519\nboard_ed25519*\n"
    monkeypatch.chdir(tmp_path)
    outside = cli("keygen", "--name", "x")
    assert outside.code == 1 and "not inside a repository" in outside.err
    assert cli("keygen", "--from", "x").code == 2  # v1's flag is gone


def test_a_key_others_can_read_is_refused(tmp_path, monkeypatch):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "repo")
    key = Path(cli("keygen", "--name", "agent").ok().result["path"])
    for mode in (0o644, 0o640, 0o604):
        key.chmod(mode)
        run = cli("post", "rev/log", "hi")
        assert (run.code, run.out) == (1, "") and "can be read by others" in run.err and "chmod 600" in run.err
    key.chmod(0o600)
    run = cli("post", "rev/log", "hi")
    assert "can be read by others" not in run.err  # private again: the key loads (no board is running here)


def test_client_chosen_names_cannot_start_with_dash_or_underscore(board, keyring):
    for name in ("-flag", "_board", "-"):
        with refusal(400, "bad-request"):
            board.register(keyring["stranger"], name)
    run = cli("register", "--name", "_sneaky", key=keyring["stranger"])
    assert (run.code, run.out) == (1, "") and "not allowed" in run.err


def test_a_generated_key_is_an_ordinary_openssh_key(tmp_path, monkeypatch):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "repo")
    r = cli("keygen", "--name", "agent").ok().result
    pub = Path(r["path"] + ".pub")
    derived = ssh_keygen("-y", "-f", r["path"]).stdout.decode().split()
    assert derived[:2] == pub.read_text().split()[:2]
    assert fingerprint_of(pub) == r["fingerprint"]


@pytest.mark.parametrize("name", ["Stefan ", "a.b", "Stefаn"])
def test_keygen_and_register_refuse_a_bad_name_before_anything(board, tmp_path, monkeypatch, name):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.chdir(tmp_path / "repo")
    run = cli("keygen", "--name", name)
    assert run.code != 0 and not (tmp_path / "repo" / "scratch").exists()
    head = board.head_id()
    run = cli("register", "--name", name, key=Key.make(tmp_path, "k"))
    assert run.code != 0 and board.head_id() == head


def test_other_key_types_and_encrypted_keys_are_refused(board, team, tmp_path):
    ecdsa = Key.make(tmp_path, "ec", kind="ecdsa")
    locked = Key.make(tmp_path, "locked", passphrase="secret")
    run = cli("post", "rev/log", "hi", key=ecdsa)
    assert run.code == 1 and "only ed25519" in run.err
    run = cli("register", "--name", "locked-agent", key=locked)
    assert run.code == 1 and "passphrase-protected" in run.err
    head = board.head_id()
    values = {"name": "ecdsa-agent", "note": "", "repo": "", "key": new_client_key(),
              "nonce": str(head)}
    signature = ecdsa.sign("board2-register", canonical(
        ("name", "note", "repo", "key", "nonce"), values))
    r = request(board.url + "/register", {**values, "signature": signature})
    assert (r.status, r.json()["error"]) == (400, "bad-request")
    assert "only ed25519" in json.dumps(r.json())
    assert board.head_id() == head
