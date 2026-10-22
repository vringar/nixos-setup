"""Message board v2 on the wire: what a signature covers (and what it must
never be replayed as), the head as a one-use nonce, idempotent retries,
many writers at once, the HTTP layer's limits, and the database file
failing under the board. See board_harness."""

import os
import re
import socket
import sqlite3
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    DROP, FIRST_ID, HANG_TIMEOUT, KINDS, V1_KINDS, board, canonical, cli, error_answer,
    isolated, keyring, new_client_key, owner_env, parse_answer, program, proxy, refusal, request,
    sent_signature_verifies, team, text, wait_until, wire,
)

pytestmark = pytest.mark.usefixtures("isolated")


# --- what a signature covers --------------------------------------------------------

def test_the_canonical_encoding_separates_what_key_value_lines_conflate(board, team):
    """`name:<byte length>:value\\n`: a body cannot smuggle in a field."""
    fields = KINDS["post"][1]
    one = {"path": "rev/log", "body": "a\nkey:1:b", "key": "k", "nonce": "0"}
    assert canonical(fields, one) == b"path:7:rev/log\nbody:9:a\nkey:1:b\nkey:1:k\nnonce:1:0\n"
    assert canonical(("body",), {"body": "ü"}) == "body:2:ü\n".encode()  # bytes, not characters
    a = team.alice
    signed = board.signed("post", a.key, {"path": "rev/log", "body": "a\nkey:1:b",
                                          "key": new_client_key()})
    tampered = {**signed, "body": "a", "key": "b\nkey:1:" + signed["key"]}
    r = board.send("post", tampered)
    assert (r.status, r.json()["error"]) == (400, "bad-request")
    assert board.send("post", signed).status == 200
    utf8 = board.signed("post", a.key, {"path": "rev/log", "body": "ü 🙂", "key": new_client_key()})
    assert board.send("post", utf8).status == 200


def test_the_board_holds_a_signature_to_exactly_what_it_signed(board, team):
    a = team.alice
    a.create("rev/other", "thread")
    values = {"path": "rev/log", "body": "signed by ssh-keygen", "key": new_client_key()}
    head = board.head_id()
    for change, nonce in (({"body": "not what was signed"}, head), ({"path": "rev/other"}, head),
                          ({"nonce": str(head)}, head - 1)):
        body = {**board.signed("post", a.key, {**values, "key": new_client_key()}, nonce=nonce),
                **change}
        r = board.send("post", body)
        assert (r.status, r.json()["error"]) == (400, "bad-request"), change
    signed_values = {name: text(values.get(name)) for name in KINDS["post"][1]}
    signed_values["nonce"] = str(head)
    data = canonical(KINDS["post"][1], signed_values)
    sha256 = a.key.sign("board2-post", data, "-O", "hashalg=sha256")
    r = board.send("post", {**wire(signed_values), "signature": sha256})
    assert r.status == 400 and "sha512" in r.text
    assert board.head_id() == head
    r = board.send("post", {**wire(signed_values), "signature": a.key.sign("board2-post", data)})
    assert r.status == 200


def _sample(board, team, keyring, action):
    """A key and the field values of a write `action` it may make."""
    a, b = team.alice, team.bob
    if action in ("create", "post", "file", "note"):
        a.create("rev/state", "note", body="v1")
        a.create("rev/q", "queue")
    values = {
        "create": {"path": "rev/new", "kind": "thread"},
        "post": {"path": "rev/log", "body": "hi"},
        "note": {"path": "rev/state", "body": "v2", "base_version": 1},
        "file": {"path": "rev/q", "body": "a question"},
    }.get(action)
    if values:
        return a.key, values
    if action == "resolve":
        b.create("rev/bob", "queue")
        return b.key, {"item": a.ask("rev/bob", "q"), "state": "answered", "body": "a"}
    if action == "register":
        return keyring["newbie"], {"name": "newbie", "note": "n"}
    if action == "approve":
        item = board.register(keyring["newbie"], "newbie")["item"]
        return board.owner.key, {"item": item, "name": "newbie",
                                 "fingerprint": keyring["newbie"].fingerprint}
    assert action == "revoke"
    return board.owner.key, {"fingerprint": b.fingerprint, "reason": "r"}


@pytest.mark.parametrize("action", sorted(KINDS))
def test_a_signature_is_good_only_in_its_own_namespace(board, team, keyring, action):
    """A v1 signature must never verify in v2 (the fresh counter would
    accept its nonce), nor one action's signature for another's."""
    key, values = _sample(board, team, keyring, action)
    namespace, fields = KINDS[action]
    others = [ns for ns, _ in KINDS.values() if ns != namespace]
    head = board.head_id()
    for wrong in [ns for ns, _ in V1_KINDS.values()] + others:
        signed = {name: text(values.get(name)) for name in fields}
        signed.update(key=new_client_key(), nonce=str(head))
        body = {**wire(signed), "signature": key.sign(wrong, canonical(fields, signed))}
        r = board.send(action, body)
        assert (r.status, r.json()["error"]) == (400, "bad-request"), wrong
    assert board.head_id() == head
    board.write(action, key, **values)  # the right namespace


def test_a_post_signature_is_not_a_file(board, team):
    """post and file sign the same fields (path, body, key, nonce); only
    the namespace tells a message from a request in a queue."""
    team.alice.create("rev/q", "queue")
    head = board.head_id()
    body = board.signed("post", team.alice.key, {"path": "rev/q", "body": "x",
                                                 "key": new_client_key()})
    r = board.send("file", body)
    assert (r.status, r.json()["error"]) == (400, "bad-request")
    assert board.head_id() == head and board.queue_items("rev/q") == []


def test_the_cli_signs_what_ssh_keygen_verifies_and_the_board_keeps_it(board, team, keyring,
                                                                       proxy, tmp_path):
    a, b, o = team.alice, team.bob, team.owner
    b.create("rev/bob", "queue")
    a.create("rev/state", "note", body="v1")
    newbie = keyring["newbie"]
    cli("post", "rev/log", "hi\nthere 🙂", key=a.key).ok()
    cli("note", "set", "rev/state", "--base", "1", stdin="# state v2\n", key=a.key).ok()
    item = cli("ask", "rev/bob", "a question", key=a.key).ok().result["item"]
    cli("answer", item, "an answer", key=b.key).ok()
    cli("create", "thread", "rev/cli", "--description", "d", key=a.key).ok()
    registration = cli("register", "--name", "newbie", "--note", "n", key=newbie).ok().result["item"]
    cli("approve", registration, key=o.key).ok()
    cli("revoke", newbie.fingerprint, "--reason", "test", key=o.key).ok()
    for action, key in (("post", a.key), ("note", a.key), ("file", a.key), ("resolve", b.key),
                        ("create", a.key), ("register", newbie), ("approve", o.key),
                        ("revoke", o.key)):
        [sent] = proxy.sent(f"/{action}")
        assert sent_signature_verifies(tmp_path, key, action, sent), action
        [(stored,)] = board.sql("SELECT COUNT(*) FROM events WHERE signature=?", sent["signature"])
        assert stored >= 1, action  # every event carries the signature of its request
    [post] = proxy.sent("/post")
    assert post["body"] == "hi\nthere 🙂" and len(post["key"]) >= 16


# --- the head as a nonce ----------------------------------------------------------------

def test_a_signature_is_good_for_one_write(board, team):
    a = team.alice
    nonce = board.head_id()
    a.write("post", path="rev/log", body="first", nonce=nonce)
    with refusal(409, "stale-nonce") as r:
        a.write("post", path="rev/log", body="second, same nonce", nonce=nonce)
    assert r.value["head"] == board.head_id()
    assert r.value.headers["Board-Error"] == "stale-nonce"
    with refusal(409, "stale-nonce"):
        a.write("post", path="rev/log", body="a nonce from the future", nonce=board.head_id() + 1)


def test_deleting_the_newest_event_does_not_revive_its_nonce(board, team, keyring):
    a = team.alice
    body = board.signed("post", a.key, {"path": "rev/log", "body": "signed",
                                        "key": new_client_key()})
    mid = board.send("post", body).json()["id"]
    board.sql("DELETE FROM events WHERE id=?", mid)  # what board-admin's delete does
    assert board.head_id() == mid  # the highest id ever issued, not the highest left
    r = board.send("post", body)  # the key's row is gone: the nonce decides
    assert (r.status, r.json()["error"]) == (409, "stale-nonce")
    board.restart(**owner_env(keyring["owner"]))
    assert board.head_id() == mid
    assert a.post("rev/log", "after") > mid


def test_the_cli_signs_again_when_another_write_lands_first(board, team, proxy):
    heads = []

    def after(ex):
        if ex.route == "/healthz":
            heads.append(ex.answer_json()["head"])
            if len(heads) == 1:  # someone else writes right after the first read
                team.bob.post("rev/log", "concurrent")

    proxy.after = after
    run = cli("post", "rev/log", "signed post", key=team.alice.key)
    assert (run.code, run.err) == (0, "")  # transparent: nothing about the retry
    sent = proxy.sent("/post")
    assert len(sent) == 2 and [str(s["nonce"]) for s in sent] == [str(h) for h in heads[:2]]
    assert sent[0]["key"] == sent[1]["key"]  # the same post, signed again
    [mine] = [m for m in cli("read", "rev/log").ok().of("message") if m["from"] == "alice"]
    assert mine["id"] == run.result["id"]


def test_the_cli_gives_up_after_five_stale_signatures(board, team, proxy):
    def after(ex):
        if ex.route == "/healthz":  # every head is stale by the time the post arrives
            team.bob.post("rev/log", "busy")

    proxy.after = after
    run = cli("post", "rev/log", "never lands", key=team.alice.key)
    assert run.code == 1 and run.last_err.startswith("board: gave up after 5 tries")
    assert len(proxy.sent("/post")) == 5
    assert "never lands" not in [m["body"] for m in cli("read", "rev/log", "--all").ok().of("message")]


# --- idempotent retries ---------------------------------------------------------------

@pytest.mark.parametrize("action", sorted(KINDS))
def test_a_write_sent_again_answers_what_it_answered_the_first_time(board, team, keyring, action):
    """A retry of a write that landed -- re-signed against the head its
    own landing moved, or the very same bytes -- is answered as the first
    time, not as item-not-open, note-changed or stale-nonce."""
    key, values = _sample(board, team, keyring, action)
    values["key"] = new_client_key()
    first = board.signed(action, key, values)
    answered = board.send(action, first)
    assert answered.status == 200, answered.text
    head = board.head_id()
    again = board.send(action, board.signed(action, key, values))
    assert (again.status, again.json()) == (200, answered.json())
    replayed = board.send(action, first)
    assert (replayed.status, replayed.json()) == (200, answered.json())
    assert board.head_id() == head


def test_a_client_key_used_for_another_request_is_a_conflict(board, team):
    a = team.alice
    key = new_client_key()
    a.write("post", path="rev/log", body="once", key=key)
    head = board.head_id()
    with refusal(409, "conflict"):
        a.write("post", path="rev/log", body="something else", key=key)
    assert board.head_id() == head


@pytest.mark.parametrize("command", ["post", "answer", "note"])
def test_a_cli_write_whose_answer_was_lost_lands_once(board, team, proxy, command):
    a, b = team.alice, team.bob
    b.create("rev/bob", "queue")
    item = a.ask("rev/bob", "question")
    a.create("rev/state", "note", body="v1")
    argv, stdin, key, route = {
        "post": (["post", "rev/log", "exactly", "once"], "", a.key, "/post"),
        "answer": (["answer", item, "answered once"], "", b.key, "/resolve"),
        "note": (["note", "set", "rev/state", "--base", "1"], "v2 once\n", a.key, "/note"),
    }[command]
    proxy.after = lambda ex: DROP if ex.route == route and len(proxy.sent(route)) == 1 else None
    run = cli(*argv, stdin=stdin, key=key)
    assert run.code == 0 and "retrying" in run.err
    sent = proxy.sent(route)
    assert len(sent) == 2 and sent[0]["key"] == sent[1]["key"]
    if command == "post":
        assert [m["id"] for m in cli("read", "rev/log").ok().of("message")] == [run.result["id"]]
    elif command == "answer":
        [line] = cli("read", "rev/bob", "--state", "all").ok().of("item")
        assert (line["state"], line["resolution"]["id"]) == ("answered", run.result["id"])
    else:
        assert (run.result["version"], board.note("rev/state")["version"]) == (2, 2)


def test_a_cli_post_gives_up_after_three_retries(board, team, proxy):
    proxy.before = lambda ex: DROP if ex.route == "/post" else None
    run = cli("post", "rev/log", "x", key=team.alice.key)
    assert run.code == 1 and run.last_err.startswith("board: cannot reach ")
    keys = [d["key"] for d in proxy.sent("/post")]
    assert len(keys) == 4 and len(set(keys)) == 1


# --- many writers ------------------------------------------------------------------------

WRITERS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
           "juliet", "kilo", "lima"]


def test_twelve_writers_at_once_all_land(board, team, keyring, proxy):
    """The swarm's size, all signing against the same head: each stale
    signature is signed again, and every post lands once."""
    writers = [board.trusted(keyring[name], name) for name in WRITERS]
    held = threading.Barrier(2, timeout=HANG_TIMEOUT)
    reads = [0]
    lock = threading.Lock()

    def after(ex):  # the first two head reads are answered together: they collide
        if ex.route == "/healthz":
            with lock:
                reads[0] += 1
                first_two = reads[0] <= 2
            if first_two:
                held.wait()

    proxy.after = after
    runs = {}

    def write(agent):
        runs[agent.name] = cli("post", "rev/log", f"from {agent.name}", key=agent.key)

    threads = [threading.Thread(target=write, args=(w,)) for w in writers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(HANG_TIMEOUT)
    assert {name: run.code for name, run in runs.items()} == {name: 0 for name in WRITERS}
    landed = sorted(m["body"] for m in cli("read", "rev/log", "--all").ok().of("message"))
    assert landed == sorted(f"from {name}" for name in WRITERS)
    stale = [ex for ex in proxy.seen if ex.route == "/post" and ex.status == 409]
    assert stale and all(dict(ex.headers).get("Board-Error") == "stale-nonce" for ex in stale)


# --- errors -------------------------------------------------------------------------------

def test_every_refusal_names_its_code_in_the_body_and_a_header(board, team, keyring):
    a = team.alice
    head = board.head_id()
    answers = [
        (request(board.url + "/post", {"path": "rev/log", "body": "x", "key": new_client_key(),
                                       "nonce": str(head)}), 401, "unsigned"),
        (board.send("post", board.signed("post", keyring["stranger"], {
            "path": "rev/log", "body": "x", "key": new_client_key()})), 403, "not-registered"),
        (board.send("post", board.signed("post", a.key, {
            "path": "rev/nope", "body": "x", "key": new_client_key()})), 404, "not-found"),
        (board.send("post", board.signed("post", a.key, {
            "path": "rev/log", "body": "x", "key": new_client_key()}, nonce=head - 1)),
         409, "stale-nonce"),
        (board.send("create", board.signed("create", a.key, {
            "path": "rev/Bad", "kind": "thread", "key": new_client_key()})), 400, "bad-request"),
        (board.get("/n/rev/nope"), 404, "not-found"),
    ]
    for answer, status, code in answers:
        body = answer.json()
        assert (answer.status, body["error"]) == (status, code)
        assert isinstance(body["message"], str) and body["message"]
        assert answer.headers["Board-Error"] == code
    assert board.head_id() == head


# --- the HTTP layer, kept from v1 ------------------------------------------------------------

def exchange(board, wire_bytes, *, shut=False):
    """Send raw bytes, and read the answer until the server closes."""
    with socket.create_connection(("127.0.0.1", board.server.port), timeout=HANG_TIMEOUT) as s:
        s.sendall(wire_bytes)
        if shut:
            s.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := s.recv(65536):
            answer += chunk
        return answer


@pytest.mark.parametrize("length,status", [("+2", b"400"), ("2 2", b"400"),
                                           (str(2 * 1024 * 1024), b"413")])
def test_bad_or_huge_content_lengths_are_refused(board, length, status):
    head = board.head_id()
    answer = exchange(board, f"POST /post HTTP/1.1\r\nHost: b\r\nContent-Length: {length}"
                             "\r\n\r\nhi".encode())
    assert answer.startswith(b"HTTP/1.1 " + status)
    error_answer(*parse_answer(answer))  # the HTTP layer's refusals are v2 errors too
    assert board.head_id() == head


POLL = b"GET /poll?since=0&path=_board/register&timeout=%d HTTP/1.1\r\nHost: b\r\n\r\n"


@pytest.mark.parametrize("close", ["fin", "rst"])
def test_a_client_that_leaves_is_not_logged_as_an_error(board, close):
    """A watcher restarted, a `board watch | head`, a browser tab closed:
    at most one plain line in the log, never a fault."""
    head = board.head_id()
    with socket.create_connection(("127.0.0.1", board.server.port)) as s:
        s.sendall(POLL % 1)
        if close == "rst":  # an abortive close: the server's write gets a reset
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    time.sleep(3)  # the poll times out and is answered into the closed socket
    assert board.head_id() == head
    assert board.server.proc.poll() is None
    err = board.server.err()
    assert "Traceback" not in err and "panicked" not in err and len(err.splitlines()) <= 1


def test_stalled_clients_are_timed_out(board):
    head = board.head_id()
    idle = socket.create_connection(("127.0.0.1", board.server.port), timeout=HANG_TIMEOUT)
    stalled = socket.create_connection(("127.0.0.1", board.server.port), timeout=HANG_TIMEOUT)
    try:
        stalled.sendall(b"GET /healthz HTTP/1.1\r\nHost: b")
        timed_out = stalled.recv(65536)
        assert timed_out.startswith(b"HTTP/1.1 408")
        error_answer(*parse_answer(timed_out))
        assert idle.recv(65536) == b""
    finally:
        idle.close()
        stalled.close()
    assert board.head_id() == head


def serving(board):
    """Whether the board answers /healthz now; a connection turned away at
    the limit may be reset before its 503 is read."""
    try:
        return board.get("/healthz").status == 200
    except OSError:
        return False


def fill_up(board, request_bytes=b""):
    held = []
    for _ in range(400):
        held.append(socket.create_connection(("127.0.0.1", board.server.port), timeout=HANG_TIMEOUT))
        held[-1].sendall(request_bytes)
    probe = socket.create_connection(("127.0.0.1", board.server.port), timeout=HANG_TIMEOUT)
    held.append(probe)
    answer = b""
    try:
        while chunk := probe.recv(65536):  # turned away: the board closes it
            answer += chunk
    except ConnectionResetError:
        pass  # closed with the 503 unread: what was read is the answer
    return held, answer


def test_the_board_turns_away_connections_past_its_limit(board):
    held, answer = fill_up(board)
    try:
        assert answer.startswith(b"HTTP/1.1 503")
        assert b"the board has too many connections open" in answer
        error_answer(*parse_answer(answer))
    finally:
        for s in held:
            s.close()
    wait_until(lambda: serving(board), "the board to serve again")


def test_polls_whose_clients_left_free_their_connections(board):
    held, answer = fill_up(board, POLL % 300)
    for s in held:
        s.close()
    assert answer.startswith(b"HTTP/1.1 503")
    started = time.monotonic()
    wait_until(lambda: serving(board), "the board to serve again")
    assert time.monotonic() - started < 300


# --- the database file failing under the board ------------------------------------------------

def _hold_lock(db):
    """Another program on the database file, holding its write lock."""
    holder = sqlite3.connect(db, isolation_level=None, timeout=HANG_TIMEOUT)
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def test_a_write_waits_out_a_brief_lock(board, team):
    body = board.signed("post", team.alice.key, {"path": "rev/log", "body": "waited",
                                                 "key": new_client_key()})
    holder = _hold_lock(board.db)
    answer = {}
    writer = threading.Thread(target=lambda: answer.update(r=board.send("post", body)))
    writer.start()
    time.sleep(0.5)
    holder.execute("COMMIT")
    holder.close()
    writer.join(HANG_TIMEOUT)
    assert answer["r"].status == 200
    assert [m["body"] for m in board.messages("rev/log")] == ["waited"]


def test_a_lock_held_too_long_is_a_503_that_writes_nothing(board, team):
    head = board.head_id()
    body = board.signed("post", team.alice.key, {"path": "rev/log", "body": "late",
                                                 "key": new_client_key()})
    holder = _hold_lock(board.db)
    try:
        r = board.send("post", body)
        assert r.status == 503 and r.headers["content-type"] == "application/json"
        assert "database busy" in r.text and "nothing was written" in r.text
        assert board.messages("rev/log") == []  # reads go on
        run = cli("post", "rev/log", "late", key=team.alice.key)
        assert run.code == 1 and "503" in run.last_err and "database busy" in run.last_err
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert board.head_id() == head
    assert "database is locked" in board.server.err()
    assert board.send("post", board.signed("post", team.alice.key, {
        "path": "rev/log", "body": "late", "key": body["key"]})).status == 200


def test_a_read_only_database_starts_serves_reads_and_refuses_writes(board, team, keyring):
    """The owner is bound and the board's own nodes made at the first
    start; a later start has nothing to write, so a read-only file still
    starts and serves."""
    team.alice.post("rev/log", "before")
    board.server.stop()
    os.chmod(board.db, 0o444)
    try:
        board.server.start(**owner_env(keyring["owner"]))
        assert [m["body"] for m in board.messages("rev/log")] == ["before"]
        assert board.healthz()["owner"]["fingerprint"] == board.owner.fingerprint
        r = board.send("post", board.signed("post", team.alice.key, {
            "path": "rev/log", "body": "new", "key": new_client_key()}))
        assert r.status == 500 and "readonly database" in r.text
        assert board.get("/view/rev/log").status == 200
        run = cli("post", "rev/log", "new", key=team.alice.key)
        assert run.code == 1 and "500" in run.last_err and "readonly database" in run.last_err
    finally:
        os.chmod(board.db, 0o644)


# --- the server process ---------------------------------------------------------------

def _listening(port):
    """The addresses something listens on for this TCP port, from the
    kernel's own table."""
    found = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            rows = Path(table).read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for row in rows:
            local, state = row.split()[1], row.split()[3]
            address, hex_port = local.rsplit(":", 1)
            if state == "0A" and int(hex_port, 16) == port:  # 0A: LISTEN
                found.add(address)
    return found


def test_an_empty_board_addr_is_the_default_not_every_interface(board, keyring):
    board.restart(BOARD_ADDR="", **owner_env(keyring["owner"]))
    assert _listening(board.server.port) == {"0100007F"}  # 127.0.0.1, not 0.0.0.0


def test_port_zero_logs_the_port_the_system_chose(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
    env.update(BOARD_DB=str(tmp_path / "board-v2.db"), BOARD_PORT="0")
    proc = subprocess.Popen([program("board-server")], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        while not (line := proc.stdout.readline().decode()).startswith("board on "):
            assert line, "board-server never said where it listens"
        port = int(re.match(r"board on http://127\.0\.0\.1:(\d+)", line).group(1))
        assert port != 0
        health = request(f"http://127.0.0.1:{port}/healthz").json()
        assert health["head"] >= FIRST_ID and health["owner"] is None
    finally:
        proc.terminate()
        proc.wait(HANG_TIMEOUT)


def test_the_server_exits_cleanly_on_sigterm(board):
    # It has a handler, so it also stops as PID 1 of a PID namespace (the
    # user unit's PrivatePIDs=), where SIGTERM without one is dropped.
    board.server.stop()
    assert board.server.returncode == 0
