"""A black-box harness for the message board.

It drives `board`, `board-server` and `board-admin` only as programs: through
their argv, environment, stdio, HTTP, a terminal and the database file. It
never imports them, so the same tests hold for any implementation of the
programs.

Faults are injected at those same edges, never through hooks in the
programs: an HTTP proxy in front of the server loses answers, holds them or
lets another write race in; a fake `systemctl` first on PATH stands in for
the user service manager; and the tests lock the database file or make it
read-only themselves.

The programs come from $BOARD_BIN_DIR if it is set, else from building the
in-tree package (apps/message-board) with nix-build, so a plain `pytest`
tests what this checkout would deploy.
"""

import codecs
import fcntl
import functools
import http.client
import http.server
import json
import os
import pty
import queue
import re
import select
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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# How long a server start, a request or a program's exit may take: a bound
# on a hang, not a speed check. On a loaded box a single SQLite commit (an
# fsync) has stalled for half a minute.
HANG_TIMEOUT = 120

RULE = "names are 1-64 ASCII letters, digits, '_' and '-'"
SYSTEM_AREA = "_system"
PIN_TOPIC = "_pin"

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
                "BOARD_OWNER_NAME", "BOARD_OWNER_PUBKEY", "BOARD_ADMIN_LOG"):
        monkeypatch.delenv(var, raising=False)
    url = dead_url()
    monkeypatch.setenv("BOARD_URL", url)
    monkeypatch.setenv("BOARD_PORT", url.rsplit(":", 1)[1])
    monkeypatch.setenv("BOARD_ADMIN_SERVICE", "")
    monkeypatch.chdir(tmp_path)


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


class Refused(Exception):
    """The board answered with an error status."""

    def __init__(self, response):
        self.status, self.headers, self.text = response.status, response.headers, response.text
        try:
            self.body = json.loads(response.text)
        except ValueError:
            self.body = None
        error = self.body.get("error") if isinstance(self.body, dict) else response.text
        super().__init__(f"{response.status}: {error}")
        self.error = error


class BadRequest(Refused):
    """400: a bad name, a bad signature, a malformed request."""


class Forbidden(Refused):
    """403: a claimed name without its key, a signed pin that is not theirs."""


class NotFound(Refused):
    """404."""


class Conflict(Refused):
    """409: a client key reused for a different message."""


class StaleNonce(Conflict):
    """409 with Board-Error: stale-nonce: the signed head is no longer the head."""

    @property
    def head(self):
        return self.body["head"]


class StalePin(Conflict):
    """409: the pin changed since base_version."""

    @property
    def version(self):
        return self.body["version"]


class ServerError(Refused):
    """5xx."""


def refusal(response):
    if response.status == 409:
        if response.headers.get("Board-Error") == "stale-nonce":
            return StaleNonce(response)
        if "version" in (json.loads(response.text) if response.text.startswith("{") else {}):
            return StalePin(response)
        return Conflict(response)
    cls = {400: BadRequest, 403: Forbidden, 404: NotFound}.get(response.status)
    return (cls or (ServerError if response.status >= 500 else Refused))(response)


# --- signing ------------------------------------------------------------------

MESSAGE = ("board-message", ("area", "topic", "from", "body", "key", "nonce"))
PIN = ("board-pin", ("area", "topic", "from", "body", "base_version", "nonce"))


def canonical(fields, values):
    """The bytes a signature covers, as the board's contract defines them:
    `<name>:<utf-8 byte length>:<value>\\n` per field, in order. This is the
    test suite's own copy, the oracle the programs are held to."""
    out = b""
    for name in fields:
        raw = values[name].encode()
        out += f"{name}:{len(raw)}:".encode() + raw + b"\n"
    return out


def ssh_keygen(*args, data=None, check=True):
    return subprocess.run(["ssh-keygen", *map(str, args)], input=data,
                          capture_output=True, check=check)


def armored_body(text):
    """An armored SSH signature's base64, without the BEGIN/END lines."""
    return "".join(line for line in text.splitlines() if not line.startswith("-----"))


def fingerprint_of(pub_path):
    return ssh_keygen("-l", "-f", pub_path).stdout.split()[1].decode()


class Key:
    """A key made by ssh-keygen itself, on disk."""

    def __init__(self, path):
        self.path = Path(path)
        self.pub = self.path.with_name(self.path.name + ".pub")
        self.public_line = " ".join(self.pub.read_text().split()[:2])
        self.fingerprint = fingerprint_of(self.pub)

    @classmethod
    def make(cls, directory, name, kind="ed25519", passphrase=""):
        path = Path(directory) / name
        ssh_keygen("-q", "-t", kind, "-N", passphrase, "-C", name, "-f", path)
        return cls(path)

    def sign(self, namespace, data, *options):
        """ssh-keygen's SSHSIG over `data`, as the board sends it."""
        out = ssh_keygen("-Y", "sign", "-f", self.path, "-n", namespace, *options, data=data)
        return armored_body(out.stdout.decode())


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


@dataclass(frozen=True)
class Signer:
    """A key signing against one head: what a client holds between reading
    /healthz and sending its write."""
    key: Key
    nonce: str

    @property
    def fingerprint(self):
        return self.key.fingerprint

    def sign(self, kind, values):
        namespace, fields = kind
        return self.key.sign(namespace, canonical(fields, {**values, "nonce": self.nonce}))


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
    """A database file as an older board left it."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


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

    def start(self, **env):
        """Start it with these extra env vars, and wait until it answers."""
        self.starts += 1
        self.out_path = self.logs / f"server-{self.starts}.out"
        self.err_path = self.logs / f"server-{self.starts}.err"
        with open(self.out_path, "wb") as out, open(self.err_path, "wb") as err:
            self.proc = subprocess.Popen([program("board-server")], env=self._env(env),
                                         stdout=out, stderr=err)
        deadline = time.monotonic() + HANG_TIMEOUT
        while True:
            if self.proc.poll() is not None:
                code, self.proc = self.proc.returncode, None
                raise AssertionError(f"board-server exited with {code}:\n{self.err()}")
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=HANG_TIMEOUT) as r:
                    r.read()
                return self
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError(f"board-server never came up:\n{self.err()}")
                time.sleep(0.1)

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=HANG_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.returncode = self.proc.returncode
        self.proc = None

    def restart(self, **env):
        self.stop()
        return self.start(**env)

    def out(self):
        return self.out_path.read_text()

    def err(self):
        return self.err_path.read_text()


@contextmanager
def running(db, logs, **env):
    """A Board on `db` for the length of a with-block."""
    server = Server(db, logs)
    try:
        server.start(**env)
        yield Board(server)
    finally:
        server.stop()


class Board:
    """A running board, through its HTTP API and its database file: what a
    client and an operator can see and do."""

    def __init__(self, server):
        self.server = server
        self.last_sent = None  # the payload of the last write, as sent

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
            raise refusal(response)
        return response.json()

    # reads
    def head_id(self):
        return self._ok(self.get("/healthz"))["head"]

    def poll(self, since=0, area="", topic="", timeout=0, include_archived=True):
        q = urllib.parse.urlencode({"since": since, "area": area, "topic": topic,
                                    "timeout": timeout,
                                    "archived": "1" if include_archived else ""})
        return self._ok(request(f"{self.url}/poll?{q}", timeout=timeout + HANG_TIMEOUT))

    def query_since(self, since=0, area="", topic=""):
        return self.poll(since, area, topic, 0)

    def list_areas(self, include_archived=False):
        return self._ok(self.get(f"/areas?archived={'1' if include_archived else ''}"))

    def get_pin(self, area, topic=""):
        r = self.get("/pin?" + urllib.parse.urlencode({"area": area, "topic": topic}))
        return None if r.status == 404 else self._ok(r)

    def pins_for(self, area="", topic="", include_archived=True):
        return self._ok(self.get("/pins?" + urllib.parse.urlencode({
            "area": area, "topic": topic, "archived": "1" if include_archived else ""})))

    def list_topics(self, area):
        """The topics the web view's topic nav offers for `area`."""
        page = self.get("/?" + urllib.parse.urlencode({"area": area})).text
        nav = page.split("topics: ", 1)[1].split("</nav>", 1)[0]
        return [urllib.parse.unquote(t) for t in re.findall(r'&topic=([^"]*)"', nav)]

    # writes
    def signer(self, key):
        """`key`, signing against the head as of now."""
        return Signer(key, str(self.head_id()))

    def post(self, area, topic, sender, body, key=None, signer=None):
        data = {"area": area, "topic": topic, "from": sender, "body": body}
        if key is not None:
            data["key"] = key
        if signer is not None:
            data["nonce"] = signer.nonce
            data["signature"] = signer.sign(MESSAGE, {
                "area": area, "topic": topic, "from": sender, "body": body, "key": key or ""})
        self.last_sent = data
        return self._ok(request(self.url + "/post", data))["id"]

    def set_pin(self, area, topic, editor, body, base_version=None, signer=None):
        data = {"area": area, "topic": topic, "from": editor, "body": body}
        if base_version is not None:
            data["base_version"] = base_version
        if signer is not None:
            data["nonce"] = signer.nonce
            data["signature"] = signer.sign(PIN, {
                "area": area, "topic": topic, "from": editor, "body": body,
                "base_version": "" if base_version is None else str(base_version)})
        self.last_sent = data
        return self._ok(request(self.url + "/pin", data))["version"]

    def create_area(self, name, description=""):
        self._ok(request(self.url + "/areas", {"name": name, "description": description}))

    def set_archived(self, name, archived):
        return self._ok(request(self.url + "/archive",
                                {"area": name, "archived": archived}))["changed"]

    def signed_message(self, signing_key, /, **fields):
        """A signed /post payload, its nonce the current head."""
        values = {"area": "x", "topic": "t", "from": "agent", "body": "hello",
                  "key": "k1", "nonce": str(self.head_id()), **fields}
        values["signature"] = signing_key.sign(MESSAGE[0], canonical(MESSAGE[1], values))
        return values

    def signed_pin(self, signing_key, /, **fields):
        """A signed JSON /pin payload, its nonce the current head. base_version
        is signed as text and sent as a number, or not at all when empty."""
        values = {"area": "rev", "topic": "", "from": "agent", "body": "rules",
                  "base_version": "", "nonce": str(self.head_id()), **fields}
        signature = signing_key.sign(PIN[0], canonical(PIN[1], values))
        base = values["base_version"]
        return {**values, "base_version": int(base) if base else None, "signature": signature}

    # the database file, as board-admin and an operator see it
    def sql(self, query, *args):
        return sql(self.db, query, *args)

    def get_identity(self, name):
        rows = self.sql("SELECT name, pubkey, fingerprint, source, registered_ts"
                        " FROM identities WHERE name=?", name)
        return dict(zip(("name", "pubkey", "fingerprint", "source", "registered_ts"),
                        rows[0])) if rows else None

    def list_identities(self):
        return [self.get_identity(n) for (n,) in
                self.sql("SELECT name FROM identities ORDER BY name")]

    def delete_message(self, mid):
        """What board-admin's per-message delete does to the file."""
        return sql_changes(self.db, "DELETE FROM messages WHERE id=?", mid) > 0

    def delete_identity(self, name):
        """What board-admin's claim release does to the file."""
        return sql_changes(self.db, "DELETE FROM identities WHERE name=?", name) > 0


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

    @property
    def route(self):
        return urllib.parse.urlsplit(self.path).path

    @property
    def query(self):
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def json(self):
        return json.loads(self.body)


class Proxy:
    """Forwards to a board-server and records every exchange. `before` and
    `after` see each one: `before` returning DROP closes the connection
    without forwarding (a board that is down), `after` returning DROP
    closes it once the server has answered (an answer lost on the way back);
    either may also wait or write to the board itself."""

    def __init__(self, upstream):
        self.upstream = urllib.parse.urlsplit(upstream)
        self.seen = []
        self.before = lambda exchange: None
        self.after = lambda exchange: None
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _relay(self):
                length = int(self.headers.get("Content-Length") or 0)
                ex = Exchange(self.command, self.path, self.rfile.read(length))
                proxy.seen.append(ex)
                if proxy.before(ex) == DROP:
                    self.close_connection = True
                    return
                conn = http.client.HTTPConnection(proxy.upstream.hostname,
                                                  proxy.upstream.port, timeout=HANG_TIMEOUT)
                try:
                    headers = {"Content-Type": self.headers["Content-Type"]} \
                        if self.headers.get("Content-Type") else {}
                    conn.request(self.command, self.path, body=ex.body or None, headers=headers)
                    resp = conn.getresponse()
                    ex.answer, ex.status, ex.headers = resp.read(), resp.status, resp.getheaders()
                finally:
                    conn.close()
                if proxy.after(ex) == DROP:
                    self.close_connection = True
                    return
                self.send_response(ex.status)
                for name, value in ex.headers:
                    if name.lower() in ("content-type", "board-error", "location",
                                        "content-security-policy"):
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(ex.answer)))
                self.end_headers()
                self.wfile.write(ex.answer)

            do_GET = do_POST = _relay

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def routes(self):
        return [ex.route for ex in self.seen]

    def sent(self, route):
        """The JSON bodies POSTed to `route`."""
        return [ex.json() for ex in self.seen if ex.method == "POST" and ex.route == route]

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


def cli(*args, stdin="", env=None, timeout=HANG_TIMEOUT):
    """Run `board` in the current directory with the current environment."""
    done = subprocess.run([program("board"), *args], input=stdin.encode(), capture_output=True,
                          cwd=os.getcwd(), env=dict(os.environ) if env is None else env,
                          timeout=timeout, check=False)
    return Run(done.returncode, done.stdout.decode(), done.stderr.decode())


class Watch:
    """A `board watch` process, read a line at a time."""

    def __init__(self, *args):
        self.proc = subprocess.Popen([program("board"), "watch", *args], cwd=os.getcwd(),
                                     env=dict(os.environ), stdout=subprocess.PIPE,
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
            raise AssertionError(f"board watch exited with {self.proc.wait()}:"
                                 f" {self.proc.stderr.read().decode()}")
        return got.rstrip("\n")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        self.proc.wait(timeout=HANG_TIMEOUT)


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
class Pick:
    """Wait for a list row containing `label`, then move down to it and select
    it. A list starts on its first row (screen row 1, under the title) every
    time it is shown."""
    label: str


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
    done = subprocess.run([program("board-admin")], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout, check=False)
    return Run(done.returncode, done.stdout, done.stderr)


def drive_tui(env, steps, rows=40, cols=120):
    """Run board-admin under a pty, feed it `steps` (keystrokes as bytes, or
    Wait / Pick / Do) and wait for it to exit."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    proc = subprocess.Popen([program("board-admin")], stdin=slave, stdout=slave,
                            stderr=slave, env={**env, "TERM": "xterm"}, close_fds=True,
                            start_new_session=True)
    os.close(slave)
    term = Terminal(rows, cols)
    open_ = [True]

    def drain(seconds):
        end = time.monotonic() + seconds
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
                os.write(master, step)
                drain(0.25)
            elif isinstance(step, Wait):
                wait_for(lambda: step.text in term.text(), step.text)
            elif isinstance(step, Pick):
                wait_for(lambda: row_of(step.label) is not None, step.label)
                os.write(master, b"j" * (row_of(step.label) - 1) + b"\r")
                drain(0.25)
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
            proc.kill()
            proc.wait()
            raise AssertionError("board-admin did not exit; screen:\n" + term.text())
        drain(0.2)  # whatever it printed on the way out
    finally:
        os.close(master)
    return TuiRun(proc.returncode, term)
