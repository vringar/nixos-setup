//! The web view's HTML, escaped by construction.
//!
//! [`Html`] can only be built from string literals (markup written here)
//! and from text, which is always escaped, so nothing a message, pin, name
//! or area brings in can become markup. htmx and board-sign.js depend on
//! the pages' structure: the element ids, the classes and the hx-
//! attributes.

use message_board::store::{Area, Message, Pin};

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

    /// Another piece of HTML, as it is.
    pub fn html(&mut self, other: &Html) -> &mut Self {
        self.0.push_str(&other.0);
        self
    }

    pub fn into_bytes(self) -> Vec<u8> {
        self.0.into_bytes()
    }
}

/// What a page shows.
pub struct Page<'a> {
    pub area: &'a str,
    pub topic: &'a str,
    pub show_archived: bool,
    pub live: &'a [Area],
    pub archived: &'a [Area],
    /// When the area on show was archived, if it is.
    pub archived_ts: Option<&'a str>,
    pub topics: &'a [String],
    pub pins: Html,
    pub rows: Html,
    pub sender: &'a str,
}

const HEAD: &str = r#"</title>
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
  "#;

/// The archive (or unarchive) button for an area.
fn archive_form(area: &str, archived: bool) -> Html {
    let mut h = Html::new();
    h.markup("<form class=\"inline\" method=\"post\" action=\"/ui/archive\">\n  <input type=\"hidden\" name=\"area\" value=\"")
        .text(area)
        .markup("\">\n  <input type=\"hidden\" name=\"archived\" value=\"")
        .markup(if archived { "0" } else { "1" })
        .markup("\">\n  <button type=\"submit\">")
        .markup(if archived { "unarchive" } else { "archive area" })
        .markup("</button></form>");
    h
}

/// A nav link to an area or topic, marked when it is the one on show.
fn nav_link(h: &mut Html, area: &str, topic: Option<&str>, label: &str, current: bool) {
    h.markup("<a href=\"/?area=").text(&encoded(area));
    if let Some(topic) = topic {
        h.markup("&topic=").text(&encoded(topic));
    }
    h.markup("\"\n    ");
    if current {
        h.markup("class=\"current\"");
    }
    h.markup(">").text(label).markup("</a>");
}

pub fn page(v: &Page<'_>) -> Html {
    let mut h = Html::new();
    h.markup("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>board");
    if !v.area.is_empty() {
        h.markup(" · ").text(v.area);
    }
    h.markup(HEAD);
    for a in v.live {
        nav_link(&mut h, &a.name, None, &a.name, a.name == v.area);
    }
    h.markup("\n  ");
    if !v.archived.is_empty() {
        h.markup("<span class=\"muted\">·\n    <a href=\"/?show=archived\">archived (")
            .text(&v.archived.len().to_string())
            .markup(")</a></span>");
    }
    h.markup("</nav>\n");
    if v.show_archived && !v.archived.is_empty() {
        h.markup("<nav class=\"muted\" hx-boost:inherited=\"true\">archived:\n  ");
        for a in v.archived {
            h.markup("<a href=\"/?area=").text(&encoded(&a.name)).markup("\">").text(&a.name).markup("</a>");
        }
        h.markup("</nav>");
    }
    h.markup("\n");
    if !v.area.is_empty() {
        h.markup("<nav hx-boost:inherited=\"true\">topics: ");
        nav_link(&mut h, v.area, None, "all", v.topic.is_empty());
        h.markup("\n  ");
        for t in v.topics {
            nav_link(&mut h, v.area, Some(t), t, *t == v.topic);
        }
        h.markup("</nav>");
    }
    h.markup("\n\n<h2>");
    if v.area.is_empty() {
        h.markup("all areas");
    } else {
        h.text(v.area);
    }
    if !v.topic.is_empty() {
        h.markup(" / ").text(v.topic);
    }
    h.markup("\n  ");
    let archived = v.archived_ts.is_some();
    if !v.area.is_empty() && v.area != message_board::store::SYSTEM_AREA && !archived {
        h.html(&archive_form(v.area, archived));
    }
    h.markup("</h2>\n");
    if let Some(since) = v.archived_ts {
        h.markup("<div class=\"archived\">Archived since ")
            .text(since)
            .markup(" — hidden from\n  the area list and from reads that don't ask for archived areas. Posting here\n  or editing a pin brings it back. ")
            .html(&archive_form(v.area, archived))
            .markup("</div>");
    }
    h.markup("\n")
        .html(&v.pins)
        .markup("\n<div id=\"messages\" hx-get=\"/ui/messages?area=")
        .text(&encoded(v.area))
        .markup("&topic=")
        .text(&encoded(v.topic))
        .markup("\"\n     hx-trigger=\"every 3s\" hx-swap=\"innerHTML\">")
        .html(&v.rows)
        .markup("</div>\n<h3>post a message</h3>\n<form hx-post=\"/ui/post\" hx-target=\"#messages\" hx-swap=\"innerHTML\">\n  <p class=\"error\" hidden></p>\n  <div class=\"row\">\n    <input name=\"area\" placeholder=\"area\" value=\"")
        .text(v.area)
        .markup("\" required>\n    <input name=\"topic\" placeholder=\"topic\" value=\"")
        .text(if v.topic.is_empty() { "general" } else { v.topic })
        .markup("\" required>\n    <input name=\"from\" placeholder=\"from\" value=\"")
        .text(v.sender)
        .markup("\" required>\n  </div>\n  <textarea name=\"body\" placeholder=\"markdown body\" required></textarea>\n  <button type=\"submit\">post</button>\n</form></body></html>");
    h
}

/// The signer's mark after a verified sender or editor.
fn verified_mark(h: &mut Html, signed_by: Option<&str>) {
    if let Some(fingerprint) = signed_by {
        h.markup(" <span class=\"verified\" title=\"signed by ").text(fingerprint).markup("\">✓</span>");
    }
}

/// The message list.
pub fn rows(messages: &[Message]) -> Html {
    let mut h = Html::new();
    if messages.is_empty() {
        h.markup("<p class=\"empty\">no messages yet</p>");
    }
    for m in messages {
        h.markup("<div class=\"msg\">\n<span class=\"meta\">#")
            .text(&m.id.to_string())
            .markup("\n<a href=\"/?area=")
            .text(&encoded(&m.area))
            .markup("&topic=")
            .text(&encoded(&m.topic))
            .markup("\">[")
            .text(&m.area)
            .markup("/")
            .text(&m.topic)
            .markup("]</a>\n")
            .text(&m.sender);
        verified_mark(&mut h, m.signed_by.as_deref());
        h.markup(" · ").text(&m.ts).markup("</span>\n<div class=\"body\">").text(&m.body).markup("</div></div>\n");
    }
    h
}

/// A pin box's own query string, so it can fetch itself (cancel) or its
/// edit form without knowing what page it sits on. It is text, not
/// markup: its `&` is escaped like any other.
fn pin_query(area: &str, topic: &str) -> String {
    format!("area={}&topic={}", encoded(area), encoded(topic))
}

/// "area" or "area/topic".
fn place(area: &str, topic: &str) -> String {
    if topic.is_empty() { area.to_owned() } else { format!("{area}/{topic}") }
}

/// One pin box, or the button to pin a note where there is none.
pub fn pin(area: &str, topic: &str, pin: Option<&Pin>) -> Html {
    let mut h = Html::new();
    h.markup("\n\n");
    if let Some(p) = pin {
        h.markup("<div class=\"pin\">\n<div class=\"meta\">pinned to ")
            .text(&place(area, topic))
            .markup(" · v")
            .text(&p.version.to_string())
            .markup(" · ")
            .text(&p.editor);
        verified_mark(&mut h, p.signed_by.as_deref());
        h.markup(" · ")
            .text(&p.ts)
            .markup("\n<button hx-get=\"/ui/pin/edit?")
            .text(&pin_query(area, topic))
            .markup("\" hx-target=\"closest .pin\" hx-swap=\"outerHTML\">edit</button></div>\n<div class=\"body\">")
            .text(&p.body)
            .markup("</div></div>\n");
    } else {
        h.markup("<div class=\"pin missing\">\n<button hx-get=\"/ui/pin/edit?")
            .text(&pin_query(area, topic))
            .markup("\" hx-target=\"closest .pin\" hx-swap=\"outerHTML\">pin a note to ")
            .text(&place(area, topic))
            .markup("</button></div>\n");
    }
    h
}

/// What a pin editor shows.
pub struct PinForm<'a> {
    pub area: &'a str,
    pub topic: &'a str,
    pub sender: &'a str,
    pub body: &'a str,
    pub base_version: i64,
    pub error: &'a str,
}

/// The pin editor.
pub fn pin_form(f: &PinForm<'_>) -> Html {
    let mut h = Html::new();
    h.markup("\n<form class=\"pin\" hx-post=\"/ui/pin\" hx-target=\"this\" hx-swap=\"outerHTML\">\n<p class=\"error\"");
    if f.error.is_empty() {
        h.markup(" hidden");
    }
    h.markup(">")
        .text(f.error)
        .markup("</p>\n<input type=\"hidden\" name=\"area\" value=\"")
        .text(f.area)
        .markup("\">\n<input type=\"hidden\" name=\"topic\" value=\"")
        .text(f.topic)
        .markup("\">\n<input type=\"hidden\" name=\"base_version\" value=\"")
        .text(&f.base_version.to_string())
        .markup("\">\n<input name=\"from\" placeholder=\"from\" value=\"")
        .text(f.sender)
        .markup("\" required>\n<textarea name=\"body\" placeholder=\"markdown; leave empty to remove the pin\">")
        .text(f.body)
        .markup("</textarea>\n<button type=\"submit\">save</button>\n<button type=\"button\" hx-get=\"/ui/pin?")
        .text(&pin_query(f.area, f.topic))
        .markup("\" hx-target=\"closest .pin\" hx-swap=\"outerHTML\">cancel</button>\n</form>");
    h
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_is_escaped_and_markup_is_not() {
        let mut h = Html::new();
        h.markup("<b>").text("<script>alert('x' & \"y\")</script>").markup("</b>");
        assert_eq!(
            String::from_utf8_lossy(&h.into_bytes()),
            "<b>&lt;script&gt;alert(&#39;x&#39; &amp; &#34;y&#34;)&lt;/script&gt;</b>"
        );
    }

    #[test]
    fn a_pin_box_escapes_its_own_query() {
        let html = String::from_utf8_lossy(&pin("a b", "t&u", None).into_bytes()).into_owned();
        assert!(html.contains("hx-get=\"/ui/pin/edit?area=a+b&amp;topic=t%26u\""), "{html}");
        assert!(html.contains("pin a note to a b/t&amp;u"), "{html}");
    }
}
