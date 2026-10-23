//! The tree: containers and the leaves in them (notes, threads, queues),
//! listings with each child's status, and the reads and writes of notes
//! and threads.
//!
//! Paths are `/`-separated segments of `[a-z0-9_-]`, at most two container
//! levels deep, with leaves directly in a container: `reversing`,
//! `reversing/emu`, `reversing/charter`, `reversing/emu/log`. Only the
//! board makes segments starting with `_` (`_board`). Nodes are made
//! explicitly ([`Board::create`]); a write to a path that does not exist
//! is refused, so a typo cannot start a new node.

use std::fmt;
use std::str::FromStr;

use rusqlite::{Connection, OptionalExtension as _, Row, params};
use serde::{Deserialize, Serialize};

use super::actions::{Create, Created, NoteSaved, Post, Posted, SetNote, Signed, Stamp};
use super::events::{self, EventType, Message, NewEvent, parsed};
use super::keys::{self, Author};
use super::{Board, DbError, Error, capped, check_name, opt_text, quoted, text};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum NodeKind {
    /// Holds other nodes; its listing is its children.
    Container,
    /// A versioned document.
    Note,
    /// An append-only log of messages.
    Thread,
    /// Items with one owner, each open until resolved.
    Queue,
}

impl NodeKind {
    pub const ALL: [Self; 4] = [Self::Container, Self::Note, Self::Thread, Self::Queue];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Container => "container",
            Self::Note => "note",
            Self::Thread => "thread",
            Self::Queue => "queue",
        }
    }
}

impl fmt::Display for NodeKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for NodeKind {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        Self::ALL.into_iter().find(|k| k.as_str() == s).ok_or_else(|| format!("no node kind {s:?}"))
    }
}

pub const SEGMENT_MAX: usize = 64;
pub const PATH_RULE: &str =
    "segments of 1-64 lowercase letters, digits, '_' and '-', each starting with a letter or digit, joined by '/'";
/// The longest description, in characters: it is one line of a listing.
pub const DESCRIPTION_MAX: usize = 200;
/// How deep a path goes: two container levels, and a leaf in the second.
const DEPTH_MAX: usize = 3;
const CONTAINER_DEPTH_MAX: usize = 2;

/// A node of the tree.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Node {
    #[serde(skip)]
    pub id: i64,
    pub path: String,
    #[serde(skip)]
    pub parent: Option<i64>,
    pub kind: NodeKind,
    pub description: String,
    /// The name that made it.
    pub creator: String,
    /// A queue's owner, the name that resolves its items.
    pub owner: Option<String>,
    /// Written by the board only (`_board`, its register and trust list).
    pub generated: bool,
    /// Its `node_created` event.
    pub created: i64,
    /// The event that archived it (phase 2); `None` while live.
    pub archived: Option<i64>,
}

/// A node as a listing shows it: what it is, and how it stands.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Child {
    pub path: String,
    pub kind: NodeKind,
    pub description: String,
    pub archived: Option<i64>,
    pub creator: String,
    #[serde(flatten)]
    pub status: Status,
}

/// How a child stands, by kind: enough to tell whether to look closer.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(untagged)]
pub enum Status {
    Container {
        children: i64,
    },
    /// Version 0 is a note with no content yet.
    Note {
        version: i64,
        ts: Option<String>,
        from: Option<String>,
        through: Option<i64>,
        /// Messages in `through`'s thread after it: how stale the summary is.
        behind: Option<i64>,
    },
    Thread {
        last_id: Option<i64>,
        last_ts: Option<String>,
        messages: i64,
    },
    Queue {
        owner: String,
        open: i64,
        oldest_open_ts: Option<String>,
    },
}

/// One version of a note, with its body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct NoteVersion {
    pub path: String,
    pub version: i64,
    /// Its `note_version` event.
    pub id: i64,
    pub ts: String,
    pub from: String,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    /// The thread message it summarizes up to.
    pub through: Option<i64>,
    /// Messages in `through`'s thread after it.
    pub behind: Option<i64>,
    pub body: String,
}

/// One version of a note, as its history lists it: without the body.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Version {
    pub version: i64,
    pub id: i64,
    pub ts: String,
    pub from: String,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    pub through: Option<i64>,
}

/// Which messages of a thread to read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Range {
    /// The newest `n` ([`super::THREAD_TAIL`] by default).
    Last(i64),
    /// Every message after this id.
    Since(i64),
    All,
}

/// Messages of a thread, oldest first.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ThreadPage {
    pub messages: Vec<Message>,
    /// How many messages come before the first of these.
    pub earlier: i64,
    /// More were asked for than [`super::READ_ROWS_MAX`] messages or
    /// [`super::READ_BYTES_MAX`] bytes: with `Last`, the oldest are missing,
    /// otherwise the newest.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub truncated: bool,
}

/// The segments of `path` if the board could hold it. Only the board's own
/// paths (`board_made`) may have segments starting with '_'; anyone may
/// read them.
fn segments(path: &str, board_made: bool) -> Result<Vec<&str>, Error> {
    let segments: Vec<&str> = path.split('/').collect();
    let rest = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-';
    let first = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit() || (board_made && b == b'_');
    let good = |segment: &&str| {
        let mut bytes = segment.bytes();
        segment.len() <= SEGMENT_MAX && bytes.next().is_some_and(first) && bytes.all(rest)
    };
    if !segments.iter().all(good) {
        let why = if !board_made && segments.iter().any(|s| s.starts_with('_')) {
            "only the board makes names starting with '_'".to_owned()
        } else {
            format!("paths are {PATH_RULE}")
        };
        return Err(Error::BadRequest(format!("path {} is not allowed: {why}", quoted(path))));
    }
    if segments.len() > DEPTH_MAX {
        return Err(Error::BadRequest(format!(
            "path {} is too deep: containers nest {CONTAINER_DEPTH_MAX} deep, and leaves sit directly in them",
            quoted(path)
        )));
    }
    Ok(segments)
}

const NODE_SELECT: &str =
    "SELECT id, path, parent, kind, description, creator, owner, generated, created, archived FROM nodes";

fn node_row(r: &Row<'_>) -> rusqlite::Result<Node> {
    Ok(Node {
        id: r.get(0)?,
        path: text(r, 1)?,
        parent: r.get(2)?,
        kind: parsed(r, 3)?,
        description: text(r, 4)?,
        creator: text(r, 5)?,
        owner: opt_text(r, 6)?,
        generated: r.get(7)?,
        created: r.get(8)?,
        archived: r.get(9)?,
    })
}

pub(super) fn find(conn: &Connection, path: &str) -> rusqlite::Result<Option<Node>> {
    conn.query_row(&format!("{NODE_SELECT} WHERE path=?"), [path], node_row).optional()
}

/// The node at `path`.
///
/// # Errors
/// [`Error::BadRequest`] for a malformed path, [`Error::NotFound`] if
/// there is none.
pub(super) fn require(conn: &Connection, path: &str) -> Result<Node, Error> {
    segments(path, true)?;
    find(conn, path)?.ok_or_else(|| Error::NotFound(format!("no node {}", quoted(path))))
}

/// The node at `path`, which must be a `kind`.
pub(super) fn require_kind(conn: &Connection, path: &str, kind: NodeKind) -> Result<Node, Error> {
    let node = require(conn, path)?;
    if node.kind != kind {
        return Err(Error::BadRequest(format!("{} is a {}, not a {kind}", quoted(path), node.kind)));
    }
    Ok(node)
}

/// Refuse a client write to a node the board writes itself.
pub(super) fn check_writable(node: &Node) -> Result<(), Error> {
    if node.generated {
        return Err(Error::Forbidden(format!("{} is written by the board only", quoted(&node.path))));
    }
    Ok(())
}

/// A node to make.
pub(super) struct NewNode<'a> {
    pub path: &'a str,
    pub parent: Option<i64>,
    pub kind: NodeKind,
    pub description: &'a str,
    pub creator: &'a str,
    pub owner: Option<&'a str>,
    pub generated: bool,
}

/// Make a node and its `node_created` event; returns (node id, event id).
pub(super) fn insert(conn: &Connection, stamp: &Stamp<'_>, node: &NewNode<'_>) -> rusqlite::Result<(i64, i64)> {
    // The node needs its event's id and the event its node's: the node goes
    // first, with `created` filled in once the event exists.
    conn.execute(
        "INSERT INTO nodes(path, parent, kind, description, creator, owner, generated, created)
         VALUES(?,?,?,?,?,?,?,0)",
        params![node.path, node.parent, node.kind.as_str(), node.description, node.creator, node.owner, node.generated],
    )?;
    let id = conn.last_insert_rowid();
    let event = events::insert(conn, stamp, &NewEvent::new(EventType::NodeCreated, id, node.description))?;
    conn.execute("UPDATE nodes SET created=? WHERE id=?", [event, id])?;
    Ok((id, event))
}

/// The children of `parent` (`None`: the top level), by path.
pub(super) fn children(conn: &Connection, parent: Option<i64>) -> rusqlite::Result<Vec<Child>> {
    let nodes: Vec<Node> = conn
        .prepare_cached(&format!("{NODE_SELECT} WHERE parent IS ? ORDER BY path"))?
        .query_map([parent], node_row)?
        .collect::<rusqlite::Result<_>>()?;
    nodes.into_iter().map(|node| child(conn, node)).collect()
}

fn child(conn: &Connection, node: Node) -> rusqlite::Result<Child> {
    let status = match node.kind {
        NodeKind::Container => Status::Container {
            children: conn.query_row("SELECT count(*) FROM nodes WHERE parent=?", [node.id], |r| r.get(0))?,
        },
        NodeKind::Note => match versions(conn, node.id, true)?.pop() {
            Some(last) => Status::Note {
                version: last.version,
                ts: Some(last.ts),
                from: Some(last.from),
                behind: last.through.map(|through| behind(conn, through)).transpose()?,
                through: last.through,
            },
            None => Status::Note { version: 0, ts: None, from: None, through: None, behind: None },
        },
        NodeKind::Thread => {
            let (last_id, last_ts) = conn
                .query_row(
                    "SELECT id, ts FROM events WHERE node=? AND type='message' ORDER BY id DESC LIMIT 1",
                    [node.id],
                    |r| Ok((Some(r.get(0)?), Some(text(r, 1)?))),
                )
                .optional()?
                .unwrap_or_default();
            let messages =
                conn.query_row("SELECT count(*) FROM events WHERE node=? AND type='message'", [node.id], |r| r.get(0))?;
            Status::Thread { last_id, last_ts, messages }
        }
        NodeKind::Queue => {
            let (open, oldest_open_ts) = conn.query_row(
                "SELECT count(*), min(e.ts) FROM items i JOIN events e ON e.id = i.id
                  WHERE i.queue=? AND i.state='open'",
                [node.id],
                |r| Ok((r.get(0)?, opt_text(r, 1)?)),
            )?;
            Status::Queue { owner: node.owner.clone().unwrap_or_default(), open, oldest_open_ts }
        }
    };
    Ok(Child {
        path: node.path,
        kind: node.kind,
        description: node.description,
        archived: node.archived,
        creator: node.creator,
        status,
    })
}

/// How many messages follow `through` in its thread.
fn behind(conn: &Connection, through: i64) -> rusqlite::Result<i64> {
    conn.query_row(
        "SELECT count(*) FROM events
          WHERE type='message' AND node=(SELECT node FROM events WHERE id=?1) AND id>?1",
        [through],
        |r| r.get(0),
    )
}

const VERSION_SELECT: &str = "SELECT e.version, e.id, e.ts, e.actor, e.actor_fp, k.revoked, e.ref
       FROM events e LEFT JOIN keys k ON k.fingerprint = e.actor_fp
      WHERE e.node=? AND e.type='note_version'";

fn version_row(r: &Row<'_>) -> rusqlite::Result<Version> {
    Ok(Version {
        version: r.get(0)?,
        id: r.get(1)?,
        ts: text(r, 2)?,
        from: text(r, 3)?,
        signed_by: opt_text(r, 4)?,
        revoked_at: r.get(5)?,
        through: r.get(6)?,
    })
}

/// A note's versions, oldest first; only the newest if `last`.
fn versions(conn: &Connection, note: i64, last: bool) -> rusqlite::Result<Vec<Version>> {
    let sql = if last {
        format!("{VERSION_SELECT} ORDER BY e.id DESC LIMIT 1")
    } else {
        format!("{VERSION_SELECT} ORDER BY e.id")
    };
    conn.prepare_cached(&sql)?.query_map([note], version_row)?.collect()
}

/// A note's current version number, 0 for none.
pub(super) fn current_version(conn: &Connection, note: i64) -> rusqlite::Result<i64> {
    Ok(versions(conn, note, true)?.pop().map_or(0, |v| v.version))
}

/// Version `version` of a note (the newest if `None`), with its body.
pub(super) fn note_version(
    conn: &Connection,
    note: &Node,
    version: Option<i64>,
) -> rusqlite::Result<Option<NoteVersion>> {
    let sql = "SELECT e.version, e.id, e.ts, e.actor, e.actor_fp, k.revoked, e.ref, e.body
                 FROM events e LEFT JOIN keys k ON k.fingerprint = e.actor_fp
                WHERE e.node=?1 AND e.type='note_version' AND (?2 IS NULL OR e.version=?2)
                ORDER BY e.id DESC LIMIT 1";
    let found = conn.query_row(sql, params![note.id, version], |r| Ok((version_row(r)?, text(r, 7)?))).optional()?;
    let Some((v, body)) = found else {
        return Ok(None);
    };
    Ok(Some(NoteVersion {
        path: note.path.clone(),
        version: v.version,
        id: v.id,
        ts: v.ts,
        from: v.from,
        signed_by: v.signed_by,
        revoked_at: v.revoked_at,
        behind: v.through.map(|through| behind(conn, through)).transpose()?,
        through: v.through,
        body,
    }))
}

/// The current version of every note directly in `container` that has one.
pub(super) fn notes_in(conn: &Connection, container: i64) -> rusqlite::Result<Vec<NoteVersion>> {
    let notes: Vec<Node> = conn
        .prepare_cached(&format!("{NODE_SELECT} WHERE parent=? AND kind='note' ORDER BY path"))?
        .query_map([container], node_row)?
        .collect::<rusqlite::Result<_>>()?;
    let mut current = Vec::with_capacity(notes.len());
    for note in &notes {
        current.extend(note_version(conn, note, None)?);
    }
    Ok(current)
}

fn messages(conn: &Connection, thread: &Node, range: Range) -> rusqlite::Result<ThreadPage> {
    const SELECT: &str = "SELECT e.id, e.ts, e.actor, e.actor_fp, k.revoked, e.body
           FROM events e LEFT JOIN keys k ON k.fingerprint = e.actor_fp
          WHERE e.node=?1 AND e.type='message'";
    let message = |r: &Row<'_>| {
        Ok(Message {
            id: r.get(0)?,
            ts: text(r, 1)?,
            path: thread.path.clone(),
            from: text(r, 2)?,
            signed_by: opt_text(r, 3)?,
            revoked_at: r.get(4)?,
            body: text(r, 5)?,
        })
    };
    let count = |sql: &str, bound: i64| -> rusqlite::Result<i64> {
        conn.query_row(
            &format!("SELECT count(*) FROM events WHERE node=?1 AND type='message'{sql}"),
            [thread.id, bound],
            |r| r.get(0),
        )
    };
    match range {
        Range::Last(n) => {
            // LIMIT -1 would be no limit at all
            let n = n.max(0);
            let (mut newest, truncated) = capped(
                conn.prepare_cached(&format!("{SELECT} ORDER BY e.id DESC LIMIT ?2"))?
                    .query_map([thread.id, n], message)?,
                |m| m.body.len(),
            )?;
            newest.reverse();
            let before = newest.first().map_or(i64::MAX, |m| m.id);
            Ok(ThreadPage { earlier: count(" AND id<?2", before)?, messages: newest, truncated })
        }
        Range::Since(since) => {
            let (messages, truncated) = capped(
                conn.prepare_cached(&format!("{SELECT} AND e.id>?2 ORDER BY e.id"))?
                    .query_map([thread.id, since], message)?,
                |m| m.body.len(),
            )?;
            Ok(ThreadPage { messages, earlier: count(" AND id<=?2", since)?, truncated })
        }
        Range::All => {
            let (messages, truncated) = capped(
                conn.prepare_cached(&format!("{SELECT} ORDER BY e.id"))?.query_map([thread.id], message)?,
                |m| m.body.len(),
            )?;
            Ok(ThreadPage { messages, earlier: 0, truncated })
        }
    }
}

fn check_description(description: &str) -> Result<(), Error> {
    if description.chars().count() > DESCRIPTION_MAX || description.chars().any(char::is_control) {
        return Err(Error::BadRequest(format!("a description is one line of at most {DESCRIPTION_MAX} characters")));
    }
    Ok(())
}

/// Refuse a node `author` may not make where `create` would put it, under
/// `parent` (`None` at the top level). `depth` is the path's.
fn check_place(create: &Create<'_>, depth: usize, parent: Option<&Node>, author: &Author) -> Result<(), Error> {
    let path = quoted(create.path);
    let Some(parent) = parent else {
        if create.kind != NodeKind::Container {
            return Err(Error::BadRequest(format!(
                "{path} would be a top-level {}; top-level nodes are containers",
                create.kind
            )));
        }
        if !author.human {
            return Err(Error::Forbidden("only the owner makes top-level containers".to_owned()));
        }
        return Ok(());
    };
    if parent.kind != NodeKind::Container {
        return Err(Error::BadRequest(format!(
            "{} is a {}: nothing goes under a leaf",
            quoted(&parent.path),
            parent.kind
        )));
    }
    if parent.generated {
        return Err(Error::Forbidden(format!("{} is the board's own", quoted(&parent.path))));
    }
    if create.kind == NodeKind::Container && depth > CONTAINER_DEPTH_MAX {
        return Err(Error::BadRequest(format!(
            "{path} would be a third level of containers; containers nest {CONTAINER_DEPTH_MAX} deep"
        )));
    }
    Ok(())
}

/// A new node's owner: a queue's, or the author for a queue that names
/// none; nothing for any other kind.
fn owner_of<'a>(create: &Create<'a>, author: &'a Author) -> Result<Option<&'a str>, Error> {
    if create.kind != NodeKind::Queue {
        if !create.owner.is_empty() {
            return Err(Error::BadRequest(format!("only queues have an owner, and a {} is none", create.kind)));
        }
        return Ok(None);
    }
    let owner = if create.owner.is_empty() { author.name.as_str() } else { create.owner };
    check_name(owner)?;
    if owner != author.name && !author.human {
        return Err(Error::Forbidden(format!(
            "only the owner makes a queue for someone else; leave the owner out to own {path} yourself",
            path = quoted(create.path)
        )));
    }
    Ok(Some(owner))
}

/// What became of a write, from its first event; `NotFound` if that is gone
/// (board-admin deleted it).
pub(super) fn event_of(conn: &Connection, id: i64) -> Result<events::Event, Error> {
    events::get(conn, id)?.ok_or_else(|| Error::NotFound(format!("event #{id} is gone")))
}

/// What board-admin deleted.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub struct Deleted {
    pub nodes: usize,
    pub events: usize,
}

/// `ids` as an SQL list. They are the board's own integers, so they can go
/// into the statement's text.
fn id_list(ids: &[i64]) -> String {
    ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",")
}

impl Board {
    /// The node at `path`.
    ///
    /// # Errors
    /// [`Error::BadRequest`] for a malformed path, [`Error::NotFound`],
    /// and on a database fault.
    pub fn node(&self, path: &str) -> Result<Node, Error> {
        self.read(|conn| require(conn, path))
    }

    /// The children of the container at `path`, by path; the top-level
    /// nodes for an empty path.
    ///
    /// # Errors
    /// As [`Board::node`], and [`Error::BadRequest`] for a leaf.
    pub fn children(&self, path: &str) -> Result<Vec<Child>, Error> {
        self.read(|conn| {
            let parent = if path.is_empty() { None } else { Some(require_kind(conn, path, NodeKind::Container)?.id) };
            Ok(children(conn, parent)?)
        })
    }

    /// The first `limit` nodes of the whole tree as (path, kind), in tree
    /// order (a node, then what is under it): one cheap query, for a
    /// page's outline.
    ///
    /// # Errors
    /// Only the database's.
    pub fn outline(&self, limit: usize) -> Result<Vec<(String, NodeKind)>, Error> {
        self.read(|conn| {
            let mut rows: Vec<(String, NodeKind)> = conn
                .prepare_cached("SELECT path, kind FROM nodes ORDER BY path LIMIT ?")?
                .query_map([i64::try_from(limit).unwrap_or(i64::MAX)], |r| Ok((text(r, 0)?, parsed(r, 1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            rows.sort_by(|a, b| a.0.split('/').cmp(b.0.split('/')));
            Ok(rows)
        })
    }

    /// Version `version` of the note at `path`, or the newest if `None`.
    /// `Ok(None)` for a note with no version yet.
    ///
    /// # Errors
    /// As [`Board::node`], [`Error::BadRequest`] for another kind, and
    /// [`Error::NotFound`] for a version the note does not have.
    pub fn note(&self, path: &str, version: Option<i64>) -> Result<Option<NoteVersion>, Error> {
        self.read(|conn| {
            let note = require_kind(conn, path, NodeKind::Note)?;
            match (note_version(conn, &note, version)?, version) {
                (None, Some(version)) => Err(Error::NotFound(format!("{} has no version {version}", quoted(path)))),
                (found, _) => Ok(found),
            }
        })
    }

    /// Every version of the note at `path`, oldest first, without bodies.
    ///
    /// # Errors
    /// As [`Board::note`].
    pub fn note_history(&self, path: &str) -> Result<Vec<Version>, Error> {
        self.read(|conn| Ok(versions(conn, require_kind(conn, path, NodeKind::Note)?.id, false)?))
    }

    /// Messages of the thread at `path`.
    ///
    /// # Errors
    /// As [`Board::node`], and [`Error::BadRequest`] for another kind.
    pub fn thread(&self, path: &str, range: Range) -> Result<ThreadPage, Error> {
        self.read(|conn| Ok(messages(conn, &require_kind(conn, path, NodeKind::Thread)?, range)?))
    }

    /// Make a node: a top-level container (the owner only), a container in
    /// one, or a leaf in a container. A note may come with its first
    /// version; without one it is at version 0.
    ///
    /// # Errors
    /// A refusal of the signature or key (see the `store` docs);
    /// [`Error::BadRequest`] for a path, kind, owner or body that does not
    /// fit; [`Error::NotFound`] for a missing parent; [`Error::Forbidden`]
    /// as the permission rules say; [`Error::Conflict`] if the path exists.
    pub fn create(&self, signed: &Signed<'_, Create<'_>>) -> Result<Created, Error> {
        let create = &signed.action;
        let depth = segments(create.path, false)?.len();
        check_description(create.description)?;
        if create.kind != NodeKind::Note && !create.body.is_empty() {
            return Err(Error::BadRequest(format!("only notes have a body, and a {} is none", create.kind)));
        }
        self.signed(
            signed,
            keys::admit,
            |conn, stamp, author| {
                let parent = create.path.rsplit_once('/').map(|(parent, _)| require(conn, parent)).transpose()?;
                check_place(create, depth, parent.as_ref(), author)?;
                if find(conn, create.path)?.is_some() {
                    return Err(Error::Conflict(format!("{} exists already", quoted(create.path))));
                }
                let new = NewNode {
                    path: create.path,
                    parent: parent.map(|p| p.id),
                    kind: create.kind,
                    description: create.description,
                    creator: &author.name,
                    owner: owner_of(create, author)?,
                    generated: false,
                };
                let (node, event) = insert(conn, stamp, &new)?;
                // A blank body is no first version, as a blank save is no version.
                if create.kind == NodeKind::Note && !create.body.trim().is_empty() {
                    let first =
                        NewEvent { version: Some(1), ..NewEvent::new(EventType::NoteVersion, node, create.body) };
                    events::insert(conn, &stamp.later(), &first)?;
                }
                Ok(event)
            },
            |conn, id| {
                let event = event_of(conn, id)?;
                Ok(Created { path: event.path, kind: event.kind, id })
            },
        )
    }

    /// Append a message to the thread at `path`.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::BadRequest`] for an
    /// empty body or a node that is not a thread; [`Error::NotFound`].
    pub fn post(&self, signed: &Signed<'_, Post<'_>>) -> Result<Posted, Error> {
        let post = &signed.action;
        if post.body.trim().is_empty() {
            return Err(Error::BadRequest("a message needs a body".to_owned()));
        }
        self.signed(
            signed,
            keys::admit,
            |conn, stamp, _author| {
                let thread = require_kind(conn, post.path, NodeKind::Thread)?;
                check_writable(&thread)?;
                let id = events::insert(conn, stamp, &NewEvent::new(EventType::Message, thread.id, post.body))?;
                events::record_mentions(conn, id, post.body)?;
                Ok(id)
            },
            |_, id| Ok(Posted { id }),
        )
    }

    /// Save a new version of the note at `path`, over `base_version`. Its
    /// body is not searched for mentions: a state table names every agent,
    /// and each save would wake them all.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::EmptyNote`];
    /// [`Error::NoteChanged`] if `base_version` is not the current one;
    /// [`Error::Forbidden`] unless the note is the author's or the author
    /// is the owner, and for the board's own; [`Error::BadRequest`] for a
    /// `through` that is not a message.
    pub fn set_note(&self, signed: &Signed<'_, SetNote<'_>>) -> Result<NoteSaved, Error> {
        let save = &signed.action;
        if save.body.trim().is_empty() {
            return Err(Error::EmptyNote);
        }
        self.signed(
            signed,
            keys::admit,
            |conn, stamp, author| {
                let note = require_kind(conn, save.path, NodeKind::Note)?;
                check_writable(&note)?;
                if note.creator != author.name && !author.human {
                    return Err(Error::Forbidden(format!(
                        "{} is {}'s note; only they and the owner write it",
                        quoted(save.path),
                        note.creator
                    )));
                }
                let current = current_version(conn, note.id)?;
                if save.base_version != current {
                    return Err(Error::NoteChanged { version: current });
                }
                if let Some(through) = save.through
                    && events::type_of(conn, through)? != Some(EventType::Message)
                {
                    return Err(Error::BadRequest(format!("through #{through} is not a message")));
                }
                let version = current.checked_add(1).ok_or(DbError::Exhausted)?;
                let event = NewEvent {
                    version: Some(version),
                    reference: save.through,
                    ..NewEvent::new(EventType::NoteVersion, note.id, save.body)
                };
                Ok(events::insert(conn, stamp, &event)?)
            },
            |conn, id| {
                let event = event_of(conn, id)?;
                Ok(NoteSaved { path: event.path, version: event.version.unwrap_or_default(), id })
            },
        )
    }

    // board-admin's: permanent, and not reachable over HTTP.

    /// Delete the node at `path` and everything under it, with all their
    /// events and items.
    ///
    /// # Errors
    /// [`Error::Forbidden`] for the board's own nodes and for a branch that
    /// an event elsewhere points into (a note's `through`, an answer's
    /// message): it would point at nothing. As [`Board::node`] otherwise.
    pub fn delete_branch(&self, path: &str) -> Result<Deleted, Error> {
        self.write(|conn, _| {
            let top = require(conn, path)?;
            if top.generated {
                return Err(Error::Forbidden(format!("{} is the board's own", quoted(path))));
            }
            let nodes: Vec<i64> = conn
                .prepare(
                    "WITH RECURSIVE branch(id) AS
                       (SELECT ?1 UNION ALL SELECT n.id FROM nodes n JOIN branch b ON n.parent = b.id)
                     SELECT id FROM branch",
                )?
                .query_map([top.id], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            let nodes = id_list(&nodes);
            let pointing: Option<(i64, String)> = conn
                .query_row(
                    &format!(
                        "SELECT e.id, n.path FROM events e JOIN nodes n ON n.id = e.node
                          WHERE e.node NOT IN ({nodes})
                            AND e.ref IN (SELECT id FROM events WHERE node IN ({nodes})) LIMIT 1"
                    ),
                    [],
                    |r| Ok((r.get(0)?, text(r, 1)?)),
                )
                .optional()?;
            if let Some((id, at)) = pointing {
                return Err(Error::Forbidden(format!(
                    "#{id} in {} points into {}; deleting it would leave that pointing at nothing",
                    quoted(&at),
                    quoted(path)
                )));
            }
            // Items point at their resolving events and events at their
            // items: no order of deletes keeps every reference whole in
            // between, so the checks wait for the commit, where they still
            // refuse anything left dangling.
            conn.execute_batch("PRAGMA defer_foreign_keys=ON")?;
            conn.execute(
                &format!("DELETE FROM mentions WHERE event IN (SELECT id FROM events WHERE node IN ({nodes}))"),
                [],
            )?;
            let events = conn.execute(&format!("DELETE FROM events WHERE node IN ({nodes})"), [])?;
            conn.execute(&format!("DELETE FROM items WHERE queue IN ({nodes})"), [])?;
            let nodes = conn.execute(&format!("DELETE FROM nodes WHERE id IN ({nodes})"), [])?;
            Ok(Deleted { nodes, events })
        })
    }

    /// Delete one thread message.
    ///
    /// # Errors
    /// [`Error::NotFound`]; [`Error::BadRequest`] for an event that is not
    /// a message; [`Error::Forbidden`] for one a note's `through` or an
    /// answer points to.
    pub fn delete_message(&self, id: i64) -> Result<(), Error> {
        self.write(|conn, _| {
            match events::type_of(conn, id)? {
                None => return Err(Error::NotFound(format!("no event #{id}"))),
                Some(EventType::Message) => {}
                Some(other) => {
                    return Err(Error::BadRequest(format!("#{id} is a {other}; only messages are deleted one by one")));
                }
            }
            let pointing: Option<(i64, EventType, String)> = conn
                .query_row(
                    "SELECT e.id, e.type, n.path FROM events e JOIN nodes n ON n.id = e.node WHERE e.ref=? LIMIT 1",
                    [id],
                    |r| Ok((r.get(0)?, parsed(r, 1)?, text(r, 2)?)),
                )
                .optional()?;
            if let Some((by, kind, at)) = pointing {
                return Err(Error::Forbidden(format!("#{by} ({kind} on {}) points to #{id}", quoted(&at))));
            }
            conn.execute("DELETE FROM mentions WHERE event=?", [id])?;
            conn.execute("DELETE FROM events WHERE id=?", [id])?;
            Ok(())
        })
    }
}
