"""The web view of message board v2: read-only pages for the tree, the
owner's inbox and keys pages, all escaped; the board's own-host and
same-origin rules; and board-sign.js, run under node, signing exactly what
ssh-keygen signs and approving a registration as the page would.
See board_harness."""

import html
import json
import re
import shutil
import socket
import subprocess
from html.parser import HTMLParser

import pytest

from board_harness import (  # noqa: F401 (fixtures)
    KINDS, Key, board, canonical, error_answer, isolated, keyring, new_client_key,
    parse_answer, request, ssh_verify, team,
)

pytestmark = pytest.mark.usefixtures("isolated")


def visible_text(page):
    """A page's text as a reader sees it: no tags, entities decoded,
    whitespace runs as one space."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def exchange(board, method, path, host, origin=None, body=b"", kind="application/json"):
    """One raw request with exactly these Host and Origin headers: its
    (status, headers, body)."""
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close",
             f"Content-Type: {kind}", f"Content-Length: {len(body)}"]
    if origin:
        lines.append(f"Origin: {origin}")
    with socket.create_connection(("127.0.0.1", board.server.port)) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        answer = b""
        while chunk := s.recv(65536):
            answer += chunk
    return parse_answer(answer)


def test_other_sites_cannot_use_the_board_through_a_browser(board, team):
    """Any page a browser on the tailnet opens could post a form or a
    plain-text "JSON" body here without asking, and one on a name rebound
    in DNS to the board's address could read it too."""
    port = board.server.port
    head = board.head_id()
    post = json.dumps(board.signed("post", team.alice.key, {
        "path": "rev/log", "body": "obey", "key": new_client_key()})).encode()
    refused = exchange(board, "POST", "/post", f"127.0.0.1:{port}", "https://evil.example", post,
                       "text/plain")
    assert refused[0] == 403 and "another site" in refused[2]
    error_answer(*refused)
    refused = exchange(board, "POST", "/ui/approve", f"127.0.0.1:{port}", "null", b"item=1",
                       "application/x-www-form-urlencoded")
    assert refused[0] == 403
    refused = exchange(board, "GET", "/n/rev", f"evil.example:{port}")
    assert refused[0] == 403 and "not a name this board answers to" in refused[2]
    error_answer(*refused)
    assert exchange(board, "GET", "/ui/inbox", f"evil.example:{port}")[0] == 403
    assert board.head_id() == head
    # its own page, and every name the board is reached by, still work
    assert exchange(board, "POST", "/post", f"sz1.tail1234.ts.net:{port}",
                    f"http://sz1.tail1234.ts.net:{port}", post)[0] == 200
    for host in (f"localhost:{port}", f"sz1:{port}", f"[::1]:{port}", f"100.64.0.1:{port}"):
        assert exchange(board, "GET", "/healthz", host)[0] == 200, host


PAGES = ["/", "/view/rev", "/view/rev/log", "/view/rev/state", "/view/rev/q", "/ui/inbox",
         "/ui/keys"]


def _furnish(team):
    team.alice.create("rev/state", "note", body="state")
    team.alice.create("rev/q", "queue")
    team.alice.post("rev/log", "x")
    team.bob.ask("rev/q", "y")


@pytest.mark.parametrize("path", PAGES)
def test_a_page_has_a_csp_and_no_inline_script(board, team, path):
    _furnish(team)
    r = board.get(path)
    assert r.status == 200
    csp, page = r.headers["content-security-policy"], r.text
    assert "script-src 'self'" in csp and "'unsafe-eval'" not in csp
    assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", page))
    assert "hx-on" not in page  # htmx runs those through eval
    assert not re.search(r"<[^>]+\son[a-z]+=", page)  # nor inline event handlers


def test_the_signing_script_is_served(board):
    js = board.get("/static/board-sign.js")
    assert js.status == 200 and "globalThis.BoardSign" in js.text


def test_the_front_page_lists_the_top_level(board, team):
    page = board.get("/").text
    assert 'href="/view/rev"' in page and "reversing" in page and "_board" in page


def test_each_kind_of_node_has_its_page_and_every_body_is_escaped(board, team):
    a, b = team.alice, team.bob
    m1 = a.post("rev/log", "<b>bold</b> & co")
    a.post("rev/log", "two")
    a.post("rev/log", "three")
    a.create("rev/state", "note", body="v1")
    a.set_note("rev/state", "<script>alert(1)</script>\nline two", base_version=1, thread="rev/log",
               through=m1)
    b.create("rev/bob", "queue")
    a.ask("rev/bob", "<img src=x onerror=alert(2)> please")

    note = board.get("/view/rev/state").text
    assert "<script>alert(1)" not in note and "&lt;script&gt;alert(1)&lt;/script&gt;" in note
    assert f"summarizes /rev/log through #{m1}, 2 behind" in visible_text(note)
    assert 'href="/view/rev/log"' in note  # the staleness links to the thread
    assert "v2" in visible_text(note) and "alice" in visible_text(note)
    thread = board.get("/view/rev/log").text
    assert "<b>bold</b>" not in thread and "&lt;b&gt;bold&lt;/b&gt; &amp; co" in thread
    queue = board.get("/view/rev/bob").text
    assert "<img src=x" not in queue and "&lt;img src=x onerror=alert(2)&gt; please" in queue
    container = visible_text(board.get("/view/rev").text)
    assert all(p in container for p in ("rev/log", "rev/state", "rev/bob")) or \
        all(p in container for p in ("log", "state", "bob"))
    assert board.get("/view/rev/nope").status == 404


HOSTILE_NOTE = '<script>alert("note")</script>'
HOSTILE_REPO = "<img src=x onerror=alert(1)>"


def _escaped(raw, page):
    """`raw` is nowhere in `page` as markup, and there as text."""
    return raw not in page and raw in html.unescape(page)


def test_the_inbox_lists_registrations_and_the_owners_items_escaped(board, team, keyring):
    board.register(keyring["newbie"], "newbie", note=HOSTILE_NOTE, repo=HOSTILE_REPO)
    board.register(keyring["alice-2"], "alice", note="my next session key")
    team.owner.create("rev/stefan", "queue")
    team.alice.ask("rev/stefan", "<i>please</i> decide")
    page = board.get("/ui/inbox").text
    assert _escaped(HOSTILE_NOTE, page) and HOSTILE_REPO not in page
    assert _escaped("<i>please</i> decide", page)
    text = visible_text(page)
    assert "newbie" in text and keyring["newbie"].fingerprint in text
    assert f"replaces {team.alice.fingerprint}" in text  # a new key for a bound name


def test_the_keys_page_lists_the_trust_list_escaped(board, team, keyring):
    key = keyring["newbie"]
    item = board.register(key, "newbie", note=HOSTILE_NOTE, repo=HOSTILE_REPO)["item"]
    board.approve(item, "newbie", key.fingerprint)
    board.owner.revoke(team.bob.fingerprint, "gone")
    page = board.get("/ui/keys").text
    assert _escaped(HOSTILE_NOTE, page) and _escaped(HOSTILE_REPO, page)
    text = visible_text(page)
    for fingerprint in (board.owner.fingerprint, key.fingerprint, team.bob.fingerprint):
        assert fingerprint in text
    assert all(state in text for state in ("owner", "trusted", "revoked"))


def test_a_pending_key_can_be_approved_from_the_keys_page(board, team, keyring):
    """A pending key is not a dead end on the keys page: it carries the
    owner-only Approve and Decline, which swap the keys page, and the pages
    point at each other."""
    key = keyring["newbie"]
    item = board.register(key, "newbie")["item"]
    page = board.get("/ui/keys").text
    assert '<a href="/ui/inbox">inbox</a>' in page and "1 pending" in visible_text(page)
    assert 'hx-post="/ui/approve" hx-target="#keys"' in page
    assert 'hx-post="/ui/resolve" hx-target="#keys"' in page
    controls = _Controls()
    controls.feed(page)
    assert controls.shown == [], controls.shown  # hidden until the owner authorizes
    assert '<a href="/ui/keys">keys</a>' in board.get("/ui/inbox").text
    # the answer is the piece of page the form sat on
    body = board.signed("approve", board.owner.key,
                        {"item": item, "name": "newbie", "fingerprint": key.fingerprint,
                         "key": new_client_key()})
    body["view"] = "keys"
    answer = request(f"{board.url}/ui/approve", body, form=True)
    assert answer.status == 200 and '<table class="keys">' in answer.text, answer.text
    assert board.key_info(key.fingerprint)["state"] == "trusted"


class _Controls(HTMLParser):
    """The write controls of a page, and whether each is shown before any
    key is authorized: hidden itself, inside a hidden element, or inside a
    dialog that is not open."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
            "track", "wbr"}
    WORDS = re.compile(r"approve|decline|revoke|answer|cancel|archive", re.I)

    def __init__(self):
        super().__init__()
        self.stack = []  # whether each open element hides what is in it
        self.shown = []
        self.button = None

    def _hides(self, tag, attrs):
        return "hidden" in attrs or (tag == "dialog" and "open" not in attrs) \
            or attrs.get("aria-hidden") == "true"

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = any(self.stack) or self._hides(tag, attrs)
        target = attrs.get("hx-post") or attrs.get("action") or ""
        if tag == "form" and (target.startswith("/ui/") or self.WORDS.search(target)) \
                and not hidden:
            self.shown.append(target)
        if tag == "button" and not hidden and not (attrs.get("id") or "").startswith("auth-"):
            self.button = [attrs.get("value") or ""]
        if tag == "input" and attrs.get("type") == "submit" and not hidden \
                and self.WORDS.search(attrs.get("value") or ""):
            self.shown.append(attrs["value"])
        if tag not in self.VOID:
            self.stack.append(self._hides(tag, attrs))

    def handle_data(self, data):
        if self.button is not None:
            self.button.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self.button is not None:
            label = " ".join(self.button)
            if self.WORDS.search(label):
                self.shown.append(label.strip())
            self.button = None
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def test_write_controls_stay_hidden_until_a_trusted_key_is_authorized(board, team, keyring):
    """The page shows its write controls (Approve, Decline, Revoke, Answer,
    the post and note forms) only once a key the board trusts is
    authorized in it, and approve and revoke only for the owner key;
    served, before any key, none of them is shown."""
    board.register(keyring["newbie"], "newbie")
    team.owner.create("rev/stefan", "queue")
    team.alice.ask("rev/stefan", "a question")
    team.alice.create("rev/state", "note", body="v1")
    for path in ("/ui/inbox", "/ui/keys", "/view/rev/stefan", "/view/rev/log", "/view/rev/state"):
        page = board.get(path).text
        assert 'id="auth-open"' in page, path  # the way to authorize is there
        parser = _Controls()
        parser.feed(page)
        assert parser.shown == [], (path, parser.shown)


# --- board-sign.js under node -----------------------------------------------------------------

def _node():
    if shutil.which("node") is None:
        pytest.skip("node is not installed (it is in the repo's shell.nix)")


def _served_js(board, tmp_path):
    """board-sign.js as the web view serves it."""
    r = board.get("/static/board-sign.js")
    assert r.status == 200
    path = tmp_path / "board-sign.js"
    path.write_text(r.text)
    return path


# Finds the kinds in whatever shape the script exports them: [namespace,
# fields] pairs or {namespace, fields} objects, anywhere in BoardSign up to
# two levels down, keyed by namespace.
FIND_KINDS = """
const findKinds = (B) => {
  const kinds = {};
  const visit = (v, depth) => {
    if (!v || typeof v !== "object" || depth > 2) return;
    if (Array.isArray(v) && v.length === 2 && typeof v[0] === "string" && Array.isArray(v[1])) {
      kinds[v[0]] = v;
    } else if (typeof v.namespace === "string" && Array.isArray(v.fields)) {
      kinds[v.namespace] = v;
    } else if (!Array.isArray(v)) {
      for (const x of Object.values(v)) visit(x, depth + 1);
    }
  };
  visit(B, 0);
  return kinds;
};
const fieldsOf = (kind) => (Array.isArray(kind) ? kind[1] : kind.fields);
"""

SAMPLE = {"path": "rev/log", "kind": "thread", "description": "a d", "owner": "", "body":
          "line one\nfrom=evil ü🙂", "base_version": "3", "thread": "rev/log", "through": "10020", "item": "10022",
          "state": "answered", "ref": "", "event": "10360", "archived": "1", "name": "newbie", "note": "repo, purpose",
          "repo": "/home/x/repo", "reason": "replaced", "key": "0f3c", "nonce": "10041"}


def test_browser_signing_matches_ssh_keygen_for_every_kind(board, tmp_path, keyring):
    """board-sign.js: the same field table, the same canonical bytes, and
    the very signature ssh-keygen makes (ed25519 is deterministic, and
    SSHSIG fixes the framing)."""
    _node()
    key = keyring["alice"]
    locked = Key.make(tmp_path, "locked", passphrase="secret")
    ecdsa = Key.make(tmp_path, "ec", kind="ecdsa")
    values = {**SAMPLE, "fingerprint": key.fingerprint}
    script = tmp_path / "drive.js"
    script.write_text(FIND_KINDS + """
const fs = require("fs");
const [js, key, locked, ecdsa, values, namespaces] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
const hex = (b) => Buffer.from(b).toString("hex");
const refused = (path) => { try { B.parsePrivateKey(fs.readFileSync(path, "utf8")); return null; }
                             catch (e) { return e.message; } };
(async () => {
  const signer = await B.importKey(fs.readFileSync(key, "utf8"));
  const kinds = findKinds(B), v = JSON.parse(values), out = {};
  for (const ns of JSON.parse(namespaces)) {
    const kind = kinds[ns];
    if (!kind) { out[ns] = null; continue; }
    const mine = Object.fromEntries(fieldsOf(kind).map((f) => [f, v[f]]));
    out[ns] = { fields: fieldsOf(kind), bytes: hex(B.canonical(kind, mine)),
                signature: await B.sign(signer, kind, mine) };
  }
  console.log(JSON.stringify({ kinds: out, fingerprint: signer.fingerprint,
                               extractable: signer.key.extractable,
                               locked: refused(locked), ecdsa: refused(ecdsa) }));
})();
""")
    namespaces = [ns for ns, _ in KINDS.values()]
    run = subprocess.run(["node", script, _served_js(board, tmp_path), key.path, locked.path,
                          ecdsa.path, json.dumps(values), json.dumps(namespaces)],
                         capture_output=True, text=True, check=True)
    got = json.loads(run.stdout)
    for namespace, fields in KINDS.values():
        kind = got["kinds"][namespace]
        assert kind is not None, f"board-sign.js has no {namespace}"
        data = canonical(fields, {f: values[f] for f in fields})
        assert tuple(kind["fields"]) == fields, namespace
        assert bytes.fromhex(kind["bytes"]) == data, namespace
        assert kind["signature"] == key.sign(namespace, data), namespace
        assert ssh_verify(tmp_path, key.public_line, namespace, data, kind["signature"])
    assert (got["fingerprint"], got["extractable"]) == (key.fingerprint, False)
    assert "passphrase-protected" in got["locked"]
    assert "only ed25519" in got["ecdsa"]


def test_the_page_approves_signing_the_head_of_the_moment(board, team, keyring, tmp_path):
    """What the inbox's Approve does, run under node against a real board:
    read the head, sign as the owner, POST the form; once more when
    another write landed in between."""
    _node()
    newbie = keyring["newbie"]
    item = board.register(newbie, "newbie")["item"]
    script = tmp_path / "drive.js"
    script.write_text(FIND_KINDS + """
const fs = require("fs");
const [js, ownerKey, agentKey, base, item, fingerprint] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
const kinds = findKinds(B);
const realFetch = globalThis.fetch;
let log = [], races = 1;
(async () => {
  const owner = await B.importKey(fs.readFileSync(ownerKey, "utf8"));
  const agent = await B.importKey(fs.readFileSync(agentKey, "utf8"));
  globalThis.fetch = async (url, init) => {
    const response = await realFetch(url, init);
    const path = new URL(url, base).pathname;
    log.push(path);
    if (path === "/healthz" && races > 0) {  // someone else writes right after the read
      races--;
      const { head } = await response.clone().json();
      const values = { path: "rev/log", body: "concurrent", key: crypto.randomUUID(),
                       nonce: String(head) };
      values.signature = await B.sign(agent, kinds["board2-post"], values);
      const raced = await realFetch(base + "/post", { method: "POST",
        headers: { "content-type": "application/json" }, body: JSON.stringify(values) });
      if (raced.status !== 200) throw new Error(`the racing post failed: ${await raced.text()}`);
    }
    return response;
  };
  const r = await B.postSigned(owner, kinds["board2-approve"], base + "/ui/approve",
                               { item, name: "newbie", fingerprint, key: crypto.randomUUID() });
  console.log(JSON.stringify({ log, status: r.status, text: await r.text() }));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), board.owner.key.path,
                          team.alice.key.path, board.url, str(item), newbie.fingerprint],
                         capture_output=True, text=True, check=True)
    got = json.loads(run.stdout)
    assert got["log"] == ["/healthz", "/ui/approve", "/healthz", "/ui/approve"]
    assert got["status"] < 400, got["text"]
    assert board.key_info(newbie.fingerprint)["state"] == "trusted"
    assert [m["body"] for m in board.messages("rev/log")] == ["concurrent"]


# --- the write forms: post to a thread, edit a note ---------------------------------------------

class _Fields(HTMLParser):
    """The named fields of the forms on a page, by form target."""

    def __init__(self):
        super().__init__()
        self.forms, self.current, self.textarea = {}, None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = self.forms.setdefault(attrs.get("hx-post") or attrs.get("action"), {})
        elif tag in ("input", "textarea") and self.current is not None and attrs.get("name"):
            self.current[attrs["name"]] = attrs.get("value") or ""
            self.textarea = attrs["name"] if tag == "textarea" else None

    def handle_data(self, data):
        if self.textarea:
            self.current[self.textarea] += data

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.textarea = None
        elif tag == "form":
            self.current = None


def forms(page):
    parser = _Fields()
    parser.feed(page)
    return parser.forms


def test_the_thread_and_note_pages_carry_their_write_forms(board, team):
    team.alice.create("rev/state", "note", body="v1")
    team.alice.set_note("rev/state", "v2 text", base_version=1)
    post = forms(board.get("/view/rev/log").text)["/ui/post"]
    assert post.get("path") == "rev/log" and "body" in post
    note = forms(board.get("/view/rev/state").text)["/ui/note"]
    assert (note.get("path"), note.get("base_version")) == ("rev/state", "2")
    assert "v2 text" in note.get("body", "")  # the edit starts from the current text


def test_a_queue_page_carries_the_ask_form(board, team):
    team.alice.create("rev/q", "queue")
    ask = forms(board.get("/view/rev/q").text)["/ui/ask"]
    assert ask.get("path") == "rev/q" and "body" in ask


def test_a_signed_web_ask_files_an_item_and_answers_with_the_queue(board, team):
    a = team.alice
    a.create("rev/q", "queue")
    signed = board.signed("file", a.key, {"path": "rev/q", "body": "<b>please</b>", "key": new_client_key()})
    r = request(board.url + "/ui/ask", signed, form=True)
    assert r.status == 200 and "&lt;b&gt;please&lt;/b&gt;" in r.text and "<b>" not in r.text
    [item] = [i for i in board.queue_items("rev/q") if i["body"] == "<b>please</b>"]
    assert (item["asker"], item["state"]) == ("alice", "open")
    head = board.head_id()
    r = request(board.url + "/ui/ask", {"path": "rev/q", "body": "unsigned", "key": new_client_key(),
                                        "nonce": str(head)}, form=True)
    assert r.status == 401 and r.headers["Board-Error"] == "unsigned"
    assert board.head_id() == head


def test_a_signed_web_post_lands_and_an_unsigned_one_is_refused(board, team):
    a = team.alice
    signed = board.signed("post", a.key, {"path": "rev/log", "body": "from the form",
                                          "key": new_client_key()})
    r = request(board.url + "/ui/post", signed, form=True)
    assert r.status == 200 and "from the form" in html.unescape(r.text)
    [message] = board.messages("rev/log")
    assert (message["body"], message["from"], message["signed_by"]) == \
        ("from the form", "alice", a.fingerprint)
    head = board.head_id()
    r = request(board.url + "/ui/post", {"path": "rev/log", "body": "unsigned",
                                         "key": new_client_key(), "nonce": str(head)}, form=True)
    assert r.status == 401 and r.headers["Board-Error"] == "unsigned"
    assert board.head_id() == head


def test_a_stale_web_note_edit_keeps_the_text_and_offers_the_new_base(board, team):
    """Someone else saved since the form was opened: the edit is not
    lost, not saved over theirs, and a resave is based on their version."""
    a = team.alice
    a.create("rev/state", "note", body="v1")
    team.owner.set_note("rev/state", "the owner's v2", base_version=1)
    signed = board.signed("note", a.key, {"path": "rev/state", "body": "my edit, typed for minutes",
                                          "base_version": "1", "key": new_client_key()})
    r = request(board.url + "/ui/note", signed, form=True)
    assert r.headers.get("Board-Error") != "stale-nonce"  # an edit conflict: no re-sign
    assert board.note("rev/state")["body"] == "the owner's v2"  # nothing lost
    again = forms(r.text)["/ui/note"]
    assert again.get("base_version") == "2" and "my edit, typed for minutes" in again.get("body", "")
    assert "v2" in visible_text(r.text)  # it says what changed under the edit
    resaved = board.signed("note", a.key, {**{k: again[k] for k in ("path", "body", "base_version")},
                                           "key": new_client_key()})
    assert request(board.url + "/ui/note", resaved, form=True).status == 200
    assert board.note("rev/state")["version"] == 3


def test_the_page_posts_and_edits_signing_the_head_of_the_moment(board, team, tmp_path):
    """What the page's signed submits do, run under node against a real
    board: read the head, sign, POST the form; once more when another
    write landed in between; never for a stale note edit (that is the
    human's to resolve); and give up after eight."""
    _node()
    team.alice.create("rev/state", "note", body="v1")
    script = tmp_path / "drive.js"
    script.write_text(FIND_KINDS + """
const fs = require("fs");
const [js, mine, theirs, base] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
const kinds = findKinds(B), post = kinds["board2-post"], note = kinds["board2-note"];
const realFetch = globalThis.fetch;
let log = [], races = 0, other;
globalThis.fetch = async (url, init) => {
  const response = await realFetch(url, init);
  const path = new URL(url, base).pathname;
  log.push(path);
  if (path === "/healthz" && races > 0) {  // someone else writes right after the read
    races--;
    const { head } = await response.clone().json();
    const values = { path: "rev/log", body: "concurrent", key: crypto.randomUUID(),
                     nonce: String(head) };
    values.signature = await B.sign(other, post, values);
    await realFetch(base + "/post", { method: "POST", headers: { "content-type": "application/json" },
                                      body: JSON.stringify(values) });
  }
  return response;
};
(async () => {
  const signer = await B.importKey(fs.readFileSync(mine, "utf8"));
  other = await B.importKey(fs.readFileSync(theirs, "utf8"));
  const steps = [
    [1, post, "/ui/post", { path: "rev/log", body: "from the page", key: crypto.randomUUID() }],
    [1, note, "/ui/note", { path: "rev/state", body: "web text", base_version: "1", thread: "", through: "",
                            key: crypto.randomUUID() }],
    [0, note, "/ui/note", { path: "rev/state", body: "stale edit", base_version: "1", thread: "", through: "",
                            key: crypto.randomUUID() }],
    [99, post, "/ui/post", { path: "rev/log", body: "never lands", key: crypto.randomUUID() }],
  ];
  const out = [];
  for (const [n, kind, path, values] of steps) {
    log = []; races = n;
    const r = await B.postSigned(signer, kind, base + path, values);
    out.push({ log, status: r.status, refusal: r.headers.get("Board-Error"), text: await r.text() });
  }
  console.log(JSON.stringify(out));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), team.alice.key.path,
                          team.bob.key.path, board.url], capture_output=True, text=True, check=True)
    posted, edited, stale, busy = json.loads(run.stdout)

    def once_more(path):
        return ["/healthz", path, "/healthz", path]

    assert (posted["log"], posted["status"]) == (once_more("/ui/post"), 200)
    assert "from the page" in html.unescape(posted["text"])
    assert (edited["log"], edited["status"]) == (once_more("/ui/note"), 200)
    assert stale["log"] == ["/healthz", "/ui/note"] and stale["refusal"] != "stale-nonce"
    assert "stale edit" in html.unescape(stale["text"])  # the text comes back, to resave
    assert busy["log"] == ["/healthz", "/ui/post"] * 8
    assert (busy["status"], busy["refusal"]) == (409, "stale-nonce")
    mine = [m for m in board.messages("rev/log", all=1) if m["from"] == "alice"]
    assert [(m["body"], m["signed_by"]) for m in mine] == [("from the page", team.alice.fingerprint)]
    note = board.note("rev/state")
    assert (note["version"], note["body"], note["from"]) == (2, "web text", "alice")


def test_every_page_carries_a_tree_sidebar_that_marks_the_current_path(board, team):
    _furnish(team)
    page = board.get("/view/rev/log").text
    assert '<aside class="side">' in page and "@media (min-width: 960px)" in page
    assert re.search(r'<li class="tree-thread tree-on"><a href="/view/rev/log" aria-current="page"', page)
    assert re.search(r'<li class="tree-container tree-above"><details open><summary><a href="/view/rev"', page)
    aside = page[page.index("<aside"):page.index("</aside>")]
    assert 'class="note' not in aside and 'class="card' not in aside
    assert "<nav><a href=\"/\">tree</a>" in page  # the narrow-screen way in stays
    for path in ("/", "/ui/inbox", "/ui/keys"):
        assert 'href="/view/rev"' in board.get(path).text.split("<main>")[0]


def test_the_tree_page_carries_the_create_form(board, team):
    form = forms(board.get("/").text)["/ui/create"]
    assert "path" in form and "description" in form


def test_a_signed_web_create_makes_the_node_and_a_refusal_is_plain_text(board, team):
    a = team.alice

    def create(path, kind, **extra):
        values = {"path": path, "kind": kind, "description": "", "owner": "", "body": "",
                  "key": new_client_key(), **extra}
        return request(board.url + "/ui/create", board.signed("create", a.key, values), form=True)

    r = create("rev/fresh", "thread", description="made on the web")
    assert r.status == 200 and 'href="/view/rev/fresh"' in r.text
    assert board.node("rev/fresh")["node"]["description"] == "made on the web"
    r = create("rev/fresh", "thread")  # exists already
    assert r.status == 409 and r.headers["Board-Error"] == "conflict" and "<" not in r.text
    r = create("nowhere/leaf", "note")  # the page makes parents first; the board itself does not
    assert r.status == 404
    head = board.head_id()
    r = request(board.url + "/ui/create", {"path": "rev/x", "kind": "thread", "description": "",
                                           "owner": "", "body": "", "key": new_client_key(),
                                           "nonce": str(head)}, form=True)
    assert r.status == 401 and r.headers["Board-Error"] == "unsigned" and board.head_id() == head


def test_the_page_makes_the_missing_parents_before_the_node(board, team, tmp_path):
    _node()
    script = tmp_path / "create.js"
    script.write_text("""
const fs = require("fs");
const [js, mine, base] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
(async () => {
  const signer = await B.importKey(fs.readFileSync(mine, "utf8"));
  const out = [];
  for (const path of ["fresh/area/leaf", "fresh/area/other"]) {
    const r = await B.postCreate(signer, base + "/ui/create", { path, kind: "thread", description: "d",
                                 owner: "", body: "", key: crypto.randomUUID() });
    out.push(r.status);
  }
  console.log(JSON.stringify(out));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), team.owner.key.path, board.url],
                         capture_output=True, text=True, check=True)
    assert json.loads(run.stdout) == [200, 200]  # the second finds its parents there already
    kinds = {n["path"]: n["kind"] for n in board.children("fresh/area")}
    assert kinds == {"fresh/area/leaf": "thread", "fresh/area/other": "thread"}
    assert board.node("fresh")["node"]["kind"] == "container"


def test_the_note_form_names_the_thread_and_a_web_edit_stores_it(board, team):
    a = team.alice
    m = a.post("rev/log", "one")
    a.create("rev/state", "note", body="v1")
    form = forms(board.get("/view/rev/state").text)["/ui/note"]
    assert "thread" in form and "through" in form
    signed = board.signed("note", a.key, {"path": "rev/state", "body": "v2", "base_version": "1",
                                          "thread": "rev/log", "through": str(m),
                                          "key": new_client_key()})
    assert request(board.url + "/ui/note", signed, form=True).status == 200
    note = board.note("rev/state")
    assert (note["thread"], note["through"]) == ("rev/log", m)
    page = visible_text(board.get("/view/rev/state").text)
    assert f"summarizes /rev/log through #{m}, 0 behind" in page
    for half in ({"through": str(m)}, {"thread": "rev/log"}):  # half a pair is refused
        bad = board.signed("note", a.key, {"path": "rev/state", "body": "v3", "base_version": "2",
                                           "key": new_client_key(), **half})
        r = request(board.url + "/ui/note", bad, form=True)
        assert r.status == 400 and board.note("rev/state")["version"] == 2


def test_controls_inserted_after_authorizing_are_shown_to_the_owner(board, tmp_path):
    """The owner's Approve and Decline come served hidden and only
    board-sign.js shows them. An authorized page must show controls that
    arrive later by any route, not only ones htmx announces with its swap
    event: board-sign.js is run under node against a stand-in page, authorized
    with the owner key, and a control inserted afterwards is reported to the
    script the way a browser reports it."""
    _node()
    script = tmp_path / "drive.js"
    script.write_text("""
const fs = require("fs");
const [js, base, ownerKey] = process.argv.slice(2);
const listeners = {};
class El {
  constructor(sel, needs) { this.sel = sel; this.hidden = true; this.dataset = needs ? { needs } : {};
                            this.nodeType = 1; this.textContent = ""; this.value = ""; }
  matches(sel) { return sel === "[data-needs]" && "needs" in this.dataset; }
  querySelector() { return null; }
  addEventListener() {} click() {} reset() {}
}
const page = { "#auth-open": new El(), "#auth-status": new El(), "#auth-forget": new El(),
               "#auth-note": new El(), "#auth-error": new El(),
               "#auth-dialog": Object.assign(new El(), { close() {} }), "#auth-form": new El() };
page["#auth-form"].id = "auth-form";
page["#auth-form"].elements = { privkey: page["#auth-form"] };
const controls = [new El(null, "owner")];
let observer = null;
globalThis.MutationObserver = class { constructor(fn) { observer = fn; } observe() {} };
globalThis.window = { isSecureContext: true, crypto: globalThis.crypto };
globalThis.document = {
  documentElement: {},
  querySelector: (sel) => page[sel] ?? null,
  querySelectorAll: (sel) => (sel === "[data-needs]" ? controls : []),
  addEventListener: (name, fn) => { (listeners[name] ??= []).push(fn); },
};
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => realFetch(new URL(url, base), init);
require(js);
(async () => {
  page["#auth-form"].value = fs.readFileSync(ownerKey, "utf8");
  for (const fn of listeners.submit) fn({ target: page["#auth-form"], preventDefault() {} });
  while (page["#auth-status"].hidden) await new Promise((r) => setTimeout(r, 20));
  const late = new El(null, "owner");
  controls.push(late);
  if (observer) observer([{ addedNodes: [late] }]);
  console.log(JSON.stringify({ watching: observer !== null, shown: controls.map((c) => !c.hidden) }));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), board.url, board.owner.key.path],
                         capture_output=True, text=True, check=True, timeout=60)
    got = json.loads(run.stdout)
    assert got == {"watching": True, "shown": [True, True]}


# --- archive: the owner's control on messages and items ------------------------------------------

def test_archived_messages_and_items_have_their_own_view_and_the_controls_stay_hidden(board, team):
    a = team.alice
    team.alice.create("rev/q", "queue")
    keep, noise = a.post("rev/log", "keep this"), a.post("rev/log", "<b>noise</b>")
    item = team.bob.ask("rev/q", "a request")
    team.owner.archive(noise, "off topic")
    team.owner.archive(item)

    page = board.get("/view/rev/log").text
    assert "keep this" in page and "noise" not in page
    assert 'href="/view/rev/log?archived=1">show archived' in page
    shown = forms(page)
    assert shown["/ui/archive"] == {"view": "", "event": str(keep), "archived": "1", "reason": ""}
    assert "/ui/unarchive" not in shown
    everything = board.get("/view/rev/log?archived=1").text
    assert "&lt;b&gt;noise&lt;/b&gt;" in everything and "<b>noise" not in everything
    assert 'hide archived' in everything and visible_text(everything).count("archived") >= 2
    shown = forms(everything)
    assert shown["/ui/unarchive"] == {"view": "archived", "event": str(noise), "archived": "0", "reason": ""}
    assert shown["/ui/archive"]["event"] == str(keep)
    # the live refresh keeps the view it was loaded in
    assert "/ui/messages?path=rev%2Flog&amp;archived=1" in everything
    assert "noise" in board.get("/ui/messages?path=rev/log&archived=1").text
    assert "noise" not in board.get("/ui/messages?path=rev/log").text

    queue = board.get("/view/rev/q").text
    assert "a request" not in queue and "nothing open" in queue
    with_archived = board.get("/view/rev/q?archived=1").text
    assert "a request" in with_archived and forms(with_archived)["/ui/unarchive"]["event"] == str(item)
    assert "a request" not in board.get("/ui/inbox").text

    for path in ("/view/rev/log", "/view/rev/log?archived=1", "/view/rev/q?archived=1"):
        controls = _Controls()
        controls.feed(board.get(path).text)
        assert controls.shown == [], (path, controls.shown)  # hidden until the owner authorizes


def test_a_signed_web_archive_is_the_owners_and_answers_with_the_list(board, team):
    a = team.alice
    noise = a.post("rev/log", "noise")
    keep = a.post("rev/log", "keep")

    def send(route, key, event, archived, **extra):
        body = board.signed("archive", key, {"event": event, "archived": archived, "reason": "r",
                                             "key": new_client_key()})
        return request(f"{board.url}/ui/{route}", {**body, **extra}, form=True)

    head = board.head_id()
    r = send("archive", a.key, noise, "1")
    assert (r.status, r.headers["Board-Error"]) == (403, "forbidden") and board.head_id() == head
    unsigned = request(board.url + "/ui/archive", {"event": str(noise), "archived": "1", "reason": "",
                                                   "key": new_client_key(), "nonce": str(head)}, form=True)
    assert (unsigned.status, unsigned.headers["Board-Error"]) == (401, "unsigned")
    assert send("archive", board.owner.key, 99999, "1").status == 404

    r = send("archive", board.owner.key, noise, "1", view="")
    assert r.status == 200 and "keep" in r.text and "noise" not in r.text
    assert [m["id"] for m in board.messages("rev/log")] == [keep]
    r = send("unarchive", board.owner.key, noise, "0", view="archived")
    assert r.status == 200 and "noise" in r.text and "keep" in r.text and 'tag">archived' not in r.text
    assert [m["id"] for m in board.messages("rev/log")] == [noise, keep]
    r = send("archive", board.owner.key, noise, "1", view="archived")
    assert r.status == 200 and 'tag">archived' in r.text and "noise" in r.text
    # a form posted to the other endpoint than its field says is refused
    assert send("unarchive", board.owner.key, noise, "1").status == 400


def test_the_archive_control_is_shown_to_the_owner_key_and_no_other(board, team, tmp_path):
    """board-sign.js under node against a stand-in page: authorized with the
    owner key it shows `data-needs="owner"` controls (Archive among them),
    authorized with a trusted agent's it shows only the `trusted` ones."""
    _node()
    script = tmp_path / "drive.js"
    script.write_text("""
const fs = require("fs");
const [js, base, key] = process.argv.slice(2);
const listeners = {};
class El {
  constructor(needs) { this.hidden = true; this.dataset = needs ? { needs } : {}; this.nodeType = 1;
                       this.textContent = ""; this.value = ""; }
  matches(sel) { return sel === "[data-needs]" && "needs" in this.dataset; }
  querySelector() { return null; }
  addEventListener() {} click() {} reset() {}
}
const page = { "#auth-open": new El(), "#auth-status": new El(), "#auth-forget": new El(),
               "#auth-note": new El(), "#auth-error": new El(),
               "#auth-dialog": Object.assign(new El(), { close() {} }), "#auth-form": new El() };
page["#auth-form"].id = "auth-form";
page["#auth-form"].elements = { privkey: page["#auth-form"] };
const controls = [new El("owner"), new El("trusted")];
globalThis.MutationObserver = class { observe() {} };
globalThis.window = { isSecureContext: true, crypto: globalThis.crypto };
globalThis.document = {
  documentElement: {},
  querySelector: (sel) => page[sel] ?? null,
  querySelectorAll: (sel) => (sel === "[data-needs]" ? controls : []),
  addEventListener: (name, fn) => { (listeners[name] ??= []).push(fn); },
};
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => realFetch(new URL(url, base), init);
require(js);
(async () => {
  page["#auth-form"].value = fs.readFileSync(key, "utf8");
  for (const fn of listeners.submit) fn({ target: page["#auth-form"], preventDefault() {} });
  while (page["#auth-status"].hidden) await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify(controls.map((c) => !c.hidden)));
})();
""")
    served = _served_js(board, tmp_path)
    for key, shown in ((board.owner.key, [True, True]), (team.alice.key, [False, True])):
        run = subprocess.run(["node", script, served, board.url, key.path], capture_output=True,
                             text=True, check=True, timeout=60)
        assert json.loads(run.stdout) == shown, key
    # and the served page puts every archive form in an owner control
    team.owner.archive(team.alice.post("rev/log", "m"))
    page = board.get("/view/rev/log?archived=1").text
    assert page.count('data-needs="owner" hidden') == page.count("hx-post=\"/ui/unarchive\"") \
        + page.count("hx-post=\"/ui/archive\"") > 0
# --- resolving items from a queue page ----------------------------------------------------------

def _web(board, path, key, action, view=None, **values):
    """A signed form post to /ui/<path>, as the page sends it."""
    values.setdefault("key", new_client_key())
    body = board.signed(action, key, values)
    if view:
        body["view"] = view
    return request(f"{board.url}/ui/{path}", body, form=True)


def _stand_in_gate(board, tmp_path, key_path, controls):
    """Which of `controls` ([needs, name] pairs) board-sign.js shows once
    authorized with the key at `key_path`, run under node against a stand-in
    page."""
    _node()
    script = tmp_path / "gate.js"
    script.write_text("""
const fs = require("fs");
const [js, base, key, specs] = process.argv.slice(2);
const listeners = {};
class El {
  constructor(needs, name) { this.hidden = true; this.dataset = {}; if (needs) this.dataset.needs = needs;
                             if (name) this.dataset.name = name; this.nodeType = 1;
                             this.textContent = ""; this.value = ""; }
  matches() { return false; }
  querySelector() { return null; }
  addEventListener() {} click() {} reset() {}
}
const page = { "#auth-open": new El(), "#auth-status": new El(), "#auth-forget": new El(),
               "#auth-note": new El(), "#auth-error": new El(),
               "#auth-dialog": Object.assign(new El(), { close() {} }), "#auth-form": new El() };
page["#auth-form"].id = "auth-form";
page["#auth-form"].elements = { privkey: page["#auth-form"] };
const controls = JSON.parse(specs).map(([needs, name]) => new El(needs, name));
globalThis.MutationObserver = class { observe() {} };
globalThis.window = { isSecureContext: true, crypto: globalThis.crypto };
globalThis.document = {
  documentElement: {},
  querySelector: (sel) => page[sel] ?? null,
  querySelectorAll: (sel) => (sel === "[data-needs]" ? controls : []),
  addEventListener: (name, fn) => { (listeners[name] ??= []).push(fn); },
};
const realFetch = globalThis.fetch;
globalThis.fetch = (url, init) => realFetch(new URL(url, base), init);
require(js);
(async () => {
  page["#auth-form"].value = fs.readFileSync(key, "utf8");
  for (const fn of listeners.submit) fn({ target: page["#auth-form"], preventDefault() {} });
  while (page["#auth-status"].hidden) await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify(controls.map((c) => !c.hidden)));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), board.url, key_path,
                          json.dumps(controls)], capture_output=True, text=True, check=True,
                         timeout=60)
    return json.loads(run.stdout)


def test_a_queue_page_offers_answer_and_decline_on_each_open_item(board, team):
    team.bob.create("rev/bob", "queue")
    open_one = team.alice.ask("rev/bob", "please look")
    done = team.alice.ask("rev/bob", "already done")
    team.bob.answer(done, "ok")
    page = board.get("/view/rev/bob").text
    resolves = [f for f in re.findall(r"<form[^>]*/ui/resolve.*?</form>", page, re.S)]
    assert len(resolves) == 3 and all("hx-target=\"#queue-items\"" in f for f in resolves)
    assert all(f'name="item" value="{open_one}"' in f for f in resolves)  # none for the answered one
    assert sorted(re.findall(r'name="state" value="(\w+)"', page)) == \
        ["answered", "cancelled", "declined"]
    controls = _Controls()
    controls.feed(page)
    assert controls.shown == [], controls.shown
    # the answer is the queue's items, with the item resolved
    r = _web(board, "resolve", team.bob.key, "resolve", view="queue", item=open_one,
             state="answered", body="looked")
    assert r.status == 200 and "2 resolved items" in visible_text(r.text) and "looked" in r.text
    assert "nothing open" in r.text


def test_the_store_decides_who_resolves_from_a_queue_page(board, team):
    """Mirrors the store: the queue's owner answers; the queue's owner or
    the owner key declines; nobody else does."""
    team.bob.create("rev/bob", "queue")
    item = team.alice.ask("rev/bob", "question")

    def resolve(who, state):
        return _web(board, "resolve", who.key, "resolve", view="queue", item=item, state=state,
                    body="because")

    for who, state in ((team.owner, "answered"), (team.carol, "declined"), (team.alice, "answered")):
        r = resolve(who, state)
        assert r.status == 403 and r.headers["Board-Error"] == "forbidden", (who.name, state)
    assert board.item(item)["item"]["state"] == "open"
    assert resolve(team.owner, "declined").status == 200  # the owner key declines any item
    assert board.item(item)["item"]["state"] == "declined"
    second = team.alice.ask("rev/bob", "again")
    r = _web(board, "resolve", team.bob.key, "resolve", view="queue", item=second,
             state="declined", body="no")  # the queue's owner declines
    assert r.status == 200 and board.item(second)["item"]["state"] == "declined"


def test_the_page_shows_resolve_controls_to_the_key_the_store_lets_use_them(board, team, tmp_path):
    controls = [["named", "bob"], ["owner", "bob"], ["named", "alice"], ["owner", "alice"],
                ["owner", None], ["trusted", None]]
    assert _stand_in_gate(board, tmp_path, team.bob.key.path, controls) == \
        [True, True, False, False, False, True]
    assert _stand_in_gate(board, tmp_path, board.owner.key.path, controls) == \
        [False, True, False, True, True, True]
    assert _stand_in_gate(board, tmp_path, team.carol.key.path, controls) == \
        [False, False, False, False, False, True]


def test_the_asker_cancels_from_the_queue_page_and_nobody_else_does(board, team):
    team.bob.create("rev/bob", "queue")
    mine = team.alice.ask("rev/bob", "never mind soon")
    page = board.get("/view/rev/bob").text
    assert 'data-needs="named" data-name="alice" hidden' in page  # the cancel control: alice's
    for who in (team.bob, team.carol, board.owner):  # not the owner of the queue, nor the owner key
        r = _web(board, "resolve", who.key, "resolve", view="queue", item=mine, state="cancelled")
        assert r.status == 403 and r.headers["Board-Error"] == "forbidden", who.name
    assert board.item(mine)["item"]["state"] == "open"
    r = _web(board, "resolve", team.alice.key, "resolve", view="queue", item=mine,
             state="cancelled", body="found it")
    assert r.status == 200 and "found it" in r.text and "1 resolved item" in visible_text(r.text)
    item = board.item(mine)["item"]
    assert (item["state"], item["resolution"]["from"]) == ("cancelled", "alice")
    # a cancel needs no reason; and the owner key cancels what it asked itself
    own = board.owner.ask("rev/bob", "from stefan")
    assert 'data-name="' + board.owner.name + '"' in board.get("/view/rev/bob").text
    assert _web(board, "resolve", board.owner.key, "resolve", view="queue", item=own,
                state="cancelled").status == 200


def test_the_page_shows_cancel_to_the_asker_alone(board, team, tmp_path):
    controls = [["named", "alice"], ["named", "bob"]]
    assert _stand_in_gate(board, tmp_path, team.alice.key.path, controls) == [True, False]
    assert _stand_in_gate(board, tmp_path, board.owner.key.path, controls) == [False, False]


# --- registering from the page ------------------------------------------------------------------

def test_every_page_offers_registration_for_a_key_the_board_does_not_know(board, team):
    team.owner.create("rev/q", "queue")
    for path in ("/", "/view/rev/log", "/view/rev/q", "/ui/inbox", "/ui/keys"):
        form = forms(board.get(path).text)["/ui/register"]
        assert {"name", "repo", "note"} <= set(form), path
    parser = _Controls()
    parser.feed(board.get("/").text)
    assert parser.shown == []


def test_a_signed_web_registration_waits_for_the_owner_and_shows_the_result(board, team, keyring):
    key = keyring["webnewbie"]
    r = _web(board, "register", key, "register", name="webnewbie", note="<b>for the phone</b>",
             repo="/home/x/repo")
    assert r.status == 200 and "webnewbie" in r.text and key.fingerprint in r.text, r.text
    assert "<b>for the phone</b>" not in r.text
    info = board.key_info(key.fingerprint)
    assert (info["name"], info["state"], info["note"]) == ("webnewbie", "pending", "<b>for the phone</b>")
    [waiting] = [r for r in board.get_json("/registrations") if r["name"] == "webnewbie"]
    assert waiting["repo"] == "/home/x/repo"
    assert "webnewbie" in board.get("/ui/inbox").text
    # a refusal is plain text with its code: a name that cannot be one
    r = _web(board, "register", keyring["other"], "register", name="no spaces allowed!")
    assert r.status >= 400 and r.headers["Board-Error"] and "<" not in r.text
    # and nothing unsigned is taken
    r = request(f"{board.url}/ui/register", {"name": "x", "note": "", "repo": "", "key": new_client_key(),
                                             "nonce": str(board.head_id())}, form=True)
    assert r.status == 401


def test_the_page_registers_an_unregistered_key_and_only_that_key(board, team, keyring, tmp_path):
    controls = [["unregistered", None], ["trusted", None]]
    assert _stand_in_gate(board, tmp_path, keyring["fresh"].path, controls) == [True, False]
    assert _stand_in_gate(board, tmp_path, team.alice.key.path, controls) == [False, True]
    script = tmp_path / "register.js"
    script.write_text("""
const fs = require("fs");
const [js, mine, base] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
(async () => {
  const signer = await B.importKey(fs.readFileSync(mine, "utf8"));
  const r = await B.postSigned(signer, B.KINDS.register, base + "/ui/register",
    { name: "fresh", note: "from node", repo: "", key: crypto.randomUUID() });
  console.log(JSON.stringify({ status: r.status, text: await r.text() }));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), keyring["fresh"].path, board.url],
                         capture_output=True, text=True, check=True, timeout=60)
    got = json.loads(run.stdout)
    assert got["status"] == 200 and "fresh" in got["text"], got
    assert board.key_info(keyring["fresh"].fingerprint)["state"] == "pending"


# --- paging ------------------------------------------------------------------------------------

def _count(page):
    return len(re.findall(r'<div class="msg">', page))


def test_a_thread_page_shows_the_last_100_and_pages_older_by_links(board, team):
    ids = [team.alice.post("rev/log", f"line {n}") for n in range(130)]
    page = board.get("/view/rev/log").text
    assert _count(page) == 100 and "30 earlier messages are not shown" in visible_text(page)
    assert f"#{ids[29]} " not in page and f"#{ids[30]} " in page
    assert 'href="/view/rev/log?last=200"' in page and 'href="/view/rev/log?all=1"' in page
    older = board.get("/view/rev/log?last=200").text
    assert _count(older) == 130 and "earlier message" not in older  # nothing left: no banner
    assert _count(board.get("/view/rev/log?last=110").text) == 110
    assert "newest only" in board.get("/view/rev/log?last=110").text
    assert _count(board.get("/view/rev/log?all=1").text) == 130
    after = board.get(f"/view/rev/log?since={ids[119]}").text
    assert _count(after) == 10 and "120 earlier messages" in visible_text(after)
    assert board.get("/view/rev/log?last=5&all=1").status == 400
    # the refresh keeps the window the page is on, and so does a post
    assert 'hx-get="/ui/messages?path=rev%2Flog&amp;last=100"' in page
    assert _count(board.get("/ui/messages?path=rev/log&last=120").text) == 120
    assert _count(board.get("/ui/messages?path=rev/log").text) == 100
    form = forms(board.get("/view/rev/log?last=120").text)["/ui/post"]
    assert form["last"] == "120" and forms(board.get("/view/rev/log?all=1").text)["/ui/post"]["all"] == "1"
    body = board.signed("post", team.alice.key, {"path": "rev/log", "body": "one more", "key": new_client_key()})
    body["last"] = "120"
    r = request(f"{board.url}/ui/post", body, form=True)
    assert r.status == 200 and _count(r.text) == 120 and "one more" in r.text


BIGGER = "x" * (700 * 1024)


def test_a_queue_page_and_the_inbox_page_on_when_the_board_cuts_the_read(board, team):
    team.owner.create("rev/q", "queue", owner="alice")
    asked = [team.bob.ask("rev/q", BIGGER) for _ in range(13)]
    page = board.get("/view/rev/q").text
    assert "The board cuts a read" in visible_text(page)
    [first_after] = re.findall(r'href="/view/rev/q\?after=(\d+)"', page)
    assert int(first_after) in asked
    seen, after = [], 0
    while True:  # following the links reaches every item once
        page = board.get(f"/view/rev/q?after={after}").text
        seen += [int(i) for i in re.findall(r"<div class=\"meta\">#(\d+) ·", page)]
        link = re.findall(r'href="/view/rev/q\?after=(\d+)"', page)
        if not link:
            break
        after = int(link[0])
    assert seen == asked
    # the inbox lists the owner's open items: alice is not the owner key
    team.owner.create("rev/stefan", "queue")
    [team.bob.ask("rev/stefan", BIGGER) for _ in range(13)]
    inbox = board.get("/ui/inbox").text
    assert "more wait" in visible_text(inbox) and "/ui/inbox?after=" in inbox
    assert board.get("/ui/inbox?after=999999999").status == 200


# --- errors show the board's code --------------------------------------------------------------

def test_a_refusal_shows_the_board_code_with_the_message(board, team, tmp_path):
    """A refused form's message comes with its code in Board-Error; the page
    shows `code: message`, as the CLI does. Run under node against a real
    refusal."""
    _node()
    team.bob.create("rev/bob", "queue")
    item = team.alice.ask("rev/bob", "q")
    script = tmp_path / "refusal.js"
    script.write_text("""
const fs = require("fs");
const [js, mine, base, item] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
(async () => {
  const signer = await B.importKey(fs.readFileSync(mine, "utf8"));
  const r = await B.postSigned(signer, B.KINDS.resolve, base + "/ui/resolve",
    { item, state: "answered", body: "not mine to answer", ref: "", key: crypto.randomUUID() });
  const text = await r.text();
  console.log(JSON.stringify({ status: r.status, text, shown: B.refusalText(r, text),
                               bare: B.refusalText({ headers: new Headers() }, "plain") }));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), team.carol.key.path, board.url,
                          str(item)], capture_output=True, text=True, check=True, timeout=60)
    got = json.loads(run.stdout)
    assert got["status"] == 403 and got["text"] and got["shown"] == f"forbidden: {got['text']}"
    assert got["bare"] == "plain"


# --- retries that cannot double-post -------------------------------------------------------------

def test_a_lost_answer_is_resent_as_the_same_write_and_lands_once(board, team, tmp_path):
    """The page sends a write again when no answer comes, as `board` does,
    under the one client key it signed first: a write that had landed is
    answered again, not made twice. And a form sent once more after a lost
    answer keeps its key, until its content changes or it lands."""
    _node()
    script = tmp_path / "lost.js"
    script.write_text("""
const fs = require("fs");
const [js, mine, base] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
B.RETRY.transportWaitsMs = [5, 5, 5];
const realFetch = globalThis.fetch;
let posts = 0, mode = "ok";  // "lose": the board gets it, the answer never arrives; "down": it never gets there
globalThis.fetch = async (url, init) => {
  const isPost = init?.method === "POST";
  if (isPost) posts++;
  if (isPost && mode === "down") throw new TypeError("fetch failed");
  const response = await realFetch(url, init);
  if (isPost && mode === "lose") { mode = "ok"; throw new TypeError("fetch failed"); }
  return response;
};
(async () => {
  const signer = await B.importKey(fs.readFileSync(mine, "utf8"));
  const out = {};
  mode = "lose"; posts = 0;
  const r = await B.postSigned(signer, B.KINDS.post, base + "/ui/post",
    { path: "rev/log", body: "once", key: crypto.randomUUID() });
  out.lost = { status: r.status, posts, text: await r.text() };
  mode = "down"; posts = 0;
  try {
    await B.postSigned(signer, B.KINDS.post, base + "/ui/post", { path: "rev/log", body: "never", key: crypto.randomUUID() });
    out.down = "resolved";
  } catch (e) { out.down = { error: e.message, posts }; }
  const form = {};
  const a = B.clientKey(form, { body: "x" }), again = B.clientKey(form, { body: "x" });
  const edited = B.clientKey(form, { body: "y" });
  B.keyUsed(form);
  out.keys = [a === again, a === edited, B.clientKey(form, { body: "y" }) === edited];
  console.log(JSON.stringify(out));
})();
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path), team.alice.key.path, board.url],
                         capture_output=True, text=True, check=True, timeout=60)
    got = json.loads(run.stdout)
    assert got["lost"]["status"] == 200 and got["lost"]["posts"] == 2 and "once" in got["lost"]["text"]
    assert got["down"] == {"error": "fetch failed", "posts": 4}  # the first try and three resends
    assert [m["body"] for m in board.messages("rev/log")] == ["once"]  # once, and never the other
    assert got["keys"] == [True, False, False]


# --- live refresh ------------------------------------------------------------------------------

def test_queue_inbox_and_keys_pages_refresh_themselves_from_fragment_routes(board, team):
    team.owner.create("rev/q", "queue")
    team.owner.create("rev/stefan", "queue")
    pages = {"/view/rev/q": "/ui/live/queue?path=rev%2Fq&amp;after=0",
             "/ui/inbox": "/ui/live/inbox?after=0", "/ui/keys": "/ui/live/keys"}
    for path, source in pages.items():
        page = board.get(path).text
        assert f'data-live hx-get="{source}" hx-trigger="every 5s"' in page, path
        assert "[" not in re.search(r'hx-trigger="every[^"]*"', page).group(0)  # no filter: htmx evals them
    team.bob.ask("rev/q", "fresh item")
    team.bob.ask("rev/stefan", "for stefan")
    queue = board.get("/ui/live/queue?path=rev/q")
    assert queue.status == 200 and "fresh item" in queue.text and "<html" not in queue.text
    inbox = board.get("/ui/live/inbox")
    assert "for stefan" in inbox.text and "<html" not in inbox.text
    keys = board.get("/ui/live/keys")
    assert '<table class="keys">' in keys.text and "<html" not in keys.text
    # a failing refresh is a fragment too, not a page swapped into the page
    missing = board.get("/ui/live/queue?path=rev/nope")
    assert missing.status == 404 and "<html" not in missing.text and 'class="error"' in missing.text
    assert missing.headers["Board-Error"] == "not-found"


def test_a_live_piece_holds_off_while_something_is_being_typed(board, tmp_path):
    _node()
    script = tmp_path / "busy.js"
    script.write_text("""
require(process.argv[2]);
const busy = globalThis.BoardSign.liveBusy;
const field = (value, defaultValue = "") => ({ value, defaultValue });
const live = ({ fields = [], open = false, error = false, focused = false } = {}) => ({
  contains: () => focused,
  querySelectorAll: () => fields,
  querySelector: (sel) => (sel.startsWith("details") && (open || error) ? {} : null),
});
const typing = { matches: () => true }, link = { matches: () => false };
console.log(JSON.stringify([
  busy(live(), link),                                          // nothing going on
  busy(live({ fields: [field("")] }), link),                   // untouched fields
  busy(live({ fields: [field("half an answer")] }), link),     // text typed in
  busy(live({ fields: [field("x", "x")] }), link),             // a field that came with its text
  busy(live({ focused: true }), typing),                       // focus in a field
  busy(live({ focused: true }), link),                         // focus on a link
  busy(live({ open: true }), link),                            // a fold opened
  busy(live({ error: true }), link),                           // a refusal shown
]));
""")
    run = subprocess.run(["node", script, _served_js(board, tmp_path)], capture_output=True, text=True,
                         check=True, timeout=60)
    assert json.loads(run.stdout) == [False, False, True, False, True, False, True, True]


# --- signing needs a secure context ------------------------------------------------------------

def test_an_insecure_page_says_why_it_cannot_sign_and_what_to_do(board, tmp_path):
    """Over http://sz1:8777 the browser withholds WebCrypto's signing. The
    Authorize dialog says so, with the page's own address and the ways out,
    and takes no key; a secure page has none of that."""
    _node()
    script = tmp_path / "insecure.js"
    script.write_text("""
const [js, secure] = process.argv.slice(2);
class El {
  constructor() { this.hidden = true; this.disabled = false; this.dataset = {}; this.nodeType = 1;
                  this.textContent = ""; this.value = ""; }
  matches() { return false; }
  querySelector() { return null; }
  addEventListener() {}
}
const page = Object.fromEntries(["#auth-open", "#auth-status", "#auth-forget", "#auth-note", "#auth-error",
  "#auth-dialog", "#auth-form", "#auth-insecure", "#auth-origin", "#auth-submit"].map((s) => [s, new El()]));
page["#auth-form"].elements = { privkey: new El() };
globalThis.location = { origin: "http://sz1:8777" };
globalThis.MutationObserver = class { observe() {} };
globalThis.window = { isSecureContext: secure === "1", crypto: globalThis.crypto };
globalThis.document = {
  documentElement: {}, hidden: false,
  querySelector: (sel) => page[sel] ?? null,
  querySelectorAll: () => [],
  addEventListener() {},
};
require(js);
console.log(JSON.stringify({
  opens: !page["#auth-open"].disabled, explained: !page["#auth-insecure"].hidden,
  origin: page["#auth-origin"].textContent, note: page["#auth-note"].textContent,
  keyTaken: !page["#auth-form"].elements.privkey.disabled, submit: !page["#auth-submit"].disabled,
}));
""")
    served = _served_js(board, tmp_path)

    def run(secure):
        out = subprocess.run(["node", script, served, secure], capture_output=True, text=True,
                             check=True, timeout=60)
        return json.loads(out.stdout)

    assert run("0") == {"opens": True, "explained": True, "origin": "http://sz1:8777",
                        "note": "signing needs HTTPS or localhost", "keyTaken": False, "submit": False}
    secure = run("1")
    assert (secure["explained"], secure["keyTaken"], secure["submit"], secure["note"]) == \
        (False, True, True, "")
    page = board.get("/").text
    assert "tailscale serve --bg 8777" in page and "ssh -L 8777:127.0.0.1:8777" in page
