"""A black-box harness for the message board, v2: a tree of containers,
notes, threads and queues, and every write signed by a key the owner
approved.

It drives `board`, `board-server` and `board-admin` only as programs: through
their argv, environment, stdio, HTTP, a terminal and the database file. It
never imports them, so the same tests hold for any implementation of the
programs.

Faults are injected at those same edges, never through hooks in the
programs: an HTTP proxy in front of the server loses answers, holds them or
lets another write race in; a fake `systemctl` first on PATH stands in for
the user service manager; and the tests lock the database file or make it
read-only themselves.

Signatures are made by `ssh-keygen -Y sign` over this module's own copy of
the canonical encoding (`canonical`, `KINDS`): the oracle the programs are
held to, not a copy of theirs.

The programs come from $BOARD_BIN_DIR if it is set, else from building the
in-tree package (apps/message-board) with nix-build, so a plain `pytest`
tests what this checkout would deploy.
"""

import codecs
import fcntl
import functools
import hashlib
import http.client
import http.server
import json
import os
import pty
import queue
import select
import shlex
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from proc_lifetime import TIED, kill_tree, spawn  # noqa: F401 (tests import them from here)

REPO = Path(__file__).resolve().parent.parent

# How long a server start, a request or a program's exit may take: a bound
# on a hang, not a speed check. On a loaded box a single SQLite commit (an
# fsync) has stalled for half a minute.
HANG_TIMEOUT = 120

# The v2 counter starts here, so that no v2 id can be mistaken for a v1 one.
FIRST_ID = 10001
# The name the test boards bind to the owner key: the human of the
# permission matrix.
OWNER = "Stefan"
# Signed writes in a row that may go stale before a client gives up.
NONCE_TRIES = 8
# The line `board` prints for a v1 command form.
V1_POINTER = "board: v2 has no areas; see the message-board skill (board brief)"
# Exit status of `board` for an identity refusal (not registered, pending,
# declined, revoked), so a launcher can tell it apart from other failures.
IDENTITY_EXIT = 3
IDENTITY_CODES = ("not-registered", "registration-pending", "registration-declined", "key-revoked")

_PACKAGE = ("let sources = import ./npins; pkgs = import sources.nixpkgs {};"
            " in import ./apps/message-board {inherit pkgs sources;}")


@functools.cache
def bin_dir():
    """The directory holding board, board-server and board-admin."""
    given = os.environ.get("BOARD_BIN_DIR")
    if given:
        return Path(given)
    built = subprocess.run(["nix-build", "--no-out-link", "-E", _PACKAGE], cwd=REPO,
                           capture_output=True, text=True, check=False)
    if built.returncode != 0:
        raise RuntimeError(f"building apps/message-board failed:\n{built.stderr[-6000:]}")
    return Path(built.stdout.split()[-1]) / "bin"


def program(name):
    return str(bin_dir() / name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def dead_url():
    """An address nothing listens on."""
    return f"http://127.0.0.1:{free_port()}"


def wait_until(test, what="the condition"):
    deadline = time.monotonic() + HANG_TIMEOUT
    while not test():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def isolate(monkeypatch, tmp_path):
    """No test may reach the real board or its service: every client default
    points at an address nothing listens on, and board-admin manages no unit.
    `board` signs with the nearest scratch/board_ed25519 above the cwd, so the
    tests run in their own directory, where no key is."""
    for var in ("BOARD_KEY", "BOARD_AREA", "BOARD_TOPIC", "BOARD_SINCE", "BOARD_DB",
                "BOARD_OWNER_NAME", "BOARD_OWNER_PUBKEY", "BOARD_ADMIN_LOG", "BOARD_ADDR"):
        monkeypatch.delenv(var, raising=False)
    url = dead_url()
    monkeypatch.setenv("BOARD_URL", url)
    monkeypatch.setenv("BOARD_PORT", url.rsplit(":", 1)[1])
    monkeypatch.setenv("BOARD_ADMIN_SERVICE", "")
    monkeypatch.chdir(tmp_path)


def digest(path):
    """A file's sha256: to show a program left it alone."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- HTTP ---------------------------------------------------------------------

@dataclass
class Response:
    status: int
    headers: object  # http.client.HTTPMessage: case-insensitive
    text: str

    def json(self):
        return json.loads(self.text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 303 is an answer to look at, not one to follow


_opener = urllib.request.build_opener(_NoRedirect)


def request(url, data=None, *, form=False, raw=None, timeout=HANG_TIMEOUT):
    """GET `url`, or POST `data` as JSON (or as a form, or `raw` form bytes);
    any status is returned."""
    body = raw
    headers = {} if raw is None else {"content-type": "application/x-www-form-urlencoded"}
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["content-type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="GET" if body is None else "POST")
    try:
        with _opener.open(req, timeout=timeout) as r:
            return Response(r.status, r.headers, r.read().decode())
    except urllib.error.HTTPError as e:
        return Response(e.code, e.headers, e.read().decode())


def parse_answer(raw):
    """A raw HTTP answer as (status, headers with lowercase names, body)."""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = dict((k.strip().lower(), v.strip()) for k, _, v in
                   (line.partition(":") for line in lines[1:]))
    return int(lines[0].split(" ", 2)[1]), headers, body.decode()


def error_answer(status, headers, body):
    """The error code of a v2 error answer, checked to be one: JSON
    `{"error": code, "message": text, ...}` and the code in Board-Error."""
    value = json.loads(body)
    assert isinstance(value.get("error"), str) and isinstance(value.get("message"), str), body
    assert headers.get("board-error") == value["error"], (headers, body)
    return value["error"]


class Refused(Exception):
    """The board answered with an error status. v2 errors are
    `{"error": "<code>", "message": "...", ...}` plus `Board-Error: <code>`;
    `code` is the body's code, `body` the whole object (its extra fields:
    `head`, `version`, `item`, `revoked_at`, ...)."""

    def __init__(self, response):
        self.status, self.headers, self.text = response.status, response.headers, response.text
        try:
            self.body = json.loads(response.text)
        except ValueError:
            self.body = None
        if not isinstance(self.body, dict):
            self.body = {}
        self.code = self.body.get("error", response.text)
        self.message = self.body.get("message", "")
        super().__init__(f"{response.status} {self.code}: {self.message or response.text}")

    def __getitem__(self, name):
        return self.body[name]


def refused(response):
    return Refused(response)


@contextmanager
def refusal(status, code=None):
    """`with refusal(403, "forbidden") as r: ...`: the block must be refused
    with that status (and code); `r.value` is the Refused."""
    holder = SimpleNamespace(value=None)
    try:
        yield holder
    except Refused as e:
        holder.value = e
        assert e.status == status, f"expected {status} {code}, got {e}"
        if code is not None:
            assert e.code == code, f"expected {status} {code}, got {e}"
    else:
        raise AssertionError(f"expected a {status} {code or ''} refusal, but it was accepted")


# --- signing ------------------------------------------------------------------

# Each write action: its SSHSIG namespace, and its fields in signing order.
# `key` is the client's idempotency key, `nonce` the board's head at signing.
KINDS = {
    "create": ("board2-create", ("path", "kind", "description", "owner", "body", "key", "nonce")),
    "post": ("board2-post", ("path", "body", "key", "nonce")),
    "note": ("board2-note", ("path", "body", "base_version", "thread", "through", "key", "nonce")),
    "file": ("board2-file", ("path", "body", "key", "nonce")),
    "resolve": ("board2-resolve", ("item", "state", "body", "ref", "key", "nonce")),
    "register": ("board2-register", ("name", "note", "repo", "key", "nonce")),
    "approve": ("board2-approve", ("item", "name", "fingerprint", "key", "nonce")),
    "revoke": ("board2-revoke", ("fingerprint", "reason", "key", "nonce")),
    "archive": ("board2-archive", ("event", "archived", "reason", "key", "nonce")),
}
# The blob store's two: `blob` travels as headers of a streamed PUT /blob and `blobdel` as the JSON of
# POST /blob-delete, so the tests that post every KINDS write to /<action> leave them out.
BLOB_KINDS = {
    "blob": ("board2-blob", ("sha256", "size", "media_type", "filename", "key", "nonce")),
    "blobdel": ("board2-blobdel", ("sha256", "key", "nonce")),
}
ALL_KINDS = {**KINDS, **BLOB_KINDS}
# The v1 kinds: a signature made for one of these must never verify in v2.
V1_KINDS = {
    "message": ("board-message", ("area", "topic", "from", "body", "key", "nonce")),
    "pin": ("board-pin", ("area", "topic", "from", "body", "base_version", "nonce")),
}

# How a signed write travels: a JSON object of its signed fields, every one
# a string exactly as signed ("" for an absent optional one), plus
# "signature". The web view's forms carry the same fields.
def wire(values):
    """The JSON body for signed `values` (all text)."""
    assert all(isinstance(v, str) for v in values.values()), values
    return dict(values)


def canonical(fields, values):
    """The bytes a signature covers, as the board's contract defines them:
    `<name>:<utf-8 byte length>:<value>\\n` per field, in order. This is the
    test suite's own copy, the oracle the programs are held to."""
    out = b""
    for name in fields:
        raw = values[name].encode()
        out += f"{name}:{len(raw)}:".encode() + raw + b"\n"
    return out


def text(value):
    """A field's signed text: absent is ""."""
    return "" if value is None else str(value)


def new_client_key():
    """A write's idempotency key, as `board` makes one."""
    return uuid.uuid4().hex


def ssh_keygen(*args, data=None, check=True):
    # never the terminal's stdin: a prompt (an existing file to overwrite)
    # fails instead of hanging the suite
    return subprocess.run(["ssh-keygen", *map(str, args)], **TIED, input=b"" if data is None else data,
                          capture_output=True, check=check)


def armored_body(armored):
    """An armored SSH signature's base64, without the BEGIN/END lines."""
    return "".join(line for line in armored.splitlines() if not line.startswith("-----"))


def fingerprint_of(pub_path):
    return ssh_keygen("-l", "-f", pub_path).stdout.split()[1].decode()


class Key:
    """A key made by ssh-keygen itself, on disk."""

    def __init__(self, path):
        self.path = Path(path)
        self.pub = self.path.with_name(self.path.name + ".pub")
        self.public_line = " ".join(self.pub.read_text().split()[:2])
        self.fingerprint = fingerprint_of(self.pub)

    def __repr__(self):
        return f"Key({self.path.name}, {self.fingerprint})"

    @classmethod
    def make(cls, directory, name, kind="ed25519", passphrase=""):
        path = Path(directory) / name
        ssh_keygen("-q", "-t", kind, "-N", passphrase, "-C", name, "-f", path)
        return cls(path)

    def sign(self, namespace, data, *options):
        """ssh-keygen's SSHSIG over `data`, as the board sends it."""
        out = ssh_keygen("-Y", "sign", "-f", self.path, "-n", namespace, *options, data=data)
        return armored_body(out.stdout.decode())


class Keyring:
    """Keys by name, made once per test session: a board's database is new
    in every test, so the same key file can register afresh in each."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.keys = {}

    def __getitem__(self, name):
        if name not in self.keys:
            self.keys[name] = Key.make(self.dir, name)
        return self.keys[name]


def ssh_verify(directory, public_line, namespace, data, signature):
    """`ssh-keygen -Y verify` of a board signature; True if it accepts it."""
    directory = Path(directory)
    allowed = directory / "allowed_signers"
    allowed.write_text(f'signer namespaces="{namespace}" {public_line}\n')
    body = "".join(signature.split())
    sig = directory / "data.sig"
    sig.write_text("-----BEGIN SSH SIGNATURE-----\n"
                   + "\n".join(body[i:i + 70] for i in range(0, len(body), 70))
                   + "\n-----END SSH SIGNATURE-----\n")
    out = ssh_keygen("-Y", "verify", "-f", allowed, "-I", "signer", "-n", namespace,
                     "-s", sig, data=data, check=False)
    return out.returncode == 0


def sent_signature_verifies(directory, key, action, sent):
    """Whether a write body a client sent carries ssh-keygen-verifiable
    SSHSIG over its own fields, in `action`'s namespace."""
    namespace, fields = KINDS[action]
    values = {name: text(sent.get(name)) for name in fields}
    return ssh_verify(directory, key.public_line, namespace, canonical(fields, values),
                      sent["signature"])


def distance(a, b):
    """Levenshtein distance, the test suite's own: for claims about test data."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# --- the database file ----------------------------------------------------------

def sql(db, query, *args):
    """Rows of one statement against the board's file, committed."""
    conn = sqlite3.connect(db, timeout=HANG_TIMEOUT)
    try:
        cur = conn.execute(query, args)
        rows = cur.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def sql_changes(db, query, *args):
    """How many rows one write statement changed."""
    conn = sqlite3.connect(db, timeout=HANG_TIMEOUT)
    try:
        changed = conn.execute(query, args).rowcount
        conn.commit()
        return changed
    finally:
        conn.close()


def sql_many(db, query, rows):
    """One statement for each of `rows`, in one transaction."""
    conn = sqlite3.connect(db, timeout=HANG_TIMEOUT)
    try:
        conn.executemany(query, rows)
        conn.commit()
    finally:
        conn.close()


def make_db(path, script):
    """A database file as another program left it."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


# A database as the v1 board left it: WAL, its tables, some history.
V1_DB = """
    PRAGMA journal_mode=WAL;
    CREATE TABLE areas(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
                       created_ts TEXT NOT NULL, archived_ts TEXT);
    CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                          area TEXT NOT NULL, topic TEXT NOT NULL, sender TEXT NOT NULL,
                          body TEXT NOT NULL, client_key TEXT, signed_by TEXT, signature TEXT);
    CREATE INDEX idx_messages_area ON messages(area, id);
    CREATE UNIQUE INDEX idx_messages_client_key ON messages(client_key);
    CREATE TABLE pins(area TEXT NOT NULL, topic TEXT NOT NULL, body TEXT NOT NULL,
                      version INTEGER NOT NULL, editor TEXT NOT NULL, ts TEXT NOT NULL,
                      signed_by TEXT, PRIMARY KEY(area, topic));
    CREATE TABLE identities(name TEXT PRIMARY KEY, pubkey TEXT NOT NULL,
                            fingerprint TEXT NOT NULL,
                            source TEXT NOT NULL CHECK(source IN ('tofu', 'owner')),
                            registered_ts TEXT NOT NULL);
    CREATE TABLE pin_versions(id INTEGER PRIMARY KEY AUTOINCREMENT, area TEXT NOT NULL,
                              topic TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                              editor TEXT NOT NULL, base_version INTEGER, signed_by TEXT,
                              signature TEXT, ts TEXT NOT NULL);
    INSERT INTO areas VALUES('reversing', '', '2026-09-01T00:00:00Z', NULL);
    INSERT INTO messages(ts, area, topic, sender, body)
        VALUES('2026-09-01T00:00:00Z', 'reversing', 'ghidra', 'Stefan', 'from the v1 era');
    INSERT INTO pins VALUES('reversing', '', 'v1 rules', 1, 'Stefan', '2026-09-01T00:00:00Z', NULL);
"""


def make_v1_db(path):
    make_db(path, V1_DB)
    return path


# --- the server -------------------------------------------------------------------

class Server:
    """A real `board-server` process on a private port and database."""

    def __init__(self, db, logs):
        self.db = Path(db)
        self.logs = Path(logs)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self.returncode = None
        self.starts = 0

    def _env(self, extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
        env.update(BOARD_DB=str(self.db), BOARD_ADDR="127.0.0.1", BOARD_PORT=str(self.port))
        env.update(extra)
        return env

    def launch(self, **env):
        """Start it with these extra env vars; don't wait for it."""
        self.starts += 1
        self.out_path = self.logs / f"server-{self.starts}.out"
        self.err_path = self.logs / f"server-{self.starts}.err"
        with open(self.out_path, "wb") as out, open(self.err_path, "wb") as err:
            self.proc = spawn([program("board-server")], env=self._env(env),
                              stdout=out, stderr=err)
        return self.proc

    def start(self, **env):
        """Start it with these extra env vars, and wait until it answers."""
        self.launch(**env)
        try:
            return self._wait_until_up()
        except BaseException:  # nobody holds a server that never started: not even a caller's finally
            self.stop()
            raise

    def _wait_until_up(self):
        deadline = time.monotonic() + HANG_TIMEOUT
        while True:
            if self.proc.poll() is not None:
                code, self.proc = self.proc.returncode, None
                raise AssertionError(f"board-server exited with {code}:\n{self.out()}{self.err()}")
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=HANG_TIMEOUT) as r:
                    r.read()
                return self
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError(f"board-server never came up:\n{self.err()}") from None
                time.sleep(0.1)

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=HANG_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            kill_tree(self.proc)  # whatever is left of it, however stop got here
        self.returncode = self.proc.returncode
        self.proc = None

    def restart(self, **env):
        self.stop()
        return self.start(**env)

    def out(self):
        return self.out_path.read_text()

    def err(self):
        return self.err_path.read_text()

    def log(self):
        """Everything it printed, both streams."""
        return self.out() + self.err()


def owner_env(key, name=OWNER):
    """The env that makes board-server bind `name` to `key` at start."""
    return {"BOARD_OWNER_NAME": name, "BOARD_OWNER_PUBKEY": str(key.pub)}


@contextmanager
def running(db, logs, owner_key=None, **env):
    """A Board on `db` for the length of a with-block, its owner `owner_key`."""
    server = Server(db, logs)
    try:
        server.start(**(owner_env(owner_key) if owner_key else {}), **env)
        yield Board(server, owner_key)
    finally:
        server.stop()


def items_of(answer):
    """A list some endpoint answers, bare or as the one list in an object."""
    if isinstance(answer, list):
        return answer
    lists = [v for v in answer.values() if isinstance(v, list)]
    assert len(lists) == 1, answer
    return lists[0]


class Board:
    """A running board, through its HTTP API and its database file: what a
    client and an operator can see and do. Writes are signed here, with the
    suite's own canonical bytes and ssh-keygen."""

    def __init__(self, server, owner_key=None, owner_name=OWNER):
        self.server = server
        self.owner = Agent(self, owner_key, owner_name) if owner_key else None
        self.last_sent = None  # the body of the last write, as sent

    @property
    def url(self):
        return self.server.url

    @property
    def db(self):
        return self.server.db

    def restart(self, **env):
        self.server.restart(**env)

    def get(self, path):
        return request(self.url + path)

    def _ok(self, response):
        if response.status != 200:
            raise refused(response)
        return response.json()

    def get_json(self, path, /, **query):
        q = urllib.parse.urlencode(query, doseq=True)
        return self._ok(self.get(path + (f"?{q}" if q else "")))

    # reads
    def healthz(self):
        return self.get_json("/healthz")

    def head_id(self):
        return self.healthz()["head"]

    def node(self, path, **query):
        """GET /n/<path>: container {node, children}, note {node, note},
        thread {node, messages, earlier}, queue {node, items}."""
        return self.get_json(f"/n/{path}", **query)

    def children(self, path=""):
        """A container's children; "" is the top level ({node: null, children})."""
        return self.node(path)["children"]

    def messages(self, path, **query):
        return self.node(path, **query)["messages"]

    def note(self, path, **query):
        return self.node(path, **query)["note"]

    def queue_items(self, path, **query):
        return self.node(path, **query)["items"]

    def event(self, eid):
        return self.get_json(f"/event/{eid}")

    def item(self, item):
        return self.get_json(f"/item/{item}")

    def items(self, **query):
        return items_of(self.get_json("/items", **query))

    def poll(self, since=0, timeout=0, **filters):
        """GET /poll: {events, cursor}. Filters take a value or a list:
        path, mention, not_from, asked_by, owner, item."""
        q = {"since": since, "timeout": timeout}
        q.update({k: v for k, v in filters.items() if v is not None})
        query = urllib.parse.urlencode(q, doseq=True)
        return self._ok(request(f"{self.url}/poll?{query}", timeout=timeout + HANG_TIMEOUT))

    def brief(self, *paths, as_=None):
        query = {"path": list(paths)}
        if as_:
            query["as"] = as_
        return self.get_json("/brief", **query)

    def keys(self):
        return items_of(self.get_json("/keys"))

    def key_info(self, fingerprint):
        """GET /keys/<fp>, the fingerprint percent-encoded ('/', '+', ':')."""
        return self.get_json(f"/keys/{urllib.parse.quote(fingerprint, safe='')}")

    def trust_note(self):
        return self.note("_board/trust")

    # writes
    def signed(self, action, key, values, nonce=None):
        """The body of a signed `action` with these field values (any not
        given are ""), its nonce the head as of now unless given."""
        namespace, fields = ALL_KINDS["archive" if action == "unarchive" else action]
        unknown = set(values) - set(fields)
        assert not unknown, f"{action} has no fields {unknown}"
        signed = {name: text(values.get(name)) for name in fields}
        signed["nonce"] = text(self.head_id() if nonce is None else nonce)
        body = wire(signed)
        body["signature"] = key.sign(namespace, canonical(fields, signed))
        return body

    def send(self, action, body):
        self.last_sent = body
        return request(f"{self.url}/{action}", body)

    def write(self, action, signer, /, *, nonce=None, **values):
        """A signed write by the key `signer`; its answer, or Refused. A stale
        nonce is signed again (as every client does) unless `nonce` is pinned.
        `key` among `values` is the idempotency key; a fresh one by default
        (`signer` is positional-only so the two names cannot collide)."""
        values.setdefault("key", new_client_key())
        for _ in range(NONCE_TRIES):
            response = self.send(action, self.signed(action, signer, values, nonce))
            if response.status == 200:
                return response.json()
            failure = refused(response)
            if failure.code != "stale-nonce" or nonce is not None:
                raise failure
        raise failure

    def as_key(self, key, name=None):
        return Agent(self, key, name)

    def register(self, key, name, note="", repo=""):
        return self.write("register", key, name=name, note=note, repo=repo)

    def approve(self, item, name, fingerprint):
        assert self.owner, "this board has no owner key"
        return self.owner.write("approve", item=item, name=name, fingerprint=fingerprint)

    def trusted(self, key, name):
        """`key`, registered as `name` and approved by the owner."""
        item = self.register(key, name)["item"]
        self.approve(item, name, key.fingerprint)
        return Agent(self, key, name)

    # the database file
    def sql(self, query, *args):
        return sql(self.db, query, *args)

    def node_id(self, path):
        [(nid,)] = self.sql("SELECT id FROM nodes WHERE path=?", path)
        return nid


@dataclass
class Agent:
    """A key writing to a board: what one agent (or the owner) can do."""
    board: Board
    key: Key
    name: str = None

    @property
    def fingerprint(self):
        return self.key.fingerprint

    def write(self, action, /, **values):
        return self.board.write(action, self.key, **values)

    def create(self, path, kind, description="", owner="", body=""):
        return self.write("create", path=path, kind=kind, description=description, owner=owner,
                          body=body)

    def post(self, path, body):
        """A thread message; its id."""
        return self.write("post", path=path, body=body)["id"]

    def set_note(self, path, body, base_version, thread="", through=""):
        """A new note version: {version, id, ...}."""
        return self.write("note", path=path, body=body, base_version=base_version,
                          thread=thread, through=through)

    def ask(self, path, body):
        """File an item in a queue; the item id."""
        return self.write("file", path=path, body=body)["item"]

    def resolve(self, item, state, body="", ref=""):
        return self.write("resolve", item=item, state=state, body=body, ref=ref)

    def answer(self, item, body="done", ref=""):
        return self.resolve(item, "answered", body, ref)

    def decline(self, item, body="no"):
        return self.resolve(item, "declined", body)

    def cancel(self, item, body=""):
        return self.resolve(item, "cancelled", body)

    def approve(self, item, name, fingerprint):
        return self.write("approve", item=item, name=name, fingerprint=fingerprint)

    def revoke(self, fingerprint, reason=""):
        return self.write("revoke", fingerprint=fingerprint, reason=reason)

    def archive(self, event, reason=""):
        """POST /archive: {id, event, archived: true}."""
        return self.write("archive", event=event, archived="1", reason=reason)

    def unarchive(self, event, reason=""):
        """POST /unarchive: {id, event, archived: false}."""
        return self.write("unarchive", event=event, archived="0", reason=reason)


# --- a proxy: the network edge, where tests break the connection -------------------

DROP = "drop"


@dataclass
class Exchange:
    """One request through the proxy, and the server's answer to it."""
    method: str
    path: str
    body: bytes
    status: int = None
    answer: bytes = b""
    headers: list = field(default_factory=list)
    lost: Exception = None  # the board went away before it answered

    @property
    def route(self):
        return urllib.parse.urlsplit(self.path).path

    @property
    def query(self):
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    @property
    def queries(self):
        """Every value of every query parameter."""
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def json(self):
        return json.loads(self.body)

    def answer_json(self):
        return json.loads(self.answer)


class Proxy:
    """Forwards to a board-server and records every exchange. `before` and
    `after` see each one: `before` returning DROP closes the connection
    without forwarding (a board that is down), `after` returning DROP
    closes it once the server has answered (an answer lost on the way back);
    either may also wait or write to the board itself.

    Two drops are routine and quiet: the board going away mid-request (a
    test stopped or restarted it; the client's connection is closed, as
    the board's was, and `Exchange.lost` says why), and the client going
    away before its answer (a watch that was stopped). Anything else that
    fails in here -- a hook's own assertion, a write a hook made that the
    board refused -- is a fault of the test: it lands in `errors`, which
    the `proxy` fixture fails the test on, instead of a traceback on
    stderr that fails nothing."""

    def __init__(self, upstream):
        self.upstream = urllib.parse.urlsplit(upstream)
        self.seen = []
        self.errors = []
        self.before = lambda exchange: None
        self.after = lambda exchange: None
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _relay(self):
                length = int(self.headers.get("Content-Length") or 0)
                ex = Exchange(self.command, self.path, self.rfile.read(length))
                proxy.seen.append(ex)
                if proxy.before(ex) == DROP:
                    self.close_connection = True
                    return
                conn = http.client.HTTPConnection(proxy.upstream.hostname,
                                                  proxy.upstream.port, timeout=HANG_TIMEOUT * 4)
                try:
                    headers = {"Content-Type": self.headers["Content-Type"]} \
                        if self.headers.get("Content-Type") else {}
                    conn.request(self.command, self.path, body=ex.body or None, headers=headers)
                    resp = conn.getresponse()
                    ex.answer, ex.status, ex.headers = resp.read(), resp.status, resp.getheaders()
                except OSError as e:  # http.client.RemoteDisconnected is one
                    ex.lost = e
                    self.close_connection = True
                    return
                finally:
                    conn.close()
                if proxy.after(ex) == DROP:
                    self.close_connection = True
                    return
                try:
                    self.send_response(ex.status)
                    for name, value in ex.headers:
                        if name.lower() in ("content-type", "board-error", "location",
                                            "content-security-policy"):
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(ex.answer)))
                    self.end_headers()
                    self.wfile.write(ex.answer)
                except OSError:  # the client left before its answer
                    self.close_connection = True

            do_GET = do_POST = _relay

        class Quiet(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                error = sys.exc_info()[1]
                if not isinstance(error, ConnectionError):  # a client that left: routine
                    proxy.errors.append(error)

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def routes(self):
        return [ex.route for ex in self.seen]

    def sent(self, route):
        """The JSON bodies POSTed to `route`."""
        return [ex.json() for ex in self.seen if ex.method == "POST" and ex.route == route]

    def writes(self):
        """Every POST that went through."""
        return [ex for ex in self.seen if ex.method == "POST"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# --- the board CLI ------------------------------------------------------------------

@dataclass
class Run:
    code: int
    out: str
    err: str

    @property
    def last_err(self):
        """The final stderr line: the one a failing command exits with."""
        lines = self.err.splitlines()
        return lines[-1] if lines else ""

    @property
    def lines(self):
        """stdout as JSON Lines: each line one JSON object, "type" first."""
        return [json_line(line) for line in self.out.split("\n") if line]

    @property
    def result(self):
        """The one object a command that is not a stream prints."""
        lines = self.lines
        assert len(lines) == 1, (self.out, self.err)
        return lines[0]

    def of(self, kind):
        """The lines of one type."""
        return [line for line in self.lines if line["type"] == kind]

    def ok(self):
        """Itself, checked to have succeeded: exit 0, nothing on stderr."""
        assert (self.code, self.err) == (0, ""), self
        return self


def json_line(line):
    """One line of `board` output, checked to be a JSON object that starts
    with its "type" and has no line break of any kind in it."""
    assert line == line.strip() and len(line.splitlines()) == 1, repr(line)
    value = json.loads(line)
    assert isinstance(value, dict) and next(iter(value)) == "type", line
    return value


def _env(env, key):
    env = dict(os.environ) if env is None else dict(env)
    if key is not None:
        env["BOARD_KEY"] = str(key.path if isinstance(key, Key) else key)
    return env


def cli(*args, stdin="", env=None, key=None, timeout=HANG_TIMEOUT):
    """Run `board` in the current directory with the current environment,
    signing with `key` (a Key or a path) if given."""
    done = subprocess.run([program("board"), *map(str, args)], **TIED, input=stdin.encode(),
                          capture_output=True, cwd=os.getcwd(), env=_env(env, key),
                          timeout=timeout, check=False)
    return Run(done.returncode, done.stdout.decode(), done.stderr.decode())


class Process:
    """A long-running `board` command (watch, wait), read a line at a time."""

    def __init__(self, *args, key=None, env=None):
        self.args = [str(a) for a in args]
        self.proc = spawn([program("board"), *self.args], cwd=os.getcwd(),
                          env=_env(env, key), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for raw in self.proc.stdout:
            self.lines.put(raw.decode())
        self.lines.put(None)

    def line(self, timeout=HANG_TIMEOUT):
        got = self.lines.get(timeout=timeout)
        if got is None:
            self.lines.put(None)
            raise AssertionError(f"board {' '.join(self.args)} exited with {self.proc.wait()}:"
                                 f" {self.proc.stderr.read().decode()}")
        assert got.endswith("\n"), got
        return got[:-1]

    def event(self, timeout=HANG_TIMEOUT):
        """The next line, parsed (see `json_line`)."""
        return json_line(self.line(timeout))

    def nothing_for(self, seconds):
        """Whether it printed nothing for this long."""
        try:
            got = self.lines.get(timeout=seconds)
        except queue.Empty:
            return True
        self.lines.put(got)  # out of order now, but the caller is about to fail
        return False

    def wait(self, timeout=HANG_TIMEOUT):
        """Its exit status, and its stderr."""
        code = self.proc.wait(timeout=timeout)
        return code, self.proc.stderr.read().decode()

    def rest(self):
        """The lines it printed until it exited."""
        out = []
        while (got := self.lines.get(timeout=HANG_TIMEOUT)) is not None:
            out.append(json_line(got.rstrip("\n")))
        self.lines.put(None)
        return out

    def stop(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
            self.proc.wait(timeout=HANG_TIMEOUT)
        finally:
            kill_tree(self.proc)


class Watch(Process):
    """A `board watch` process."""

    def __init__(self, *args, key=None, env=None):
        super().__init__("watch", *args, key=key, env=env)

    def events(self, n, timeout=HANG_TIMEOUT):
        return [self.event(timeout) for _ in range(n)]


@contextmanager
def watching(*args, key=None, env=None):
    watch = Watch(*args, key=key, env=env)
    try:
        yield watch
    finally:
        watch.stop()


def run_command_line(command, key=None):
    """A `board ...` command line the board handed out (brief's `watch`),
    started as it is written."""
    argv = shlex.split(command)
    assert argv[0] == "board", command
    return Process(*argv[1:], key=key)


# --- fixtures the board's test files share ---------------------------------------------
#
# A test file imports these by name to use them.

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    isolate(monkeypatch, tmp_path)


@pytest.fixture(scope="session")
def keyring(tmp_path_factory):
    return Keyring(tmp_path_factory.mktemp("keys"))


@pytest.fixture
def board(isolated, tmp_path, monkeypatch, keyring):
    """A running board-server on a fresh database, its owner key bound to
    OWNER; `board` the CLI talks to it."""
    server = Server(tmp_path / "board-v2.db", tmp_path / "logs")
    try:
        server.start(**owner_env(keyring["owner"]))
        monkeypatch.setenv("BOARD_URL", server.url)
        yield Board(server, keyring["owner"])
    finally:
        server.stop()


@pytest.fixture
def proxy(board, monkeypatch):
    """The board behind a proxy; `board` the CLI talks to the proxy (the
    suite's own requests still go straight to the board)."""
    p = Proxy(board.url)
    monkeypatch.setenv("BOARD_URL", p.url)
    yield p
    p.close()
    assert p.errors == [], f"the proxy failed (a test hook, not the board): {p.errors!r}"


@pytest.fixture
def team(board, keyring):
    """The owner and three trusted agents, and a top-level container `rev`
    the owner made, with a thread `rev/log` in it."""
    t = SimpleNamespace(owner=board.owner,
                        alice=board.trusted(keyring["alice"], "alice"),
                        bob=board.trusted(keyring["bob"], "bob"),
                        carol=board.trusted(keyring["carol"], "carol"))
    t.owner.create("rev", "container", description="reversing")
    t.alice.create("rev/log", "thread", description="the log")
    return t


# --- a user service manager stand-in --------------------------------------------------

_SYSTEMCTL = """#!{python}
# A stand-in for `systemctl --user` with one unit: records each call in the
# state file and plays the unit's state. Stopping it stops the real
# board-server whose pid and port the test put in the state file.
import json, os, signal, socket, sys, time
path = {state!r}
with open(path) as f:
    state = json.load(f)
args = sys.argv[1:]
state["calls"].append(args)
code = 0
verb = args[1] if len(args) > 2 and args[0] == "--user" else None
if verb == "is-active":
    code = 0 if state["active"] else 3
elif verb == "stop":
    if state["stop_works"]:
        if state["pid"]:
            os.kill(state["pid"], signal.SIGTERM)
            deadline = time.monotonic() + {hang}
            while time.monotonic() < deadline:  # stopped once its port is closed
                try:
                    socket.create_connection(("127.0.0.1", state["port"]), timeout=1).close()
                except OSError:
                    break
                time.sleep(0.05)
        state["active"] = False
    if state["stop_fails"]:
        print("Job for message-board.service canceled.", file=sys.stderr)
        code = 1
elif verb == "start":
    if state["start_fails"]:
        print("Job for message-board.service failed.", file=sys.stderr)
        code = 1
    else:
        state["active"] = True
else:
    print(f"fake systemctl: unexpected call {{args}}", file=sys.stderr)
    code = 2
with open(path + ".tmp", "w") as f:
    json.dump(state, f)
os.replace(path + ".tmp", path)
sys.exit(code)
"""


class FakeSystemd:
    """A `systemctl` first on PATH, managing one unit: `server`, if given, is
    the real board-server that unit runs. `stop_fails`: the unit stops, but
    systemctl says it failed, as when it is interrupted or its wait times
    out while systemd finishes the stop anyway."""

    def __init__(self, directory, active=True, server=None, stop_works=True, start_fails=False,
                 stop_fails=False):
        self.dir = Path(directory) / "fake-systemd"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = self.dir / "state.json"
        self.state.write_text(json.dumps({
            "calls": [], "active": active, "stop_works": stop_works,
            "start_fails": start_fails, "stop_fails": stop_fails,
            "pid": server.proc.pid if server and server.proc else None,
            "port": server.port if server else None}))
        script = self.dir / "systemctl"
        script.write_text(_SYSTEMCTL.format(python=sys.executable, state=str(self.state),
                                            hang=HANG_TIMEOUT))
        script.chmod(0o755)

    def path(self):
        return f"{self.dir}:{os.environ.get('PATH', '')}"

    def calls(self):
        """The verbs systemctl was called with, in order."""
        return [c[1] for c in json.loads(self.state.read_text())["calls"]]


# --- a terminal: enough of an xterm to know what a full-screen program drew -----------

class Terminal:
    """A screen grid fed with a program's output. Every full-screen program
    draws with cursor movement and erases, and skips cells that are already
    right, so its output only reads as text once it is put on a grid."""

    def __init__(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.y = self.x = 0
        self.pending_wrap = False
        self.insert = False
        self.top, self.bottom = 0, rows - 1
        self.saved = (0, 0)
        self.last = " "
        self.alt = False  # on the alternate screen: a full-screen program is drawing
        self.buf = ""
        self.snapshots = []
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.grid)

    def lines(self):
        return [("".join(row)).rstrip() for row in self.grid]

    def seen(self, text):
        """Whether `text` was on the screen at any point."""
        return any(text in shot for shot in self.snapshots)

    def feed(self, data):
        self.buf += self._decoder.decode(data)
        i = 0
        while i < len(self.buf):
            if self.buf[i] == "\x1b":
                end = self._escape(i)
                if end is None:  # incomplete: wait for the rest
                    break
                i = end
                continue
            self._char(self.buf[i])
            i += 1
        self.buf = self.buf[i:]
        self.snapshots.append(self.text())

    # -- output
    def _char(self, ch):
        code = ord(ch)
        if ch == "\r":
            self.x, self.pending_wrap = 0, False
        elif ch in "\n\x0b\x0c":
            self._linefeed()
        elif ch == "\b":
            self.x, self.pending_wrap = max(0, self.x - 1), False
        elif ch == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
        elif code < 0x20 or code == 0x7f:
            pass  # BEL, SO/SI and the like draw nothing
        else:
            self._print(ch)

    def _print(self, ch):
        width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if self.pending_wrap:
            self.x, self.pending_wrap = 0, False
            self._linefeed()
        if self.insert:
            row = self.grid[self.y]
            row[self.x:] = ([" "] * width + row[self.x:])[:self.cols - self.x]
        self.grid[self.y][self.x] = ch
        if width == 2 and self.x + 1 < self.cols:
            self.grid[self.y][self.x + 1] = ""
        self.last = ch
        if self.x + width >= self.cols:
            self.x, self.pending_wrap = self.cols - 1, True
        else:
            self.x += width

    def _linefeed(self):
        if self.y == self.bottom:
            self._scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _scroll_up(self, n):
        for _ in range(n):
            del self.grid[self.top]
            self.grid.insert(self.bottom, [" "] * self.cols)

    def _scroll_down(self, n):
        for _ in range(n):
            del self.grid[self.bottom]
            self.grid.insert(self.top, [" "] * self.cols)

    def _move(self, y=None, x=None):
        if y is not None:
            self.y = max(0, min(self.rows - 1, y))
        if x is not None:
            self.x = max(0, min(self.cols - 1, x))
        self.pending_wrap = False

    def _erase(self, y, x0, x1):
        for x in range(max(0, x0), min(self.cols, x1)):
            self.grid[y][x] = " "

    # -- escapes
    def _escape(self, i):
        """Apply the escape sequence at buf[i]; the index after it, or None."""
        buf = self.buf
        if i + 1 >= len(buf):
            return None
        kind = buf[i + 1]
        if kind == "[":
            j = i + 2
            while j < len(buf) and not ("\x40" <= buf[j] <= "\x7e"):
                j += 1
            if j >= len(buf):
                return None
            self._csi(buf[i + 2:j], buf[j])
            return j + 1
        if kind in "]P_^":  # OSC, DCS, APC, PM: until BEL or ST, ignored
            j = i + 2
            while j < len(buf):
                if buf[j] == "\x07":
                    return j + 1
                if buf[j] == "\x1b" and j + 1 < len(buf) and buf[j + 1] == "\\":
                    return j + 2
                j += 1
            return None
        if kind in "()*+":  # character set designation
            return i + 3 if i + 2 < len(buf) else None
        if kind == "7":
            self.saved = (self.y, self.x)
        elif kind == "8":
            self._move(*self.saved)
        elif kind == "M":
            if self.y == self.top:
                self._scroll_down(1)
            else:
                self._move(y=self.y - 1)
        elif kind == "D":
            self._linefeed()
        elif kind == "E":
            self.x = 0
            self._linefeed()
        elif kind == "c":
            self.__init__(self.rows, self.cols)
        elif kind not in "=>":  # keypad modes draw nothing
            raise ValueError(f"terminal: unhandled escape ESC {kind!r}")
        return i + 2

    def _csi(self, body, final):
        private = body[:1] if body[:1] in "?>=<" else ""
        params_text = body[len(private):].rstrip(" !\"#$%&'()*+,-./")
        intermediate = body[len(private) + len(params_text):]
        params = [int(p) if p.isdigit() else 0 for p in params_text.split(";")] \
            if params_text else []

        def arg(n, default=1):
            return params[n] if len(params) > n and params[n] else default

        if private or intermediate:
            if final in "hl" and private == "?":
                if {47, 1047, 1049} & set(params):
                    self.grid = [[" "] * self.cols for _ in range(self.rows)]
                    self.alt = final == "h"
                return
            if final in "uqmc" or private == ">":
                return  # keyboard modes, cursor style, version queries: nothing drawn
            raise ValueError(f"terminal: unhandled CSI {private}{params_text}{intermediate}{final}")
        if final == "m" or final == "t":
            return  # colours and attributes; window operations
        if final in "Hf":
            self._move(arg(0) - 1, arg(1) - 1)
        elif final == "d":
            self._move(y=arg(0) - 1)
        elif final in "G`":
            self._move(x=arg(0) - 1)
        elif final == "A":
            self._move(y=max(self.top if self.y >= self.top else 0, self.y - arg(0)))
        elif final == "B":
            self._move(y=min(self.bottom if self.y <= self.bottom else self.rows - 1,
                             self.y + arg(0)))
        elif final == "C":
            self._move(x=self.x + arg(0))
        elif final == "D":
            self._move(x=self.x - arg(0))
        elif final == "E":
            self._move(self.y + arg(0), 0)
        elif final == "F":
            self._move(self.y - arg(0), 0)
        elif final == "K":
            mode = arg(0, 0)
            if mode == 0:
                self._erase(self.y, self.x, self.cols)
            elif mode == 1:
                self._erase(self.y, 0, self.x + 1)
            else:
                self._erase(self.y, 0, self.cols)
        elif final == "J":
            mode = arg(0, 0)
            if mode == 0:
                self._erase(self.y, self.x, self.cols)
                for y in range(self.y + 1, self.rows):
                    self._erase(y, 0, self.cols)
            elif mode == 1:
                for y in range(0, self.y):
                    self._erase(y, 0, self.cols)
                self._erase(self.y, 0, self.x + 1)
            else:
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "P":
            row = self.grid[self.y]
            n = min(arg(0), self.cols - self.x)
            row[self.x:] = row[self.x + n:] + [" "] * n
        elif final == "@":
            row = self.grid[self.y]
            n = min(arg(0), self.cols - self.x)
            row[self.x:] = ([" "] * n + row[self.x:])[:self.cols - self.x]
        elif final == "X":
            self._erase(self.y, self.x, self.x + arg(0))
        elif final in "LM":
            if self.top <= self.y <= self.bottom:
                for _ in range(arg(0)):
                    if final == "L":
                        del self.grid[self.bottom]
                        self.grid.insert(self.y, [" "] * self.cols)
                    else:
                        del self.grid[self.y]
                        self.grid.insert(self.bottom, [" "] * self.cols)
        elif final == "S":
            self._scroll_up(arg(0))
        elif final == "T":
            self._scroll_down(arg(0))
        elif final == "r":
            self.top, self.bottom = arg(0) - 1, arg(1, self.rows) - 1
            self._move(0, 0)
        elif final == "b":
            for _ in range(arg(0)):
                self._print(self.last)
        elif final in "hl":
            if 4 in params:
                self.insert = final == "h"
        elif final == "s":
            self.saved = (self.y, self.x)
        elif final == "u":
            self._move(*self.saved)
        elif final == "c":
            return  # device attributes query: nothing drawn
        else:
            raise ValueError(f"terminal: unhandled CSI {params_text}{final}")


# --- driving board-admin through a pty --------------------------------------------------

@dataclass
class Wait:
    """Read until `text` is on the screen."""
    text: str


@dataclass
class Select:
    """Wait for a list row containing `label` and move down to it. A list
    starts on its first row (screen row 1, under the title) every time it is
    shown."""
    label: str


@dataclass
class Pick(Select):
    """Select the row containing `label`, then choose it (Enter)."""


@dataclass
class Do:
    """Run `fn()` at this point of the session."""
    fn: object


@dataclass
class Kill:
    """Send board-admin a signal."""
    signum: int


@dataclass
class TuiRun:
    code: int
    term: Terminal

    def seen(self, text):
        return self.term.seen(text)


def admin(env, timeout=HANG_TIMEOUT):
    """Run board-admin without a terminal (for the refusals that come before
    it draws anything)."""
    done = subprocess.run([program("board-admin")], **TIED, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    return Run(done.returncode, done.stdout, done.stderr)


def drive_tui(env, steps, rows=40, cols=120):
    """Run board-admin under a pty, feed it `steps` (keystrokes as bytes, or
    Wait / Select / Pick / Do / Kill) and wait for it to exit. Keys sent
    after it has exited are dropped, so a session may end with a few spare
    `q`s to back out of wherever it is."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    try:
        proc = spawn([program("board-admin")], stdin=slave, stdout=slave,
                     stderr=slave, env={**env, "TERM": "xterm"}, close_fds=True)
    finally:
        os.close(slave)
    term = Terminal(rows, cols)
    open_ = [True]

    def drain(seconds):
        end = time.monotonic() + seconds
        if not open_[0]:  # the pty is gone: wait out the time, don't spin on it
            time.sleep(seconds)
            return
        while open_[0] and time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:  # EIO: the program is gone and so is the pty
                    open_[0] = False
                    return
                if not chunk:
                    open_[0] = False
                    return
                term.feed(chunk)

    def send(keys):
        if proc.poll() is None:
            try:
                os.write(master, keys)
            except OSError:
                pass
        drain(0.25)

    def wait_for(test, what):
        deadline = time.monotonic() + HANG_TIMEOUT
        while not test():
            if time.monotonic() > deadline or not open_[0]:
                raise AssertionError(f"board-admin never showed {what!r}; screen:\n{term.text()}")
            drain(0.05)

    def row_of(label):
        for y, line in enumerate(term.lines()):
            if y and label in line:
                return y
        return None

    try:
        drain(0.2)
        for step in steps:
            if isinstance(step, bytes):
                send(step)
            elif isinstance(step, Wait):
                wait_for(lambda: step.text in term.text(), step.text)
            elif isinstance(step, Select):
                wait_for(lambda: row_of(step.label) is not None, step.label)
                send(b"j" * (row_of(step.label) - 1) + (b"\r" if isinstance(step, Pick) else b""))
            elif isinstance(step, Do):
                step.fn()
            elif isinstance(step, Kill):
                proc.send_signal(step.signum)
                drain(0.25)
            else:
                raise TypeError(step)
        deadline = time.monotonic() + HANG_TIMEOUT
        while proc.poll() is None and time.monotonic() < deadline:
            drain(0.1)
        if proc.poll() is None:
            raise AssertionError("board-admin did not exit; screen:\n" + term.text())
        drain(0.2)  # whatever it printed on the way out
    finally:
        os.close(master)
        kill_tree(proc)
    return TuiRun(proc.returncode, term)
