"""@mentions in the web view: the names the page offers, marking a known
name in a body, and the textarea's completion and warning, whose logic
board-sign.js exports and which runs here under node (with a stand-in for
the few parts of the DOM it touches: there is no browser in the suite).
See board_harness."""

import json
import re
import shutil

import pytest

from board_harness import OWNER, board, isolated, keyring, team  # noqa: F401 (fixtures)
from proc_lifetime import run

pytestmark = pytest.mark.usefixtures("isolated")


def _node():
    if shutil.which("node") is None:
        pytest.skip("node is not installed (it is in the repo's shell.nix)")


def _js(board, tmp_path):
    r = board.get("/static/board-sign.js")
    assert r.status == 200
    path = tmp_path / "board-sign.js"
    path.write_text(r.text)
    return path


def node(tmp_path, source, *args):
    script = tmp_path / "drive.js"
    script.write_text(source)
    done = run(["node", script, *map(str, args)], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(done.stdout)


def marked(page):
    return re.findall(r'<span class="at">(@[^<]*)</span>', page)


# --- the names, and a known one marked in a body ------------------------------------------------

def test_the_board_lists_the_names_an_at_can_reach_and_nobody_else(board, team, keyring):
    board.register(keyring["dave"], "pending-dave")
    names = {k["name"] for k in board.get("/keys").json() if k["state"] in ("owner", "trusted")}
    assert names == {OWNER, "alice", "bob", "carol"}
    # what the page marks is exactly that
    team.alice.post("rev/log", "@alice @bob @carol @pending-dave @nobody")
    page = board.get("/view/rev/log").text
    assert marked(page) == ["@alice", "@bob", "@carol"]


def test_a_known_name_is_marked_in_messages_notes_and_items_and_all_text_stays_escaped(board, team):
    team.alice.create("rev/q", "queue")
    team.alice.create("rev/state", "note", body="@bob owns <b>this</b>")
    team.alice.post("rev/log", "hi @bob, not @bobby or @Bob or @bob-2; mail x@carol <i>")
    team.alice.ask("rev/q", "@carol please, @nobody")
    thread = board.get("/view/rev/log").text
    assert marked(thread) == ["@bob", "@carol"]  # x@carol: the board counts it as a mention too
    assert "@bobby" in thread and "&lt;i&gt;" in thread and "<i>" not in thread
    assert marked(board.get("/view/rev/q").text) == ["@carol"]
    note = board.get("/view/rev/state").text
    assert marked(note) == ["@bob"] and "&lt;b&gt;this&lt;/b&gt;" in note


def test_an_unknown_name_is_accepted_by_the_board_and_recorded(board, team):
    """The store takes any @name, as the CLI does: it only matters to a watch for that name."""
    mid = team.alice.post("rev/log", "ping @nobody-at-all")
    assert board.sql("SELECT name FROM mentions WHERE event=?", mid) == [("nobody-at-all",)]


# --- board-sign.js: the logic ---------------------------------------------------------------------

LOGIC = """
const [js] = process.argv.slice(2);
require(js);
const B = globalThis.BoardSign;
const out = {};
out.scan = ["hi @al, @bob_2! mail a@b @ @@c é@d @" + "x".repeat(65) + " @" + "y".repeat(64).slice(0, 3),
            "", "none"].map((t) => B.mentionScan(t).map((m) => [m.name, t.slice(m.start, m.end)]));
out.names = B.mentionNames([{name: "b", state: "trusted"}, {name: "a", state: "owner"}, {name: "p", state: "pending"},
                            {name: "r", state: "revoked"}, {name: "b", state: "trusted"}]);
out.unknown = [B.mentionUnknown("@a @zed @zed @b @Zed", ["a", "b"]),
               B.mentionUnknown("@a @ze", ["a"], 6), B.mentionUnknown("@a @ze", ["a"], 3), B.mentionUnknown("none", [])];
out.query = [["hi @al", 6], ["@", 1], ["hi @", 4], ["mail a@b", 8], ["hi al", 5], ["@al x", 3], ["x @al-b_9", 9], ["@@a", 3]]
  .map(([t, c]) => B.mentionQuery(t, c));
out.matches = [B.mentionMatches(["alice", "bob", "malice", "Albert"], "al"), B.mentionMatches(["a", "b"], ""),
               B.mentionMatches(["alice"], "x"), B.mentionMatches(Array.from("abcdefghijkl"), "")];
out.insert = [B.mentionInsert("hi @al there", 3, 6, "alice"), B.mentionInsert("@", 0, 1, "bob"),
              B.mentionInsert("x @alXY z", 2, 5, "alice")];
console.log(JSON.stringify(out));
"""


def test_the_scanning_completion_and_warning_logic(board, tmp_path):
    _node()
    got = node(tmp_path, LOGIC, _js(board, tmp_path))
    assert got["scan"] == [
        [["al", "@al"], ["bob_2", "@bob_2"], ["b", "@b"], ["c", "@c"], ["d", "@d"], ["yyy", "@yyy"]], [], []]
    assert got["names"] == ["a", "b"]
    assert got["unknown"] == [["zed", "Zed"], [], ["ze"], []]
    assert got["query"] == [
        {"start": 3, "query": "al"}, {"start": 0, "query": ""}, {"start": 3, "query": ""}, None, None,
        {"start": 0, "query": "al"}, {"start": 2, "query": "al-b_9"}, {"start": 1, "query": "a"}]
    assert got["matches"] == [["alice", "Albert", "malice"], ["a", "b"], [], list("abcdefgh")]
    assert got["insert"] == [
        {"text": "hi @alice there", "caret": 10}, {"text": "@bob ", "caret": 5}, {"text": "x @alice z", "caret": 9}]


def test_the_logic_is_the_boards_rule(board, team, tmp_path):
    """Every body the script scans gives the names the board recorded for it."""
    _node()
    bodies = ["hi @carol, look", "@carolyn x", "@Carol", "é@carolé a@b@c @ @@x", "ask @ghidra-agent then @ghidra.",
              "@" + "x" * 64, "@" + "x" * 65, "tail @bob_2"]
    ids = [team.alice.post("rev/log", b) for b in bodies]
    recorded = [sorted(n for (n,) in board.sql("SELECT name FROM mentions WHERE event=?", i)) for i in ids]
    got = node(tmp_path, """
const [js, bodies] = process.argv.slice(2);
require(js);
console.log(JSON.stringify(JSON.parse(bodies).map((t) => Array.from(new Set(globalThis.BoardSign.mentionScan(t).map((m) => m.name))).sort())));
""", _js(board, tmp_path), json.dumps(bodies))
    assert got == recorded


# --- board-sign.js: the textarea, against a stand-in DOM -------------------------------------------

STAND_IN = r"""
const [js, base] = process.argv.slice(2);
const listeners = {};
const fire = (type, init) => { const e = { type, preventDefault() { this.prevented = true; }, ...init };
                               for (const h of listeners[type] ?? []) h(e); return e; };
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.kids = []; this.parent = null; this.dataset = {}; this.style = {};
                     this.value = ""; this.selectionStart = 0; this.className = ""; this.id = ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === document.body; }
  append(...k) { for (const x of k) { x.parent = this; this.kids.push(x); } }
  replaceChildren(...k) { this.kids = []; this.append(...k); }
  remove() { if (this.parent) this.parent.kids = this.parent.kids.filter((k) => k !== this); this.parent = null; }
  after(x) { x.remove(); x.parent = this.parent; this.parent.kids.splice(this.parent.kids.indexOf(this) + 1, 0, x); }
  get nextElementSibling() { return this.parent?.kids[this.parent.kids.indexOf(this) + 1] ?? null; }
  matches(sel) {
    if (sel === "textarea[name=body]") return this.tag === "textarea" && this.attrs.name === "body";
    if (sel === ".mention-warn") return this.className === "mention-warn";
    return false;
  }
  closest(sel) { return sel === "#mention-list li" && this.tag === "li" && this.parent?.id === "mention-list" ? this : null; }
  getBoundingClientRect() { return { left: 10, bottom: 20 }; }
  setSelectionRange(a) { this.selectionStart = a; }
  dispatchEvent(e) { fire(e.type, { target: this }); }
  reset() { this.value = ""; fire("reset", { target: this }); }
  querySelector(sel) { return sel === "textarea[name=body]" ? this.kids.find((k) => k.tag === "textarea") ?? null : null; }
  set textContent(t) { this._text = t; }
  get textContent() { return this._text ?? ""; }
}
const document = {
  addEventListener: (t, h) => (listeners[t] ??= []).push(h),
  createElement: (t) => new El(t), body: new El("body"), documentElement: {}, activeElement: null, hidden: false,
  querySelector: () => null, querySelectorAll: () => [],
};
globalThis.document = document;
globalThis.window = { isSecureContext: true, crypto: globalThis.crypto };
globalThis.location = { origin: base };
globalThis.MutationObserver = class { observe() {} };
const realFetch = globalThis.fetch;
let fetches = 0;
globalThis.fetch = (url, init) => { fetches++; return realFetch(new URL(url, base), init); };
require(js);

const form = new El("form"); form.setAttribute("hx-post", "/ui/post");
const box = new El("textarea"); box.setAttribute("name", "body"); box.form = form;
form.append(box); document.body.append(form);
const other = new El("textarea"); other.setAttribute("name", "note"); other.form = form;  // not a body
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const type = async (text) => { box.value = text; box.selectionStart = text.length; document.activeElement = box;
                               fire("input", { target: box }); await sleep(150); };
const list = () => document.body.kids.find((k) => k.id === "mention-list");
const shown = () => list()?.kids.map((li) => li.textContent) ?? [];
const warning = () => box.nextElementSibling?.className === "mention-warn" ? box.nextElementSibling.textContent : null;
const key = (k) => fire("keydown", { target: box, key: k });
const out = {};
(async () => {
  await type("hi @");
  out.all = shown();
  out.roles = [list().attrs.role, list().kids[0].attrs.role, list().kids[0].attrs["aria-selected"],
               box.attrs["aria-expanded"], box.attrs["aria-activedescendant"], box.attrs.role];
  await type("hi @a");
  out.filtered = shown();
  out.warnWhileTyping = warning();
  await type("hi @al ");              // a space ends the token: closed, and a name nobody has is flagged
  out.afterSpace = [list() === undefined, warning()];
  out.spaceEnter = [key("Enter").prevented, key(" ").prevented];  // nothing intercepted with no list
  await type("x @");
  const down = key("ArrowDown");
  out.down = [down.prevented, box.attrs["aria-activedescendant"], list().kids.map((li) => li.attrs["aria-selected"])];
  key("ArrowDown"); key("ArrowUp"); key("ArrowUp");
  out.wrapped = box.attrs["aria-activedescendant"];
  key("Escape");
  out.escaped = [list() === undefined, box.attrs["aria-expanded"], box.value];
  await type("x @b");
  const tab = key("Tab");
  out.tab = [tab.prevented, box.value, box.selectionStart, list() === undefined, warning()];
  await type("y @");
  fire("mousedown", { target: list().kids[0], preventDefault() { this.prevented = true; } });
  out.click = [box.value, list() === undefined];
  await type("@nobody and @alice");
  out.warn = warning();
  box.value = "sent"; fire("focusout", { target: box });
  await type("@");
  fire("focusout", { target: box }); document.activeElement = null; await sleep(150);
  out.blurClosed = list() === undefined;
  form.reset(); await sleep(50);
  out.reset = [warning(), list() === undefined];
  // a box that is no body of a signed form is left alone
  fire("input", { target: other }); await sleep(50);
  const note = new El("textarea"); note.setAttribute("name", "body"); const f2 = new El("form");
  f2.setAttribute("hx-post", "/elsewhere"); note.form = f2; f2.append(note); document.body.append(f2);
  note.value = "@"; note.selectionStart = 1; document.activeElement = note;
  fire("input", { target: note }); await sleep(50);
  out.untouched = [list() === undefined, note.nextElementSibling];
  console.log(JSON.stringify(out));
})();
"""


def test_the_textarea_completes_and_warns_without_getting_in_the_way(board, team, tmp_path):
    _node()
    got = node(tmp_path, STAND_IN, _js(board, tmp_path), board.url)
    names = sorted(["alice", "bob", "carol", OWNER])
    assert got["all"] == [f"@{n}" for n in names]
    assert got["roles"] == ["listbox", "option", "true", "true", "mention-0", "combobox"]
    assert got["filtered"] == ["@alice", f"@{OWNER}", "@carol"]  # starting with it first, then holding it
    assert got["warnWhileTyping"] is None
    assert got["afterSpace"] == [True, "no one is called @al: the board will still take it"]
    assert got["spaceEnter"] == [None, None]
    assert got["down"][:2] == [True, "mention-1"] and got["down"][2][:2] == ["false", "true"]
    assert got["wrapped"] == "mention-0"  # down, down, up, up from the second entry
    assert got["escaped"] == [True, "false", "x @"]
    assert got["tab"][0] is True and got["tab"][1] == "x @bob " and got["tab"][2:] == [7, True, None]
    assert got["click"] == [f"y @{names[0]} ", True]
    assert got["warn"] == "no one is called @nobody: the board will still take it"
    assert got["blurClosed"] is True and got["reset"] == [None, True]
    assert got["untouched"] == [True, None]
