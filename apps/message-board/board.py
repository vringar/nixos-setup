#!/usr/bin/env python3
"""A minimal local message board with long-poll delivery.

Single host, stdlib only, one command with subcommands:

    board serve                              run the HTTP server
    board post <area> <topic> <from> <body>  post a markdown message
    board watch [--area A] [--topic T]       long-poll -> one line per message
                [--since ID|head]            'head' = only messages from now on
    board read [--area A] [--topic T]        one-shot: pins + full backlog, then exit
    board pin <area> [topic] [--set]         show a pinned note, or replace it from stdin
    board areas [--archived]                 list areas
    board archive|unarchive <area>           hide/restore an area (nothing is deleted)

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
        CREATE TABLE IF NOT EXISTS pins(
            area TEXT NOT NULL,
            topic TEXT NOT NULL,  -- '' = the area-wide pin
            body TEXT NOT NULL,
            version INTEGER NOT NULL,
            editor TEXT NOT NULL,
            ts TEXT NOT NULL,
            PRIMARY KEY(area, topic));
        """
    )
    # Boards created before archiving existed lack the column.
    if "archived_ts" not in {r[1] for r in conn.execute("PRAGMA table_info(areas)")}:
        conn.execute("ALTER TABLE areas ADD COLUMN archived_ts TEXT")
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
    """Create the area if new, revive it if archived (caller holds _cv).

    Every write goes through here, so any activity in an archived area brings
    it back: a late agent can never write into an area nobody can see.
    """
    row = _db.execute(
        "SELECT description, archived_ts FROM areas WHERE name=?", (name,)).fetchone()
    if row is not None and row[1] is not None:
        _set_archived(name, False)
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


def _set_archived(name, archived):
    """Flip an existing area's archive state and announce it (caller holds _cv)."""
    _db.execute("UPDATE areas SET archived_ts=? WHERE name=?",
                (_now() if archived else None, name))
    _db.commit()
    _insert(SYSTEM_AREA, "areas", "board",
            f"area {'archived' if archived else 'unarchived'}: **{name}**")


def set_archived(name, archived):
    """Archive or unarchive an area. Returns False if it already was.

    Archiving hides an area from listings and unscoped reads; nothing is
    deleted, and the next post or pin edit in the area unarchives it.
    """
    if name == SYSTEM_AREA:
        raise ValueError(f"{SYSTEM_AREA} cannot be archived")
    with _cv:
        row = _db.execute("SELECT archived_ts FROM areas WHERE name=?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        if (row[0] is not None) == archived:
            return False
        _set_archived(name, archived)
        _cv.notify_all()
    return True


def list_areas(include_archived=False):
    sql = "SELECT name, description, created_ts, archived_ts FROM areas"
    if not include_archived:
        sql += " WHERE archived_ts IS NULL"
    with _cv:
        rows = _db.execute(sql + " ORDER BY created_ts").fetchall()
    return [{"name": r[0], "description": r[1], "created_ts": r[2], "archived_ts": r[3]}
            for r in rows]


_LIVE = " AND area NOT IN (SELECT name FROM areas WHERE archived_ts IS NOT NULL)"


def _query(since, area, topic, limit=500, include_archived=True):
    sql = "SELECT id, ts, area, topic, sender, body FROM messages WHERE id>?"
    args = [since]
    if not include_archived:
        sql += _LIVE
    if area:
        sql += " AND area=?"
        args.append(area)
    if topic:  # an area pin applies to every topic, so its updates do too
        sql += " AND topic IN (?, ?)"
        args += [topic, PIN_TOPIC]
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


def poll(since, area, topic, timeout, include_archived=True):
    deadline = time.monotonic() + timeout
    with _cv:  # query and wait under the same lock -> no lost wakeup
        while True:
            rows = _query(since, area, topic, include_archived=include_archived)
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
    """The last `limit` messages for a scope, oldest first (for the web view).

    Archived areas only show when opened by name.
    """
    with _cv:
        rows = _query(0, area, topic, limit=100000, include_archived=bool(area))
    return rows[-limit:]


# Pins: one editable note per area ('' topic) and per topic, shown above the
# log. The log itself stays append-only; every pin save is also posted to it,
# which both wakes watchers and keeps the pin's full history.
PIN_TOPIC = "_pin"  # where area-wide pin updates are announced


class StalePin(Exception):
    """The pin changed since the editor loaded it."""


def get_pin(area, topic=""):
    with _cv:
        r = _db.execute(
            "SELECT area, topic, body, version, editor, ts FROM pins"
            " WHERE area=? AND topic=?", (area, topic)).fetchone()
    return None if r is None else dict(
        zip(("area", "topic", "body", "version", "editor", "ts"), r))


def pins_for(area="", topic="", include_archived=True):
    """The pins a reader of this scope should see: area-wide first, then the
    topic's own -- or every topic's, when no topic is given. No area: all."""
    sql = "SELECT area, topic FROM pins WHERE 1"
    args = []
    if not include_archived:
        sql += _LIVE
    if area:
        sql += " AND area=?"
        args.append(area)
        if topic:
            sql += " AND topic IN ('', ?)"
            args.append(topic)
    with _cv:
        keys = _db.execute(sql + " ORDER BY area, topic", args).fetchall()
        return [get_pin(a, t) for a, t in keys]


def set_pin(area, topic, editor, body, base_version=None):
    """Create, replace or (empty body) remove a pin; announce it on the log.

    `base_version` is the version the editor started from; a mismatch raises
    StalePin instead of silently overwriting someone else's edit.
    """
    with _cv:
        cur = get_pin(area, topic)
        current = cur["version"] if cur else 0
        if base_version is not None and base_version != current:
            raise StalePin(current)
        where = f"{area}/{topic}" if topic else area
        if not body.strip():
            _db.execute("DELETE FROM pins WHERE area=? AND topic=?", (area, topic))
            note = f"pin removed: {where}"
            version = current
        else:
            version = current + 1
            _db.execute(
                "INSERT OR REPLACE INTO pins VALUES(?,?,?,?,?,?)",
                (area, topic, body, version, editor, _now()))
            note = f"pin updated: {where} v{version}\n\n{body}"
        _db.commit()
        _ensure_area(area)
        _insert(area, topic or PIN_TOPIC, editor, note)
        _cv.notify_all()
    return version


def list_topics(area):
    with _cv:
        return [r[0] for r in _db.execute(
            "SELECT topic FROM messages WHERE area=? UNION"
            " SELECT topic FROM pins WHERE area=? AND topic<>'' ORDER BY 1",
            (area, area))]


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
  .pin { border: 1px solid #e3c96b; background: #fffbea; border-radius: 6px;
         padding: .5rem .7rem; margin: .6rem 0; }
  .pin.missing { background: none; border-style: dashed; }
  .pin .body { margin-top: .3rem; }
  .pin textarea { min-height: 12rem; }
  .error { color: #a00; margin: 0 0 .4rem; }
  nav .current { font-weight: 600; }
  nav .muted, nav .muted a { color: #888; }
  .inline { display: inline; }
  h2 form button { font-size: 12px; font-weight: normal; vertical-align: middle; }
  .archived { background: #f3f3f3; border: 1px solid #ccc; border-radius: 6px;
              padding: .4rem .7rem; }
  form input, form textarea { width: 100%; box-sizing: border-box; margin: .2rem 0; }
  form textarea { min-height: 5rem; }
  .row { display: flex; gap: .5rem; }
</style></head><body>
<h1>message board</h1>
<nav>areas: <a href="/">all</a>
  {% for a in areas %}<a href="/?area={{ a.name|urlencode }}"
    {% if a.name == area %}class="current"{% endif %}>{{ a.name }}</a>{% endfor %}
  {% if archived_areas %}<span class="muted">·
    <a href="/?show=archived">archived ({{ archived_areas|length }})</a></span>{% endif %}</nav>
{% if show_archived and archived_areas %}<nav class="muted">archived:
  {% for a in archived_areas %}<a href="/?area={{ a.name|urlencode }}">{{ a.name }}</a>{% endfor %}</nav>{% endif %}
{% if area %}<nav>topics: <a href="/?area={{ area|urlencode }}"
    {% if not topic %}class="current"{% endif %}>all</a>
  {% for t in topics %}<a href="/?area={{ area|urlencode }}&topic={{ t|urlencode }}"
    {% if t == topic %}class="current"{% endif %}>{{ t }}</a>{% endfor %}</nav>{% endif %}
{% set archive_form %}<form class="inline" method="post" action="/ui/archive">
  <input type="hidden" name="area" value="{{ area }}">
  <input type="hidden" name="archived" value="{{ "0" if archived_ts else "1" }}">
  <button type="submit">{{ "unarchive" if archived_ts else "archive area" }}</button></form>{% endset %}
<h2>{{ area or "all areas" }}{% if topic %} / {{ topic }}{% endif %}
  {% if area and area != system_area and not archived_ts %}{{ archive_form }}{% endif %}</h2>
{% if archived_ts %}<div class="archived">Archived since {{ archived_ts }} — hidden from
  the area list and from reads that don't ask for archived areas. Posting here
  or editing a pin brings it back. {{ archive_form }}</div>{% endif %}
{{ pins|safe }}
<div id="messages" hx-get="/ui/messages?area={{ area|urlencode }}&topic={{ topic|urlencode }}"
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
<span class="meta">#{{ m.id }}
<a href="/?area={{ m.area|urlencode }}&topic={{ m.topic|urlencode }}">[{{ m.area }}/{{ m.topic }}]</a>
{{ m['from'] }} · {{ m.ts }}</span>
<div class="body">{{ m.body }}</div></div>
{% else %}<p class="empty">no messages yet</p>{% endfor %}"""

# One pin box. `q` is the pin's own query string, so the box can re-fetch
# itself (cancel) or its edit form without knowing what page it sits on.
_PIN = """{% set q = "area=" ~ (area|urlencode) ~ "&topic=" ~ (topic|urlencode) %}
{% set where = area ~ ("/" ~ topic if topic else "") %}
{% if pin %}<div class="pin">
<div class="meta">pinned to {{ where }} · v{{ pin.version }} · {{ pin.editor }} · {{ pin.ts }}
<button hx-get="/ui/pin/edit?{{ q }}" hx-target="closest .pin" hx-swap="outerHTML">edit</button></div>
<div class="body">{{ pin.body }}</div></div>
{% else %}<div class="pin missing">
<button hx-get="/ui/pin/edit?{{ q }}" hx-target="closest .pin" hx-swap="outerHTML">pin a note to {{ where }}</button></div>
{% endif %}"""

_PIN_FORM = """{% set q = "area=" ~ (area|urlencode) ~ "&topic=" ~ (topic|urlencode) %}
<form class="pin" hx-post="/ui/pin" hx-target="this" hx-swap="outerHTML">
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<input type="hidden" name="area" value="{{ area }}">
<input type="hidden" name="topic" value="{{ topic }}">
<input type="hidden" name="base_version" value="{{ base_version }}">
<input name="from" placeholder="from" value="{{ sender }}" required>
<textarea name="body" placeholder="markdown; leave empty to remove the pin">{{ body }}</textarea>
<button type="submit">save</button>
<button type="button" hx-get="/ui/pin?{{ q }}" hx-target="closest .pin" hx-swap="outerHTML">cancel</button>
</form>"""

_page_t = None
_rows_t = None
_pin_t = None
_pin_form_t = None


def _web_init():
    global _page_t, _rows_t, _pin_t, _pin_form_t
    import jinja2  # lazy: only the server needs it
    env = jinja2.Environment(autoescape=True)
    _page_t = env.from_string(_PAGE)
    _rows_t = env.from_string(_ROWS)
    _pin_t = env.from_string(_PIN)
    _pin_form_t = env.from_string(_PIN_FORM)


def render_pin(area, topic):
    return _pin_t.render(area=area, topic=topic, pin=get_pin(area, topic))


def render_pin_form(area, topic, sender="me", body=None, base_version=None, error=""):
    pin = get_pin(area, topic)
    if base_version is None:
        base_version = pin["version"] if pin else 0
    if body is None:
        body = pin["body"] if pin else ""
    return _pin_form_t.render(area=area, topic=topic, sender=sender, body=body,
                              base_version=base_version, error=error)


def render_rows(area, topic):
    return _rows_t.render(messages=recent(area, topic))


def render_page(area, topic, sender="me", show_archived=False):
    pins = ""
    if area:  # the area-wide pin, then the topic's own
        pins = render_pin(area, "") + (render_pin(area, topic) if topic else "")
    every = list_areas(include_archived=True)
    archived = [a for a in every if a["archived_ts"]]
    archived_ts = next((a["archived_ts"] for a in archived if a["name"] == area), None)
    return _page_t.render(areas=[a for a in every if not a["archived_ts"]],
                          archived_areas=archived, show_archived=show_archived,
                          archived_ts=archived_ts, system_area=SYSTEM_AREA,
                          area=area, topic=topic,
                          topics=list_topics(area) if area else [],
                          pins=pins, sender=sender, rows=render_rows(area, topic))


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

    def _read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        return {k: v[0] for k, v in
                parse_qs(self.rfile.read(length).decode("utf-8"),
                         keep_blank_values=True).items()}

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
            msgs = poll(since, area, topic, timeout,
                        include_archived=q.get("archived", [""])[0] == "1")
            if fmt == "lines":
                self._send(200, format_lines(msgs), "text/plain; charset=utf-8")
            else:
                self._send(200, json.dumps(msgs))
            return
        if u.path == "/areas":
            self._send(200, json.dumps(
                list_areas(include_archived=q.get("archived", [""])[0] == "1")))
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
        if u.path == "/pins":
            self._send(200, json.dumps(pins_for(
                q.get("area", [""])[0], q.get("topic", [""])[0],
                include_archived=q.get("archived", [""])[0] == "1")))
            return
        if u.path in ("/pin", "/ui/pin", "/ui/pin/edit"):
            area, topic = q.get("area", [""])[0], q.get("topic", [""])[0]
            if not area:
                self._send(400, json.dumps({"error": "need an 'area'"}))
            elif u.path == "/ui/pin":
                self._send(200, render_pin(area, topic), "text/html; charset=utf-8")
            elif u.path == "/ui/pin/edit":
                self._send(200, render_pin_form(area, topic), "text/html; charset=utf-8")
            else:
                pin = get_pin(area, topic)
                self._send(200 if pin else 404,
                           json.dumps(pin or {"error": "no pin"}))
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
            self._send(200, render_page(q.get("area", [""])[0], q.get("topic", [""])[0],
                                        show_archived=q.get("show", [""])[0] == "archived"),
                       "text/html; charset=utf-8")
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/ui/archive":  # plain form post -> back to a sensible page
            form = self._read_form()
            area, archived = form.get("area", ""), form.get("archived") == "1"
            try:
                set_archived(area, archived)
            except (KeyError, ValueError) as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            self.send_response(303)
            self.send_header("Location", "/" if archived else f"/?{urlencode({'area': area})}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/ui/pin":  # HTMX pin editor -> the saved pin box
            form = self._read_form()
            area, topic = form.get("area", ""), form.get("topic", "")
            if not area:
                self._send(400, json.dumps({"error": "need an 'area'"}))
                return
            try:
                set_pin(area, topic, form.get("from") or "anon", form.get("body", ""),
                        base_version=int(form.get("base_version") or 0))
                html = render_pin(area, topic)
            except StalePin as stale:
                # Keep their text; the next save is a deliberate overwrite.
                html = render_pin_form(
                    area, topic, form.get("from") or "anon", form.get("body", ""),
                    base_version=stale.args[0],
                    error=f"Someone saved v{stale.args[0]} while you were editing."
                          " Your text is below; save again to overwrite theirs.")
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/ui/post":  # HTMX form submit -> return the refreshed list
            form = self._read_form()
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
        if path == "/archive":
            try:
                changed = set_archived(data.get("area", ""), bool(data.get("archived", True)))
            except KeyError:
                self._send(404, json.dumps({"error": "no such area"}))
                return
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
                return
            self._send(200, json.dumps({"changed": changed}))
            return
        if path == "/pin":
            if not data.get("area") or "body" not in data:
                self._send(400, json.dumps({"error": "need 'area' and 'body'"}))
                return
            try:
                version = set_pin(data["area"], data.get("topic", ""),
                                  data.get("from", "anon"), data["body"],
                                  base_version=data.get("base_version"))
            except StalePin as stale:
                self._send(409, json.dumps({"error": "pin changed since base_version",
                                            "version": stale.args[0]}))
                return
            self._send(200, json.dumps({"version": version}))
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


def _client_post(path, data):
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(data).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8")


def cmd_post(args):
    print(_client_post("/post", {
        "area": args.area, "topic": args.topic,
        "from": args.sender, "body": " ".join(args.body),
    }))


def _pin_header(p):
    where = f"{p['area']}/{p['topic']}" if p["topic"] else p["area"]
    return f"=== pinned to {where} · v{p['version']} · {p['editor']} · {p['ts']} ==="


def cmd_pin(args):
    """Show a pin, or with --set replace it from stdin (empty input removes it)."""
    if args.set:
        out = json.loads(_client_post("/pin", {
            "area": args.area, "topic": args.topic or "",
            "from": args.sender, "body": sys.stdin.read(),
        }))
        print(f"board: pin saved as v{out['version']}", file=sys.stderr)
        return
    try:
        pin = json.loads(_client_get(f"/pin?{urlencode({'area': args.area, 'topic': args.topic or ''})}"))
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        sys.exit(f"board: no pin on {args.area}{'/' + args.topic if args.topic else ''}")
    print(_pin_header(pin))
    print(pin["body"].rstrip())


def read_all(since, area, topic, archived=False):
    """Every message after `since`, oldest first, paging past the poll cap."""
    out = []
    while True:
        params = urlencode({"since": since, "area": area, "topic": topic,
                            "timeout": 0, "archived": "1" if archived else ""})
        page = json.loads(_client_get(f"/poll?{params}"))
        if not page:
            return out
        out.extend(page)
        since = page[-1]["id"]


def cmd_read(args):
    """One-shot backlog read: full bodies for context, not doorbell lines.

    Ends by reporting a resume cursor on stderr. The head is taken *before*
    reading, so `watch --since <cursor>` misses nothing posted after the read
    and repeats nothing the read already showed.
    """
    if args.area and not args.archived:
        archived = {a["name"] for a in json.loads(_client_get("/areas?archived=1"))
                    if a["archived_ts"]}
        if args.area in archived:  # say so, rather than print an empty read
            sys.exit(f"board: area {args.area} is archived; add --archived to read it")
    head = _head()  # one-shot: a down board fails fast instead of retrying
    since = head if args.since == "head" else args.since
    scope = urlencode({"area": args.area or "", "topic": args.topic or "",
                       "archived": "1" if args.archived else ""})
    pins = json.loads(_client_get(f"/pins?{scope}"))
    msgs = read_all(since, args.area or "", args.topic or "", args.archived)
    cursor = max([head] + [m["id"] for m in msgs])
    if args.json:
        print(json.dumps({"pins": pins, "messages": msgs}, indent=2))
    else:
        for p in pins:  # standing instructions come before the log
            print(_pin_header(p))
            print(p["body"].rstrip())
            print()
        for m in msgs:
            print(f"#{m['id']}  {m['ts']}  [{m['area']}/{m['topic']}] {m['from']}")
            print(m["body"].rstrip())
            print()
    print(f"board: read through id {cursor}; follow with --since {cursor}",
          file=sys.stderr)


def cmd_areas(args):
    for a in json.loads(_client_get(f"/areas?archived={'1' if args.archived else ''}")):
        desc = f" — {a['description']}" if a["description"] else ""
        mark = f"  [archived {a['archived_ts']}]" if a["archived_ts"] else ""
        print(f"{a['name']}{desc}  ({a['created_ts']}){mark}")


def cmd_archive(args):
    try:
        out = json.loads(_client_post("/archive", {"area": args.area,
                                                   "archived": args.archived}))
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 404):
            raise
        sys.exit(f"board: {json.loads(exc.read())['error']}: {args.area}")
    state = "archived" if args.archived else "unarchived"
    print(f"board: {args.area} {state}" if out["changed"]
          else f"board: {args.area} was already {state}", file=sys.stderr)


def _since_arg(value):
    """--since takes a message id, or 'head' for "only what happens next"."""
    if value == "head":
        return value
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a message id or 'head'")


def _head():
    """The board's current head id (one request, no retry)."""
    return int(json.loads(_client_get("/healthz"))["head"])


def _resolve_since(value):
    """Turn 'head' into the board's current head id, for a watcher.

    Retries rather than failing: a persistent watcher is routinely started
    before the service is up, and dying there would look like "no news".
    """
    if value != "head":
        return value
    while True:
        try:
            return _head()
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

    sr = sub.add_parser("read", help="print the full backlog of an area/topic and exit")
    sr.add_argument("--area", default=os.environ.get("BOARD_AREA", ""))
    sr.add_argument("--topic", default=os.environ.get("BOARD_TOPIC", ""))
    sr.add_argument("--since", type=_since_arg, metavar="ID|head", default="0",
                    help="start after this message id (default: everything)")
    sr.add_argument("--json", action="store_true", help="emit a JSON object")
    sr.add_argument("--archived", action="store_true",
                    help="include archived areas (needed to read one by name)")
    sr.set_defaults(fn=cmd_read)

    spin = sub.add_parser("pin", help="show an area/topic pin, or --set it from stdin")
    spin.add_argument("area")
    spin.add_argument("topic", nargs="?", default="",
                      help="omit for the area-wide pin")
    spin.add_argument("--set", action="store_true",
                      help="replace the pin with stdin (empty stdin removes it)")
    spin.add_argument("--from", dest="sender", default=os.environ.get("USER", "anon"))
    spin.set_defaults(fn=cmd_pin)

    sa = sub.add_parser("areas", help="list areas")
    sa.add_argument("--archived", action="store_true", help="include archived areas")
    sa.set_defaults(fn=cmd_areas)

    for name, archived, text in (
            ("archive", True, "hide an area from listings and unscoped reads"),
            ("unarchive", False, "bring an archived area back")):
        sx = sub.add_parser(name, help=text)
        sx.add_argument("area")
        sx.set_defaults(fn=cmd_archive, archived=archived)

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except urllib.error.URLError as exc:
        # A down board is an expected condition, not a crash: one line, no trace.
        sys.exit(f"board: cannot reach {BASE_URL}: {exc.reason}")


if __name__ == "__main__":
    main()
