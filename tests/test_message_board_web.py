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
    a.set_note("rev/state", "<script>alert(1)</script>\nline two", base_version=1, through=m1)
    b.create("rev/bob", "queue")
    a.ask("rev/bob", "<img src=x onerror=alert(2)> please")

    note = board.get("/view/rev/state").text
    assert "<script>alert(1)" not in note and "&lt;script&gt;alert(1)&lt;/script&gt;" in note
    assert f"summarized through #{m1}, 2 behind" in visible_text(note)
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


class _Controls(HTMLParser):
    """The write controls of a page, and whether each is shown before any
    key is authorized: hidden itself, inside a hidden element, or inside a
    dialog that is not open."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
            "track", "wbr"}
    WORDS = re.compile(r"approve|decline|revoke|answer|cancel", re.I)

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
          "line one\nfrom=evil ü🙂", "base_version": "3", "through": "10020", "item": "10022",
          "state": "answered", "ref": "", "name": "newbie", "note": "repo, purpose",
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
    human's to resolve); and give up after five."""
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
    [1, note, "/ui/note", { path: "rev/state", body: "web text", base_version: "1", through: "",
                            key: crypto.randomUUID() }],
    [0, note, "/ui/note", { path: "rev/state", body: "stale edit", base_version: "1", through: "",
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
    assert busy["log"] == ["/healthz", "/ui/post"] * 5
    assert (busy["status"], busy["refusal"]) == (409, "stale-nonce")
    mine = [m for m in board.messages("rev/log", all=1) if m["from"] == "alice"]
    assert [(m["body"], m["signed_by"]) for m in mine] == [("from the page", team.alice.fingerprint)]
    note = board.note("rev/state")
    assert (note["version"], note["body"], note["from"]) == (2, "web text", "alice")
