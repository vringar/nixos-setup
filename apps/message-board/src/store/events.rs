//! Events: the rows every write appends, the board's only counter, and the
//! `@name` mentions taken from them.

use std::collections::BTreeSet;
use std::fmt;
use std::str::FromStr;

use rusqlite::Error::FromSqlConversionFailure;
use rusqlite::types::Type;
use rusqlite::{Connection, OptionalExtension as _, Row, params};
use serde::{Deserialize, Serialize};

use super::actions::Stamp;
use super::{Board, DbError, NAME_MAX, NodeKind, opt_text, text};

/// What an event records.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventType {
    NodeCreated,
    /// A message or an item archived, with `ref` its id; a node archived
    /// (phase 2, not written yet) has no `ref`.
    NodeArchived,
    /// As `NodeArchived`, the other way.
    NodeUnarchived,
    Message,
    NoteVersion,
    ItemFiled,
    ItemAnswered,
    ItemDeclined,
    ItemCancelled,
    /// Phase 2, as `NodeArchived`.
    ItemReassigned,
}

impl EventType {
    pub const ALL: [Self; 10] = [
        Self::NodeCreated,
        Self::NodeArchived,
        Self::NodeUnarchived,
        Self::Message,
        Self::NoteVersion,
        Self::ItemFiled,
        Self::ItemAnswered,
        Self::ItemDeclined,
        Self::ItemCancelled,
        Self::ItemReassigned,
    ];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NodeCreated => "node_created",
            Self::NodeArchived => "node_archived",
            Self::NodeUnarchived => "node_unarchived",
            Self::Message => "message",
            Self::NoteVersion => "note_version",
            Self::ItemFiled => "item_filed",
            Self::ItemAnswered => "item_answered",
            Self::ItemDeclined => "item_declined",
            Self::ItemCancelled => "item_cancelled",
            Self::ItemReassigned => "item_reassigned",
        }
    }
}

impl fmt::Display for EventType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for EventType {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        Self::ALL.into_iter().find(|t| t.as_str() == s).ok_or_else(|| format!("no event type {s:?}"))
    }
}

/// A column of one of the board's enums, parsed. The schema's CHECK
/// constraints keep anything else out, so a failure here is a fault.
pub(super) fn parsed<T: FromStr<Err = String>>(row: &Row<'_>, column: usize) -> rusqlite::Result<T> {
    text(row, column)?.parse().map_err(|err: String| FromSqlConversionFailure(column, Type::Text, err.into()))
}

/// One event, as `board show` and a watch see it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Event {
    pub id: i64,
    pub ts: String,
    pub event: EventType,
    /// The node it happened on: the thread, note or queue (or, for
    /// `node_created`, the new node).
    pub path: String,
    /// That node's kind.
    pub kind: NodeKind,
    /// The writer's name; `None` only for a registration, whose key has
    /// no name yet.
    pub from: Option<String>,
    /// The signing key's fingerprint; `None` for the board's own events.
    pub signed_by: Option<String>,
    /// If the signing key has been revoked since: the event that did it.
    /// The event itself stays as it was, signed by a key trusted then.
    pub revoked_at: Option<i64>,
    pub item: Option<i64>,
    /// A `note_version`'s version.
    pub version: Option<i64>,
    /// A note version's `through`, an answer's message, the message or item
    /// an archive event archives.
    #[serde(rename = "ref")]
    pub reference: Option<i64>,
    pub body: String,
    /// Archived: a message or filing that is, and an item's resolution
    /// while its item is.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub archived: bool,
}

/// The longest summary of a body, in characters.
const SUMMARY_MAX: usize = 200;

impl Event {
    /// The event as one doorbell line: a note's new version by its number
    /// (its body is the whole note), a node's creation or archiving by what
    /// it did (its body is the description, if any), the archiving of a
    /// message or an item by its id (its body is the reason), anything else
    /// by the first line of its body, cut at 200 characters.
    #[must_use]
    pub fn summary(&self) -> String {
        if self.event == EventType::NoteVersion {
            return format!("note updated: {} v{}", self.path, self.version.unwrap_or_default());
        }
        if let (EventType::NodeArchived | EventType::NodeUnarchived, Some(target)) = (self.event, self.reference) {
            let what = if self.kind == NodeKind::Queue { "item" } else { "message" };
            let did = if self.event == EventType::NodeArchived { "archived" } else { "unarchived" };
            return format!("{did} {what} #{target} in {}", self.path);
        }
        match self.event {
            EventType::NodeCreated => return format!("created {} {}", self.kind, self.path),
            EventType::NodeArchived => return format!("archived {} {}", self.kind, self.path),
            EventType::NodeUnarchived => return format!("unarchived {} {}", self.kind, self.path),
            _ => {}
        }
        let first = self.body.trim().lines().next().unwrap_or_default();
        if first.chars().count() > SUMMARY_MAX {
            format!("{}...", first.chars().take(SUMMARY_MAX - 3).collect::<String>())
        } else {
            first.to_owned()
        }
    }
}

/// A thread message.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Message {
    pub id: i64,
    pub ts: String,
    pub path: String,
    pub from: String,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    pub body: String,
    /// Only in a read that asked for archived messages.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub archived: bool,
}

/// Whether event `e` is hidden by an archive: the message or filing itself, or
/// an item's resolution while the item's filing is. The archive events (which
/// name the item they belong to) are never hidden.
macro_rules! event_archived {
    () => {
        "(e.archived IS NOT NULL OR (e.item IS NOT NULL AND e.type NOT IN ('node_archived','node_unarchived') \
          AND e.item IN (SELECT id FROM events WHERE archived IS NOT NULL)))"
    };
}
pub(super) use event_archived;

/// The columns [`event_row`] reads, from [`EVENT_FROM`]. Their names are
/// distinct, so a query may select them in a subquery and refer to them
/// by name outside it.
pub(super) const EVENT_FIELDS: &str = concat!(
    "e.id, e.ts, e.type, n.path, n.kind, e.actor, e.actor_fp, k.revoked, e.item, e.version, e.ref, e.body, ",
    event_archived!()
);
pub(super) const EVENT_FROM: &str =
    "events e JOIN nodes n ON n.id = e.node LEFT JOIN keys k ON k.fingerprint = e.actor_fp";

/// How many columns [`event_row`] reads; a query may select more after them.
pub(super) const EVENT_COLUMNS: usize = 13;

pub(super) fn event_row(r: &Row<'_>) -> rusqlite::Result<Event> {
    Ok(Event {
        id: r.get(0)?,
        ts: text(r, 1)?,
        event: parsed(r, 2)?,
        path: text(r, 3)?,
        kind: parsed(r, 4)?,
        from: opt_text(r, 5)?,
        signed_by: opt_text(r, 6)?,
        revoked_at: r.get(7)?,
        item: r.get(8)?,
        version: r.get(9)?,
        reference: r.get(10)?,
        body: text(r, 11)?,
        archived: r.get(12)?,
    })
}

pub(super) fn get(conn: &Connection, id: i64) -> rusqlite::Result<Option<Event>> {
    conn.query_row(&format!("SELECT {EVENT_FIELDS} FROM {EVENT_FROM} WHERE e.id=?"), [id], event_row).optional()
}

/// The type of event `id`, if there is one.
pub(super) fn type_of(conn: &Connection, id: i64) -> rusqlite::Result<Option<EventType>> {
    conn.query_row("SELECT type FROM events WHERE id=?", [id], |r| parsed(r, 0)).optional()
}

/// Whether event `id` is archived (a message or an item's filing).
pub(super) fn is_archived(conn: &Connection, id: i64) -> rusqlite::Result<bool> {
    Ok(conn
        .query_row("SELECT archived IS NOT NULL FROM events WHERE id=?", [id], |r| r.get(0))
        .optional()?
        .unwrap_or(false))
}

/// The node event `id` belongs to, if there is such an event.
pub(super) fn event_node(conn: &Connection, id: i64) -> rusqlite::Result<Option<i64>> {
    conn.query_row("SELECT node FROM events WHERE id=?", [id], |r| r.get(0)).optional()
}

/// An event to append.
pub(super) struct NewEvent<'a> {
    pub kind: EventType,
    pub node: i64,
    pub item: Option<i64>,
    pub body: &'a str,
    pub version: Option<i64>,
    pub reference: Option<i64>,
    /// A note version's thread.
    pub thread: Option<&'a str>,
}

impl<'a> NewEvent<'a> {
    pub fn new(kind: EventType, node: i64, body: &'a str) -> Self {
        Self { kind, node, item: None, body, version: None, reference: None, thread: None }
    }
}

/// Append an event; returns its id.
pub(super) fn insert(conn: &Connection, stamp: &Stamp<'_>, event: &NewEvent<'_>) -> rusqlite::Result<i64> {
    let (client_key, request_hash) = stamp.keyed.unzip();
    conn.execute(
        "INSERT INTO events(ts, type, node, item, actor, actor_fp, body, version, ref, thread, client_key,
                            request_hash, signature)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        params![
            stamp.now,
            event.kind.as_str(),
            event.node,
            event.item,
            stamp.actor,
            stamp.actor_fp,
            event.body,
            event.version,
            event.reference,
            event.thread,
            client_key,
            request_hash,
            stamp.signature
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

/// The names `body` mentions: each `@` followed by a whole name, so that
/// `@ghidra` is in "ask @ghidra." but not in "ask @ghidra-agent". Case
/// matters, as it does for names.
pub(super) fn mentioned(body: &str) -> BTreeSet<&str> {
    let in_name = |c: char| c.is_ascii_alphanumeric() || c == '_' || c == '-';
    body.match_indices('@')
        .filter_map(|(at, tag)| body.get(at.saturating_add(tag.len())..))
        .map(|rest| rest.split(|c| !in_name(c)).next().unwrap_or_default())
        .filter(|name| (1..=NAME_MAX).contains(&name.len()))
        .collect()
}

/// Record the names event `id`'s body mentions, for watches by mention.
pub(super) fn record_mentions(conn: &Connection, id: i64, body: &str) -> rusqlite::Result<()> {
    let mut insert = conn.prepare_cached("INSERT OR IGNORE INTO mentions(name, event) VALUES(?, ?)")?;
    for name in mentioned(body) {
        insert.execute(params![name, id])?;
    }
    Ok(())
}

impl Board {
    /// One event, with its full body.
    ///
    /// # Errors
    /// On a database fault.
    pub fn event(&self, id: i64) -> Result<Option<Event>, DbError> {
        self.read_db(|conn| get(conn, id))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// v1's `a_mention_is_the_name_and_not_a_longer_one`, moved from the
    /// client's filter to extraction at write time.
    #[test]
    fn a_mention_is_the_name_and_not_a_longer_one() {
        let has = |body, name| mentioned(body).contains(name);
        assert!(has("hi @ghidra, look", "ghidra"));
        assert!(has("@ghidra", "ghidra"));
        assert!(has("ask @ghidra-agent then @ghidra.", "ghidra"));
        assert!(!has("ask @ghidra-agent", "ghidra"));
        assert!(!has("@ghidra_2 and @ghidraX", "ghidra"));
        assert!(!has("ghidra without the at", "ghidra"));
        assert!(has("é@ghidraé", "ghidra"));
        assert_eq!(mentioned("ask @ghidra-agent then @ghidra. @Ghidra @@x a@b@c @"), {
            BTreeSet::from(["Ghidra", "b", "c", "ghidra", "ghidra-agent", "x"])
        });
        // a run longer than any name is no name
        assert!(mentioned(&format!("@{}", "x".repeat(NAME_MAX + 1))).is_empty());
        assert_eq!(mentioned(&format!("@{}", "x".repeat(NAME_MAX))).len(), 1);
    }

    #[test]
    fn a_doorbell_is_the_first_line_cut_at_200() {
        let event = |kind, body: &str| Event {
            id: 1,
            ts: String::new(),
            event: kind,
            path: "a/n".to_owned(),
            kind: NodeKind::Note,
            from: None,
            signed_by: None,
            revoked_at: None,
            item: None,
            version: Some(3),
            reference: None,
            body: body.to_owned(),
            archived: false,
        };
        assert_eq!(event(EventType::Message, "\n  first\nsecond").summary(), "first");
        assert_eq!(event(EventType::Message, &"é".repeat(300)).summary(), format!("{}...", "é".repeat(197)));
        assert_eq!(event(EventType::Message, "").summary(), "");
        assert_eq!(event(EventType::NoteVersion, "# the whole note").summary(), "note updated: a/n v3");
        assert_eq!(event(EventType::NodeCreated, "").summary(), "created note a/n");
        assert_eq!(event(EventType::NodeCreated, "a long description").summary(), "created note a/n");
        // an archive event with a ref is a message's or an item's
        let archive =
            |kind, node_kind, reason: &str| Event { reference: Some(10_360), kind: node_kind, ..event(kind, reason) };
        let summary = |e: Event| e.summary();
        assert_eq!(
            summary(archive(EventType::NodeArchived, NodeKind::Thread, "noise")),
            "archived message #10360 in a/n"
        );
        assert_eq!(
            summary(archive(EventType::NodeUnarchived, NodeKind::Thread, "")),
            "unarchived message #10360 in a/n"
        );
        assert_eq!(summary(archive(EventType::NodeArchived, NodeKind::Queue, "")), "archived item #10360 in a/n");
        // a node's own archiving has no ref
        assert_eq!(event(EventType::NodeArchived, "").summary(), "archived note a/n");
    }

    #[test]
    fn event_types_read_back() {
        for kind in EventType::ALL {
            assert_eq!(kind.as_str().parse::<EventType>(), Ok(kind));
            assert_eq!(serde_json::to_value(kind).ok(), Some(serde_json::Value::from(kind.as_str())));
        }
        assert!("pin".parse::<EventType>().is_err());
    }
}
