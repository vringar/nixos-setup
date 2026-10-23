//! The web view: the tree, read-only but for posting to a thread and
//! editing a note; the owner's inbox (registrations, the owner's queues)
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
    Post,
    Note,
    Approve,
    Resolve,
    Revoke,
}

impl Form {
    pub fn named(name: &str) -> Option<Self> {
        Some(match name {
            "post" => Self::Post,
            "note" => Self::Note,
            "approve" => Self::Approve,
            "resolve" => Self::Resolve,
            "revoke" => Self::Revoke,
            _ => return None,
        })
    }

    const fn write(self) -> Write {
        match self {
            Self::Post => Write::Post,
            Self::Note => Write::Note,
            Self::Approve => Write::Approve,
            Self::Resolve => Write::Resolve,
            Self::Revoke => Write::Revoke,
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
        return page(board, "tree", None, &html::children(&board.children("")?));
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
        NodeKind::Thread => html::thread(&node, &board.thread(path, Range::Last(RECENT))?),
        NodeKind::Queue => html::queue(&node, &board.queue(path, StateFilter::All, 0)?.items),
    };
    page(board, path, Some(path), &content)
}

/// A note as `#note` holds it: version `version` (the current one if
/// `None`), and the thread its summary is measured against.
fn note_html(board: &Board, node: &Node, version: Option<i64>) -> Result<Html, Failure> {
    let shown = board.note(&node.path, version)?;
    let thread = match shown.as_ref().and_then(|n| n.through) {
        Some(through) => board.event(through)?.map(|event| event.path),
        None => None,
    };
    let history = board.note_history(&node.path)?;
    let current = match (&shown, history.last()) {
        (Some(shown), Some(last)) => shown.version == last.version,
        _ => true,
    };
    Ok(html::note(&NoteView { node, note: shown.as_ref(), thread: thread.as_deref(), history: &history, current }))
}

fn path(q: &Params) -> Result<&str, StoreError> {
    q.text("path")
}

/// `GET /ui/messages?path=`: a thread's messages, as its page refreshes them.
pub fn messages(board: &Board, q: &Params) -> Answer {
    let path = path(q)?;
    Ok(fragment(html::messages(path, &board.thread(path, Range::Last(RECENT))?)))
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

pub fn keys(board: &Board) -> Answer {
    page(board, "keys", None, &html::swapped("keys", &html::keys(&board.keys()?)))
}

/// A signed form's write; the piece of page it changed, or the refusal
/// (which board-sign.js shows by the form). A note saved over a version
/// that came in between gets its editor back instead, with the editor's
/// text and that version as the base: saving again is then a deliberate
/// overwrite.
pub fn write(board: &Board, form: Form, fields: &Params) -> Answer {
    match writes::apply(board, form.write(), fields) {
        Ok(_) => {}
        Err(Failure::Board(StoreError::NoteChanged { version })) => {
            let error = format!(
                "Someone saved v{version} while you were editing. Your text is below; save again to overwrite theirs."
            );
            return Ok(fragment(html::note_form(&NoteForm {
                path: path(fields)?,
                body: fields.text("body")?,
                base_version: version,
                through: fields.number("through")?,
                error: &error,
            })));
        }
        Err(other) => return Err(other),
    }
    Ok(fragment(match form {
        Form::Post => {
            let path = path(fields)?;
            html::messages(path, &board.thread(path, Range::Last(RECENT))?)
        }
        Form::Note => note_html(board, &board.node(path(fields)?)?, None)?,
        Form::Approve | Form::Resolve => inbox_html(board)?,
        Form::Revoke => html::keys(&board.keys()?),
    }))
}
