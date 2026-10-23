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
    Child, Item, ItemState, Key, KeyState, Node, NodeKind, NoteVersion, Registration, Status, ThreadPage, Version,
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

const HEAD: &str = r#"</title>
<script src="/static/htmx.min.js"></script>
<script src="/static/board-sign.js" defer></script>
<style>
  body { font: 14px system-ui, sans-serif; max-width: 960px; margin: 1.5rem auto;
         padding: 0 1rem; color: #1a1a1a; }
  nav a { margin-right: .6rem; }
  .crumbs { color: #666; margin: .3rem 0 .8rem; }
  table { border-collapse: collapse; width: 100%; margin: .6rem 0; }
  th, td { text-align: left; vertical-align: top; padding: .3rem .5rem; border-bottom: 1px solid #eee; }
  th { font-weight: 600; color: #555; font-size: 12px; }
  code { font: 12px ui-monospace, monospace; word-break: break-all; }
  .meta { color: #666; font-size: 12px; }
  .body { white-space: pre-wrap; margin-top: .15rem; }
  .empty { color: #999; }
  .error { color: #a00; margin: 0 0 .4rem; }
  .flag { color: #8a5a00; margin: .2rem 0; }
  .revoked { color: #a00; }
  #messages { border: 1px solid #ddd; border-radius: 6px; padding: .5rem;
              margin: .8rem 0; max-height: 60vh; overflow-y: auto; }
  .msg, .item { padding: .4rem 0; border-bottom: 1px solid #f0f0f0; }
  .msg:last-child, .item:last-child { border-bottom: 0; }
  .note, .card { border: 1px solid #e3c96b; background: #fffbea; border-radius: 6px;
                 padding: .5rem .7rem; margin: .6rem 0; }
  .card { border-color: #ddd; background: none; }
  .resolution { margin: .3rem 0 0 1rem; }
  .controls form { display: inline-block; margin: .3rem .6rem .3rem 0; vertical-align: top; }
  form input, form textarea { box-sizing: border-box; margin: .2rem 0; }
  form textarea { width: 100%; min-height: 5rem; }
  #note textarea { min-height: 14rem; }
  .auth { float: right; font-size: 12px; color: #666; }
  dialog { width: min(36rem, 90vw); }
  dialog textarea { width: 100%; min-height: 10rem; font: 11px monospace; }
  .side { display: none; }
  .side ul { list-style: none; margin: 0; padding: 0; }
  .side li a { display: block; padding: .1rem .4rem; border-radius: 4px; color: #333;
               text-decoration: none; overflow-wrap: anywhere; }
  .side li a:hover { background: #f2f2f2; }
  .side li.container > a { font-weight: 600; }
  .side li.on > a { background: #e8f0fe; color: #0b3d91; }
  .side li.above > a { color: #0b3d91; }
  .side .more { color: #999; padding: .1rem .4rem; }
  @media (min-width: 960px) {
    body { max-width: none; margin: 1.5rem 1.5rem; }
    .shell { display: grid; grid-template-columns: 15rem minmax(0, 1fr); gap: 1.5rem; align-items: start; }
    .side { display: block; position: sticky; top: 1rem; max-height: calc(100vh - 2rem);
            overflow-y: auto; border-right: 1px solid #eee; padding-right: .5rem; }
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
  <p class="error" id="auth-error" hidden></p>
  <textarea name="privkey" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" required
            autocomplete="off" spellcheck="false"></textarea>
  <button type="submit">authorize</button>
  <button type="button" id="auth-cancel">cancel</button>
</form></dialog>
<h1>message board</h1>
<nav><a href="/">tree</a> <a href="/ui/inbox">inbox</a> <a href="/ui/keys">keys</a></nav>
"#;

/// A whole page: the layout around `content`, with a breadcrumb for the
/// node at `path`, if it is one.
/// How many nodes the sidebar lists; the rest are one click away in the tree.
pub const OUTLINE_MAX: usize = 300;

/// The tree as the sidebar shows it: one link per node, indented by depth,
/// with the node at `current` and the containers above it marked. Wide
/// screens show it; narrow ones hide it, and the "tree" link remains.
fn sidebar(h: &mut Html, outline: &[(String, NodeKind)], current: Option<&str>) {
    if outline.is_empty() {
        return;
    }
    h.markup("<aside class=\"side\"><nav aria-label=\"tree\"><ul>\n");
    for (path, kind) in outline {
        let depth = path.matches('/').count();
        let name = path.rsplit('/').next().unwrap_or(path);
        let on = current == Some(path.as_str());
        let above = current.is_some_and(|c| c.strip_prefix(path.as_str()).is_some_and(|rest| rest.starts_with('/')));
        h.markup("<li class=\"").markup(kind.as_str());
        if on {
            h.markup(" on");
        } else if above {
            h.markup(" above");
        }
        h.markup("\" style=\"padding-left:")
            .number(i64::try_from(depth).unwrap_or(0) * 12)
            .markup("px\"><a href=\"/view/")
            .text(path)
            .markup("\"");
        if on {
            h.markup(" aria-current=\"page\"");
        }
        h.markup(" title=\"").text(path).markup("\">").text(name).markup("</a></li>\n");
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

/// What a made node's form answers: a link to it.
pub fn created(path: &str, kind: NodeKind) -> Html {
    let mut h = Html::new();
    h.markup("<p>made the ").markup(kind.as_str()).markup(" ");
    link(&mut h, path);
    h.markup("</p>");
    h
}

/// A thread's messages, oldest first, with how many came before them.
pub fn messages(path: &str, page: &ThreadPage) -> Html {
    let mut h = Html::new();
    if page.earlier > 0 {
        h.markup("<p class=\"meta\">");
        plural(&mut h, page.earlier, "earlier message is", "earlier messages are");
        h.markup(" not shown: <code>board read ").text(path).markup(" --all</code></p>\n");
    }
    if page.messages.is_empty() {
        h.markup("<p class=\"empty\">no messages yet</p>");
    }
    for m in &page.messages {
        h.markup("<div class=\"msg\"><div class=\"meta\">#").number(m.id).markup(" · ");
        author(&mut h, &m.from, m.signed_by.as_deref(), m.revoked_at);
        h.markup(" · ").text(&m.ts).markup("</div><div class=\"body\">").text(&m.body).markup("</div></div>\n");
    }
    h
}

/// A thread: its messages, refreshed every 3 s, and the form to post.
pub fn thread(node: &Node, page: &ThreadPage) -> Html {
    let mut h = heading(node);
    h.markup("<div id=\"messages\" hx-get=\"/ui/messages?path=")
        .text(&encoded(&node.path))
        .markup("\" hx-trigger=\"every 3s\" hx-swap=\"innerHTML\">")
        .html(&messages(&node.path, page))
        .markup("</div>\n");
    if !node.generated {
        h.markup("<div data-needs=\"trusted\" hidden>\n<form hx-post=\"/ui/post\" hx-target=\"#messages\" hx-swap=\"innerHTML\">\n");
        form_error(&mut h, "");
        hidden_input(&mut h, "path", &node.path);
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
    h.markup(" · ").text(&item.ts).markup(" · ").text(item.state.as_str()).markup("</div>\n");
    h.markup("<div class=\"body\">").text(&item.body).markup("</div>\n");
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

/// A queue: its open items oldest first, and the resolved ones folded away.
pub fn queue(node: &Node, items: &[Item]) -> Html {
    let mut h = heading(node);
    let (open, resolved): (Vec<&Item>, Vec<&Item>) = items.iter().partition(|i| i.state == ItemState::Open);
    if open.is_empty() {
        h.markup("<p class=\"empty\">nothing open</p>\n");
    }
    for i in open {
        h.markup("<div class=\"item\">");
        item(&mut h, i, false);
        h.markup("</div>\n");
    }
    if !resolved.is_empty() {
        h.markup("<details><summary>");
        plural(&mut h, i64::try_from(resolved.len()).unwrap_or(i64::MAX), "resolved item", "resolved items");
        h.markup("</summary>\n");
        for i in resolved {
            h.markup("<div class=\"item\">");
            item(&mut h, i, false);
            h.markup("</div>\n");
        }
        h.markup("</details>\n");
    }
    h
}

/// A form that resolves `item` as `state`, with the reason asked for;
/// an answer may also name the message that holds it.
fn resolve_form(h: &mut Html, item: i64, state: ItemState) {
    h.markup("<form hx-post=\"/ui/resolve\" hx-target=\"#inbox\" hx-swap=\"innerHTML\">\n");
    form_error(h, "");
    hidden_input(h, "item", &item.to_string());
    hidden_input(h, "state", state.as_str());
    if state == ItemState::Answered {
        h.markup("<textarea name=\"body\" placeholder=\"the answer\" required></textarea>\n");
        h.markup("<input name=\"ref\" inputmode=\"numeric\" size=\"8\" placeholder=\"message #\">\n");
        h.markup("<button type=\"submit\">Answer</button>\n");
    } else {
        h.markup(
            "<input name=\"body\" placeholder=\"why decline\" required>\n<button type=\"submit\">Decline</button>\n",
        );
    }
    h.markup("</form>\n");
}

/// One registration: the key, what it says it is for, what approving it
/// would do, and the owner's two answers.
fn registration(h: &mut Html, r: &Registration) {
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
    h.markup("<div class=\"controls\" data-needs=\"owner\" hidden>\n<form hx-post=\"/ui/approve\" hx-target=\"#inbox\" hx-swap=\"innerHTML\">\n");
    form_error(h, "");
    hidden_input(h, "item", &r.item.to_string());
    hidden_input(h, "name", &r.name);
    hidden_input(h, "fingerprint", &r.fingerprint);
    h.markup("<button type=\"submit\">Approve</button>\n</form>\n");
    resolve_form(h, r.item, ItemState::Declined);
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
}

/// The owner's inbox, the content of `#inbox`: the registrations waiting,
/// by the name they ask for, then the open items in the owner's queues.
pub fn inbox(v: &Inbox<'_>) -> Html {
    let mut h = Html::new();
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
            registration(&mut h, r);
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
            resolve_form(&mut h, i.id, ItemState::Answered);
            resolve_form(&mut h, i.id, ItemState::Declined);
            h.markup("</div></div>\n");
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
/// with Revoke for each trusted one.
pub fn keys(keys: &[Key]) -> Html {
    let mut h = Html::new();
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
            resolution: None,
        };
        let page = text(inbox(&Inbox { owner: Some("Stefan"), registrations: &registrations, items: &[item] }));
        assert!(!page.contains(HOSTILE) && page.contains(ESCAPED), "{page}");
        assert!(page.contains("replaces <code>SHA256:old</code>"), "{page}");
        assert!(page.contains("2 keys ask for this name"), "{page}");
        assert_eq!(page.matches("<h3>alice</h3>").count(), 1, "{page}");
        // every form sits in a control the page shows only to the owner key
        assert_eq!(page.matches("<form").count(), 6, "{page}");
        assert_eq!(page.matches("data-needs=\"owner\" hidden").count(), 3, "{page}");
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
    fn the_sidebar_marks_the_current_page_and_its_ancestors() {
        let outline = vec![
            ("rev".to_owned(), NodeKind::Container),
            ("rev/emu".to_owned(), NodeKind::Container),
            ("rev/emu/log".to_owned(), NodeKind::Thread),
            ("rev/emu-x".to_owned(), NodeKind::Note),
        ];
        let page = text(page("t", Some("rev/emu/log"), &Html::new(), &outline));
        assert!(page.contains("<li class=\"container above\""), "{page}");
        assert!(page.contains("<li class=\"thread on\" style=\"padding-left:24px\"><a href=\"/view/rev/emu/log\" aria-current=\"page\""), "{page}");
        assert!(page.contains("<li class=\"note\""), "{page}");
        assert!(page.contains("@media (min-width: 960px)") && page.contains("<main>"), "{page}");
        assert!(!text(page_without_outline()).contains("<aside"));
    }

    fn page_without_outline() -> Html {
        page("t", None, &Html::new(), &[])
    }
}
