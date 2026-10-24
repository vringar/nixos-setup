"""The blob store on message board v2: files other agents need, addressed by the sha256 of their
bytes, uploaded with `PUT /blob`, referenced from bodies as `board-blob://<64 hex>`, kept alive by
live references (an open item, a thread message that is not archived, the latest version of a
note), and evicted only under upload pressure. See board_harness.

Tiny limits come through the server's environment: BOARD_BLOB_CAP, BOARD_BLOB_MAX and
BOARD_BLOB_GRACE.
"""

import hashlib
import os
import random
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    HANG_TIMEOUT, Pick, Server, Wait, board, cli, cli_bytes, drive_tui, isolated, keyring, owner_env, raw_request,
    refusal, running, sql, team, wait_until, Refused, proxy,
)

pytestmark = pytest.mark.usefixtures("isolated")

MIB = 1 << 20


def data(n, seed=b"seed"):
    """n bytes that depend on `seed`."""
    return hashlib.shake_256(seed).digest(n)


def sha(blob):
    return hashlib.sha256(blob).hexdigest()


def ref(hash_):
    return f"board-blob://{hash_}"


def restart(board, keyring, **env):
    """The board's server again, with these limits (its database and blob directory stay)."""
    board.server.restart(**owner_env(keyring["owner"]), **env)


def head_unchanged(board):
    """A context that fails if the board's head moved."""
    class Held:
        def __enter__(self):
            self.head = board.head_id()

        def __exit__(self, *exc):
            if exc[0] is None:
                assert board.head_id() == self.head

    return Held()


def nothing_stored(board):
    assert board.blob_rows() == [] and board.blob_files() == {} and board.blob_tmp() == []


def start_upload(board, key, blob, **kw):
    """Open an upload, get its `100 Continue`, and send nothing more: the socket."""
    headers = board.blob_headers(key, sha(blob), len(blob), **kw)
    host, port = board.url.removeprefix("http://").split(":")
    sock = socket.create_connection((host, int(port)), timeout=HANG_TIMEOUT)
    head = f"PUT /blob HTTP/1.1\r\nHost: {host}:{port}\r\nExpect: 100-continue\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
    sock.sendall(head.encode())
    assert sock.recv(4096).startswith(b"HTTP/1.1 100"), "no 100 Continue"
    return sock


def settled(board):
    """Wait until no upload is in flight any more: no temp file is left."""
    wait_until(lambda: board.blob_tmp() == [], "the temp files to go")


# --- upload and download ----------------------------------------------------------------------

def test_a_blob_round_trips_under_its_hash(board, team):
    blob = data(5 * MIB)
    answer = team.alice.put(blob, filename="trace.v1.bin", media_type="text/plain")
    h = sha(blob)
    assert answer == {"hash": h, "url": f"/sha256/{h}", "reference": ref(h), "size": len(blob),
                      "media_type": "text/plain", "filename": "trace.v1.bin", "existed": False}
    status, headers, body = board.get_blob(h)
    assert (status, body == blob) == (200, True)
    assert headers["etag"] == f'"{h}"'
    assert headers["content-disposition"] == (
        "attachment; filename=\"trace.v1.bin\"; filename*=UTF-8''trace.v1.bin")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-type"] == "application/octet-stream"  # the stored type is metadata
    assert headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert headers["cache-control"] == "private, immutable, max-age=31536000"
    [row] = board.sql("SELECT size, media_type, filename, uploader, uploader_fp, head FROM blobs "
                      "WHERE sha256=?", h)
    assert row == (len(blob), "text/plain", "trace.v1.bin", "alice", team.alice.fingerprint,
                   board.head_id())
    path = board.blobs_dir / h[:2] / h
    assert path.read_bytes() == blob
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert board.blob_tmp() == []
    # what the board knows of it
    info = board.get_json(f"/blobs/{h}")
    assert (info["hash"], info["size"], info["from"], info["pinned"], info["refs"]) == \
        (h, len(blob), "alice", False, 0)
    assert info["signed_by"] == team.alice.fingerprint and info["signature"]


def test_an_upload_does_not_move_the_head_or_make_an_event(board, team):
    head = board.head_id()
    team.alice.put(data(1000))
    assert board.head_id() == head
    assert board.poll(since=head, path="rev/log")["events"] == []


def test_a_second_put_of_the_same_bytes_reads_no_body_and_changes_nothing_but_the_clock(board, team):
    blob = data(3 * MIB)
    first = team.alice.put(blob, filename="a.bin", media_type="text/plain")
    [(ts_before,)] = board.sql("SELECT ts FROM blobs")
    time.sleep(1.2)
    # another key, another name; the body is never sent and the server never asks for it
    answer = board.put_raw(team.bob.key, blob, filename="other.bin", send_body=False)
    assert (answer.status, answer.continued) == (200, False)
    assert answer.json() == {**first, "existed": True}  # the first uploader's name and type
    [(ts_after, uploader, filename)] = board.sql("SELECT ts, uploader, filename FROM blobs")
    assert ts_after > ts_before and (uploader, filename) == ("alice", "a.bin")
    assert len(board.blob_files()) == 1 and len(board.blob_rows()) == 1
    # a client that sends the body regardless (no Expect) is still answered
    answer = board.put_raw(team.bob.key, blob, expect=False)
    assert (answer.status, answer.json()["existed"]) == (200, True)
    assert team.bob.put(blob)["existed"] is True


def test_identical_uploads_at_once_store_one_file_and_say_so_once(board, team):
    blob = data(20 * MIB)

    def upload(_):
        while True:
            try:
                return team.alice.put(blob)["existed"]
            except Refused as busy:
                assert busy.code == "busy"  # at most four at once; a client asks again
                time.sleep(0.2)

    with ThreadPoolExecutor(8) as pool:
        existed = list(pool.map(upload, range(8)))
    assert existed.count(False) == 1 and existed.count(True) == 7
    assert board.blob_rows() == [(sha(blob), len(blob))]
    assert board.blob_files() == {sha(blob): len(blob)}
    settled(board)


def test_an_interrupted_upload_leaves_nothing_and_the_retry_works(board, team):
    blob = data(4 * MIB)
    sock = start_upload(board, team.alice.key, blob)
    sock.sendall(blob[:2 * MIB])
    wait_until(lambda: board.blob_tmp() != [], "the temp file")
    sock.close()
    settled(board)
    nothing_stored(board)
    assert team.alice.put(blob)["existed"] is False
    assert board.blob_files() == {sha(blob): len(blob)}


def test_a_killed_server_leaves_a_temp_file_that_the_next_start_deletes(board, team, keyring):
    blob = data(4 * MIB)
    sock = start_upload(board, team.alice.key, blob)
    sock.sendall(blob[:MIB])
    wait_until(lambda: board.blob_tmp() != [], "the temp file")
    board.server.proc.kill()
    board.server.proc.wait()
    sock.close()
    assert len(board.blob_tmp()) == 1
    restart(board, keyring)
    nothing_stored(board)
    assert team.alice.put(blob)["existed"] is False


def test_a_second_server_on_the_same_state_exits_with_a_reason(board, keyring, tmp_path):
    second = Server(board.db, tmp_path / "second-logs")
    try:
        second.launch(**owner_env(keyring["owner"]))
        code = second.proc.wait(timeout=HANG_TIMEOUT)
        assert code != 0
        assert "another board-server" in second.err()
    finally:
        second.stop()
    assert board.head_id() > 0  # the first one is untouched


def test_stray_temp_files_are_purged_at_start(board, keyring):
    board.server.stop()
    stray = board.blobs_dir / "tmp" / "left-over.part"
    stray.write_bytes(b"half")
    board.server.start(**owner_env(keyring["owner"]))
    assert not stray.exists()


def test_bytes_that_do_not_hash_to_the_signed_hash_are_not_stored(board, team):
    blob = data(MIB)
    other = data(MIB, b"other")
    answer = board.put_raw(team.alice.key, other, sha256=sha(blob))
    assert answer.status == 400 and answer.json()["error"] == "hash-mismatch"
    assert (answer.json()["expected"], answer.json()["got"]) == (sha(blob), sha(other))
    assert answer.headers["board-error"] == "hash-mismatch"
    settled(board)
    nothing_stored(board)


# --- what an upload is refused for, before a byte of the body ---------------------------------

def refused_early(board, key, blob, status, code, **kw):
    answer = board.put_raw(key, blob, send_body=False, **kw)
    assert (answer.status, answer.continued) == (status, False), answer
    assert answer.json()["error"] == code and answer.headers["board-error"] == code
    nothing_stored(board)
    return answer


def test_a_missing_length_a_chunked_body_and_a_bad_address_are_refused_early(board, team):
    blob, a = data(1000), team.alice
    refused_early(board, a.key, blob, 411, "length-required", drop=("Content-Length",))
    refused_early(board, a.key, blob, 501, "not-implemented", drop=("Content-Length",),
                  headers={"Transfer-Encoding": "chunked"})
    for bad in (sha(blob).upper(), sha(blob)[:63], "../../etc/passwd", ""):
        refused_early(board, a.key, blob, 400, "bad-request", headers={"Board-Sha256": bad})
    refused_early(board, a.key, b"", 400, "bad-request")


def test_a_bad_file_name_or_media_type_is_refused_not_rewritten(board, team):
    blob, a = data(1000), team.alice
    for name in ("a/b", "a\\b", "x" * 300):
        refused_early(board, a.key, blob, 400, "bad-request", filename=name)
    refused_early(board, a.key, blob, 400, "bad-request", headers={"Board-Filename": "a%0Ab"})
    refused_early(board, a.key, blob, 400, "bad-request", headers={"Board-Filename": "%zz"})
    for media in ("text", "a b/c", "a/b/c", "/x"):
        refused_early(board, a.key, blob, 400, "bad-request", headers={"Content-Type": media})
    assert not list(board.blobs_dir.glob("??"))  # nothing touched the file system


def test_a_blob_over_the_limit_is_refused_before_it_is_sent(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_MAX=str(MIB))
    refused = refused_early(board, team.alice.key, data(MIB + 1), 413, "blob-too-large")
    assert (refused.json()["size"], refused.json()["max"]) == (MIB + 1, MIB)
    assert team.alice.put(data(MIB))["existed"] is False


def test_signature_and_identity_are_checked_before_the_body(board, team, keyring):
    blob, a = data(1000), team.alice
    refused_early(board, a.key, blob, 401, "unsigned", drop=("Board-Signature",))
    refused_early(board, a.key, blob, 400, "bad-request",
                  headers={"Board-Signature": board.blob_headers(a.key, sha(blob), 999)["Board-Signature"]})
    refused_early(board, keyring["stranger"], blob, 403, "not-registered")
    board.register(keyring["newbie"], "newbie")
    refused_early(board, keyring["newbie"], blob, 403, "registration-pending")
    team.owner.revoke(team.carol.fingerprint)
    refused_early(board, team.carol.key, blob, 403, "key-revoked")
    # a signature for another action is no upload's
    other = board.blob_headers(a.key, sha(blob), len(blob))
    other["Board-Signature"] = a.key.sign("board2-post", b"x")
    refused_early(board, a.key, blob, 400, "bad-request", headers=other)


def test_a_stale_nonce_is_refused_early_and_the_cli_signs_again(board, team, proxy, tmp_path):
    blob = data(1000)
    refused = refused_early(board, team.alice.key, blob, 409, "stale-nonce", nonce=board.head_id() - 1)
    assert refused.json()["head"] == board.head_id()
    # the head moves between the CLI's read of it and its upload, once
    moved = []

    def before(exchange):
        if exchange.method == "PUT" and not moved:
            moved.append(team.bob.post("rev/log", "a write that lands in between"))

    proxy.before = before
    (tmp_path / "f").write_bytes(blob)
    run = cli("put", tmp_path / "f", key=team.alice.key).ok().result
    assert run["existed"] is False and moved
    assert [ex.status for ex in proxy.seen if ex.method == "PUT"] == [409, 200]


def test_a_full_store_refuses_before_the_body_and_names_what_pins_it(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB))
    a = team.alice
    pinned = data(700_000)
    a.post("rev/log", f"keep {ref(a.put(pinned)['hash'])}")
    refused = refused_early_with(board, a.key, data(500_000), 507, "blob-store-full")
    body = refused.json()
    assert (body["used"], body["cap"], body["needed"], body["pinned"]) == (700_000, MIB, 500_000, 700_000)
    assert body["pinned_by"] == [{"path": "rev/log", "bytes": 700_000, "blobs": 1}]
    assert "rev/log" in body["message"] and "archive or delete those messages" in body["message"]


def refused_early_with(board, key, blob, status, code, **kw):
    """refused_early, for a board that holds other blobs."""
    answer = board.put_raw(key, blob, send_body=False, **kw)
    assert (answer.status, answer.continued) == (status, False), answer
    assert answer.json()["error"] == code
    assert sha(blob) not in dict(board.blob_rows())
    return answer


def test_a_fifth_upload_at_once_is_turned_away_busy(board, team):
    held = [start_upload(board, team.alice.key, data(MIB, bytes([n]))) for n in range(4)]
    try:
        answer = board.put_raw(team.alice.key, data(MIB, b"fifth"), send_body=False)
        assert (answer.status, answer.json()["error"]) == (503, "busy")
        assert answer.headers["retry-after"] == "5"
    finally:
        for sock in held:
            sock.close()
    settled(board)
    assert team.alice.put(data(MIB, b"fifth"))["existed"] is False


# --- references in bodies -----------------------------------------------------------------------

@pytest.fixture
def stored(board, team):
    blob = data(2000, b"ref")
    return team.alice.put(blob, filename="ref.bin")["hash"]


def refs_of(board, event):
    return board.sql("SELECT sha256 FROM blob_refs WHERE event=? ORDER BY sha256", event)


def test_the_reference_grammar(board, team, stored):
    a, h = team.alice, stored
    for body in (ref(h), f"see {ref(h)}", f"({ref(h)})", f"[t]({ref(h)})", f'"{ref(h)}"', f"x={ref(h)}",
                 f"{ref(h)}.", f"{ref(h)},", f"{ref(h)}).", f"<{ref(h)}>", f"a\n{ref(h)}\nb",
                 f"'{ref(h)}'", f"{ref(h)}*", f"`{ref(h)}`"):
        assert refs_of(board, a.post("rev/log", body)) == [(h,)], body
    # these are text: the scheme is part of a longer word, a path, or is escaped
    for body in (f"x{ref(h)}", f"my-{ref(h)}", f"a.{ref(h)}", f"a+{ref(h)}", f"\\{ref(h)}", f"a_{ref(h)}"):
        assert refs_of(board, a.post("rev/log", body)) == [], body
    for body in (ref(h[:63]), ref(h + "0"), ref(h.upper()), ref(h[:12]), ref(h) + ".bin", ref(h) + "/x",
                 ref(h) + "#x", ref(h) + "%20", ref(h) + "é", "see board-blob:// here"):
        with head_unchanged(board), refusal(400, "bad-reference"):
            a.post("rev/log", body)
    many = " ".join(ref(f"{n:064x}") for n in range(33))
    with head_unchanged(board), refusal(400, "bad-reference") as r:
        a.post("rev/log", many)
    assert "32" in r.value.message


def test_a_reference_to_no_blob_is_refused_with_nothing_written(board, team):
    h = "ab" * 32
    a, b = team.alice, team.bob
    b.create("rev/bob", "queue")
    b.create("rev/state", "note")
    item = a.ask("rev/bob", "plain question")
    other = a.ask("rev/bob", "another question")
    third = a.ask("rev/bob", "a third")
    writes = [
        lambda: a.post("rev/log", f"look {ref(h)}"),
        lambda: a.ask("rev/bob", f"look {ref(h)}"),
        lambda: b.answer(item, f"here {ref(h)}"),
        lambda: b.decline(other, f"no, but {ref(h)}"),
        lambda: a.cancel(third, f"never mind {ref(h)}"),
        lambda: b.set_note("rev/state", f"state {ref(h)}", 0),
        lambda: a.create("rev/new", "note", body=f"first {ref(h)}"),
    ]
    for write in writes:
        with head_unchanged(board), refusal(400, "unknown-blob") as r:
            write()
        assert r.value["hash"] == h and "board put" in r.value.message
    assert board.sql("SELECT count(*) FROM blob_refs") == [(0,)]


def test_references_are_recorded_when_a_body_is_written_and_never_made_on_read(board, team):
    a, b = team.alice, team.bob
    one, two = (a.put(data(1500, bytes([n])))["hash"] for n in range(2))
    b.create("rev/bob", "queue")
    b.create("rev/state", "note")
    message = a.post("rev/log", f"{ref(one)} and {ref(two)} and {ref(one)}")
    item = a.ask("rev/bob", f"file {ref(one)}")
    answer = b.answer(item, f"ok {ref(two)}")["id"]
    version = b.set_note("rev/state", f"state {ref(one)}", 0)["id"]
    first = a.create("rev/first", "note", body=f"first {ref(two)}")
    assert refs_of(board, message) == sorted([(one,), (two,)])
    assert refs_of(board, item) == [(one,)] and refs_of(board, answer) == [(two,)]
    assert refs_of(board, version) == [(one,)]
    [(note_version,)] = board.sql("SELECT id FROM events WHERE node=? AND type='note_version'",
                                  board.node_id("rev/first"))
    assert refs_of(board, note_version) == [(two,)] and first["id"] < note_version
    rows = board.sql("SELECT * FROM blob_refs ORDER BY event, sha256")
    for _ in range(2):
        cli("read", "rev/log", "--all").ok()
        cli("show", message).ok()
        cli("brief", "rev", key=a.key).ok()
        board.get("/")
    assert board.sql("SELECT * FROM blob_refs ORDER BY event, sha256") == rows


def lines_after(run, kind, event):
    """The `blob_ref` lines that follow the `kind` line of `event`."""
    out, seen = [], False
    for line in run.lines:
        if seen and line["type"] == "blob_ref":
            out.append(line)
        elif seen:
            break
        seen = seen or (line["type"] == kind and line.get("id") == event)
    return out


def test_reads_show_what_each_reference_stands_at(board, team):
    a, b = team.alice, team.bob
    blob = data(1234, b"x")
    h = a.put(blob, filename="x.bin")["hash"]
    b.create("rev/bob", "queue")
    b.create("rev/state", "note")
    message = a.post("rev/log", f"see {ref(h)}")
    a.post("rev/log", "no files here")
    item = a.ask("rev/bob", f"file {ref(h)}")
    b.set_note("rev/state", f"state {ref(h)}", 0)
    expected = {"type": "blob_ref", "event": message, "hash": h, "reference": ref(h), "state": "pinned",
                "size": 1234, "filename": "x.bin"}
    thread = cli("read", "rev/log", "--all").ok()
    assert lines_after(thread, "message", message) == [expected]
    assert list(lines_after(thread, "message", message)[0]) == list(expected)
    assert [line["type"] for line in thread.lines] == ["message", "blob_ref", "message", "cursor"]
    shown = cli("show", message).ok()
    assert [line["type"] for line in shown.lines] == ["message", "blob_ref"]
    assert shown.of("blob_ref") == [expected]
    queue = cli("read", "rev/bob", key=b.key).ok()
    assert lines_after(queue, "item", item) == [{**expected, "event": item}]
    note = cli("read", "rev/state").ok()
    assert [line["type"] for line in note.lines] == ["note", "blob_ref", "cursor"]
    brief = cli("brief", "rev", key=b.key).ok()
    assert [r["state"] for r in brief.of("blob_ref")] == ["pinned", "pinned"]  # the open item, the note
    # a resolved item's references are released, and a removed blob's are gone
    b.answer(item, f"done {ref(h)}")
    shown = cli("show", item).ok()
    assert [r["state"] for r in shown.of("blob_ref")] == ["pinned", "pinned"]  # the message pins it still
    board.sql("DELETE FROM blobs")
    gone = cli("show", item).ok().of("blob_ref")
    assert gone and all((r["state"], r["size"], r["filename"]) == ("gone", None, None) for r in gone)


def test_blobs_lists_newest_first_with_filters_and_a_summary(board, team):
    a = team.alice
    kept = a.put(data(1000, b"kept"), filename="kept.bin")["hash"]
    time.sleep(1.1)
    loose = a.put(data(2000, b"loose"), filename="loose.bin")["hash"]
    a.post("rev/log", f"keep {ref(kept)}")
    listed = cli("blobs", key=a.key).ok().of("blob")
    assert [b["hash"] for b in listed] == [loose, kept]
    first = listed[0]
    assert list(first) == ["type", "hash", "url", "reference", "size", "media_type", "filename", "from",
                           "signed_by", "revoked_at", "ts", "refs", "pinned"]
    assert first["url"] == f"{board.url}/sha256/{loose}" and first["reference"] == ref(loose)
    assert (first["from"], first["refs"], first["pinned"]) == ("alice", 0, False)
    assert [b["hash"] for b in cli("blobs", "--unreferenced").ok().of("blob")] == [loose]
    assert cli("blobs", "--evictable").ok().of("blob") == []  # inside the grace
    mine = cli("blobs", "--mine", key=team.bob.key).ok()
    assert mine.lines == []
    assert len(cli("blobs", "--mine", key=a.key).ok().of("blob")) == 2
    limited = cli("blobs", "--limit", "1")
    assert [b["hash"] for b in limited.of("blob")] == [loose] and "more than --limit" in limited.err
    summary = cli("blobs", "--summary").ok().result
    assert summary == {"type": "blobsummary", "used": 3000, "cap": 50_000_000_000, "count": 2,
                       "pinned_bytes": 1000, "grace_secs": 3600, "max": 8 * 1024 ** 3}
    # the read side is open: nobody's key is needed
    assert board.blobs()["count"] == 2 and board.get_json(f"/blobs/{kept}")["pinned"] is True


# --- download ranges ----------------------------------------------------------------------------

def test_ranges_etags_and_unsatisfiable_ranges(board, team):
    blob = data(100_000)
    h = team.alice.put(blob)["hash"]
    for header, a, b in (("bytes=10-19", 10, 19), ("bytes=99990-", 99_990, 99_999),
                         ("bytes=-10", 99_990, 99_999), ("bytes=5-1000000", 5, 99_999)):
        status, headers, body = board.get_blob(h, Range=header)
        assert (status, body) == (206, blob[a:b + 1]), header
        assert headers["content-range"] == f"bytes {a}-{b}/100000"
    status, headers, body = board.get_blob(h, Range="bytes=100000-")
    assert (status, body, headers["content-range"]) == (416, b"", "bytes */100000")
    status, _, body = board.get_blob(h, Range="bytes=0-1,5-6")  # several: ignored
    assert (status, body == blob) == (200, True)
    status, headers, body = board.get_blob(h, **{"If-None-Match": f'"{h}"'})
    assert (status, body, headers["etag"]) == (304, b"", f'"{h}"')
    assert board.get_blob(h, **{"If-None-Match": '"other"'})[0] == 200
    # not a hash, and a hash nothing was stored under
    assert board.get_blob("../../etc/passwd")[0] == 404
    assert board.get_blob(h.upper())[0] == 404
    status, _, body = board.get_blob("cd" * 32)
    assert status == 404 and b"no such blob" in body and b"reference it from a note" in body


def test_a_row_without_its_file_is_a_500_not_a_state(board, team):
    h = team.alice.put(data(1000))["hash"]
    path = board.blobs_dir / h[:2] / h
    path.chmod(0o600)
    path.unlink()
    status, headers, body = board.get_blob(h)
    assert (status, headers["board-error"]) == (500, "blob-missing")
    assert "blob-missing" in board.server.log() or "has a row but" in board.server.log()


# --- who deletes what ---------------------------------------------------------------------------

def test_delete_by_the_uploader_or_the_owner_and_never_while_pinned(board, team):
    a, b, o = team.alice, team.bob, team.owner
    gone = a.put(data(1000, b"1"))["hash"]
    with refusal(403, "forbidden"):
        b.delete_blob(gone)
    assert a.delete_blob(gone) == {"hash": gone}
    assert board.blob_rows() == [] and board.blob_files() == {}
    status, _, body = board.get_blob(gone)
    assert status == 404 and b"keep a file" in body
    with refusal(404, "not-found"):
        a.delete_blob(gone)
    # the owner deletes anyone's
    theirs = b.put(data(1000, b"2"))["hash"]
    assert o.delete_blob(theirs) == {"hash": theirs}
    # pinned by a message: refused for the uploader and the owner alike
    h = a.put(data(1000, b"3"))["hash"]
    first = a.post("rev/log", f"keep {ref(h)}")
    second = a.post("rev/log", f"and {ref(h)}")
    for who in (a, o):
        with refusal(409, "blob-in-use") as r:
            who.delete_blob(h)
        assert r.value["refs"] == [{"event": first, "path": "rev/log"}, {"event": second, "path": "rev/log"}]
        assert r.value["total"] == 2 and "2 live holders" in r.value.message
    assert board.blob_files() == {h: 1000}
    # archived messages release it, and then it can go; the same bytes can come back
    o.archive(first)
    o.archive(second)
    assert a.delete_blob(h) == {"hash": h}
    assert a.put(data(1000, b"3"))["existed"] is False
    # the CLI says the same
    c = cli("blob-rm", h, key=a.key).ok().result
    assert c == {"type": "blob_removed", "hash": h}
    run = cli("blob-rm", h, key=a.key)
    assert run.code == 1 and run.last_err.startswith("board: not-found:")


def test_a_message_hard_deleted_in_board_admin_frees_its_blob(tmp_path, keyring):
    db = tmp_path / "board-v2.db"
    with running(db, tmp_path / "logs", keyring["owner"]) as b:
        alice = b.trusted(keyring["alice"], "alice")
        b.owner.create("proj", "container")
        alice.create("proj/chatter", "thread")
        h = alice.put(data(1000))["hash"]
        message = alice.post("proj/chatter", f"noise {ref(h)}")
        with refusal(409, "blob-in-use"):
            alice.delete_blob(h)
    env = {**os.environ, "BOARD_DB": str(db), "BOARD_ADMIN_SERVICE": "",
           "BOARD_ADMIN_LOG": str(tmp_path / "audit.log")}
    run = drive_tui(env, [Wait("_board"), Pick("proj"), Pick("chatter"), Pick(f"#{message}"), b"d",
                          Wait("Delete permanently? [y/N]"), b"y", *[b"q"] * 5])
    assert run.code == 0
    assert sql(db, "SELECT count(*) FROM blob_refs") == [(0,)]
    with running(db, tmp_path / "logs2", keyring["owner"]) as b:
        alice = b.as_key(keyring["alice"], "alice")
        assert alice.delete_blob(h) == {"hash": h}


# --- what keeps a blob ----------------------------------------------------------------------------

def states(board, event, key=None):
    run = cli("show", event, key=key).ok()
    return [(r["event"], r["state"]) for r in run.of("blob_ref")]


def test_an_open_item_pins_and_resolving_releases_both_filing_and_answer(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a, b = team.alice, team.bob
    b.create("rev/bob", "queue")
    f = a.put(data(300_000, b"filed"))["hash"]
    time.sleep(1.1)
    r = a.put(data(300_000, b"answer"))["hash"]
    item = a.ask("rev/bob", f"look {ref(f)}")
    with refusal(409, "blob-in-use"):
        a.delete_blob(f)
    resolution = b.answer(item, f"here {ref(r)}")["id"]
    assert states(board, item) == [(item, "released"), (resolution, "released")]
    assert board.get_blob(f)[0] == 200 and board.get_blob(r)[0] == 200  # the links still work
    # and once the store is under pressure and the grace is over, they are gone
    time.sleep(1.2)
    a.put(data(900_000, b"pressure"))
    assert states(board, item) == [(item, "gone"), (resolution, "gone")]
    assert board.get_blob(f)[0] == 404 and board.get_blob(r)[0] == 404
    # declining and cancelling release as well
    keep = a.put(data(100_000, b"keep"))["hash"]
    declined, cancelled = a.ask("rev/bob", f"x {ref(keep)}"), a.ask("rev/bob", f"y {ref(keep)}")
    with refusal(409, "blob-in-use"):
        a.delete_blob(keep)
    b.decline(declined, "no")
    with refusal(409, "blob-in-use"):  # the other open item still pins it
        a.delete_blob(keep)
    a.cancel(cancelled)
    assert a.delete_blob(keep) == {"hash": keep}


def test_a_blob_only_an_answer_references_is_protected_by_the_grace_alone(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="3")
    a, b = team.alice, team.bob
    b.create("rev/bob", "queue")
    old = a.put(data(400_000, b"old"))["hash"]
    time.sleep(3.2)
    young = a.put(data(400_000, b"young"))["hash"]
    first, second = a.ask("rev/bob", "one?"), a.ask("rev/bob", "two?")
    b.answer(first, f"see {ref(old)}")
    b.answer(second, f"see {ref(young)}")
    # unpinning did not restart the clock: the old one is evictable at once, the young one not
    board.sql("UPDATE blobs SET ts=ts")  # (nothing: the age is the upload time)
    assert [x["hash"] for x in cli("blobs", "--evictable").ok().of("blob")] == [old]
    a.put(data(400_000, b"pressure"))  # evicts the old one only
    assert dict(board.blob_rows()).keys() >= {young} and old not in dict(board.blob_rows())
    # the answer's submission still needed the blob to exist
    third = a.ask("rev/bob", "three?")
    with refusal(400, "unknown-blob"):
        b.answer(third, f"see {ref(old)}")


def test_a_thread_message_pins_until_it_is_archived(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a = team.alice
    h = a.put(data(600_000))["hash"]
    message = a.post("rev/log", f"keep {ref(h)}")
    time.sleep(1.2)
    with refusal(507, "blob-store-full"):
        a.put(data(600_000, b"other"))
    assert states(board, message) == [(message, "pinned")]
    team.owner.archive(message)
    assert states(board, message) == [(message, "released")]
    a.put(data(600_000, b"other"))
    assert states(board, message) == [(message, "gone")]
    assert cli("read", "rev/log", "--all", "--archived").ok().of("blob_ref")[0]["archived"] is True


def test_saving_a_note_replaces_its_pins_and_old_versions_show_released_then_gone(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a = team.alice
    h = a.put(data(600_000))["hash"]
    a.create("rev/state", "note")
    v1 = a.set_note("rev/state", f"state {ref(h)}", 0)
    time.sleep(1.2)
    with refusal(507, "blob-store-full"):  # pinned by the latest version
        a.put(data(600_000, b"other"))
    # saved again with the link: still pinned
    v2 = a.set_note("rev/state", f"state again {ref(h)}", 1)
    assert states(board, v2["id"]) == [(v2["id"], "pinned")]
    assert states(board, v1["id"]) == [(v1["id"], "pinned")]  # nothing else pins it, but it is pinned
    # saved without it: released, and the history shows the old version's reference released
    v3 = a.set_note("rev/state", "no files any more", 2)
    old = cli("read", "rev/state", "--version", "1").ok()
    assert [(r["event"], r["state"]) for r in old.of("blob_ref")] == [(v1["id"], "released")]
    assert board.get_blob(h)[0] == 200
    a.put(data(600_000, b"other"))  # now there is room
    old = cli("read", "rev/state", "--version", "1").ok()
    assert [(r["event"], r["state"]) for r in old.of("blob_ref")] == [(v1["id"], "gone")]
    assert v3["version"] == 3


def test_a_files_note_keeps_its_blobs_through_pressure(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a = team.alice
    h = a.put(data(600_000))["hash"]
    team.owner.create("rev/files", "note", body=f"files:\n- {ref(h)}\n")
    time.sleep(1.2)
    with refusal(507, "blob-store-full"):
        a.put(data(600_000, b"other"))
    assert board.get_blob(h)[0] == 200


# --- the cap and eviction -------------------------------------------------------------------------

def test_eviction_takes_the_oldest_unpinned_past_the_grace_and_only_under_pressure(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a = team.alice
    blobs = {}
    for name in "abc":
        blobs[name] = a.put(data(300_000, name.encode()))["hash"]
        time.sleep(1.1)
    # no pressure, no eviction, however long it has been
    assert set(dict(board.blob_rows())) == set(blobs.values())
    # a new one needs room: the oldest goes
    blobs["d"] = a.put(data(300_000, b"d"))["hash"]
    assert set(dict(board.blob_rows())) == {blobs[n] for n in "bcd"}
    assert set(board.blob_files()) == {blobs[n] for n in "bcd"}
    # an upload of the same bytes again protects: b is the newest now, so c is next
    assert a.put(data(300_000, b"b"))["existed"] is True
    time.sleep(1.1)
    blobs["e"] = a.put(data(300_000, b"e"))["hash"]
    assert set(dict(board.blob_rows())) == {blobs[n] for n in "bde"}
    assert board.sql("SELECT count(*) FROM blobs WHERE sha256=?", blobs["c"]) == [(0,)]


def test_a_pinned_blob_is_never_evicted_and_everything_pinned_refuses(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a = team.alice
    team.owner.create("rev/other", "thread")
    one, two = a.put(data(400_000, b"1"))["hash"], a.put(data(400_000, b"2"))["hash"]
    a.post("rev/log", f"big {ref(one)}")
    a.post("rev/other", f"small {ref(two)}")
    time.sleep(1.2)
    rows = board.blob_rows()
    with refusal(507, "blob-store-full") as r:
        a.put(data(400_000, b"3"))
    assert r.value["pinned_by"][0] == {"path": "rev/log", "bytes": 400_000, "blobs": 1}
    assert {p["path"] for p in r.value["pinned_by"]} == {"rev/log", "rev/other"}
    assert r.value["evictable"] == 0 and "nothing can be evicted" in r.value.message
    assert board.blob_rows() == rows


def test_within_the_grace_nothing_is_evicted_and_a_refused_or_aborted_upload_evicts_nothing(board, team,
                                                                                            keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="3600")
    a = team.alice
    for n in range(3):
        a.put(data(300_000, bytes([n])))
    rows = board.blob_rows()
    with refusal(507, "blob-store-full") as r:
        a.put(data(300_000, b"new"))
    assert r.value["in_grace"] == 900_000 and r.value["evictable"] == 0
    assert "under the 1 h grace" in r.value.message
    assert board.blob_rows() == rows
    # an aborted upload that would have needed room evicts nothing either
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    time.sleep(1.2)
    sock = start_upload(board, a.key, data(300_000, b"abort"))
    sock.sendall(b"x" * 1000)
    sock.close()
    settled(board)
    assert board.blob_rows() == rows
    # and a hash mismatch evicts nothing
    answer = board.put_raw(a.key, data(300_000, b"liar"), sha256=sha(data(300_000, b"other")))
    assert answer.json()["error"] == "hash-mismatch" and board.blob_rows() == rows


def test_a_reference_that_lands_between_the_plan_and_the_commit_saves_the_blob(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a = team.alice
    victim = a.put(data(600_000, b"victim"))["hash"]
    time.sleep(1.2)
    newcomer = data(600_000, b"newcomer")
    sock = start_upload(board, a.key, newcomer)  # planned: the victim can make room
    a.post("rev/log", f"keep {ref(victim)}")  # ... and now it is pinned
    sock.sendall(newcomer)
    answer = sock.recv(65536).decode()
    sock.close()
    assert answer.startswith("HTTP/1.1 507") and "blob-store-full" in answer
    assert dict(board.blob_rows()).keys() == {victim}
    assert board.get_blob(victim)[0] == 200
    settled(board)


def test_unarchiving_needs_the_files_back(board, team, keyring):
    restart(board, keyring, BOARD_BLOB_CAP=str(MIB), BOARD_BLOB_GRACE="1")
    a, o = team.alice, team.owner
    blob = data(600_000, b"gone")
    h = a.put(blob)["hash"]
    message = a.post("rev/log", f"keep {ref(h)}")
    o.archive(message)
    time.sleep(1.2)
    a.put(data(600_000, b"pressure"))  # the released blob goes
    with refusal(409, "blob-gone") as r:
        o.unarchive(message)
    assert r.value["hashes"] == [h] and ref(h) in r.value.message and "board put" in r.value.message
    assert [m["id"] for m in board.messages("rev/log")] == []
    time.sleep(1.2)
    # upload the same bytes again, and it comes back
    a.delete_blob(sha(data(600_000, b"pressure")))
    a.put(blob)
    assert o.unarchive(message)["archived"] is False
    assert [r["state"] for r in cli("show", message).ok().of("blob_ref")] == ["pinned"]


# --- never a link to a missing file -----------------------------------------------------------

def test_a_reference_is_never_shown_as_a_link_to_a_file_that_is_gone(board, team, keyring):
    """A random run of uploads, references, resolves, note saves, deletes, evictions and archives on
    a small store: after every step each reference reads as what the file system holds."""
    restart(board, keyring, BOARD_BLOB_CAP=str(2 * MIB), BOARD_BLOB_GRACE="1")
    rng = random.Random(20261024)
    a, b, o = team.alice, team.bob, team.owner
    b.create("rev/bob", "queue")
    a.create("rev/state", "note")
    uploaded, messages, items, version = [], [], [], [0]

    def pick():
        return rng.choice(uploaded) if uploaded else "00" * 32

    def upload():
        blob = data(rng.choice([200_000, 400_000, 700_000]), str(rng.random()).encode())
        try:
            uploaded.append(a.put(blob)["hash"])
        except Refused as full:
            assert full.code == "blob-store-full"

    def reference(write):
        try:
            return write(pick())
        except Refused as bad:
            assert bad.code == "unknown-blob"

    def post():
        if (m := reference(lambda h: a.post("rev/log", f"msg {ref(h)}"))):
            messages.append(m)

    def ask():
        if (i := reference(lambda h: a.ask("rev/bob", f"q {ref(h)}"))):
            items.append(i)

    def resolve():
        if items:
            item = items.pop(rng.randrange(len(items)))
            reference(lambda h: b.answer(item, f"a {ref(h)}") if rng.random() < .6 else b.decline(item, "no"))

    def note():
        body = f"state {ref(pick())}" if rng.random() < .5 else f"plain {rng.random()}"
        try:
            version[0] = a.set_note("rev/state", body, version[0])["version"]
        except Refused as bad:
            assert bad.code == "unknown-blob"

    def delete():
        try:
            a.delete_blob(pick())
        except Refused as bad:
            assert bad.code in ("blob-in-use", "not-found")

    def archive():
        if messages:
            message = rng.choice(messages)
            try:
                o.archive(message)
            except Refused:  # already archived
                try:
                    o.unarchive(message)
                except Refused as bad:
                    assert bad.code == "blob-gone"

    def wait_out_the_grace():
        time.sleep(1.1)

    steps = [upload, upload, upload, post, post, ask, resolve, note, delete, archive, wait_out_the_grace]
    for step_no in range(60):
        rng.choice(steps)()
        check_references(board, step_no)
    page = board.get("/ui/messages?path=rev/log&archived=1&all=1").text
    for hash_ in uploaded:
        exists = board.get_blob(hash_)[0] == 200
        assert (f'href="/sha256/{hash_}"' in page) <= exists, hash_


def check_references(board, step):
    """Every reference reads as pinned or released only if its file is there, gone only if it is not."""
    events = [e for (e,) in board.sql("SELECT DISTINCT event FROM blob_refs")]
    for start in range(0, len(events), 100):
        refs = board.get_json("/blob-refs", event=events[start:start + 100])["refs"]
        for r in refs:
            status, _, body = board.get_blob(r["hash"])
            if r["state"] == "gone":
                assert status == 404 and b"no such blob" in body, (step, r)
                assert r["size"] is None
            else:
                assert status == 200 and len(body) == r["size"], (step, r)
    # every blob a live holder pins is there, by an oracle that does not use the server's query
    pinned = board.sql("""
        SELECT DISTINCT r.sha256 FROM blob_refs r JOIN events e ON e.id = r.event
         WHERE e.archived IS NULL AND (e.type = 'message'
            OR (e.type = 'item_filed' AND (SELECT state FROM items WHERE id = e.id) = 'open')
            OR (e.type = 'note_version' AND e.id = (SELECT max(id) FROM events
                                                    WHERE node = e.node AND type = 'note_version')))""")
    stored = {h for h, _ in board.blob_rows()}
    assert {h for (h,) in pinned} <= stored, (step, pinned, stored)
    assert set(board.blob_files()) == stored, step


# --- the web view ---------------------------------------------------------------------------------

def test_the_web_view_links_what_exists_and_says_what_was_removed(board, team, keyring):
    a = team.alice
    there = a.put(data(1234, b"there"), filename="<i>x&y.bin")["hash"]
    removed = a.put(data(10, b"removed"))["hash"]
    message = a.post("rev/log", f"get {ref(there)}, not {ref(removed)}. \\{ref(there)} <script>")
    board.sql("DELETE FROM blobs WHERE sha256=?", removed)
    for page in (board.get("/view/rev/log").text, board.get("/ui/messages?path=rev/log").text):
        assert f'<a class="blob" href="/sha256/{there}" download>&lt;i&gt;x&amp;y.bin (1.2 kB)</a>,' in page
        assert f'{ref(removed)} <span class="gone">(file removed)</span>.' in page
        assert f"\\{ref(there)}" in page and page.count("<a class=\"blob\"") == 1  # the escaped form is text
        assert "<script>" not in page and "&lt;script&gt;" in page
    assert message > 0


def test_the_web_view_links_item_and_note_bodies_too(board, team):
    a, b = team.alice, team.bob
    h = a.put(data(100, b"w"))["hash"]
    b.create("rev/bob", "queue")
    a.create("rev/state", "note", body=f"note {ref(h)}")
    item = a.ask("rev/bob", f"ask {ref(h)}")
    b.answer(item, f"answer {ref(h)}")
    assert board.get("/view/rev/state").text.count(f'href="/sha256/{h}"') == 1
    queue = board.get("/view/rev/bob").text
    assert queue.count(f'href="/sha256/{h}"') == 2  # the filing and the answer
    assert board.get("/ui/note?path=rev/state").text.count(f'href="/sha256/{h}"') == 1


# --- the command line -----------------------------------------------------------------------------

def test_put_get_and_blobs_on_the_command_line(board, team, tmp_path):
    a = team.alice
    src = tmp_path / "trace.bin"
    src.write_bytes(data(3 * MIB))
    h = sha(src.read_bytes())
    put = cli("put", src, key=a.key).ok().result
    assert list(put) == ["type", "hash", "url", "reference", "size", "media_type", "filename", "existed"]
    assert (put["hash"], put["url"], put["reference"], put["filename"], put["existed"]) == \
        (h, f"{board.url}/sha256/{h}", ref(h), "trace.bin", False)
    again = cli("put", src, "--name", "renamed.bin", key=team.bob.key).ok().result
    assert again["existed"] is True and again["filename"] == "trace.bin"
    out = tmp_path / "copy.bin"
    got = cli("get", ref(h), "-o", out, key=a.key).ok().result
    assert (got["type"], got["hash"], got["size"], "existed" not in got) == ("blob", h, 3 * MIB, True)
    assert out.read_bytes() == src.read_bytes() and not (tmp_path / "copy.bin.part").exists()
    # standard output carries the bytes, and the blob's line goes to stderr
    code, stdout, err = cli_bytes("get", h, "-o", "-", key=a.key)
    assert (code, stdout) == (0, src.read_bytes()) and '"type":"blob"' in err
    # without -o a redirected standard output gets the bytes as well
    assert cli_bytes("get", ref(h))[1] == src.read_bytes()
    # a hash that no blob has
    run = cli("get", "cd" * 32, "-o", tmp_path / "no")
    assert run.code == 1 and run.last_err.startswith("board: not-found: no such blob")
    assert not (tmp_path / "no").exists() and not (tmp_path / "no.part").exists()
    bad = cli("get", "abc", "-o", tmp_path / "x")
    assert bad.code == 2


def test_put_reads_standard_input_and_names_the_file_as_asked(board, team):
    put = cli("put", "-", "--name", "from stdin.txt", "--type", "text/plain; charset=utf-8",
              stdin="hello blob\n", key=team.alice.key).ok().result
    h = sha(b"hello blob\n")
    assert (put["hash"], put["filename"], put["media_type"], put["size"]) == \
        (h, "from stdin.txt", "text/plain", 11)
    status, headers, body = board.get_blob(h)
    assert body == b"hello blob\n"
    assert 'filename="from stdin.txt"' in headers["content-disposition"]
    unnamed = cli("put", "-", stdin="no name", key=team.alice.key).ok().result
    assert unnamed["filename"] == ""
    assert 'filename="' + sha(b"no name") + '"' in board.get_blob(sha(b"no name"))[1]["content-disposition"]


def test_put_refusals_exit_as_the_board_says(board, team, keyring, tmp_path):
    big = tmp_path / "f"
    big.write_bytes(b"x" * 100)
    run = cli("put", tmp_path / "missing", key=team.alice.key)
    assert run.code == 1 and "cannot read" in run.last_err
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    run = cli("put", empty, key=team.alice.key)
    assert run.code == 1 and "empty" in run.last_err
    assert cli("put", big, "--type", "nonsense", key=team.alice.key).code == 2
    assert cli("put", big, "--name", "a/b", key=team.alice.key).code == 2
    # a stranger: the identity exit
    assert cli("put", big, key=keyring["stranger"]).code == 3
    assert cli("put", big).code == 3
    restart(board, keyring, BOARD_BLOB_MAX="50")
    run = cli("put", big, key=team.alice.key)
    assert run.code == 1 and run.last_err.startswith("board: blob-too-large:")
    restart(board, keyring, BOARD_BLOB_CAP="50")
    run = cli("put", big, key=team.alice.key)
    assert run.code == 1 and run.last_err.startswith("board: blob-store-full:")


def test_a_corrupted_file_is_caught_by_get(board, team, tmp_path):
    blob = data(100_000)
    h = team.alice.put(blob)["hash"]
    path = board.blobs_dir / h[:2] / h
    path.chmod(0o600)
    flipped = bytearray(blob)
    flipped[5] ^= 1
    path.write_bytes(bytes(flipped))
    out = tmp_path / "out.bin"
    run = cli("get", h, "-o", out, key=team.alice.key)
    assert run.code == 1 and run.last_err.startswith("board: hash-mismatch:")
    assert not out.exists() and not (tmp_path / "out.bin.part").exists()
    # to standard output the bytes are out already, but the exit says so
    code, _, err = cli_bytes("get", h, "-o", "-", key=team.alice.key)
    assert code == 1 and "hash-mismatch" in err


def test_the_server_logs_hash_and_size_and_never_the_signature(board, team):
    blob = data(1000)
    answer = team.alice.put(blob)
    sent = board.blob_headers(team.alice.key, sha(blob), len(blob))
    assert board.put_raw(team.alice.key, blob, headers=sent).status == 200
    log = board.server.log()
    assert answer["hash"] in log
    assert sent["Board-Signature"] not in log


def test_a_revoked_key_s_blobs_stay_and_say_so(board, team):
    h = team.bob.put(data(1000))["hash"]
    revoked = team.owner.revoke(team.bob.fingerprint)["id"]
    assert board.get_json(f"/blobs/{h}")["revoked_at"] == revoked
    assert board.get_blob(h)[0] == 200
