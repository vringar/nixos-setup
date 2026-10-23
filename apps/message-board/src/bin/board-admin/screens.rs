//! The screens, and how each one is read from the board: the "model" of
//! the Elm-style structure (see `app.rs` for the whole of it).
//!
//! One screen is up at a time. Every screen is read fresh from the
//! database when it is shown, so after a deletion what is on the screen is
//! what is in the file. A list shown on the way in starts on its first
//! row; shown again on the way back, it starts on the row that was left
//! (or the next one, if that was just deleted).

use std::fmt::Display;

use message_board::store::{Board, Child, Item, Message, NodeKind, Range, StateFilter, Status};

use crate::Failure;
use crate::pager::Pager;
use crate::picker::{Picker, reselect};

pub enum Screen {
    /// The children of a container; the top level for the empty path.
    Tree { path: String, picker: Picker<Child> },
    /// A note's newest version.
    Note { path: String, pager: Pager },
    /// A thread's messages, oldest first.
    Thread { path: String, picker: Picker<Message> },
    /// One message, in full.
    Message { message: Message, pager: Pager },
    /// A queue's items, in every state, oldest first.
    Queue { path: String, picker: Picker<Item> },
    /// One item, and every event about it.
    Item { path: String, id: i64, pager: Pager },
    /// Deleting a node and everything in it: what will go, one line per
    /// node, and the path to type out to confirm it.
    ConfirmBranch { path: String, nodes: Vec<String>, typed: String },
    /// Deleting one message.
    ConfirmMessage { message: Message },
}

/// The container `path` is in: `""` (the top level) for a top-level one.
pub fn parent(path: &str) -> &str {
    path.rsplit_once('/').map_or("", |(parent, _)| parent)
}

/// `n` of something, singular or plural: "1 child", "3 children". For
/// any integer type: the store counts in `i64`, a `Vec` in `usize`.
fn count<N: Copy + Display + PartialEq + From<u8>>(n: N, one: &str, many: &str) -> String {
    if n == N::from(1) { format!("{n} {one}") } else { format!("{n} {many}") }
}

/// The first line of a body, cut to 80 characters, for a list row.
fn first_line(body: &str) -> String {
    body.trim_start().lines().next().unwrap_or_default().chars().take(80).collect()
}

/// Who wrote an event: its name, else the key (a registration's key has
/// no name yet), else the board itself.
fn who(from: Option<&str>, signed_by: Option<&str>) -> String {
    match (from, signed_by) {
        (Some(name), _) => name.to_owned(),
        (None, Some(fingerprint)) => format!("key {fingerprint}"),
        (None, None) => "the board".to_owned(),
    }
}

/// How an event was signed, and whether the key has been revoked since.
fn signature(signed_by: Option<&str>, revoked_at: Option<i64>) -> String {
    let revoked = revoked_at.map(|at| format!(" (key revoked at #{at})")).unwrap_or_default();
    match signed_by {
        Some(fingerprint) => format!("signed by {fingerprint}{revoked}"),
        None => "the board's own".to_owned(),
    }
}

/// How a child stands, as its listing row says it.
fn status(status: &Status) -> String {
    match status {
        Status::Container { children } => count(*children, "child", "children"),
        Status::Note { version: 0, .. } => "no content yet".to_owned(),
        Status::Note { version, ts, from, through, behind } => {
            let by = from.as_deref().unwrap_or("?");
            let at = ts.as_deref().unwrap_or("?");
            let through = through.map(|id| format!(", through #{id}, {} behind", behind.unwrap_or(0)));
            format!("v{version} by {by} at {at}{}", through.unwrap_or_default())
        }
        Status::Thread { messages: 0, .. } => "no messages".to_owned(),
        Status::Thread { last_id, last_ts, messages } => format!(
            "{}, the last #{} at {}",
            count(*messages, "message", "messages"),
            last_id.unwrap_or(0),
            last_ts.as_deref().unwrap_or("?")
        ),
        Status::Queue { owner, open: 0, .. } => format!("{owner}'s, none open"),
        Status::Queue { owner, open, oldest_open_ts } => {
            format!("{owner}'s, {open} open since {}", oldest_open_ts.as_deref().unwrap_or("?"))
        }
    }
}

/// A container's children, `came_from` selected if it is one of them.
///
/// # Errors
/// If the container cannot be read.
pub fn tree(board: &Board, path: &str, came_from: Option<&str>) -> Result<Screen, Failure> {
    let children = board.children(path)?;
    let selected = reselect(children.iter().map(|child| child.path.as_str()), came_from);
    // Paths padded to the longest, so that the kinds line up in a column.
    let width = children.iter().map(|child| child.path.chars().count()).max().unwrap_or(0);
    let row = |child: &Child| {
        let archived = if child.archived.is_some() { "[archived] " } else { "" };
        let description =
            if child.description.is_empty() { String::new() } else { format!("  -- {}", child.description) };
        format!("{:<width$}  {:<9}  {archived}{}{description}", child.path, child.kind.as_str(), status(&child.status))
    };
    let (title, footer) = if path.is_empty() {
        ("board-admin -- the board".to_owned(), "enter: open    d: delete it and everything in it    q: quit")
    } else {
        (format!("board-admin -- {path}"), "enter: open    d: delete it and everything in it    q: back")
    };
    Ok(Screen::Tree { path: path.to_owned(), picker: Picker::new(title, children, row, footer, selected) })
}

/// The screen a child opens into: a container's children, or the leaf.
///
/// # Errors
/// If it cannot be read.
pub fn open(board: &Board, child: &Child) -> Result<Screen, Failure> {
    match child.kind {
        NodeKind::Container => tree(board, &child.path, None),
        NodeKind::Note => note(board, child),
        NodeKind::Thread => thread(board, &child.path, None),
        NodeKind::Queue => queue(board, &child.path, None),
    }
}

fn note(board: &Board, child: &Child) -> Result<Screen, Failure> {
    let path = &child.path;
    let mut lines = Vec::new();
    if !child.description.is_empty() {
        lines.push(child.description.clone());
    }
    match board.note(path, None)? {
        None => lines.push(format!("made by {}; no version yet", child.creator)),
        Some(note) => {
            let signed = signature(note.signed_by.as_deref(), note.revoked_at);
            lines.push(format!("v{} by {} at {}, {signed}", note.version, note.from, note.ts));
            if let Some(through) = note.through {
                let behind = count(note.behind.unwrap_or(0), "message", "messages");
                lines.push(format!("summarizes its thread through #{through}; {behind} since"));
            }
            lines.push(String::new());
            lines.extend(note.body.lines().map(str::to_owned));
        }
    }
    let pager = Pager::new(format!("board-admin -- {path} (note)"), lines, "j/k: scroll    q: back");
    Ok(Screen::Note { path: path.clone(), pager })
}

/// A thread's messages, `came_from` selected (or the one after it).
///
/// # Errors
/// If the thread cannot be read.
pub fn thread(board: &Board, path: &str, came_from: Option<i64>) -> Result<Screen, Failure> {
    // All of it: an operator looking for what to delete needs every row,
    // not the tail `board read` gives.
    let messages = board.thread(path, Range::All)?.messages;
    let selected = reselect(messages.iter().map(|message| message.id), came_from);
    let title = format!("board-admin -- {path} (thread, {})", count(messages.len(), "message", "messages"));
    let row = |m: &Message| format!("#{}  {}  {}: {}", m.id, m.ts, m.from, first_line(&m.body));
    let picker = Picker::new(title, messages, row, "enter: read a message    q: back", selected);
    Ok(Screen::Thread { path: path.to_owned(), picker })
}

/// One message, to read and maybe delete.
pub fn message(message: Message) -> Screen {
    let signed = signature(message.signed_by.as_deref(), message.revoked_at);
    let mut lines = vec![format!("from {} at {}, {signed}", message.from, message.ts), String::new()];
    lines.extend(message.body.lines().map(str::to_owned));
    let title = format!("board-admin -- {} #{}", message.path, message.id);
    let pager = Pager::new(title, lines, "d: delete this message    j/k: scroll    q: back");
    Screen::Message { message, pager }
}

/// A queue's items, `came_from` selected.
///
/// # Errors
/// If the queue cannot be read.
pub fn queue(board: &Board, path: &str, came_from: Option<i64>) -> Result<Screen, Failure> {
    let owner = board.node(path)?.owner.unwrap_or_default();
    let items = board.queue(path, StateFilter::All, 0)?.items;
    let selected = reselect(items.iter().map(|item| item.id), came_from);
    let title = format!("board-admin -- {path} ({owner}'s queue, {})", count(items.len(), "item", "items"));
    let row = |item: &Item| {
        let asker = item.asker.as_deref().unwrap_or("a new key");
        format!("#{}  {}  {:<10}  {asker}: {}", item.id, item.ts, item.state.as_str(), first_line(&item.body))
    };
    let picker = Picker::new(title, items, row, "enter: read an item    q: back", selected);
    Ok(Screen::Queue { path: path.to_owned(), picker })
}

/// One item of the queue at `path`, and every event about it.
///
/// # Errors
/// If it cannot be read, and if it is gone: it was in the list a moment
/// ago, and with the service down nothing else should be writing.
pub fn item(board: &Board, path: &str, id: i64) -> Result<Screen, Failure> {
    let detail = board
        .item(id)?
        .ok_or_else(|| Failure::Exit(format!("item #{id} is gone: something else is writing to the database")))?;
    let item = &detail.item;
    let asker = item.asker.as_deref().unwrap_or("a new key");
    let mut lines = vec![format!("asked by {asker}, for {}: {}", item.owner, item.state)];
    for event in &detail.events {
        let by = who(event.from.as_deref(), event.signed_by.as_deref());
        let reference = event.reference.map(|id| format!(", ref #{id}")).unwrap_or_default();
        lines.push(String::new());
        lines.push(format!("#{}  {}  {} by {by}{reference}", event.id, event.ts, event.event));
        lines.extend(event.body.lines().map(|line| format!("    {line}")));
    }
    let pager = Pager::new(format!("board-admin -- {path} #{id}"), lines, "j/k: scroll    q: back");
    Ok(Screen::Item { path: path.to_owned(), id, pager })
}

/// `child`, and everything under it after it, each parent before its
/// children.
fn branch(board: &Board, child: Child, nodes: &mut Vec<Child>) -> Result<(), Failure> {
    let container = (child.kind == NodeKind::Container).then(|| child.path.clone());
    nodes.push(child);
    if let Some(path) = container {
        for grandchild in board.children(&path)? {
            branch(board, grandchild, nodes)?;
        }
    }
    Ok(())
}

/// The question before `child` is deleted with everything in it, listing
/// all that goes: each node, and what it holds.
///
/// # Errors
/// If the branch cannot be read.
pub fn confirm_branch(board: &Board, child: &Child) -> Result<Screen, Failure> {
    let mut nodes = Vec::new();
    branch(board, child.clone(), &mut nodes)?;
    let width = nodes.iter().map(|node| node.path.chars().count()).max().unwrap_or(0);
    let mut lines = Vec::with_capacity(nodes.len());
    for node in &nodes {
        let holds = match &node.status {
            Status::Container { children } => count(*children, "child", "children"),
            // Versions are numbered from 1 up, so the newest is the count.
            Status::Note { version, .. } => count(*version, "version", "versions"),
            Status::Thread { messages, .. } => count(*messages, "message", "messages"),
            // A listing counts the open items only; every item goes.
            Status::Queue { open, .. } => {
                let items = board.queue(&node.path, StateFilter::All, 0)?.items.len();
                format!("{}, {open} open", count(items, "item", "items"))
            }
        };
        lines.push(format!("{:<width$}  {:<9}  {holds}", node.path, node.kind.as_str()));
    }
    Ok(Screen::ConfirmBranch { path: child.path.clone(), nodes: lines, typed: String::new() })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn small_things_read_right() {
        assert_eq!(parent("rev/emu/log"), "rev/emu");
        assert_eq!(parent("rev"), "");
        assert_eq!(count(1_i64, "child", "children"), "1 child");
        assert_eq!(count(0_usize, "item", "items"), "0 items");
        assert_eq!(first_line("\n  first\nsecond"), "first");
        assert_eq!(who(None, Some("SHA256:x")), "key SHA256:x");
        assert_eq!(signature(Some("SHA256:x"), Some(10_005)), "signed by SHA256:x (key revoked at #10005)");
    }
}
