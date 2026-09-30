#!/usr/bin/env python3
"""A minimal local message board with long-poll delivery.

Single host, stdlib only, one command with subcommands:

    board serve                              run the HTTP server
    board post <area> <topic> <from> <body>  post a markdown message
    board watch [--area A] [--topic T]       long-poll -> one line per message
                [--since ID|head]            'head' = only messages from now on
    board areas                              list areas

The address is two levels:

    area   -- a whole workstream (e.g. "reversing", "openwpm"). The isolation
              boundary: a watcher subscribed to one area never sees another.
    topic  -- a channel within an area (e.g. "ghidra", "fuzz", "crashes").

State is a SQLite database (WAL), so restarts re-derive the board and message
ids stay monotonic. Long-poll delivery is driven by an in-process condition
variable: a post wakes any waiting pollers immediately.

The server binds to 127.0.0.1 ONLY (no authentication yet) -- do not expose it
off-host without adding auth first.

Config via env: BOARD_PORT (8777), BOARD_DB (SQLite path, default ./board.db),
BOARD_URL (client endpoint, default http://127.0.0.1:<BOARD_PORT>).
"""
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# Localhost only. Binding wider requires authentication (not built yet), so the
# address is deliberately not configurable.
ADDR = "127.0.0.1"
PORT = int(os.environ.get("BOARD_PORT", "8777"))
DB = os.environ.get("BOARD_DB", "board.db")
BASE_URL = os.environ.get("BOARD_URL", f"http://{ADDR}:{PORT}")
# Static assets (htmx.min.js) — set by the packaged wrapper; falls back to a
# ./static dir next to this file for dev runs.
STATIC_DIR = os.environ.get(
    "BOARD_STATIC_DIR", str(Path(__file__).resolve().parent / "static"))

SYSTEM_AREA = "_system"

# ---------------------------------------------------------------------------
# Server core (importable + unit-testable without the HTTP layer)
# ---------------------------------------------------------------------------

# One connection, reused across request threads. Every access is made while
# holding _cv (a reentrant condition lock), which serializes DB use and lets a
# post atomically insert-then-notify without racing a poller's wait.
_cv = threading.Condition()
_db = None


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db(path):
    """Open (and migrate) the board database, storing it as the module global."""
    global _db
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS areas(
            name TEXT PRIMARY KEY,
            description TEXT NOT NULL DEFAULT '',
            created_ts TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            area TEXT NOT NULL,
            topic TEXT NOT NULL,
            sender TEXT NOT NULL,
            body TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_messages_area ON messages(area, id);
        """
    )
    conn.commit()
    _db = conn
    return conn


def head_id():
    with _cv:
        return _db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]


def _insert(area, topic, sender, body):
    """Low-level append. Caller must hold _cv. Does not notify."""
    cur = _db.execute(
        "INSERT INTO messages(ts, area, topic, sender, body) VALUES(?,?,?,?,?)",
        (_now(), area, topic, sender, body),
    )
    _db.commit()
    return cur.lastrowid


def _ensure_area(name, description=None):
    """Create the area if new (caller holds _cv). Announce real new areas."""
    row = _db.execute("SELECT description FROM areas WHERE name=?", (name,)).fetchone()
    if row is None:
        _db.execute(
            "INSERT INTO areas(name, description, created_ts) VALUES(?,?,?)",
            (name, description or "", _now()),
        )
        _db.commit()
        if name != SYSTEM_AREA:
            _ensure_area(SYSTEM_AREA)
            _insert(SYSTEM_AREA, "areas", "board",
                    f"new area: **{name}**" + (f" — {description}" if description else ""))
    elif description and not row[0]:
        _db.execute("UPDATE areas SET description=? WHERE name=?", (description, name))
        _db.commit()


def post(area, topic, sender, body):
    with _cv:
        _ensure_area(area)
        mid = _insert(area, topic, sender, body)
        _cv.notify_all()
    return mid


def create_area(name, description):
    with _cv:
        _ensure_area(name, description)
        _cv.notify_all()


def list_areas():
    with _cv:
        rows = _db.execute(
            "SELECT name, description, created_ts FROM areas ORDER BY created_ts"
        ).fetchall()
    return [{"name": r[0], "description": r[1], "created_ts": r[2]} for r in rows]


def _query(since, area, topic, limit=500):
    sql = "SELECT id, ts, area, topic, sender, body FROM messages WHERE id>?"
    args = [since]
    if area:
        sql += " AND area=?"
        args.append(area)
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    return [
        {"id": r[0], "ts": r[1], "area": r[2], "topic": r[3],
         "from": r[4], "body": r[5]}
        for r in _db.execute(sql, args).fetchall()
    ]


def query_since(since, area, topic):
    """Non-blocking read (used by tests and /msg)."""
    with _cv:
        return _query(since, area, topic)


def poll(since, area, topic, timeout):
    deadline = time.monotonic() + timeout
    with _cv:  # query and wait under the same lock -> no lost wakeup
        while True:
            rows = _query(since, area, topic)
            if rows:
                return rows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            _cv.wait(remaining)


def _summary(body):
    first = body.strip().splitlines()[0] if body.strip() else ""
    return first if len(first) <= 200 else first[:197] + "..."


def format_lines(msgs):
    return "".join(
        f"{m['id']}\t[{m['area']}/{m['topic']}] {m['from']}: {_summary(m['body'])}\n"
        for m in msgs
    )


def recent(area, topic, limit=100):
    """The last `limit` messages for a scope, oldest first (for the web view)."""
    with _cv:
        rows = _query(0, area, topic, limit=100000)
    return rows[-limit:]


# ---------------------------------------------------------------------------
# Web view (Jinja + HTMX). Jinja is imported lazily in _web_init so the core
# and its unit tests stay stdlib-only.
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>board{% if area %} · {{ area }}{% endif %}</title>
<script src="/static/htmx.min.js"></script>
<style>
  body { font: 14px system-ui, sans-serif; max-width: 820px; margin: 1.5rem auto;
         padding: 0 1rem; color: #1a1a1a; }
  nav a { margin-right: .6rem; }
  #messages { border: 1px solid #ddd; border-radius: 6px; padding: .5rem;
              margin: .8rem 0; max-height: 55vh; overflow-y: auto; }
  .msg { padding: .4rem 0; border-bottom: 1px solid #f0f0f0; }
  .msg:last-child { border-bottom: 0; }
  .meta { color: #666; font-size: 12px; }
  .body { white-space: pre-wrap; margin-top: .15rem; }
  .empty { color: #999; }
  form input, form textarea { width: 100%; box-sizing: border-box; margin: .2rem 0; }
  form textarea { min-height: 5rem; }
  .row { display: flex; gap: .5rem; }
</style></head><body>
<h1>message board</h1>
<nav>areas: <a href="/">all</a>
  {% for a in areas %}<a href="/?area={{ a.name }}">{{ a.name }}</a>{% endfor %}</nav>
<h2>{{ area or "all areas" }}{% if topic %} / {{ topic }}{% endif %}</h2>
<div id="messages" hx-get="/ui/messages?area={{ area }}&topic={{ topic }}"
     hx-trigger="every 3s" hx-swap="innerHTML">{{ rows|safe }}</div>
<h3>post a message</h3>
<form hx-post="/ui/post" hx-target="#messages" hx-swap="innerHTML"
      hx-on::after-request="if(event.detail.successful) this.querySelector('[name=body]').value=''">
  <div class="row">
    <input name="area" placeholder="area" value="{{ area }}" required>
    <input name="topic" placeholder="topic" value="{{ topic or 'general' }}" required>
    <input name="from" placeholder="from" value="{{ sender }}" required>
  </div>
  <textarea name="body" placeholder="markdown body" required></textarea>
  <button type="submit">post</button>
</form></body></html>"""

_ROWS = """{% for m in messages %}<div class="msg">
<span class="meta">#{{ m.id }} [{{ m.area }}/{{ m.topic }}] {{ m['from'] }} · {{ m.ts }}</span>
<div class="body">{{ m.body }}</div></div>
{% else %}<p class="empty">no messages yet</p>{% endfor %}"""

_page_t = None
_rows_t = None


def _web_init():
    global _page_t, _rows_t
    import jinja2  # lazy: only the server needs it
    env = jinja2.Environment(autoescape=True)
    _page_t = env.from_string(_PAGE)
    _rows_t = env.from_string(_ROWS)


def render_rows(area, topic):
    return _rows_t.render(messages=recent(area, topic))


def render_page(area, topic, sender="me"):
    return _page_t.render(areas=list_areas(), area=area, topic=topic,
                          sender=sender, rows=render_rows(area, topic))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # stay quiet; the DB is the record
        pass

    def handle_one_request(self):
        # A client that walks away mid-response -- a watcher restarted, a
        # `board watch | head`, a browser tab closed -- is routine, not a
        # fault. Swallow it so socketserver does not dump a traceback into
        # the service log for every disconnect.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send(self, code, body, ctype="application/json"):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/poll":
            since = int(q.get("since", ["0"])[0])
            area = q.get("area", [""])[0]
            topic = q.get("topic", [""])[0]
            timeout = min(float(q.get("timeout", ["50"])[0]), 300.0)
            fmt = q.get("format", ["json"])[0]
            msgs = poll(since, area, topic, timeout)
            if fmt == "lines":
                self._send(200, format_lines(msgs), "text/plain; charset=utf-8")
            else:
                self._send(200, json.dumps(msgs))
            return
        if u.path == "/areas":
            self._send(200, json.dumps(list_areas()))
            return
        if u.path.startswith("/msg/"):
            try:
                mid = int(u.path.split("/", 2)[2])
                hit = next((m for m in query_since(mid - 1, "", "") if m["id"] == mid), None)
                if hit is None:
                    raise IndexError
                self._send(200, json.dumps(hit))
            except (ValueError, IndexError):
                self._send(404, json.dumps({"error": "no such id"}))
            return
        if u.path == "/ui/messages":
            self._send(200, render_rows(q.get("area", [""])[0], q.get("topic", [""])[0]),
                       "text/html; charset=utf-8")
            return
        if u.path == "/static/htmx.min.js":
            try:
                self._send(200, (Path(STATIC_DIR) / "htmx.min.js").read_bytes(),
                           "application/javascript")
            except OSError:
                self._send(404, json.dumps({"error": "htmx.min.js not found"}))
            return
        if u.path == "/healthz":
            self._send(200, json.dumps({"head": head_id()}))
            return
        if u.path == "/":
            self._send(200, render_page(q.get("area", [""])[0], q.get("topic", [""])[0]),
                       "text/html; charset=utf-8")
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/ui/post":  # HTMX form submit -> return the refreshed list
            length = int(self.headers.get("Content-Length", "0"))
            form = {k: v[0] for k, v in
                    parse_qs(self.rfile.read(length).decode("utf-8")).items()}
            if form.get("body"):
                post(form.get("area") or "general", form.get("topic") or "general",
                     form.get("from") or "anon", form["body"])
            self._send(200, render_rows(form.get("area", ""), form.get("topic", "")),
                       "text/html; charset=utf-8")
            return
        try:
            data = self._read_json()
        except ValueError:
            self._send(400, json.dumps({"error": "invalid JSON"}))
            return
        if path == "/post":
            if "body" not in data:
                self._send(400, json.dumps({"error": "need a 'body'"}))
                return
            mid = post(data.get("area", "general"), data.get("topic", "general"),
                       data.get("from", "anon"), data["body"])
            self._send(200, json.dumps({"id": mid}))
            return
        if path == "/areas":
            if "name" not in data:
                self._send(400, json.dumps({"error": "need a 'name'"}))
                return
            create_area(data["name"], data.get("description", ""))
            self._send(200, json.dumps({"ok": True}))
            return
        self._send(404, json.dumps({"error": "not found"}))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_serve(_args):
    init_db(DB)
    _web_init()
    srv = ThreadingHTTPServer((ADDR, PORT), Handler)
    srv.daemon_threads = True
    print(f"board on http://{ADDR}:{PORT}  db={DB}  head={head_id()}", flush=True)
    srv.serve_forever()


def _client_get(path):
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return resp.read().decode("utf-8")


def cmd_post(args):
    payload = json.dumps({
        "area": args.area, "topic": args.topic,
        "from": args.sender, "body": " ".join(args.body),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/post", data=payload,
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.read().decode("utf-8"))


def cmd_areas(_args):
    for a in json.loads(_client_get("/areas")):
        desc = f" — {a['description']}" if a["description"] else ""
        print(f"{a['name']}{desc}  ({a['created_ts']})")


def _since_arg(value):
    """--since takes a message id, or 'head' for "only what happens next"."""
    if value == "head":
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a message id or 'head'")


def _resolve_since(value):
    """Turn 'head' into the board's current head id.

    Retries rather than failing: a persistent watcher is routinely started
    before the service is up, and dying there would look like "no news".
    """
    if value != "head":
        return value
    while True:
        try:
            return int(json.loads(_client_get("/healthz"))["head"])
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            print(f"board-unreachable: {exc}", flush=True)
            time.sleep(2)


def cmd_watch(args):
    """Long-poll and print one line per new message: a Monitor event stream."""
    since = _resolve_since(args.since)
    while True:
        params = urlencode({
            "since": since, "area": args.area or "", "topic": args.topic or "",
            "timeout": args.timeout, "format": "lines",
        })
        try:
            with urllib.request.urlopen(
                f"{BASE_URL}/poll?{params}", timeout=args.timeout + 10
            ) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            # Emit on failure too: silence must never look like "no news"
            # (Monitor coverage rule). Back off so a down server doesn't spin.
            print(f"board-unreachable: {exc}", flush=True)
            time.sleep(2)
            continue
        lines = text.splitlines()
        if not lines:
            continue  # long-poll returned empty: no new messages
        for line in lines:
            print(line, flush=True)  # one line per message -> one event each
        since = int(lines[-1].split("\t", 1)[0])


def main(argv=None):
    p = argparse.ArgumentParser(prog="board", description="local long-poll message board")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the HTTP server").set_defaults(fn=cmd_serve)

    sp = sub.add_parser("post", help="post a markdown message")
    sp.add_argument("area")
    sp.add_argument("topic")
    sp.add_argument("sender", metavar="from")
    sp.add_argument("body", nargs="+")
    sp.set_defaults(fn=cmd_post)

    sw = sub.add_parser("watch", help="long-poll one area/topic (Monitor stream)")
    sw.add_argument("--area", default=os.environ.get("BOARD_AREA", ""))
    sw.add_argument("--topic", default=os.environ.get("BOARD_TOPIC", ""))
    sw.add_argument("--since", type=_since_arg, metavar="ID|head",
                    default=os.environ.get("BOARD_SINCE", "0"),
                    help="start after this message id, or 'head' for only new messages")
    sw.add_argument("--timeout", type=int, default=50)
    sw.set_defaults(fn=cmd_watch)

    sub.add_parser("areas", help="list areas").set_defaults(fn=cmd_areas)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
