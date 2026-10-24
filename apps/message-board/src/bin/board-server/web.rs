//! The web view: the tree, read-only but for posting to a thread,
//! filing a request in a queue and editing a note; the owner's inbox (registrations, the owner's queues)
//! and keys pages. Its forms are signed in the browser by board-sign.js
//! and post the JSON API's fields, form-encoded, under `/ui/`; each
//! answers with the piece of page it changed.

use message_board::blob;
use message_board::store::{
    BlobQuery, Board, Error as StoreError, EventType, ItemPage, ItemQuery, Limits, Node, NodeKind, REGISTER_QUEUE,
    Range, StateFilter,
};

use crate::api;
use crate::html::{self, Html, Inbox, Links, NoteForm, NoteView};
use crate::http::Response;
use crate::routes::{Answer, Failure, Params};
use crate::writes::{self, Fields, Write};

/// How many messages a thread's page shows unless asked for another window.
const RECENT: i64 = 100;

/// The window on a thread that `fields` ask for (`last`, `since` or `all`,
/// as the API reads them): the newest [`RECENT`] unless they say otherwise.
fn window(fields: &Params) -> Result<Range, StoreError> {
    match api::range(fields)? {
        Range::Last(_) if fields.number("last")?.is_none() => Ok(Range::Last(RECENT)),
        other => Ok(other),
    }
}

/// How many messages before a permalinked one its window starts, when it is
/// older than the page's newest [`RECENT`].
const CONTEXT: i64 = 20;

/// How many items a queue page lists before a permalinked one without
/// starting its window late: well within what the board answers in one read.
const ITEMS_FREE: i64 = 1000;

/// `GET /m/<id>`: the page that shows event `id`, with it highlighted: a
/// message in its thread (in a window around it if it is not among the
/// newest), a note version as that version of the note, an item, or what
/// resolved it, in its queue (the archived ones shown if it is one), and any
/// other event at its node.
pub fn permalink(board: &Board, id: &str) -> Answer {
    let missing = || StoreError::NotFound(format!("no event #{id}: ids are the numbers on the board, from 10001"));
    let id: i64 = id.bytes().all(|b| b.is_ascii_digit()).then(|| id.parse().ok()).flatten().ok_or_else(missing)?;
    let mut event = board.event(id)?.ok_or_else(missing)?;
    // An archive event stands for what it archived.
    if let (EventType::NodeArchived | EventType::NodeUnarchived, Some(target)) = (event.event, event.reference) {
        event = board.event(target)?.unwrap_or(event);
    }
    let mut query: Vec<String> = Vec::new();
    let mut anchor = None;
    match event.event {
        EventType::Message => {
            if event.archived {
                query.push("archived=1".to_owned());
            }
            if let Some(since) = board.message_window(&event.path, event.id, event.archived, RECENT, CONTEXT)? {
                query.push(format!("since={since}"));
            }
            anchor = Some(event.id);
        }
        EventType::NoteVersion => {
            query.push(format!("version={}", event.version.unwrap_or_default()));
            anchor = Some(event.id);
        }
        EventType::ItemFiled
        | EventType::ItemAnswered
        | EventType::ItemDeclined
        | EventType::ItemCancelled
        | EventType::ItemReassigned => {
            let item = event.item.unwrap_or(event.id);
            if event.archived {
                query.push("archived=1".to_owned());
            }
            if let Some(after) = board.item_window(&event.path, item, event.archived, ITEMS_FREE, CONTEXT)? {
                query.push(format!("after={after}"));
            }
            query.push(format!("focus={item}"));
            anchor = Some(item);
        }
        EventType::NodeCreated | EventType::NodeArchived | EventType::NodeUnarchived => {}
    }
    let path: Vec<String> = event.path.split('/').map(blob::pct_encode).collect();
    let mut location = format!("/view/{}", path.join("/"));
    if !query.is_empty() {
        location.push('?');
        location.push_str(&query.join("&"));
    }
    let anchor = anchor.map_or_else(String::new, |anchor| format!("#m{anchor}"));
    Ok(Response::redirect(&format!("{location}{anchor}")))
}

/// A signed form of the web view, by the path it posts to under `/ui/`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Form {
    Create,
    Post,
    Note,
    Ask,
    Approve,
    Resolve,
    Revoke,
    Archive,
    Unarchive,
    Register,
}

impl Form {
    pub fn named(name: &str) -> Option<Self> {
        Some(match name {
            "create" => Self::Create,
            "post" => Self::Post,
            "note" => Self::Note,
            "ask" => Self::Ask,
            "approve" => Self::Approve,
            "resolve" => Self::Resolve,
            "revoke" => Self::Revoke,
            "archive" => Self::Archive,
            "unarchive" => Self::Unarchive,
            "register" => Self::Register,
            _ => return None,
        })
    }

    const fn write(self) -> Write {
        match self {
            Self::Create => Write::Create,
            Self::Post => Write::Post,
            Self::Note => Write::Note,
            Self::Ask => Write::File,
            Self::Approve => Write::Approve,
            Self::Resolve => Write::Resolve,
            Self::Revoke => Write::Revoke,
            Self::Archive => Write::Archive,
            Self::Unarchive => Write::Unarchive,
            Self::Register => Write::Register,
        }
    }
}

fn page(board: &Board, title: &str, path: Option<&str>, content: &Html) -> Result<Response, Failure> {
    Ok(Response::html(200, html::page(title, path, content, &board.outline(html::OUTLINE_MAX)?)))
}

/// A thread's messages as its page shows them, its blob references linked
/// where the blob is there.
fn messages_html(board: &Board, path: &str, range: Range, archived: bool) -> Result<Html, Failure> {
    let page = board.thread_with(path, range, archived)?;
    let links = Links::of(board, page.messages.iter().map(|m| m.body.as_str()))?;
    Ok(html::messages(path, &page, range, RECENT, archived, &links))
}

/// The bodies of items and their resolutions: where a page's blob references are.
fn item_bodies(page: &ItemPage) -> impl Iterator<Item = &str> {
    page.items.iter().flat_map(|i| std::iter::once(i.body.as_str()).chain(i.resolution.iter().map(|r| r.body.as_str())))
}

/// A queue's items as its page shows them.
fn queue_items_html(
    board: &Board,
    path: &str,
    after: i64,
    focus: Option<i64>,
    archived: bool,
) -> Result<Html, Failure> {
    let page = board.queue_with(path, StateFilter::All, after, archived)?;
    let links = Links::of(board, item_bodies(&page))?;
    Ok(html::queue_items(path, &page, focus, archived, &links))
}

fn fragment(content: Html) -> Response {
    Response::html(200, content)
}

/// `/` and `/view/<path>`: a node's page, by its kind; the top level for
/// an empty path.
pub fn view(board: &Board, path: &str, q: &Params) -> Answer {
    if path.is_empty() {
        let mut h = html::create_form();
        h.html(&html::children(&board.children("")?));
        return page(board, "tree", None, &h);
    }
    let node = board.node(path)?;
    let content = match node.kind {
        NodeKind::Container => {
            let mut h = html::heading(&node);
            h.html(&html::children(&board.children(path)?));
            h
        }
        NodeKind::Note => {
            let mut h = html::heading(&node);
            h.html(&html::swapped("note", &note_html(board, &node, q.number("version")?)?));
            h
        }
        NodeKind::Thread => {
            let range = window(q)?;
            let archived = q.flag("archived");
            let page = board.thread_with(path, range, archived)?;
            let links = Links::of(board, page.messages.iter().map(|m| m.body.as_str()))?;
            html::thread(&node, &page, range, RECENT, archived, &links)
        }
        NodeKind::Queue => {
            let archived = q.flag("archived");
            let after = q.number("after")?.unwrap_or(0);
            let page = board.queue_with(path, StateFilter::All, after, archived)?;
            let links = Links::of(board, item_bodies(&page))?;
            html::queue(&node, &page, after, q.number("focus")?, archived, &links)
        }
    };
    page(board, path, Some(path), &content)
}

/// A note as `#note` holds it: version `version` (the current one if
/// `None`), (the thread it summarizes, if any, is in the version).
fn note_html(board: &Board, node: &Node, version: Option<i64>) -> Result<Html, Failure> {
    let shown = board.note(&node.path, version)?;
    let history = board.note_history(&node.path)?;
    let current = match (&shown, history.last()) {
        (Some(shown), Some(last)) => shown.version == last.version,
        _ => true,
    };
    let links = Links::of(board, shown.iter().map(|n| n.body.as_str()))?;
    Ok(html::note(&NoteView { node, note: shown.as_ref(), history: &history, current, links: &links }))
}

/// Whether the page a form sat on shows what is archived (`shown=archived`),
/// so that its answer does too.
fn shown_archived(fields: &Params) -> bool {
    fields.get("shown") == Some("archived")
}

fn path(q: &Params) -> Result<&str, StoreError> {
    q.text("path")
}

/// `GET /ui/messages?path=`: a thread's messages, as its page refreshes them.
pub fn messages(board: &Board, q: &Params) -> Answer {
    let path = path(q)?;
    let range = window(q)?;
    let archived = q.flag("archived");
    Ok(fragment(messages_html(board, path, range, archived)?))
}

/// `GET /ui/note?path=`: the note, as it is now (an edit's way back).
pub fn note(board: &Board, q: &Params) -> Answer {
    Ok(fragment(note_html(board, &board.node(path(q)?)?, None)?))
}

/// The inbox's content: what waits for the owner.
fn inbox_html(board: &Board, after: i64) -> Result<Html, Failure> {
    let owner = board.owner()?;
    let registrations = board.registrations()?;
    let page = match &owner {
        Some(owner) => board.items(&ItemQuery {
            owner: Some(&owner.name),
            state: StateFilter::Open,
            after,
            ..ItemQuery::default()
        })?,
        None => ItemPage { items: Vec::new(), truncated: false },
    };
    let more_after = page.items.last().filter(|_| page.truncated).map(|item| item.id);
    // The register queue is the owner's too; its items are the registrations.
    let items: Vec<_> = page.items.into_iter().filter(|item| item.path != REGISTER_QUEUE).collect();
    let owner = owner.as_ref().map(|owner| owner.name.as_str());
    let links = Links::of(board, items.iter().map(|i| i.body.as_str()))?;
    Ok(html::inbox(&Inbox { owner, registrations: &registrations, items: &items, more_after, links: &links }))
}

/// `GET /ui/live/queue?path=&after=&archived=`: a queue's items, as its page
/// refreshes them: the archived ones only if its page shows them.
pub fn live_queue(board: &Board, q: &Params) -> Answer {
    let path = path(q)?;
    let archived = q.flag("archived");
    let after = q.number("after")?.unwrap_or(0);
    Ok(fragment(queue_items_html(board, path, after, q.number("focus")?, archived)?))
}

/// `GET /ui/live/inbox?after=`: the inbox, as its page refreshes it.
pub fn live_inbox(board: &Board, q: &Params) -> Answer {
    Ok(fragment(inbox_html(board, q.number("after")?.unwrap_or(0))?))
}

/// `GET /ui/live/keys`: the trust list, as its page refreshes it.
pub fn live_keys(board: &Board) -> Answer {
    Ok(fragment(keys_html(board)?))
}

/// `GET /ui/blobs?after=`: the blobs, newest upload first, a page at a time.
pub fn blobs(board: &Board, limits: &Limits, q: &Params) -> Answer {
    /// How many blobs a page lists.
    const PAGE: usize = 50;
    let listing = board.blobs(&BlobQuery {
        unreferenced: false,
        evictable: false,
        mine: None,
        limit: PAGE,
        after: q.get("after").filter(|after| !after.is_empty()),
        grace: limits.grace,
    })?;
    let referrers = listing.blobs.iter().map(|b| board.blob_referrers(&b.hash)).collect::<Result<Vec<_>, _>>()?;
    let rows: Vec<html::BlobRow<'_>> =
        listing.blobs.iter().zip(&referrers).map(|(info, referrers)| html::BlobRow { info, referrers }).collect();
    let more_after = listing.blobs.last().filter(|_| listing.truncated).map(|b| b.hash.as_str());
    let view = html::BlobsView {
        rows: &rows,
        stats: board.blob_stats()?,
        cap: limits.cap,
        grace: limits.grace,
        now: message_board::timefmt::unix_now().map_err(StoreError::from)?,
        more_after,
    };
    page(board, "blobs", None, &html::blobs(&view))
}

pub fn inbox(board: &Board, q: &Params) -> Answer {
    let after = q.number("after")?.unwrap_or(0);
    let url = format!("/ui/live/inbox?after={after}");
    page(board, "inbox", None, &html::live("inbox", &url, &inbox_html(board, after)?))
}

fn keys_html(board: &Board) -> Result<Html, Failure> {
    Ok(html::keys(&board.keys()?, &board.registrations()?, &board.blob_usage()?))
}

pub fn keys(board: &Board) -> Answer {
    page(board, "keys", None, &html::live("keys", "/ui/live/keys", &keys_html(board)?))
}

/// A signed form's write; the piece of page it changed, or the refusal
/// (which board-sign.js shows by the form). A note saved over a version
/// that came in between gets its editor back instead, with the editor's
/// text and that version as the base: saving again is then a deliberate
/// overwrite.
pub fn write(board: &Board, form: Form, fields: &Params) -> Answer {
    let outcome = match writes::apply(board, form.write(), fields) {
        Ok(outcome) => outcome,
        Err(Failure::Board(StoreError::NoteChanged { version })) => {
            let error = format!(
                "Someone saved v{version} while you were editing. Your text is below; save again to overwrite theirs."
            );
            return Ok(fragment(html::note_form(&NoteForm {
                path: path(fields)?,
                body: fields.text("body")?,
                base_version: version,
                thread: fields.text("thread")?,
                through: fields.number("through")?,
                error: &error,
            })));
        }
        Err(other) => return Err(other),
    };
    Ok(fragment(match form {
        Form::Create => {
            let kind = outcome.get("kind").and_then(|kind| kind.as_str()).and_then(|kind| kind.parse().ok());
            match (outcome.get("path").and_then(|path| path.as_str()), kind) {
                (Some(path), Some(kind)) => html::created(path, kind),
                _ => html::error_fragment("created, but the answer was unreadable"),
            }
        }
        Form::Post => {
            let path = path(fields)?;
            let range = window(fields)?;
            let archived = shown_archived(fields);
            messages_html(board, path, range, archived)?
        }
        Form::Note => note_html(board, &board.node(path(fields)?)?, None)?,
        Form::Ask => {
            let archived = shown_archived(fields);
            let path = path(fields)?;
            queue_items_html(board, path, 0, None, archived)?
        }
        // The message's or the item's list, as the page shows it.
        Form::Archive | Form::Unarchive => {
            let archived = fields.get("view") == Some("archived");
            let target =
                outcome.get("event").and_then(serde_json::Value::as_i64).and_then(|id| board.event(id).transpose());
            let target = target
                .ok_or_else(|| StoreError::BadRequest("archived, but the answer was unreadable".to_owned()))??;
            match target.kind {
                NodeKind::Queue => queue_items_html(board, &target.path, 0, None, archived)?,
                _ => messages_html(board, &target.path, Range::Last(RECENT), archived)?,
            }
        }
        // The page the form sat on gets its own piece back.
        Form::Resolve if fields.get("view") == Some("queue") => {
            let item = fields.number("item")?.and_then(|id| board.item(id).transpose()).transpose()?;
            match item {
                Some(detail) => {
                    let archived = shown_archived(fields);
                    let path = &detail.item.path;
                    queue_items_html(board, path, 0, None, archived)?
                }
                None => html::error_fragment("resolved, but the item cannot be found"),
            }
        }
        Form::Approve | Form::Resolve if fields.get("view") == Some("keys") => keys_html(board)?,
        Form::Approve | Form::Resolve => inbox_html(board, fields.number("after")?.unwrap_or(0))?,
        Form::Revoke => keys_html(board)?,
        Form::Register => match (outcome.get("name"), outcome.get("fingerprint"), outcome.get("item")) {
            (Some(name), Some(fingerprint), Some(item)) => match (name.as_str(), fingerprint.as_str(), item.as_i64()) {
                (Some(name), Some(fingerprint), Some(item)) => html::registered(name, fingerprint, item),
                _ => html::error_fragment("registered, but the answer was unreadable"),
            },
            _ => html::error_fragment("registered, but the answer was unreadable"),
        },
    }))
}
