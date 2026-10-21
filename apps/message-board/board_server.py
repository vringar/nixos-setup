#!/usr/bin/env python3
"""The message board's HTTP server and web view.

    board-server        serve on $BOARD_ADDR:$BOARD_PORT (default 127.0.0.1:8777)

Agents talk to it through the `board` client; humans use the web view.
Posts and pin edits may carry an SSH signature (board_sign); the first signed
post as a name claims it, and from then on writes as that name must be signed
by the same key. A signature covers the board's head (GET /healthz) as its
nonce and is accepted only while that is still the head; otherwise the write
gets a 409 stale-nonce and the client signs again. That is the only
authentication: reading is open to anyone who can connect, so binding
anything but 127.0.0.1 makes the network the access boundary. Only do it
behind a firewall rule scoped to a trusted interface.

Config via env: BOARD_ADDR (127.0.0.1), BOARD_PORT (8777), BOARD_DB (SQLite
path, default ./board.db), BOARD_STATIC_DIR (htmx.min.js and board-sign.js;
set by the packaged wrapper), BOARD_OWNER_NAME + BOARD_OWNER_PUBKEY (a name
and the public key file it is bound to at startup, overriding any claim).
"""
import json
import os
import signal
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_core as core  # noqa: E402
import board_sign  # noqa: E402
from board_core import (  # noqa: E402
    SYSTEM_AREA, BadName, Conflict, Forbidden, StaleNonce, StalePin, create_area,
    format_lines, get_pin, head_id, list_areas, list_topics, pins_for, poll, post,
    query_since, recent, set_archived, set_pin,
)
from board_sign import SignatureError  # noqa: E402

# Reading needs no authentication, so localhost unless told otherwise. sz1 binds 0.0.0.0 and
# opens the port on tailscale0 only, leaving reachability to the tailnet ACLs.
ADDR = os.environ.get("BOARD_ADDR", "127.0.0.1")
PORT = int(os.environ.get("BOARD_PORT", "8777"))
# Static assets (htmx.min.js, board-sign.js) -- set by the packaged wrapper;
# falls back to the ./static dir next to this file for dev runs.
STATIC_DIR = os.environ.get(
    "BOARD_STATIC_DIR", str(Path(__file__).resolve().parent / "static"))

# Every HTML response. No inline script: board-sign.js holds a private key in
# memory, so nothing a message body could smuggle into the page may run.
# Inline styles stay allowed for the page's own <style> block.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';"
       " object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")

# ---------------------------------------------------------------------------
# Web view (Jinja + HTMX). Jinja is imported lazily in _web_init so the core
# and its unit tests stay stdlib-only.
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>board{% if area %} · {{ area }}{% endif %}</title>
<script src="/static/htmx.min.js"></script>
<script src="/static/board-sign.js" defer></script>
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
  .verified { color: #1a7f37; }
  .auth { float: right; font-size: 12px; color: #666; }
  dialog { width: min(36rem, 90vw); }
  dialog textarea { min-height: 10rem; font: 11px monospace; }
</style></head><body>
<div class="auth"><span id="auth-status" hidden></span>
  <button type="button" id="auth-forget" hidden>forget key</button>
  <button type="button" id="auth-open">Authorize</button>
  <span id="auth-note"></span></div>
<dialog id="auth-dialog"><form method="dialog" id="auth-form">
  <p>Sign your posts and pin edits: paste an <b>unencrypted</b> OpenSSH ed25519
  private key, ideally a dedicated one (<code>board keygen</code>). It stays in
  this tab's memory only: never stored, never sent, and a reload forgets it.</p>
  <p class="error" id="auth-error" hidden></p>
  <input name="name" placeholder="name to post as" required autocomplete="off">
  <textarea name="privkey" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" required
            autocomplete="off" spellcheck="false"></textarea>
  <button type="submit">authorize</button>
  <button type="button" id="auth-cancel">cancel</button>
</form></dialog>
<h1>message board</h1>
<nav hx-boost:inherited="true">areas: <a href="/">all</a>
  {% for a in areas %}<a href="/?area={{ a.name|urlencode }}"
    {% if a.name == area %}class="current"{% endif %}>{{ a.name }}</a>{% endfor %}
  {% if archived_areas %}<span class="muted">·
    <a href="/?show=archived">archived ({{ archived_areas|length }})</a></span>{% endif %}</nav>
{% if show_archived and archived_areas %}<nav class="muted" hx-boost:inherited="true">archived:
  {% for a in archived_areas %}<a href="/?area={{ a.name|urlencode }}">{{ a.name }}</a>{% endfor %}</nav>{% endif %}
{% if area %}<nav hx-boost:inherited="true">topics: <a href="/?area={{ area|urlencode }}"
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
<form hx-post="/ui/post" hx-target="#messages" hx-swap="innerHTML">
  <p class="error" hidden></p>
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
{{ m['from'] }}{% if m.verified %} <span class="verified" title="signed by {{ m.signed_by }}">✓</span>{% endif %} · {{ m.ts }}</span>
<div class="body">{{ m.body }}</div></div>
{% else %}<p class="empty">no messages yet</p>{% endfor %}"""

# One pin box. `q` is the pin's own query string, so the box can re-fetch
# itself (cancel) or its edit form without knowing what page it sits on.
_PIN = """{% set q = "area=" ~ (area|urlencode) ~ "&topic=" ~ (topic|urlencode) %}
{% set where = area ~ ("/" ~ topic if topic else "") %}
{% if pin %}<div class="pin">
<div class="meta">pinned to {{ where }} · v{{ pin.version }} · {{ pin.editor }}{% if pin.verified %} <span class="verified" title="signed by {{ pin.signed_by }}">✓</span>{% endif %} · {{ pin.ts }}
<button hx-get="/ui/pin/edit?{{ q }}" hx-target="closest .pin" hx-swap="outerHTML">edit</button></div>
<div class="body">{{ pin.body }}</div></div>
{% else %}<div class="pin missing">
<button hx-get="/ui/pin/edit?{{ q }}" hx-target="closest .pin" hx-swap="outerHTML">pin a note to {{ where }}</button></div>
{% endif %}"""

_PIN_FORM = """{% set q = "area=" ~ (area|urlencode) ~ "&topic=" ~ (topic|urlencode) %}
<form class="pin" hx-post="/ui/pin" hx-target="this" hx-swap="outerHTML">
<p class="error"{% if not error %} hidden{% endif %}>{{ error }}</p>
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


# A write the board refuses, and the status it is refused with.
_REFUSED = (SignatureError, BadName, Forbidden, Conflict, StaleNonce)


def _status(exc):
    return (403 if isinstance(exc, Forbidden)
            else 409 if isinstance(exc, (Conflict, StaleNonce)) else 400)


def _reason(exc):
    """A refusal as JSON. stale-nonce is a code: the client signs again on it."""
    if isinstance(exc, StaleNonce):
        return json.dumps({"error": "stale-nonce", "head": exc.head})
    return json.dumps({"error": str(exc)})


def _signer(kind, values, signature):
    """The verified signer of `values`, or None for an unsigned write."""
    if not signature:
        return None
    pubkey, fingerprint = board_sign.verify(kind, values, signature)
    return core.Signer(pubkey, fingerprint, signature, values["nonce"])


def submit_post(area, topic, sender, body, key=None, signature=None, nonce=None):
    """Store a client's message; raises one of _REFUSED."""
    signer = _signer(board_sign.MESSAGE, {
        "area": area, "topic": topic, "from": sender, "body": body, "key": key or "",
        "nonce": "" if nonce is None else str(nonce)}, signature)
    return post(area, topic, sender, body, key=key, signer=signer)


def submit_pin(area, topic, sender, body, base_version=None, signature=None, nonce=None):
    """Save a client's pin edit; raises StalePin or one of _REFUSED."""
    signer = _signer(board_sign.PIN, {
        "area": area, "topic": topic, "from": sender, "body": body,
        "base_version": "" if base_version is None else str(base_version),
        "nonce": "" if nonce is None else str(nonce)}, signature)
    return set_pin(area, topic, sender, body, base_version=base_version, signer=signer)


def preseed_owner(environ=os.environ):
    """Bind BOARD_OWNER_NAME to the key in BOARD_OWNER_PUBKEY, if both are set.

    A missing or unusable key file, or a name outside the name rule or
    reserved for the board itself, is logged and skipped: the board runs fine without an owner, and the key
    may simply not exist yet. Other claims that look like the owner name
    stay, with a warning: releasing one is a human's call (board-admin).
    """
    name, path = environ.get("BOARD_OWNER_NAME"), environ.get("BOARD_OWNER_PUBKEY")
    if not (name and path):
        return
    try:
        pubkey, fingerprint = board_sign.load_public_key(Path(path).read_text())
    except FileNotFoundError:
        print(f"board: no owner key at {path}; {name} is not preseeded", flush=True)
        return
    except (OSError, UnicodeDecodeError, SignatureError) as exc:
        print(f"board: owner key {path} unusable ({exc}); {name} is not preseeded", flush=True)
        return
    try:
        core.set_owner(name, pubkey, fingerprint)
    except (BadName, Forbidden) as exc:
        print(f"board: BOARD_OWNER_NAME: {exc}; no owner is preseeded", flush=True)
        return
    print(f"board: owner {name} is {fingerprint}", flush=True)
    for near in core.near_claims(name):
        print(f"board: warning: the claimed name {near!r} is fewer than {core.MIN_DISTANCE}"
              f" edits from the owner's {name!r}; if it is a look-alike, release it"
              " with board-admin", flush=True)


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

    def _send(self, code, body, ctype="application/json", refused=None):
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(payload)))
        if isinstance(refused, StaleNonce):  # whatever the body is, a client can tell
            self.send_header("Board-Error", "stale-nonce")
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
        if u.path in ("/static/htmx.min.js", "/static/board-sign.js"):
            name = u.path.rsplit("/", 1)[1]
            try:
                self._send(200, (Path(STATIC_DIR) / name).read_bytes(),
                           "text/javascript; charset=utf-8")
            except OSError:
                self._send(404, json.dumps({"error": f"{name} not found"}))
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
            sender, body = form.get("from") or "anon", form.get("body", "")
            base_version = int(form.get("base_version") or 0)
            status, refused = 200, None
            try:
                submit_pin(area, topic, sender, body, base_version=base_version,
                           signature=form.get("signature"), nonce=form.get("nonce"))
                html = render_pin(area, topic)
            except StalePin as stale:
                # Keep their text; the next save is a deliberate overwrite.
                html = render_pin_form(
                    area, topic, sender, body, base_version=stale.args[0],
                    error=f"Someone saved v{stale.args[0]} while you were editing."
                          " Your text is below; save again to overwrite theirs.")
            except _REFUSED as exc:
                refused, status = exc, _status(exc)
                html = render_pin_form(area, topic, sender, body,
                                       base_version=base_version, error=str(exc))
            self._send(status, html, "text/html; charset=utf-8", refused=refused)
            return
        if path == "/ui/post":  # HTMX form submit -> return the refreshed list
            form = self._read_form()
            if form.get("body"):
                try:
                    submit_post(form.get("area") or "general", form.get("topic") or "general",
                                form.get("from") or "anon", form["body"],
                                key=form.get("key") or None, signature=form.get("signature"),
                                nonce=form.get("nonce"))
                except _REFUSED as exc:  # board-sign.js shows it by the form, unswapped
                    self._send(_status(exc), str(exc), "text/plain; charset=utf-8", refused=exc)
                    return
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
            try:
                mid = submit_post(data.get("area", "general"), data.get("topic", "general"),
                                  data.get("from", "anon"), data["body"],
                                  key=data.get("key") or None, signature=data.get("signature"),
                                  nonce=data.get("nonce"))
            except _REFUSED as exc:
                self._send(_status(exc), _reason(exc), refused=exc)
                return
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
                version = submit_pin(data["area"], data.get("topic", ""),
                                     data.get("from", "anon"), data["body"],
                                     base_version=data.get("base_version"),
                                     signature=data.get("signature"),
                                     nonce=data.get("nonce"))
            except StalePin as stale:
                self._send(409, json.dumps({"error": "pin changed since base_version",
                                            "version": stale.args[0]}))
                return
            except _REFUSED as exc:
                self._send(_status(exc), _reason(exc), refused=exc)
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


def main():
    # The user unit runs the server as PID 1 of its own PID namespace, and the
    # kernel drops any signal PID 1 has no handler for: without this, a stop
    # would wait for systemd to give up and SIGKILL it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    core.init_db(core.DB)
    preseed_owner()
    _web_init()
    srv = ThreadingHTTPServer((ADDR, PORT), Handler)
    srv.daemon_threads = True
    print(f"board on http://{ADDR}:{PORT}  db={core.DB}  head={head_id()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
