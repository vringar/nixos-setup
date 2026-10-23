//! The web view: the tree, read-only but for posting to a thread,
//! filing a request in a queue and editing a note; the owner's inbox (registrations, the owner's queues)
//! and keys pages. Its forms are signed in the browser by board-sign.js
//! and post the JSON API's fields, form-encoded, under `/ui/`; each
//! answers with the piece of page it changed.

use message_board::store::{Board, Error as StoreError, ItemQuery, Node, NodeKind, REGISTER_QUEUE, Range, StateFilter};

use crate::html::{self, Html, Inbox, NoteForm, NoteView};
use crate::http::Response;
use crate::routes::{Answer, Failure, Params};
use crate::writes::{self, Fields, Write};

/// How many messages a thread's page shows.
const RECENT: i64 = 100;

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
            let archived = q.flag("archived");
            html::thread(&node, &board.thread_with(path, Range::Last(RECENT), archived)?, archived)
        }
        NodeKind::Queue => {
            let archived = q.flag("archived");
            html::queue(&node, &board.queue_with(path, StateFilter::All, 0, archived)?.items, archived)
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
    Ok(html::note(&NoteView { node, note: shown.as_ref(), history: &history, current }))
}

fn path(q: &Params) -> Result<&str, StoreError> {
    q.text("path")
}

/// `GET /ui/messages?path=`: a thread's messages, as its page refreshes them.
pub fn messages(board: &Board, q: &Params) -> Answer {
    let path = path(q)?;
    let archived = q.flag("archived");
    Ok(fragment(html::messages(path, &board.thread_with(path, Range::Last(RECENT), archived)?, archived)))
}

/// `GET /ui/note?path=`: the note, as it is now (an edit's way back).
pub fn note(board: &Board, q: &Params) -> Answer {
    Ok(fragment(note_html(board, &board.node(path(q)?)?, None)?))
}

/// The inbox's content: what waits for the owner.
fn inbox_html(board: &Board) -> Result<Html, Failure> {
    let owner = board.owner()?;
    let registrations = board.registrations()?;
    let items = match &owner {
        // The register queue is the owner's too; its items are the registrations.
        Some(owner) => board
            .items(&ItemQuery { owner: Some(&owner.name), state: StateFilter::Open, ..ItemQuery::default() })?
            .items
            .into_iter()
            .filter(|item| item.path != REGISTER_QUEUE)
            .collect(),
        None => Vec::new(),
    };
    let owner = owner.as_ref().map(|owner| owner.name.as_str());
    Ok(html::inbox(&Inbox { owner, registrations: &registrations, items: &items }))
}

pub fn inbox(board: &Board) -> Answer {
    page(board, "inbox", None, &html::swapped("inbox", &inbox_html(board)?))
}

fn keys_html(board: &Board) -> Result<Html, Failure> {
    Ok(html::keys(&board.keys()?, &board.registrations()?))
}

pub fn keys(board: &Board) -> Answer {
    page(board, "keys", None, &html::swapped("keys", &keys_html(board)?))
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
            html::messages(path, &board.thread(path, Range::Last(RECENT))?, false)
        }
        Form::Note => note_html(board, &board.node(path(fields)?)?, None)?,
        Form::Ask => html::queue_items(&board.queue(path(fields)?, StateFilter::All, 0)?.items, false),
        // The message's or the item's list, as the page shows it.
        Form::Archive | Form::Unarchive => {
            let archived = fields.get("view") == Some("archived");
            let target =
                outcome.get("event").and_then(serde_json::Value::as_i64).and_then(|id| board.event(id).transpose());
            let target = target
                .ok_or_else(|| StoreError::BadRequest("archived, but the answer was unreadable".to_owned()))??;
            match target.kind {
                NodeKind::Queue => {
                    html::queue_items(&board.queue_with(&target.path, StateFilter::All, 0, archived)?.items, archived)
                }
                _ => html::messages(
                    &target.path,
                    &board.thread_with(&target.path, Range::Last(RECENT), archived)?,
                    archived,
                ),
            }
        }
        // The page the form sat on gets its own piece back.
        Form::Resolve if fields.get("view") == Some("queue") => {
            let item = fields.number("item")?.and_then(|id| board.item(id).transpose()).transpose()?;
            match item {
                Some(detail) => html::queue_items(&board.queue(&detail.item.path, StateFilter::All, 0)?.items),
                None => html::error_fragment("resolved, but the item cannot be found"),
            }
        }
        Form::Approve | Form::Resolve if fields.get("view") == Some("keys") => keys_html(board)?,
        Form::Approve | Form::Resolve => inbox_html(board)?,
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
