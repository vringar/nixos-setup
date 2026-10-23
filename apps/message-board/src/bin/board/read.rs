//! Reads: a node by its kind, the startup brief, one event, items across
//! queues. None of them needs a key, except to know whose items to load.

use std::collections::BTreeMap;

use message_board::store::{self, EventType, NodeKind};

use crate::api::{Api, node_url, query};
use crate::identity::{self, bound_name};
use crate::wire::{self, Brief, Item, ItemDetail, ItemList, Line, Message, NodeRead, Role};
use crate::{Failure, emit, warn};

/// Which messages of a thread `read` prints.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Range {
    /// The board's default: the last 50.
    Tail,
    Last(i64),
    Since(i64),
    All,
}

/// Which items of a queue: `open` (the board's default) or `all`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum StateArg {
    Open,
    All,
}

impl StateArg {
    fn as_str(self) -> &'static str {
        match self {
            Self::Open => "open",
            Self::All => "all",
        }
    }
}

/// `read`'s options; each is for one kind of node.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReadOptions {
    pub range: Range,
    pub version: Option<i64>,
    pub history: bool,
    pub state: Option<StateArg>,
    pub archived: bool,
}

impl ReadOptions {
    /// The query that asks for them; the board ignores what does not apply.
    fn query(&self) -> String {
        let mut pairs = Vec::new();
        match self.range {
            Range::Tail => {}
            Range::Last(n) => pairs.push(("last", n.to_string())),
            Range::Since(id) => pairs.push(("since", id.to_string())),
            Range::All => pairs.push(("all", "1".to_owned())),
        }
        if let Some(version) = self.version {
            pairs.push(("version", version.to_string()));
        }
        if self.history {
            pairs.push(("history", "1".to_owned()));
        }
        if let Some(state) = self.state {
            pairs.push(("state", state.as_str().to_owned()));
        }
        if self.archived {
            pairs.push(("archived", "1".to_owned()));
        }
        query(&pairs)
    }

    /// The first option given that is not for a `kind`, if any: a reader
    /// who asks a note for `--last 5` has the wrong path, and should hear so
    /// rather than get the note.
    fn misplaced(&self, kind: NodeKind) -> Option<&'static str> {
        let given = [
            (
                self.range != Range::Tail && !(kind == NodeKind::Queue && matches!(self.range, Range::Since(_))),
                NodeKind::Thread,
                "--since/--last/--all",
            ),
            (self.version.is_some() || self.history, NodeKind::Note, "--version/--history"),
            (self.state.is_some(), NodeKind::Queue, "--state"),
        ];
        let archived = self.archived && kind == NodeKind::Note;
        given
            .into_iter()
            .find(|(set, applies, _)| *set && *applies != kind)
            .map(|(_, _, flag)| flag)
            .or(archived.then_some("--archived"))
    }
}

fn missing(what: &str, field: &str) -> Failure {
    Failure::BadAnswer { what: what.to_owned(), cause: format!("no {field}") }
}

/// Print the node at `path` by its kind (the top level for none), then the
/// cursor. The head is read first, so that `watch --since <cursor>` misses
/// nothing written after the read.
pub fn read(api: &Api, path: &str, options: &ReadOptions) -> Result<(), Failure> {
    let head = api.head()?;
    let url = format!("{}{}", node_url(path), options.query());
    let read: NodeRead = api.get(&url)?;
    let kind = read.node.as_ref().map_or(NodeKind::Container, |node| node.kind);
    let shown = if path.is_empty() { "the top level" } else { path };
    if let Some(flag) = options.misplaced(kind) {
        return Err(Failure::Arguments(format!("{flag} does not apply here: {shown} is a {kind}")));
    }
    // The cursor passes everything printed, whatever came after the head.
    let mut since = head;
    let mut earlier = None;
    match kind {
        NodeKind::Container => {
            for child in read.children.as_ref().ok_or_else(|| missing(&url, "children"))? {
                emit(&Line::Child(child))?;
            }
        }
        NodeKind::Note if options.history => {
            for version in read.history.as_ref().ok_or_else(|| missing(&url, "history"))? {
                since = since.max(version.id);
                emit(&Line::Version { path, version })?;
            }
        }
        NodeKind::Note => match &read.note {
            Some(note) => {
                since = since.max(note.id);
                emit(&Line::Note(note))?;
            }
            None => warn(&format!("board: {path} has no content yet (version 0); the first note set takes --base 0"))?,
        },
        NodeKind::Thread => {
            let messages = read.messages.as_ref().ok_or_else(|| missing(&url, "messages"))?;
            for message in messages {
                since = since.max(message.id);
                emit(&Line::Message(message))?;
            }
            // Cut short going forward: the cursor stops at the last one
            // printed, so that reading on from it misses nothing.
            if read.truncated == Some(true) && !matches!(options.range, Range::Tail | Range::Last(_)) {
                since = messages.last().map_or(since, |last| last.id);
            }
            earlier = Some(read.earlier.ok_or_else(|| missing(&url, "earlier"))?);
        }
        NodeKind::Queue => {
            let items = read.items.as_ref().ok_or_else(|| missing(&url, "items"))?;
            for item in items {
                since = since.max(item.id);
                emit(&Line::Item(item))?;
            }
            if read.truncated == Some(true) {
                since = items.last().map_or(since, |last| last.id);
            }
        }
    }
    if read.truncated == Some(true) {
        warn(&format!("board: {shown}: truncated; {}", read.hint.as_deref().unwrap_or("page on")))?;
    }
    emit(&Line::Cursor { since, earlier, watch: None })
}

/// `word` as one shell word. Paths and names are shell-safe by the board's
/// rules, so this quotes nothing in practice; it is what keeps a printed
/// command, which agents run verbatim, from ever being more than one.
fn shell_word(word: &str) -> String {
    let safe = |c: char| c.is_ascii_alphanumeric() || matches!(c, '/' | '_' | '-' | '.');
    if !word.is_empty() && word.chars().all(safe) {
        word.to_owned()
    } else {
        format!("'{}'", word.replace('\'', r"'\''"))
    }
}

/// The watch a brief hands out: its paths, the reader's filters (`--me`, or
/// those of `--as NAME`, whose name `--me` would not be), and the cursor.
fn watch_command(paths: &[String], name: Option<&str>, since: i64) -> String {
    let mut words = vec!["board".to_owned(), "watch".to_owned()];
    words.extend(paths.iter().map(|path| shell_word(path)));
    match name {
        None => words.push("--me".to_owned()),
        Some(name) => {
            for flag in ["--mention", "--asked-by", "--owner", "--not-from"] {
                words.push(flag.to_owned());
                words.push(shell_word(name));
            }
        }
    }
    words.push("--since".to_owned());
    words.push(since.to_string());
    words.join(" ")
}

/// The startup read: for each container on `paths`, root first, its
/// children and the notes directly in it; then the open items of the
/// reader's queues and those it asked; then the cursor, with the watch to
/// arm. The reader is `--as NAME`, else the signing key's bound name; a
/// key without one gets the notes and listings only.
pub fn brief(api: &Api, paths: &[String], as_name: Option<&str>) -> Result<(), Failure> {
    if let Some(name) = as_name {
        store::check_name(name).map_err(|err| Failure::Exit(err.to_string()))?;
    }
    let name = match as_name {
        Some(name) => Some(name.to_owned()),
        None => match identity::signer().and_then(|signer| bound_name(api, &signer)) {
            Ok(name) => Some(name),
            // Reading needs no key: say why there are no items, and go on.
            Err(why) if why.is_identity() => {
                warn(&format!("board: {why}; so no items, only notes and listings"))?;
                None
            }
            Err(err) => return Err(err),
        },
    };
    let mut pairs: Vec<(&str, String)> = paths.iter().map(|path| ("path", path.clone())).collect();
    pairs.extend(name.iter().map(|name| ("as", name.clone())));
    let mut brief: Brief = api.get(&format!("/brief{}", query(&pairs)))?;
    for container in &brief.containers {
        for child in &container.children {
            emit(&Line::Child(child))?;
        }
        for note in &container.notes {
            emit(&Line::Note(note))?;
        }
    }
    for (items, role) in [(&mut brief.owned, Role::Owner), (&mut brief.asked, Role::Asker)] {
        for item in items.iter_mut() {
            item.role = Some(role);
            emit(&Line::Item(item))?;
        }
    }
    let watch = watch_command(&brief.watch, as_name, brief.head);
    emit(&Line::Cursor { since: brief.head, earlier: None, watch: Some(watch) })
}

/// One event in full: a message, a note version (with how far behind its
/// thread it is now), the item a filing event made (as it stands, with its
/// resolution), the event that resolved an item, or a node.
pub fn show(api: &Api, id: i64) -> Result<(), Failure> {
    let event: store::Event = api.get::<wire::WireEvent>(&format!("/event/{id}"))?.into();
    match event.event {
        EventType::Message => emit(&Line::Message(&Message {
            id: event.id,
            ts: event.ts,
            path: event.path,
            from: event.from.unwrap_or_default(),
            signed_by: event.signed_by,
            revoked_at: event.revoked_at,
            body: event.body,
            archived: event.archived,
        })),
        EventType::NoteVersion => {
            let version = event.version.ok_or_else(|| missing(&format!("/event/{id}"), "version"))?;
            let url = format!("{}{}", node_url(&event.path), query(&[("version", version.to_string())]));
            let read: NodeRead = api.get(&url)?;
            emit(&Line::Note(read.note.as_ref().ok_or_else(|| missing(&url, "note"))?))
        }
        EventType::ItemFiled => {
            let item = event.item.ok_or_else(|| missing(&format!("/event/{id}"), "item"))?;
            let detail: ItemDetail = api.get(&format!("/item/{item}"))?;
            emit(&Line::Item(&detail.item))
        }
        // What resolved an item is the event that did it; the item is `show <item id>`.
        EventType::ItemAnswered | EventType::ItemDeclined | EventType::ItemCancelled | EventType::ItemReassigned => {
            emit(&Line::Resolution {
                id: event.id,
                ts: &event.ts,
                event: event.event,
                path: &event.path,
                item: event.item.ok_or_else(|| missing(&format!("/event/{id}"), "item"))?,
                from: event.from.as_deref(),
                signed_by: event.signed_by.as_deref(),
                revoked_at: event.revoked_at,
                reference: event.reference,
                body: &event.body,
                archived: event.archived,
            })
        }
        // An archive event of a message or item names it in `ref`.
        EventType::NodeArchived | EventType::NodeUnarchived if event.reference.is_some() => emit(&Line::Archive {
            id: event.id,
            ts: &event.ts,
            event: event.event,
            path: &event.path,
            from: event.from.as_deref(),
            signed_by: event.signed_by.as_deref(),
            revoked_at: event.revoked_at,
            reference: event.reference.unwrap_or_default(),
            body: &event.body,
        }),
        EventType::NodeCreated | EventType::NodeArchived | EventType::NodeUnarchived => emit(&Line::Node {
            id: event.id,
            ts: &event.ts,
            path: &event.path,
            kind: event.kind,
            from: event.from.as_deref(),
            signed_by: event.signed_by.as_deref(),
            revoked_at: event.revoked_at,
            // a node's creation records its description as the event's body
            description: &event.body,
        }),
    }
}

/// Whose items `items` lists.
pub enum Whose<'a> {
    /// Owned or asked by the signing key's name.
    Mine,
    Filtered {
        owner: Option<&'a str>,
        asker: Option<&'a str>,
    },
}

pub fn items(api: &Api, whose: &Whose<'_>, state: StateArg, after: Option<i64>, archived: bool) -> Result<(), Failure> {
    let list = |owner: Option<&str>, asker: Option<&str>| -> Result<Vec<Item>, Failure> {
        let mut pairs = vec![("state", state.as_str().to_owned())];
        if archived {
            pairs.push(("archived", "1".to_owned()));
        }
        pairs.extend(owner.map(|name| ("owner", name.to_owned())));
        pairs.extend(asker.map(|name| ("asker", name.to_owned())));
        pairs.extend(after.map(|id| ("after", id.to_string())));
        let list: ItemList = api.get(&format!("/items{}", query(&pairs)))?;
        if list.truncated == Some(true) {
            warn(&format!("board: items: truncated; {}", list.hint.as_deref().unwrap_or("page on")))?;
        }
        Ok(list.items)
    };
    let items = match whose {
        Whose::Mine => {
            let name = bound_name(api, &identity::signer()?)?;
            // Owned and asked, once each (an item one asks in one's own
            // queue is both), oldest first.
            let mut merged: BTreeMap<i64, Item> = BTreeMap::new();
            for item in list(Some(&name), None)?.into_iter().chain(list(None, Some(&name))?) {
                merged.entry(item.id).or_insert(item);
            }
            merged.into_values().collect()
        }
        Whose::Filtered { owner, asker } => {
            for name in owner.iter().chain(asker) {
                store::check_name(name).map_err(|err| Failure::Exit(err.to_string()))?;
            }
            list(*owner, *asker)?
        }
    };
    for item in &items {
        emit(&Line::Item(item))?;
    }
    Ok(())
}

/// `board registrations`: the pending ones, oldest first, a line each.
pub fn registrations(api: &Api) -> Result<(), Failure> {
    let pending: Vec<crate::wire::Registration> = api.get("/registrations")?;
    pending.iter().try_for_each(|registration| emit(&Line::Registration(registration)))
}

#[cfg(test)]
mod tests {
    use super::*;

    const DEFAULT: ReadOptions =
        ReadOptions { range: Range::Tail, version: None, history: false, state: None, archived: false };

    #[test]
    fn read_options_go_with_their_kind() {
        use NodeKind::{Container, Note, Queue, Thread};
        for kind in [Container, Note, Thread, Queue] {
            assert_eq!(DEFAULT.misplaced(kind), None);
        }
        let last = ReadOptions { range: Range::Last(5), ..DEFAULT };
        assert_eq!(last.misplaced(Thread), None);
        assert_eq!(last.misplaced(Note), Some("--since/--last/--all"));
        let history = ReadOptions { history: true, ..DEFAULT };
        assert_eq!(history.misplaced(Note), None);
        assert_eq!(history.misplaced(Queue), Some("--version/--history"));
        assert_eq!(ReadOptions { state: Some(StateArg::All), ..DEFAULT }.misplaced(Container), Some("--state"));
        // archived messages, archived items and a container's archived children; not a note's
        let archived = ReadOptions { archived: true, ..DEFAULT };
        assert_eq!(archived.misplaced(Note), Some("--archived"));
        for kind in [Container, Thread, Queue] {
            assert_eq!(archived.misplaced(kind), None);
        }
    }

    #[test]
    fn read_options_become_the_boards_query() {
        assert_eq!(DEFAULT.query(), "");
        assert_eq!(ReadOptions { range: Range::Since(10_340), ..DEFAULT }.query(), "?since=10340");
        assert_eq!(
            ReadOptions { range: Range::All, state: Some(StateArg::All), ..DEFAULT }.query(),
            "?all=1&state=all"
        );
        assert_eq!(ReadOptions { version: Some(3), ..DEFAULT }.query(), "?version=3");
        assert_eq!(ReadOptions { archived: true, ..DEFAULT }.query(), "?archived=1");
    }

    #[test]
    fn a_brief_hands_out_a_watch_to_run_as_it_is() {
        let paths: Vec<String> = ["reversing", "reversing/emu", "reversing/emu/log"].map(str::to_owned).into();
        assert_eq!(
            watch_command(&paths, None, 10_350),
            "board watch reversing reversing/emu reversing/emu/log --me --since 10350"
        );
        assert_eq!(
            watch_command(&["reversing".to_owned()], Some("aspfuzz"), 7),
            "board watch reversing --mention aspfuzz --asked-by aspfuzz --owner aspfuzz --not-from aspfuzz --since 7"
        );
        assert_eq!(shell_word("a/b-c_d"), "a/b-c_d");
        assert_eq!(shell_word("x; rm -rf ~"), "'x; rm -rf ~'");
        assert_eq!(shell_word("it's"), r"'it'\''s'");
        assert_eq!(shell_word(""), "''");
    }
}
