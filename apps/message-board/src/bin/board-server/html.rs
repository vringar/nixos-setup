//! The web view's HTML, escaped by construction.
//!
//! [`Html`] can only be built from string literals (markup written here)
//! and from text, which is always escaped, so nothing a message, note,
//! name, path or registration brings in can become markup. htmx and
//! board-sign.js depend on the pages' structure: the element ids, the
//! classes, the `data-needs` attributes and the hx- attributes.
//!
//! Every write control sits in an element with `data-needs="trusted"` or
//! `data-needs="owner"`, served `hidden`: board-sign.js shows it once the
//! key authorized in the page is one the board trusts (or is the owner's).
//! The board checks every write anyway; hiding is for the reader.

use message_board::store::{
    Child, Item, ItemPage, ItemState, Key, KeyState, Node, NodeKind, NoteVersion, READ_BYTES_MAX, READ_ROWS_MAX, Range,
    Registration, Status, ThreadPage, Version,
};

/// A value as it goes into a URL query: form-urlencoded.
fn encoded(value: &str) -> String {
    form_urlencoded::byte_serialize(value.as_bytes()).collect()
}

/// HTML: literal markup and escaped text.
#[derive(Debug, Default)]
pub struct Html(String);

impl Html {
    pub fn new() -> Self {
        Self::default()
    }

    /// Markup written in this program. Only `&'static str` is accepted,
    /// which in practice means string literals: no runtime value is ever
    /// markup.
    pub fn markup(&mut self, markup: &'static str) -> &mut Self {
        self.0.push_str(markup);
        self
    }

    /// Text, escaped for an element's content or an attribute value.
    pub fn text(&mut self, text: &str) -> &mut Self {
        for c in text.chars() {
            match c {
                '&' => self.0.push_str("&amp;"),
                '<' => self.0.push_str("&lt;"),
                '>' => self.0.push_str("&gt;"),
                '"' => self.0.push_str("&#34;"),
                '\'' => self.0.push_str("&#39;"),
                _ => self.0.push(c),
            }
        }
        self
    }

    /// A number, as text.
    pub fn number(&mut self, n: i64) -> &mut Self {
        self.text(&n.to_string())
    }

    /// Another piece of HTML, as it is.
    pub fn html(&mut self, other: &Html) -> &mut Self {
        self.0.push_str(&other.0);
        self
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.0.into_bytes()
    }
}

const HEAD: &str = r##"</title>
<script src="/static/htmx.min.js"></script>
<script src="/static/board-sign.js" defer></script>
<style>
  :root { color-scheme: light dark;
    --bg: #fff; --fg: #1a1a1a; --muted: #666; --faint: #999; --head: #555;
    --border: #ddd; --rule: #eee; --rule-soft: #f0f0f0; --hover: #f2f2f2; --side-fg: #333; --marker: #777;
    --note-bg: #f7f7f7; --note-border: #ddd;
    --accent: #0b3d91; --accent-bg: #eef3fc;
    --error: #a00; --flag: #8a5a00;
    --field-bg: #fff; --field-fg: #1a1a1a; --field-border: #767676; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #16181d; --fg: #e4e6eb; --muted: #a0a6b0; --faint: #8b919b; --head: #b4bac4;
      --border: #3a3f48; --rule: #2a2e36; --rule-soft: #262a31; --hover: #252932; --side-fg: #d0d4db; --marker: #9aa0aa;
      --note-bg: #1e2128; --note-border: #3a3f48;
      --accent: #8ab4f8; --accent-bg: #1f2b44;
      --error: #ff8a80; --flag: #e0b252;
      --field-bg: #1e2128; --field-fg: #e4e6eb; --field-border: #5a606b; }
    a:link { color: #8ab4f8; }
    a:visited { color: #c6a1f0; }
    input, textarea, button, dialog { background: var(--field-bg); color: var(--field-fg); }
    input, textarea, button, dialog { border: 1px solid var(--field-border); border-radius: 3px; }
    dialog::backdrop { background: rgba(0, 0, 0, .5); }
  }
  body { background: var(--bg); }
  body { font: 14px system-ui, sans-serif; max-width: 960px; margin: 1.5rem auto;
         padding: 0 1rem; color: var(--fg); }
  nav a { margin-right: .6rem; }
  .crumbs { color: var(--muted); margin: .3rem 0 .8rem; }
  table { border-collapse: collapse; width: 100%; margin: .6rem 0; }
  th, td { text-align: left; vertical-align: top; padding: .3rem .5rem; border-bottom: 1px solid var(--rule); }
  th { font-weight: 600; color: var(--head); font-size: 12px; }
  code { font: 12px ui-monospace, monospace; word-break: break-all; }
  .meta { color: var(--muted); font-size: 12px; }
  .body { white-space: pre-wrap; margin-top: .15rem; }
  .empty { color: var(--faint); }
  .error { color: var(--error); margin: 0 0 .4rem; }
  .flag { color: var(--flag); margin: .2rem 0; }
  .revoked { color: var(--error); }
  #messages { border: 1px solid var(--border); border-radius: 6px; padding: .5rem;
              margin: .8rem 0; max-height: 60vh; overflow-y: auto; }
  .msg, .item { padding: .4rem 0; border-bottom: 1px solid var(--rule-soft); }
  .msg:last-child, .item:last-child { border-bottom: 0; }
  .note, .card { border: 1px solid var(--note-border); background: var(--note-bg); border-radius: 6px;
                 padding: .5rem .7rem; margin: .6rem 0; }
  .card { border-color: var(--border); background: none; }
  .resolution { margin: .3rem 0 0 1rem; }
  .archived { opacity: .65; }
  .archived .tag { border: 1px solid var(--border); border-radius: 3px; padding: 0 .3rem; margin-left: .3rem; }
  .controls form { display: inline-block; margin: .3rem .6rem .3rem 0; vertical-align: top; }
  form input, form textarea { box-sizing: border-box; margin: .2rem 0; }
  form textarea { width: 100%; min-height: 5rem; }
  #note textarea { min-height: 14rem; }
  .auth { float: right; font-size: 12px; color: var(--muted); }
  dialog { width: min(36rem, 90vw); }
  dialog textarea { width: 100%; min-height: 10rem; font: 11px monospace; }
  .side { display: none; }
  .side ul { list-style: none; margin: 0; padding: 0; }
  .side ul ul { margin-left: .5rem; padding-left: .5rem; border-left: 1px solid var(--border); }
  .side li a { display: block; padding: .1rem .4rem; color: var(--side-fg); text-decoration: none;
               overflow-wrap: anywhere; border-left: 3px solid transparent; margin-left: -3px; }
  .side li a:hover { background: var(--hover); }
  .side summary { cursor: pointer; display: flex; align-items: baseline; }
  .side summary a { flex: 1; min-width: 0; }
  .side summary::before { content: "\25B8"; width: 1em; flex: none; color: var(--marker); }
  .side details[open] > summary::before { content: "\25BE"; }
  .side li:not(.tree-container) > a { padding-left: calc(.4rem + 1em); }
  .side .ico { display: inline-block; width: 1.5em; font-size: 12px; }
  .side li.tree-container > details > summary a { font-weight: 600; }
  .side li.tree-above > details > summary a { color: var(--accent); }
  .side li.tree-on > a, .side li.tree-on > details > summary a { border-left-color: var(--accent);
      background: var(--accent-bg); color: var(--accent); font-weight: 700; }
  .side .more { color: var(--faint); padding: .1rem .4rem; }
  @media (min-width: 960px) {
    body { max-width: none; margin: 1.5rem 1.5rem; }
    .shell { display: grid; grid-template-columns: 15rem minmax(0, 1fr); gap: 1.5rem; align-items: start; }
    .side { display: block; position: sticky; top: 1rem; max-height: calc(100vh - 2rem);
            overflow-y: auto; border-right: 1px solid var(--rule); padding-right: .5rem; }
    .shell > main { max-width: 960px; }
  }
</style></head><body hx-boost:inherited="true">
<div class="auth"><span id="auth-status" hidden></span>
  <button type="button" id="auth-forget" hidden>forget key</button>
  <button type="button" id="auth-open">Authorize</button>
  <span id="auth-note"></span></div>
<dialog id="auth-dialog"><form method="dialog" id="auth-form">
  <p>Sign as one of the board's keys: paste an <b>unencrypted</b> OpenSSH ed25519
  private key, ideally a dedicated one (<code>board keygen</code>). The board knows
  the key's name; the key stays in this tab's memory only: never stored, never
  sent, and a reload forgets it.</p>
  <p class="error" id="auth-insecure" hidden>Signing needs a secure context (HTTPS or
  localhost), and this page is at <code id="auth-origin"></code>, which is neither: the browser
  withholds the signing API here. Open the board as <code>http://localhost:8777</code>
  (on the host itself, or from another machine through <code>ssh -L 8777:127.0.0.1:8777 sz1</code>),
  or serve it over HTTPS with <code>tailscale serve --bg 8777</code>.</p>
  <p class="error" id="auth-error" hidden></p>
  <textarea name="privkey" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" required
            autocomplete="off" spellcheck="false"></textarea>
  <button type="submit" id="auth-submit">authorize</button>
  <button type="button" id="auth-cancel">cancel</button>
</form></dialog>
<h1>message board</h1>
<nav><a href="/">tree</a> <a href="/ui/inbox">inbox</a> <a href="/ui/keys">keys</a></nav>
<details class="register" data-needs="unregistered" hidden><summary>this key is not registered: register it</summary>
<form hx-post="/ui/register" hx-target="#register-result" hx-swap="innerHTML">
<p class="error" hidden></p>
<input name="name" placeholder="name" required>
<input name="repo" placeholder="repo (optional)">
<textarea name="note" placeholder="what the key is for"></textarea>
<button type="submit">register</button>
</form>
<div id="register-result"></div></details>
"##;

/// A whole page: the layout around `content`, with a breadcrumb for the
/// node at `path`, if it is one.
/// How many nodes the sidebar lists; the rest are one click away in the tree.
pub const OUTLINE_MAX: usize = 300;

/// The glyph the sidebar puts before a leaf of each kind; containers have
/// the fold marker instead. Decorative only.
const LEAF_GLYPHS: [(NodeKind, &str); 3] =
    [(NodeKind::Queue, "\u{1F4E5}"), (NodeKind::Note, "\u{1F4C4}"), (NodeKind::Thread, "\u{270F}\u{FE0F}")];

/// The tree as the sidebar shows it: nested lists, with every container a
/// native `<details>` that is open when it holds the page at `current` (or is
/// it) and closed otherwise. `outline` is in tree order, so a container's
/// children follow it. Wide screens show the sidebar; narrow ones hide it,
/// and the "tree" link remains.
fn sidebar(h: &mut Html, outline: &[(String, NodeKind)], current: Option<&str>) {
    if outline.is_empty() {
        return;
    }
    h.markup("<aside class=\"side\"><nav aria-label=\"tree\"><ul>\n");
    let mut open: Vec<&str> = Vec::new();
    for (path, kind) in outline {
        while open.last().is_some_and(|top| !path.strip_prefix(top).is_some_and(|rest| rest.starts_with('/'))) {
            open.pop();
            h.markup("</ul></details></li>\n");
        }
        let name = path.rsplit('/').next().unwrap_or(path);
        let on = current == Some(path.as_str());
        let above = current.is_some_and(|c| c.strip_prefix(path.as_str()).is_some_and(|rest| rest.starts_with('/')));
        let container = *kind == NodeKind::Container;
        h.markup("<li class=\"tree-").markup(kind.as_str());
        if on {
            h.markup(" tree-on");
        } else if above {
            h.markup(" tree-above");
        }
        h.markup("\">");
        if container {
            h.markup(if on || above { "<details open><summary>" } else { "<details><summary>" });
        }
        h.markup("<a href=\"/view/").text(path).markup("\"");
        if on {
            h.markup(" aria-current=\"page\"");
        }
        h.markup(" title=\"").text(path).markup("\">");
        if let Some((_, glyph)) = LEAF_GLYPHS.iter().find(|(k, _)| k == kind) {
            h.markup("<span class=\"ico\" aria-hidden=\"true\">").text(glyph).markup("</span>");
        }
        h.text(name).markup("</a>");
        if container {
            h.markup("</summary><ul>\n");
            open.push(path);
        } else {
            h.markup("</li>\n");
        }
    }
    for _ in open {
        h.markup("</ul></details></li>\n");
    }
    if outline.len() >= OUTLINE_MAX {
        h.markup("<li class=\"more\">…</li>\n");
    }
    h.markup("</ul></nav></aside>\n");
}

pub fn page(title: &str, path: Option<&str>, content: &Html, outline: &[(String, NodeKind)]) -> Html {
    let mut h = Html::new();
    h.markup("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>board · ")
        .text(title)
        .markup(HEAD);
    h.markup("<div class=\"shell\">\n");
    sidebar(&mut h, outline, path);
    h.markup("<main>\n");
    if let Some(path) = path {
        h.markup("<div class=\"crumbs\"><a href=\"/\">board</a>");
        let mut prefix = String::new();
        for segment in path.split('/') {
            if !prefix.is_empty() {
                prefix.push('/');
            }
            prefix.push_str(segment);
            h.markup(" / <a href=\"/view/").text(&prefix).markup("\">").text(segment).markup("</a>");
        }
        h.markup("</div>\n");
    }
    h.html(content).markup("\n</main></div>\n</body></html>\n");
    h
}

pub fn error_page(status: u16, message: &str) -> Html {
    let mut content = Html::new();
    content.markup("<h2>").text(&status.to_string()).markup("</h2>\n").html(&error_fragment(message));
    page("error", None, &content, &[])
}

/// A refusal where a piece of page would have gone.
pub fn error_fragment(message: &str) -> Html {
    let mut h = Html::new();
    h.markup("<p class=\"error\">").text(message).markup("</p>");
    h
}

/// `content` in the element with id `id`, which the forms in it swap.
pub fn swapped(id: &'static str, content: &Html) -> Html {
    let mut h = Html::new();
    h.markup("<div id=\"").markup(id).markup("\">").html(content).markup("</div>\n");
    h
}

/// How often (s) a live piece of page asks for itself again.
const LIVE_EVERY: u32 = 5;

/// `content` in the element with id `id`, which asks `url` for its content
/// every few seconds. board-sign.js holds the request back while the tab is
/// hidden or something is being typed into the piece (`data-live`).
pub fn live(id: &'static str, url: &str, content: &Html) -> Html {
    let mut h = Html::new();
    h.markup("<div id=\"").markup(id).markup("\" data-live hx-get=\"").text(url).markup("\" hx-trigger=\"every ");
    h.number(i64::from(LIVE_EVERY)).markup("s\" hx-swap=\"innerHTML\">").html(content).markup("</div>\n");
    h
}

/// A form's place for a refusal, which board-sign.js fills in.
fn form_error(h: &mut Html, message: &str) {
    h.markup("<p class=\"error\"");
    if message.is_empty() {
        h.markup(" hidden");
    }
    h.markup(">").text(message).markup("</p>\n");
}

fn hidden_input(h: &mut Html, name: &'static str, value: &str) {
    h.markup("<input type=\"hidden\" name=\"").markup(name).markup("\" value=\"").text(value).markup("\">\n");
}

fn link(h: &mut Html, path: &str) {
    h.markup("<a href=\"/view/").text(path).markup("\">").text(path).markup("</a>");
}

/// Who wrote it: the name, with its key as a tooltip, and the revocation
/// of that key, if there has been one since. What it signed stays as it
/// was, signed by a key trusted then.
fn author(h: &mut Html, from: &str, signed_by: Option<&str>, revoked_at: Option<i64>) {
    h.markup("<span title=\"signed by ")
        .text(signed_by.unwrap_or("the board"))
        .markup("\">")
        .text(from)
        .markup("</span>");
    if let Some(at) = revoked_at {
        h.markup(" <span class=\"revoked\">key revoked at #").number(at).markup("</span>");
    }
}

fn plural(h: &mut Html, n: i64, one: &'static str, many: &'static str) {
    h.number(n).markup(" ").markup(if n == 1 { one } else { many });
}

/// How a child stands, by kind, in one line.
fn status(h: &mut Html, status: &Status) {
    match status {
        Status::Container { children } => plural(h, *children, "child", "children"),
        Status::Note { version: 0, .. } => {
            h.markup("no version yet");
        }
        Status::Note { version, ts, from, thread, through, behind } => {
            h.markup("v").number(*version);
            if let Some(from) = from {
                h.markup(" by ").text(from);
            }
            if let Some(ts) = ts {
                h.markup(" · ").text(ts);
            }
            if let Some(thread) = thread {
                h.markup(" · summarizes /").text(thread);
                if let (Some(through), Some(behind)) = (through, behind) {
                    h.markup(" through #").number(*through).markup(", ").number(*behind).markup(" behind");
                }
            }
        }
        Status::Thread { last_id, last_ts, messages } => {
            plural(h, *messages, "message", "messages");
            if let (Some(id), Some(ts)) = (last_id, last_ts) {
                h.markup(" · last #").number(*id).markup(" · ").text(ts);
            }
        }
        Status::Queue { owner, open, oldest_open_ts } => {
            h.text(owner).markup("'s · ").number(*open).markup(" open");
            if let Some(ts) = oldest_open_ts {
                h.markup(" · oldest ").text(ts);
            }
        }
    }
}

/// A container's listing: one row per child, and how far each has moved.
pub fn children(children: &[Child]) -> Html {
    let mut h = Html::new();
    if children.is_empty() {
        h.markup("<p class=\"empty\">nothing in here yet</p>");
        return h;
    }
    h.markup("<table class=\"children\"><thead><tr><th>path</th><th>kind</th><th>status</th><th>description</th><th>by</th></tr></thead><tbody>\n");
    for child in children {
        h.markup("<tr><td>");
        link(&mut h, &child.path);
        h.markup("</td><td>").text(child.kind.as_str()).markup("</td><td>");
        status(&mut h, &child.status);
        h.markup("</td><td>").text(&child.description).markup("</td><td>").text(&child.creator).markup("</td></tr>\n");
    }
    h.markup("</tbody></table>");
    h
}

/// A node's heading: its path, kind and description.
pub fn heading(node: &Node) -> Html {
    let mut h = Html::new();
    h.markup("<h2>").text(&node.path).markup(" <span class=\"meta\">").text(node.kind.as_str());
    if let Some(owner) = &node.owner {
        h.markup(" of ").text(owner);
    }
    h.markup(" · made by ").text(&node.creator).markup(" at #").number(node.created).markup("</span></h2>\n");
    if !node.description.is_empty() {
        h.markup("<p>").text(&node.description).markup("</p>\n");
    }
    h
}

/// The tree page's form for making a node. Missing containers along the
/// path are made too (board-sign.js signs each); `#created` takes the answer.
pub fn create_form() -> Html {
    let mut h = Html::new();
    h.markup("<div data-needs=\"trusted\" hidden>\n<form class=\"new\" hx-post=\"/ui/create\" hx-target=\"#created\" hx-swap=\"innerHTML\">\n");
    form_error(&mut h, "");
    h.markup("<input name=\"path\" placeholder=\"new/path\" required>\n<select name=\"kind\">");
    for kind in NodeKind::ALL {
        h.markup("<option>").markup(kind.as_str()).markup("</option>");
    }
    h.markup("</select>\n<input name=\"description\" placeholder=\"description (optional)\">\n<button type=\"submit\">new</button>\n</form>\n<div id=\"created\"></div></div>\n");
    h
}

/// What a registration's form answers: the request is waiting for the
/// owner, and the key is still not trusted.
pub fn registered(name: &str, fingerprint: &str, item: i64) -> Html {
    let mut h = Html::new();
    h.markup("<p>Registered <b>").text(name).markup("</b> as request #").number(item).markup(" for key <code>");
    h.text(fingerprint).markup("</code>. It cannot write until the board's owner approves it.</p>");
    h
}

/// What a made node's form answers: a link to it.
pub fn created(path: &str, kind: NodeKind) -> Html {
    let mut h = Html::new();
    h.markup("<p>made the ").markup(kind.as_str()).markup(" ");
    link(&mut h, path);
    h.markup("</p>");
    h
}

/// The owner's Archive (or, on an archived one, Unarchive) form for the
/// message or item `id`: `target` is the element the answer swaps, and
/// `show_archived` says whether the page shows archived ones (the answer then
/// does too).
fn archive_form(h: &mut Html, id: i64, archive: bool, target: &'static str, show_archived: bool) {
    h.markup("<div class=\"controls\" data-needs=\"owner\" hidden>\n<form hx-post=\"/ui/")
        .markup(if archive { "archive" } else { "unarchive" })
        .markup("\" hx-target=\"#")
        .markup(target)
        .markup("\" hx-swap=\"innerHTML\">\n");
    form_error(h, "");
    hidden_input(h, "view", if show_archived { "archived" } else { "" });
    hidden_input(h, "event", &id.to_string());
    hidden_input(h, "archived", if archive { "1" } else { "0" });
    h.markup("<input name=\"reason\" placeholder=\"reason (optional)\" maxlength=\"200\">\n<button type=\"submit\">")
        .markup(if archive { "Archive" } else { "Unarchive" })
        .markup("</button>\n</form></div>\n");
}

/// The link between a node's page showing what is archived or not.
fn archived_toggle(h: &mut Html, path: &str, show_archived: bool) {
    h.markup("<p class=\"meta\"><a href=\"/view/").text(path);
    h.markup(if show_archived { "\">hide archived</a></p>\n" } else { "?archived=1\">show archived</a></p>\n" });
}

/// The query string that asks for `range` of a thread: how the pages, the
/// refresh and the paging links say which messages are on show.
fn range_query(range: Range) -> String {
    match range {
        Range::Last(n) => format!("last={n}"),
        Range::Since(id) => format!("since={id}"),
        Range::All => "all=1".to_owned(),
    }
}

/// How many more messages "older" adds.
const OLDER_STEP: i64 = 100;

/// A link to the thread's page, showing `query`.
fn thread_link(h: &mut Html, path: &str, query: &str, label: &str, show_archived: bool) {
    h.markup("<a href=\"/view/").text(path).markup("?").text(query);
    h.markup(if show_archived { "&amp;archived=1\">" } else { "\">" }).text(label).markup("</a>");
}

/// What a window on a thread says of the messages outside it, with the
/// links that move it: older, all of them, the newest again, or on from
/// where a read the board cut short stopped.
fn thread_paging(h: &mut Html, path: &str, page: &ThreadPage, range: Range, default: i64, show_archived: bool) {
    let shown = i64::try_from(page.messages.len()).unwrap_or(i64::MAX);
    h.markup("<p class=\"meta\">");
    if page.earlier > 0 {
        plural(h, page.earlier, "earlier message is", "earlier messages are");
        h.markup(" not shown: ");
        if let (Range::Last(n), false) = (range, page.truncated) {
            thread_link(h, path, &format!("last={}", n.saturating_add(OLDER_STEP)), "show older", show_archived);
            h.markup(" · ");
        }
        thread_link(h, path, "all=1", "show all", show_archived);
        if !matches!(range, Range::Last(n) if n == default) {
            h.markup(" · ");
            thread_link(h, path, &format!("last={default}"), "newest only", show_archived);
        }
    }
    if page.truncated {
        match (range, page.messages.last()) {
            (Range::Last(_), _) | (_, None) => {
                h.markup(" The board cuts a read at ").number(i64::try_from(READ_ROWS_MAX).unwrap_or(i64::MAX));
                h.markup(" messages or ")
                    .number(i64::try_from(READ_BYTES_MAX / (1024 * 1024)).unwrap_or(0))
                    .markup(" MB: ");
                h.markup("only the newest ").number(shown).markup(" of this window are shown; ask for a smaller one.");
            }
            (_, Some(last)) => {
                h.markup(" This page stops at ").number(shown).markup(" messages (the board's cap): ");
                thread_link(h, path, &format!("since={}", last.id), "read on", show_archived);
            }
        }
    }
    h.markup("</p>\n");
}

/// A thread's messages, oldest first, and the paging around them.
pub fn messages(path: &str, page: &ThreadPage, range: Range, default: i64, show_archived: bool) -> Html {
    let mut h = Html::new();
    if page.earlier > 0 || page.truncated {
        thread_paging(&mut h, path, page, range, default, show_archived);
    }
    if page.messages.is_empty() {
        h.markup("<p class=\"empty\">no messages yet</p>");
    }
    for m in &page.messages {
        h.markup(if m.archived { "<div class=\"msg archived\">" } else { "<div class=\"msg\">" });
        h.markup("<div class=\"meta\">#").number(m.id).markup(" · ");
        author(&mut h, &m.from, m.signed_by.as_deref(), m.revoked_at);
        h.markup(" · ").text(&m.ts);
        if m.archived {
            h.markup(" <span class=\"tag\">archived</span>");
        }
        h.markup("</div><div class=\"body\">").text(&m.body).markup("</div>\n");
        archive_form(&mut h, m.id, !m.archived, "messages", show_archived);
        h.markup("</div>\n");
    }
    h
}

/// A thread: its messages, refreshed every 3 s over the same window, and
/// the form to post (which gets the same window back).
pub fn thread(node: &Node, page: &ThreadPage, range: Range, default: i64, show_archived: bool) -> Html {
    let mut h = heading(node);
    archived_toggle(&mut h, &node.path, show_archived);
    h.markup("<div id=\"messages\" hx-get=\"/ui/messages?path=")
        .text(&encoded(&node.path))
        .markup(if show_archived { "&amp;archived=1" } else { "" })
        .markup("&amp;")
        .text(&range_query(range))
        .markup("\" hx-trigger=\"every 3s\" hx-swap=\"innerHTML\">")
        .html(&messages(&node.path, page, range, default, show_archived))
        .markup("</div>\n");
    if !node.generated {
        h.markup("<div data-needs=\"trusted\" hidden>\n<form hx-post=\"/ui/post\" hx-target=\"#messages\" hx-swap=\"innerHTML\">\n");
        form_error(&mut h, "");
        hidden_input(&mut h, "path", &node.path);
        if show_archived {
            hidden_input(&mut h, "shown", "archived");
        }
        match range {
            Range::Last(n) => hidden_input(&mut h, "last", &n.to_string()),
            Range::Since(id) => hidden_input(&mut h, "since", &id.to_string()),
            Range::All => hidden_input(&mut h, "all", "1"),
        }
        h.markup("<textarea name=\"body\" placeholder=\"message\" required></textarea>\n<button type=\"submit\">post</button>\n</form></div>\n");
    }
    h
}

/// What a note shows: the version on show (`None`: there is none yet),
/// the thread its `through` is in, every version, and whether the one on
/// show is the current one (only that one is edited).
pub struct NoteView<'a> {
    pub node: &'a Node,
    pub note: Option<&'a NoteVersion>,
    pub history: &'a [Version],
    pub current: bool,
}

/// A note: the version on show, its staleness against the thread it
/// summarizes, the edit button and the history. Swapped whole into
/// `#note` (by an edit's cancel and save).
pub fn note(v: &NoteView<'_>) -> Html {
    let mut h = Html::new();
    let path = &v.node.path;
    match v.note {
        None => {
            h.markup("<p class=\"empty\">no version yet</p>\n");
        }
        Some(note) => {
            h.markup("<div class=\"note\"><div class=\"meta\">v").number(note.version).markup(" · ");
            author(&mut h, &note.from, note.signed_by.as_deref(), note.revoked_at);
            h.markup(" · ").text(&note.ts).markup(" · #").number(note.id).markup("</div>\n");
            if !v.current {
                h.markup("<p class=\"flag\">an earlier version; <a href=\"/view/")
                    .text(path)
                    .markup("\">the current one</a></p>\n");
            }
            if let Some(thread) = &note.thread {
                h.markup("<p class=\"meta\">summarizes <a href=\"/view/")
                    .text(thread)
                    .markup("\">/")
                    .text(thread)
                    .markup("</a>");
                if let (Some(through), Some(behind)) = (note.through, note.behind) {
                    h.markup(" through #").number(through).markup(", ").number(behind).markup(" behind");
                }
                h.markup("</p>\n");
            }
            h.markup("<div class=\"body\">").text(&note.body).markup("</div></div>\n");
        }
    }
    if v.current && !v.node.generated {
        // The editor starts from the version on show, which is the current one.
        h.markup("<details data-needs=\"trusted\" hidden><summary>")
            .markup(if v.note.is_some() { "edit" } else { "write the first version" })
            .markup("</summary>\n")
            .html(&note_form(&NoteForm {
                path,
                body: v.note.map_or("", |n| n.body.as_str()),
                base_version: v.note.map_or(0, |n| n.version),
                thread: v.note.and_then(|n| n.thread.as_deref()).unwrap_or(""),
                through: v.note.and_then(|n| n.through),
                error: "",
            }))
            .markup("</details>\n");
    }
    if !v.history.is_empty() {
        h.markup("<h3>history</h3>\n<ul class=\"history\">\n");
        for version in v.history.iter().rev() {
            h.markup("<li><a href=\"/view/")
                .text(path)
                .markup("?version=")
                .number(version.version)
                .markup("\">v")
                .number(version.version)
                .markup("</a> · ");
            author(&mut h, &version.from, version.signed_by.as_deref(), version.revoked_at);
            h.markup(" · ").text(&version.ts);
            if let Some(thread) = &version.thread {
                h.markup(" · summarizes /").text(thread);
                if let Some(through) = version.through {
                    h.markup(" through #").number(through);
                }
            }
            h.markup("</li>\n");
        }
        h.markup("</ul>\n");
    }
    h
}

/// What a note editor shows: the text and version it edits, and why it is
/// shown again, if it is.
pub struct NoteForm<'a> {
    pub path: &'a str,
    pub body: &'a str,
    pub base_version: i64,
    pub thread: &'a str,
    pub through: Option<i64>,
    pub error: &'a str,
}

/// The note editor: in the note's page, and again in place of the note in
/// `#note` when a save was refused because the note had changed.
pub fn note_form(f: &NoteForm<'_>) -> Html {
    let mut h = Html::new();
    h.markup("<form hx-post=\"/ui/note\" hx-target=\"#note\" hx-swap=\"innerHTML\">\n");
    form_error(&mut h, f.error);
    hidden_input(&mut h, "path", f.path);
    hidden_input(&mut h, "base_version", &f.base_version.to_string());
    h.markup("<textarea name=\"body\" required>").text(f.body).markup("</textarea>\n");
    h.markup(
        "<label>summarizes the thread (with a message, or neither) <input name=\"thread\" placeholder=\"area/group/thread\" value=\"",
    )
    .text(f.thread);
    h.markup("\"></label>\n<label>through message # (with a thread) <input name=\"through\" inputmode=\"numeric\" size=\"8\" value=\"");
    if let Some(through) = f.through {
        h.number(through);
    }
    h.markup("\"></label>\n<button type=\"submit\">save</button>\n<button type=\"button\" hx-get=\"/ui/note?path=")
        .text(&encoded(f.path))
        .markup("\" hx-target=\"#note\" hx-swap=\"innerHTML\">back</button>\n</form>");
    h
}

/// One item: who asked, what, and how it ended if it has.
fn item(h: &mut Html, item: &Item, with_queue: bool) {
    h.markup("<div class=\"meta\">#").number(item.id).markup(" · ");
    if with_queue {
        link(h, &item.path);
        h.markup(" · ");
    }
    match &item.asker {
        Some(asker) => h.markup("asked by ").text(asker),
        None => h.markup("a registration"),
    };
    h.markup(" · ").text(&item.ts).markup(" · ").text(item.state.as_str());
    if item.archived {
        h.markup(" <span class=\"tag\">archived</span>");
    }
    h.markup("</div>\n<div class=\"body\">").text(&item.body).markup("</div>\n");
    if let Some(r) = &item.resolution {
        h.markup("<div class=\"resolution\"><div class=\"meta\">#").number(r.id).markup(" ").text(r.event.as_str());
        if let Some(from) = &r.from {
            h.markup(" by ").text(from);
        }
        if let Some(reference) = r.reference {
            h.markup(" · see #").number(reference);
        }
        h.markup("</div><div class=\"body\">").text(&r.body).markup("</div></div>\n");
    }
}

/// A queue: the form to file a request, and its items (the archived ones
/// too, marked, if `show_archived`).
pub fn queue(node: &Node, page: &ItemPage, after: i64, show_archived: bool) -> Html {
    let mut h = heading(node);
    archived_toggle(&mut h, &node.path, show_archived);
    if !node.generated {
        h.markup("<div data-needs=\"trusted\" hidden>\n<form hx-post=\"/ui/ask\" hx-target=\"#queue-items\" hx-swap=\"innerHTML\">\n");
        form_error(&mut h, "");
        hidden_input(&mut h, "path", &node.path);
        if show_archived {
            hidden_input(&mut h, "shown", "archived");
        }
        h.markup("<textarea name=\"body\" placeholder=\"new request\" required></textarea>\n<button type=\"submit\">file request</button>\n</form></div>\n");
    }
    let archived = if show_archived { "&archived=1" } else { "" };
    let url = format!("/ui/live/queue?path={}&after={after}{archived}", encoded(&node.path));
    h.html(&live("queue-items", &url, &queue_items(&node.path, page, show_archived)));
    h
}

/// One item of a queue, with the owner's Archive (or Unarchive) control.
fn queue_item(h: &mut Html, i: &Item, show_archived: bool) {
    h.markup(if i.archived { "<div class=\"item archived\">" } else { "<div class=\"item\">" });
    item(h, i, false);
    archive_form(h, i.id, !i.archived, "queue-items", show_archived);
    // Only an open item is resolved, and an archived one is out of play.
    if i.state == ItemState::Open && !i.archived {
        resolve_controls(h, i, show_archived);
    }
    h.markup("</div>\n");
}

/// What an open item's queue page offers, as the store's rules allow it:
/// the queue's owner answers, the queue's owner or the owner key declines,
/// and whoever asked cancels (the owner key too, only for its own requests).
/// Each control is shown to the key it is for (`data-needs`, and the name
/// in `data-name`); the board refuses anyone else. A registration has no
/// answer: it is approved, in the inbox.
fn resolve_controls(h: &mut Html, i: &Item, show_archived: bool) {
    if let Some(asker) = &i.asker {
        h.markup("<div class=\"controls\" data-needs=\"named\" data-name=\"").text(&i.owner).markup("\" hidden>\n");
        resolve_form(h, i.id, ItemState::Answered, "queue", show_archived);
        h.markup("</div>\n");
        h.markup("<div class=\"controls\" data-needs=\"named\" data-name=\"").text(asker).markup("\" hidden>\n");
        resolve_form(h, i.id, ItemState::Cancelled, "queue", show_archived);
        h.markup("</div>\n");
    }
    h.markup("<div class=\"controls\" data-needs=\"owner\" data-name=\"").text(&i.owner).markup("\" hidden>\n");
    resolve_form(h, i.id, ItemState::Declined, "queue", show_archived);
    h.markup("</div>\n");
}

/// A queue's items, as `#queue-items` holds them: the open ones oldest
/// first, and the resolved ones folded away; `page` has the archived ones
/// only if `show_archived`.
pub fn queue_items(path: &str, page: &ItemPage, show_archived: bool) -> Html {
    let mut h = Html::new();
    let items = &page.items;
    let (open, resolved): (Vec<&Item>, Vec<&Item>) = items.iter().partition(|i| i.state == ItemState::Open);
    if open.is_empty() {
        h.markup("<p class=\"empty\">nothing open</p>\n");
    }
    for i in open {
        queue_item(&mut h, i, show_archived);
    }
    if !resolved.is_empty() {
        h.markup("<details><summary>");
        plural(&mut h, i64::try_from(resolved.len()).unwrap_or(i64::MAX), "resolved item", "resolved items");
        h.markup("</summary>\n");
        for i in resolved {
            queue_item(&mut h, i, show_archived);
        }
        h.markup("</details>\n");
    }
    if page.truncated {
        h.markup("<p class=\"meta\">The board cuts a read at ")
            .number(i64::try_from(READ_ROWS_MAX).unwrap_or(i64::MAX));
        h.markup(" items or ")
            .number(i64::try_from(READ_BYTES_MAX / (1024 * 1024)).unwrap_or(0))
            .markup(" MB: more follow. ");
        if let Some(last) = items.last() {
            h.markup("<a href=\"/view/").text(path).markup("?after=").number(last.id);
            h.markup(if show_archived { "&amp;archived=1\">next items</a>" } else { "\">next items</a>" });
        }
        h.markup("</p>\n");
    }
    h
}

/// A form that resolves `item` as `state`, with the reason asked for;
/// an answer may also name the message that holds it.
fn resolve_form(h: &mut Html, item: i64, state: ItemState, view: &'static str, show_archived: bool) {
    // A queue page's items are what its own `#queue-items` holds.
    let target = if view == "queue" { "queue-items" } else { view };
    h.markup("<form hx-post=\"/ui/resolve\" hx-target=\"#").markup(target).markup("\" hx-swap=\"innerHTML\">\n");
    form_error(h, "");
    hidden_input(h, "view", view);
    if show_archived {
        hidden_input(h, "shown", "archived");
    }
    hidden_input(h, "item", &item.to_string());
    hidden_input(h, "state", state.as_str());
    if state == ItemState::Answered {
        h.markup("<textarea name=\"body\" placeholder=\"the answer\" required></textarea>\n");
        h.markup("<input name=\"ref\" inputmode=\"numeric\" size=\"8\" placeholder=\"message #\">\n");
        h.markup("<button type=\"submit\">Answer</button>\n");
    } else if state == ItemState::Cancelled {
        h.markup("<input name=\"body\" placeholder=\"why (optional)\">\n<button type=\"submit\">Cancel</button>\n");
    } else {
        h.markup(
            "<input name=\"body\" placeholder=\"why decline\" required>\n<button type=\"submit\">Decline</button>\n",
        );
    }
    h.markup("</form>\n");
}

/// One registration: the key, what it says it is for, what approving it
/// would do, and the owner's two answers; `view` is the page (`inbox` or
/// `keys`) whose piece of page the answer swaps.
fn registration(h: &mut Html, r: &Registration, view: &'static str) {
    h.markup("<div class=\"card\"><div class=\"meta\">#").number(r.item).markup(" · ").text(&r.ts).markup("</div>\n");
    h.markup("<div>key <code>").text(&r.fingerprint).markup("</code></div>\n");
    if let Some(old) = &r.replaces {
        h.markup("<p class=\"flag\">replaces <code>").text(old).markup("</code>: approving revokes it</p>\n");
    }
    if !r.look_alikes.is_empty() {
        h.markup("<p class=\"flag\">looks like the name ").text(&r.look_alikes.join(", ")).markup("</p>\n");
    }
    h.markup("<div>repo <code>")
        .text(&r.repo)
        .markup("</code></div>\n<div class=\"body\">")
        .text(&r.note)
        .markup("</div>\n");
    h.markup("<div class=\"controls\" data-needs=\"owner\" hidden>\n<form hx-post=\"/ui/approve\" hx-target=\"#")
        .markup(view)
        .markup("\" hx-swap=\"innerHTML\">\n");
    form_error(h, "");
    hidden_input(h, "view", view);
    hidden_input(h, "item", &r.item.to_string());
    hidden_input(h, "name", &r.name);
    hidden_input(h, "fingerprint", &r.fingerprint);
    h.markup("<button type=\"submit\">Approve</button>\n</form>\n");
    resolve_form(h, r.item, ItemState::Declined, view, false);
    h.markup("</div></div>\n");
}

/// What the owner's inbox shows.
pub struct Inbox<'a> {
    /// The owner's name; `None` while no owner key is configured.
    pub owner: Option<&'a str>,
    /// Oldest first.
    pub registrations: &'a [Registration],
    /// Open items in the owner's queues, but the register queue.
    pub items: &'a [Item],
    /// The board cut the read short after this item: the next page is `?after=` it.
    pub more_after: Option<i64>,
}

/// The owner's inbox, the content of `#inbox`: the registrations waiting,
/// by the name they ask for, then the open items in the owner's queues.
pub fn inbox(v: &Inbox<'_>) -> Html {
    let mut h = Html::new();
    h.markup("<p class=\"meta\">Every key the board knows: <a href=\"/ui/keys\">keys</a></p>\n");
    if v.owner.is_none() {
        h.markup("<p class=\"flag\">No owner key is configured (BOARD_OWNER_NAME, BOARD_OWNER_PUBKEY): nothing can be approved.</p>\n");
    }
    h.markup("<h2>registrations</h2>\n");
    if v.registrations.is_empty() {
        h.markup("<p class=\"empty\">none waiting</p>\n");
    }
    // By name, in the order each name first asked.
    let mut names: Vec<&str> = Vec::new();
    for r in v.registrations {
        if !names.contains(&r.name.as_str()) {
            names.push(&r.name);
        }
    }
    for name in names {
        let asking: Vec<&Registration> = v.registrations.iter().filter(|r| r.name == name).collect();
        h.markup("<section><h3>").text(name).markup("</h3>\n");
        if asking.len() > 1 {
            h.markup("<p class=\"flag\">").number(i64::try_from(asking.len()).unwrap_or(i64::MAX));
            h.markup(" keys ask for this name: approving one declines the others</p>\n");
        }
        for r in asking {
            registration(&mut h, r, "inbox");
        }
        h.markup("</section>\n");
    }
    if let Some(owner) = v.owner {
        h.markup("<h2>open in ").text(owner).markup("'s queues</h2>\n");
        if v.items.is_empty() {
            h.markup("<p class=\"empty\">nothing open</p>\n");
        }
        for i in v.items {
            h.markup("<div class=\"card\">");
            item(&mut h, i, true);
            h.markup("<div class=\"controls\" data-needs=\"owner\" hidden>\n");
            resolve_form(&mut h, i.id, ItemState::Answered, "inbox", false);
            resolve_form(&mut h, i.id, ItemState::Declined, "inbox", false);
            h.markup("</div></div>\n");
        }
        if let Some(after) = v.more_after {
            h.markup("<p class=\"meta\">The board cuts a read at ")
                .number(i64::try_from(READ_ROWS_MAX).unwrap_or(i64::MAX));
            h.markup(" items: more wait. <a href=\"/ui/inbox?after=").number(after).markup("\">next items</a></p>\n");
        }
    }
    h
}

fn event_ref(h: &mut Html, id: Option<i64>) {
    if let Some(id) = id {
        h.markup("#").number(id);
    }
}

/// The trust list, the content of `#keys`: every key the board knows,
/// with Revoke for each trusted one, and for each pending one the
/// registration to approve or decline (or, if it cannot be found, the way
/// to the inbox).
pub fn keys(keys: &[Key], registrations: &[Registration]) -> Html {
    let mut h = Html::new();
    let pending = keys.iter().filter(|k| k.state == KeyState::Pending).count();
    if pending > 0 {
        h.markup("<p class=\"flag\">").number(i64::try_from(pending).unwrap_or(i64::MAX));
        h.markup(" pending → <a href=\"/ui/inbox\">inbox</a></p>\n");
    }
    h.markup("<p class=\"meta\">As the board writes it after every change: <a href=\"/view/_board/trust\">_board/trust</a></p>\n");
    h.markup("<table class=\"keys\"><thead><tr><th>name</th><th>state</th><th>fingerprint</th><th>approved</th><th>revoked</th><th>repo</th><th>note</th><th></th></tr></thead><tbody>\n");
    for key in keys {
        h.markup("<tr><td>").text(&key.name).markup("</td><td>").text(key.state.as_str()).markup("</td><td><code>");
        h.text(&key.fingerprint).markup("</code></td><td>");
        event_ref(&mut h, key.approved);
        h.markup("</td><td>");
        event_ref(&mut h, key.revoked);
        h.markup("</td><td><code>").text(&key.repo).markup("</code></td><td class=\"body\">").text(&key.note);
        h.markup("</td><td>");
        if key.state == KeyState::Trusted {
            h.markup("<div data-needs=\"owner\" hidden><form hx-post=\"/ui/revoke\" hx-target=\"#keys\" hx-swap=\"innerHTML\">\n");
            form_error(&mut h, "");
            hidden_input(&mut h, "fingerprint", &key.fingerprint);
            h.markup("<input name=\"reason\" placeholder=\"reason\">\n<button type=\"submit\">Revoke</button>\n</form></div>");
        }
        if key.state == KeyState::Pending {
            match registrations.iter().find(|r| r.fingerprint == key.fingerprint) {
                Some(r) => registration(&mut h, r, "keys"),
                None => {
                    h.markup("<a href=\"/ui/inbox\">inbox</a>");
                }
            }
        }
        h.markup("</td></tr>\n");
    }
    h.markup("</tbody></table>");
    h
}

#[cfg(test)]
mod tests {
    use message_board::store::{EventType, ItemResolution, NodeKind};

    use super::*;

    fn text(h: Html) -> String {
        String::from_utf8_lossy(&h.into_bytes()).into_owned()
    }

    const HOSTILE: &str = "<script>alert('x' & \"y\")</script>";
    const ESCAPED: &str = "&lt;script&gt;alert(&#39;x&#39; &amp; &#34;y&#34;)&lt;/script&gt;";

    fn message(id: i64, archived: bool) -> message_board::store::Message {
        message_board::store::Message {
            id,
            ts: "2026-10-22T00:00:00Z".to_owned(),
            path: "rev/log".to_owned(),
            from: "agent".to_owned(),
            signed_by: None,
            revoked_at: None,
            body: HOSTILE.to_owned(),
            archived,
        }
    }

    /// Archive and Unarchive are the owner's: each form sits in a control the
    /// page shows to the owner key only, and archived ones are marked.
    #[test]
    fn archive_controls_are_for_the_owner_and_archived_ones_are_marked() {
        let page =
            ThreadPage { messages: vec![message(10_010, false), message(10_011, true)], earlier: 0, truncated: false };
        let shown = text(messages("rev/log", &page, Range::Last(100), 100, true));
        assert!(!shown.contains(HOSTILE) && shown.contains(ESCAPED), "{shown}");
        assert_eq!(shown.matches("<form").count(), 2, "{shown}");
        assert_eq!(shown.matches("data-needs=\"owner\" hidden").count(), 2, "{shown}");
        assert_eq!(shown.matches("hx-post=\"/ui/archive\"").count(), 1, "{shown}");
        assert_eq!(shown.matches("hx-post=\"/ui/unarchive\"").count(), 1, "{shown}");
        assert_eq!(shown.matches("<span class=\"tag\">archived</span>").count(), 1, "{shown}");
        assert!(shown.contains("name=\"view\" value=\"archived\""), "{shown}");
        // the form posts what it signs: event, archived, reason
        assert!(shown.contains("name=\"event\" value=\"10010\"") && shown.contains("name=\"archived\" value=\"1\""));
        assert!(shown.contains("name=\"event\" value=\"10011\"") && shown.contains("name=\"archived\" value=\"0\""));
        let node = Node {
            id: 1,
            path: "rev/log".to_owned(),
            parent: None,
            kind: NodeKind::Thread,
            description: String::new(),
            creator: "agent".to_owned(),
            owner: None,
            generated: false,
            created: 1,
            archived: None,
        };
        let thread_page = text(thread(&node, &page, Range::Last(100), 100, false));
        assert!(thread_page.contains("href=\"/view/rev/log?archived=1\">show archived"), "{thread_page}");
        let with = text(thread(&node, &page, Range::Last(100), 100, true));
        assert!(with.contains("hide archived") && with.contains("&amp;archived=1"), "{with}");
    }

    #[test]
    fn text_is_escaped_and_markup_is_not() {
        let mut h = Html::new();
        h.markup("<b>").text(HOSTILE).markup("</b>");
        assert_eq!(text(h), format!("<b>{ESCAPED}</b>"));
    }

    fn registration(name: &str, fingerprint: &str) -> Registration {
        Registration {
            item: 10_020,
            ts: "2026-10-22T00:00:00Z".to_owned(),
            name: name.to_owned(),
            fingerprint: fingerprint.to_owned(),
            repo: HOSTILE.to_owned(),
            note: HOSTILE.to_owned(),
            replaces: Some("SHA256:old".to_owned()),
            look_alikes: vec!["alicia".to_owned()],
        }
    }

    #[test]
    fn the_inbox_escapes_what_a_stranger_wrote_and_hides_its_controls() {
        let registrations = [registration("alice", "SHA256:new"), registration("alice", "SHA256:other")];
        let item = Item {
            id: 10_030,
            ts: "2026-10-22T00:00:00Z".to_owned(),
            path: "rev/stefan".to_owned(),
            owner: "Stefan".to_owned(),
            asker: Some("alice".to_owned()),
            state: ItemState::Open,
            body: HOSTILE.to_owned(),
            archived: false,
            resolution: None,
        };
        let page = text(inbox(&Inbox {
            owner: Some("Stefan"),
            registrations: &registrations,
            items: &[item],
            more_after: None,
        }));
        assert!(!page.contains(HOSTILE) && page.contains(ESCAPED), "{page}");
        assert!(page.contains("replaces <code>SHA256:old</code>"), "{page}");
        assert!(page.contains("2 keys ask for this name"), "{page}");
        assert_eq!(page.matches("<h3>alice</h3>").count(), 1, "{page}");
        // every form sits in a control the page shows only to the owner key
        assert_eq!(page.matches("<form").count(), 6, "{page}");
        assert_eq!(page.matches("data-needs=\"owner\" hidden").count(), 3, "{page}");
    }

    fn key(name: &str, fingerprint: &str, state: KeyState) -> Key {
        Key {
            fingerprint: fingerprint.to_owned(),
            pubkey: String::new(),
            name: name.to_owned(),
            state,
            repo: String::new(),
            note: String::new(),
            item: None,
            approved: None,
            revoked: None,
        }
    }

    #[test]
    fn a_pending_key_can_be_approved_from_the_keys_page_and_is_never_a_dead_end() {
        let known = registration("alice", "SHA256:new");
        let keys = [
            key("alice", "SHA256:new", KeyState::Pending),
            key("bob", "SHA256:lost", KeyState::Pending),
            key("carol", "SHA256:carol", KeyState::Trusted),
        ];
        let page = text(super::keys(&keys, &[known]));
        assert!(page.contains("2 pending → <a href=\"/ui/inbox\">inbox</a>"), "{page}");
        // alice: approve and decline, each swapping the keys page and saying so; bob: a way out
        assert_eq!(page.matches("hx-post=\"/ui/approve\" hx-target=\"#keys\"").count(), 1, "{page}");
        assert_eq!(page.matches("hx-post=\"/ui/resolve\" hx-target=\"#keys\"").count(), 1, "{page}");
        assert_eq!(page.matches("name=\"view\" value=\"keys\"").count(), 2, "{page}");
        assert!(page.contains("replaces <code>SHA256:old</code>"), "{page}");
        assert!(page.contains("<td><a href=\"/ui/inbox\">inbox</a></td>"), "{page}");
        // every form, revoke included, sits in a control only the owner is shown
        assert_eq!(page.matches("<form").count(), 3, "{page}");
        assert_eq!(page.matches("data-needs=\"owner\" hidden").count(), 2, "{page}");
    }

    #[test]
    fn a_queue_page_carries_the_ask_form_above_its_items() {
        let node = Node {
            id: 5,
            path: "rev/q".to_owned(),
            parent: Some(1),
            kind: NodeKind::Queue,
            description: String::new(),
            creator: "alice".to_owned(),
            owner: None,
            generated: false,
            created: 10_010,
            archived: None,
        };
        let page = text(queue(&node, &ItemPage { items: vec![], truncated: false }, 0, false));
        assert!(page.contains("<div data-needs=\"trusted\" hidden>"), "{page}");
        assert!(page.contains("hx-post=\"/ui/ask\" hx-target=\"#queue-items\""), "{page}");
        assert!(page.contains("name=\"path\" value=\"rev/q\"") && page.contains("<textarea name=\"body\""), "{page}");
        assert!(
            page.contains("<div id=\"queue-items\" data-live hx-get=\"/ui/live/queue?path=rev%2Fq&amp;after=0\" hx-trigger=\"every 5s\" hx-swap=\"innerHTML\"><p class=\"empty\">nothing open</p>"),
            "{page}"
        );
        let generated = Node { generated: true, ..node };
        assert!(!text(queue(&generated, &ItemPage { items: vec![], truncated: false }, 0, false)).contains("/ui/ask"));
    }

    fn open_item(id: i64, asker: Option<&str>) -> Item {
        Item {
            id,
            ts: String::new(),
            path: "rev/bob".to_owned(),
            owner: "bob".to_owned(),
            asker: asker.map(str::to_owned),
            state: ItemState::Open,
            body: "q".to_owned(),
            resolution: None,
            archived: false,
        }
    }

    #[test]
    fn an_open_item_offers_the_resolutions_the_store_allows_to_the_keys_it_allows_them() {
        let page = text(queue_items(
            "rev/bob",
            &ItemPage { items: vec![open_item(10_030, Some("alice")), open_item(10_031, None)], truncated: false },
            false,
        ));
        // answer: the queue's owner only, and never for a registration
        assert_eq!(page.matches("data-needs=\"named\" data-name=\"bob\" hidden").count(), 1, "{page}");
        assert_eq!(page.matches("<button type=\"submit\">Answer</button>").count(), 1, "{page}");
        // decline: the owner key, or the queue's owner by name
        assert_eq!(page.matches("data-needs=\"owner\" data-name=\"bob\" hidden").count(), 2, "{page}");
        assert_eq!(page.matches("<button type=\"submit\">Decline</button>").count(), 2, "{page}");
        // each swaps the queue's own items
        assert_eq!(page.matches("hx-post=\"/ui/resolve\" hx-target=\"#queue-items\"").count(), 4, "{page}");
        assert_eq!(page.matches("name=\"view\" value=\"queue\"").count(), 4, "{page}");
        // cancel: the asker by name, and only for what a key asked
        assert_eq!(page.matches("data-needs=\"named\" data-name=\"alice\" hidden").count(), 1, "{page}");
        assert_eq!(page.matches("<button type=\"submit\">Cancel</button>").count(), 1, "{page}");
        // nothing but resolved items: no controls
        let mut done = open_item(10_032, Some("alice"));
        done.state = ItemState::Answered;
        assert!(
            !text(queue_items("rev/bob", &ItemPage { items: vec![done], truncated: false }, false))
                .contains("/ui/resolve")
        );
        // nor on an archived open item, which keeps only its Unarchive
        let mut gone = open_item(10_033, Some("alice"));
        gone.archived = true;
        let page = text(queue_items("rev/bob", &ItemPage { items: vec![gone], truncated: false }, true));
        assert!(!page.contains("/ui/resolve") && page.contains("hx-post=\"/ui/unarchive\""), "{page}");
        // a page that shows archived items sends that along with each form
        let page = text(queue_items(
            "rev/bob",
            &ItemPage { items: vec![open_item(10_034, Some("alice"))], truncated: false },
            true,
        ));
        assert_eq!(page.matches("name=\"shown\" value=\"archived\"").count(), 3, "{page}");
    }

    fn thread_page(ids: std::ops::Range<i64>, earlier: i64, truncated: bool) -> ThreadPage {
        use message_board::store::Message;
        ThreadPage {
            messages: ids
                .map(|id| Message {
                    id,
                    ts: String::new(),
                    path: "rev/log".to_owned(),
                    from: "alice".to_owned(),
                    signed_by: None,
                    revoked_at: None,
                    body: "m".to_owned(),
                    archived: false,
                })
                .collect(),
            earlier,
            truncated,
        }
    }

    #[test]
    fn a_thread_window_pages_older_and_all_and_keeps_its_window_in_the_refresh() {
        let node = Node {
            id: 6,
            path: "rev/log".to_owned(),
            parent: Some(1),
            kind: NodeKind::Thread,
            description: String::new(),
            creator: "alice".to_owned(),
            owner: None,
            generated: false,
            created: 10_010,
            archived: None,
        };
        let page = text(thread(&node, &thread_page(10_100..10_200, 40, false), Range::Last(100), 100, false));
        assert!(page.contains("40 earlier messages are not shown:"), "{page}");
        assert!(page.contains("<a href=\"/view/rev/log?last=200\">show older</a>"), "{page}");
        assert!(page.contains("<a href=\"/view/rev/log?all=1\">show all</a>"), "{page}");
        assert!(!page.contains("newest only"), "{page}");
        assert!(page.contains("hx-get=\"/ui/messages?path=rev%2Flog&amp;last=100\""), "{page}");
        assert!(page.contains("name=\"last\" value=\"100\""), "{page}");
        // a wider window can go back to the newest, a whole one has nothing earlier
        let wide = text(messages("rev/log", &thread_page(10_060..10_200, 0, false), Range::Last(200), 100, false));
        assert!(!wide.contains("not shown"), "{wide}");
        let part = text(messages("rev/log", &thread_page(10_150..10_160, 3, false), Range::Since(10_149), 100, false));
        assert!(
            part.contains("show all") && part.contains("last=100\">newest only") && !part.contains("show older"),
            "{part}"
        );
        // a read the board cut short says so, and pages on
        let cut = text(messages("rev/log", &thread_page(0..5, 0, true), Range::All, 100, false));
        assert!(cut.contains("This page stops at 5 messages") && cut.contains("since=4\">read on"), "{cut}");
        let cut = text(messages("rev/log", &thread_page(10..15, 9000, true), Range::Last(9999), 100, false));
        assert!(cut.contains("only the newest 5") && !cut.contains("show older"), "{cut}");
        assert_eq!(
            text(thread(&node, &thread_page(1..2, 0, false), Range::All, 100, false))
                .matches("name=\"all\" value=\"1\"")
                .count(),
            1
        );
    }

    #[test]
    fn a_queue_page_that_the_board_cut_short_says_so_and_links_the_next_page() {
        let page = text(queue_items(
            "rev/bob",
            &ItemPage {
                items: vec![open_item(10_030, Some("alice")), open_item(10_031, Some("alice"))],
                truncated: true,
            },
            false,
        ));
        assert!(page.contains("<a href=\"/view/rev/bob?after=10031\">next items</a>"), "{page}");
        assert!(
            !text(queue_items("rev/bob", &ItemPage { items: vec![], truncated: false }, false)).contains("next items")
        );
    }

    #[test]
    fn a_note_links_its_staleness_to_the_thread() {
        let node = Node {
            id: 4,
            path: "rev/state".to_owned(),
            parent: Some(1),
            kind: NodeKind::Note,
            description: String::new(),
            creator: "alice".to_owned(),
            owner: None,
            generated: false,
            created: 10_010,
            archived: None,
        };
        let version = NoteVersion {
            path: node.path.clone(),
            version: 2,
            id: 10_040,
            ts: "2026-10-22T00:00:00Z".to_owned(),
            from: "alice".to_owned(),
            signed_by: Some("SHA256:alice".to_owned()),
            revoked_at: Some(10_050),
            thread: Some("rev/log".to_owned()),
            through: Some(10_021),
            behind: Some(2),
            body: HOSTILE.to_owned(),
        };
        let view = NoteView { node: &node, note: Some(&version), history: &[], current: true };
        let page = text(note(&view));
        assert!(page.contains("summarizes <a href=\"/view/rev/log\">/rev/log</a> through #10021, 2 behind"), "{page}");
        assert!(page.contains("key revoked at #10050") && page.contains(ESCAPED), "{page}");
        assert!(page.contains("<details data-needs=\"trusted\" hidden>"), "{page}");
        assert!(page.contains("name=\"base_version\" value=\"2\""), "{page}");
    }

    #[test]
    fn an_answered_item_shows_its_answer() {
        let mut h = Html::new();
        let answered = Item {
            id: 10_030,
            ts: String::new(),
            path: "rev/bob".to_owned(),
            owner: "bob".to_owned(),
            asker: None,
            state: ItemState::Answered,
            body: "q".to_owned(),
            archived: false,
            resolution: Some(ItemResolution {
                id: 10_031,
                event: EventType::ItemAnswered,
                from: Some("bob".to_owned()),
                body: HOSTILE.to_owned(),
                reference: Some(10_029),
            }),
        };
        item(&mut h, &answered, true);
        let page = text(h);
        assert!(page.contains("a registration") && page.contains("#10031 item_answered by bob · see #10029"), "{page}");
        assert!(page.contains(ESCAPED) && page.contains("<a href=\"/view/rev/bob\">"), "{page}");
    }

    #[test]
    fn a_breadcrumb_links_every_level() {
        let page = text(page("rev/emu/log", Some("rev/emu/log"), &Html::new(), &[]));
        assert!(
            page.contains(
                "<a href=\"/\">board</a> / <a href=\"/view/rev\">rev</a> / <a href=\"/view/rev/emu\">emu</a> / <a href=\"/view/rev/emu/log\">log</a>"
            ),
            "{page}"
        );
    }

    #[test]
    fn every_page_offers_registration_to_a_key_the_board_does_not_know() {
        let page = text(page_without_outline());
        assert!(page.contains("data-needs=\"unregistered\" hidden"), "{page}");
        assert!(page.contains("hx-post=\"/ui/register\" hx-target=\"#register-result\""), "{page}");
        for name in ["name", "repo", "note"] {
            assert!(page.contains(&format!("name=\"{name}\"")), "{page}");
        }
        let done = text(registered("<b>", "SHA256:x", 10_050));
        assert!(done.contains("&lt;b&gt;") && done.contains("#10050") && !done.contains("<b><"), "{done}");
    }

    #[test]
    fn the_authorize_dialog_has_its_place_to_explain_an_insecure_page() {
        let page = text(page_without_outline());
        assert!(page.contains("<p class=\"error\" id=\"auth-insecure\" hidden>"), "{page}");
        assert!(page.contains("tailscale serve --bg 8777") && page.contains("ssh -L 8777:127.0.0.1:8777"), "{page}");
        assert!(page.contains("id=\"auth-origin\"") && page.contains("id=\"auth-submit\""), "{page}");
    }

    #[test]
    fn the_create_form_posts_the_fields_the_signer_reads() {
        let form = text(create_form());
        assert!(form.contains("hx-post=\"/ui/create\"") && form.contains("data-needs=\"trusted\""), "{form}");
        for name in ["path", "description"] {
            assert!(form.contains(&format!("name=\"{name}\"")), "{form}");
        }
        for kind in ["container", "note", "thread", "queue"] {
            assert!(form.contains(&format!("<option>{kind}</option>")), "{form}");
        }
        let made = text(created("a/<b>", NodeKind::Thread));
        assert!(made.contains("a/&lt;b&gt;") && !made.contains("<b>"), "{made}");
    }

    #[test]
    fn the_page_follows_the_colour_scheme_through_custom_properties() {
        let page = text(page_without_outline());
        assert!(
            page.contains("color-scheme: light dark") && page.contains("@media (prefers-color-scheme: dark)"),
            "{page}"
        );
        assert!(page.contains("--bg:") && page.contains("body { background: var(--bg); }"), "{page}");
        let (_, rules) = page.split_once("body { background").unwrap_or(("", ""));
        for literal in ["#fff", "#e3c96b", "#fffbea", "#0b3d91", "#a00", "#666", "#ddd"] {
            assert!(!rules.contains(literal), "{literal} outside the properties");
        }
    }

    #[test]
    fn the_sidebar_prefixes_leaves_with_a_glyph_per_kind() {
        let outline = vec![
            ("rev".to_owned(), NodeKind::Container),
            ("rev/log".to_owned(), NodeKind::Thread),
            ("rev/state".to_owned(), NodeKind::Note),
            ("rev/q".to_owned(), NodeKind::Queue),
        ];
        let page = text(page("t", None, &Html::new(), &outline));
        let ico = |glyph: &str, path: &str| {
            format!("<a href=\"/view/{path}\" title=\"{path}\"><span class=\"ico\" aria-hidden=\"true\">{glyph}</span>")
        };
        assert!(page.contains(&ico("\u{270F}\u{FE0F}", "rev/log")), "{page}");
        assert!(page.contains(&ico("\u{1F4C4}", "rev/state")), "{page}");
        assert!(page.contains(&ico("\u{1F4E5}", "rev/q")), "{page}");
        assert!(page.contains("<a href=\"/view/rev\" title=\"rev\">rev</a>"), "{page}");
    }

    #[test]
    fn the_sidebar_folds_containers_and_opens_the_current_path() {
        let outline = vec![
            ("rev".to_owned(), NodeKind::Container),
            ("rev/emu".to_owned(), NodeKind::Container),
            ("rev/emu/log".to_owned(), NodeKind::Thread),
            ("rev/emu-x".to_owned(), NodeKind::Note),
            ("ops".to_owned(), NodeKind::Container),
            ("ops/a".to_owned(), NodeKind::Note),
        ];
        let page = text(page("t", Some("rev/emu/log"), &Html::new(), &outline));
        assert!(
            page.contains("<li class=\"tree-container tree-above\"><details open><summary><a href=\"/view/rev\""),
            "{page}"
        );
        assert!(
            page.contains("<li class=\"tree-container tree-above\"><details open><summary><a href=\"/view/rev/emu\""),
            "{page}"
        );
        assert!(page.contains("<li class=\"tree-container\"><details><summary><a href=\"/view/ops\""), "{page}");
        assert!(
            page.contains("<li class=\"tree-thread tree-on\"><a href=\"/view/rev/emu/log\" aria-current=\"page\""),
            "{page}"
        );
        assert!(!aside_of(&page).contains("class=\"note"), "bare note class");
        assert_eq!(page.matches("aria-current").count(), 1, "{page}");
        assert!(page.contains("<li class=\"tree-note\"><a href=\"/view/rev/emu-x\""), "{page}");
        assert_eq!(page.matches("<details").count(), page.matches("</details>").count(), "{page}");
        assert_eq!(page.matches("<li").count(), page.matches("</li>").count(), "{page}");
        let aside =
            page.split_once("<aside").and_then(|(_, rest)| rest.split_once("</aside>")).map_or("", |(aside, _)| aside);
        assert_eq!(aside.matches("<ul>").count(), aside.matches("</ul>").count(), "{aside}");
        // emu-x is a sibling of emu, not nested under it
        assert!(
            aside.find("</details></li>\n<li class=\"tree-note\"><a href=\"/view/rev/emu-x\"").is_some(),
            "{aside}"
        );
        let off = text(page_for_sidebar_test(Some("ops/a"), &outline));
        assert!(off.contains("<details><summary><a href=\"/view/rev\""), "{off}");
        assert!(off.contains("<details open><summary><a href=\"/view/ops\""), "{off}");
        assert!(page.contains("@media (min-width: 960px)") && page.contains("<main>"), "{page}");
        assert!(!text(page_without_outline()).contains("<aside"));
    }

    fn aside_of(page: &str) -> &str {
        page.split_once("<aside").and_then(|(_, rest)| rest.split_once("</aside>")).map_or("", |(aside, _)| aside)
    }

    fn page_for_sidebar_test(path: Option<&str>, outline: &[(String, NodeKind)]) -> Html {
        page("t", path, &Html::new(), outline)
    }

    fn page_without_outline() -> Html {
        page("t", None, &Html::new(), &[])
    }
}
