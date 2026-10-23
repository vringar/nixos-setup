//! Watching: the server-side filtered long poll, and `brief`, the startup
//! read that ends with the cursor to watch from.
//!
//! There is no board-wide stream. A watch names leaves (all their events),
//! containers (what happens to their direct children: created, archived, a
//! note's new version), and people (mentions, items asked or owned), and
//! gets the events that match any of those, minus those from `not_from`.
//! The cursor it gets back is the head, so it moves past every event that
//! did not match without fetching it.
//!
//! What the owner archived is not announced: a message or an item's filing
//! that is archived, and an archived item's resolution, are skipped. The
//! archive event itself is a doorbell on its node (the thread or queue, and
//! the item's owner, asker and watchers), so that a watcher can drop what it
//! heard; it is no structural event of the container above.

use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use rusqlite::Connection;
use rusqlite::types::Value;
use serde::Serialize;

use super::events::{EVENT_COLUMNS, EVENT_FIELDS, EVENT_FROM, Event, EventType, event_archived, event_row, parsed};
use super::items::{self, Item, ItemQuery, ItemState, StateFilter};
use super::nodes::{self, Child, NodeKind, NoteVersion};
use super::{Board, DbError, Error, POLL_BYTES, POLL_CHECK, POLL_LIMIT, ROOT, head};

/// What a watch is subscribed to. A watch with no path and no filter is
/// refused; `not_from` alone is none, since it only takes events away.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Watch {
    /// Leaves (every event on them), containers (their own structural
    /// events, and their direct children's), and [`ROOT`] (the top-level
    /// nodes' structural events).
    pub paths: Vec<String>,
    /// Events whose body mentions one of these names.
    pub mentions: Vec<String>,
    /// Never events by these names.
    pub not_from: Vec<String>,
    /// Item events on items these names asked.
    pub asked_by: Vec<String>,
    /// Item events in queues these names own.
    pub owners: Vec<String>,
    /// Events on these items.
    pub items: Vec<i64>,
}

/// Why an event was delivered: which subscriptions it matched.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Why {
    /// On a subscribed leaf, or a structural event on a subscribed container.
    Path,
    /// A direct child of a subscribed container was created, archived or
    /// unarchived, or (a note) got a new version; or, watching [`ROOT`], a
    /// top-level node was created, archived or unarchived.
    Child,
    Mention,
    Asked,
    Owner,
    Item,
}

/// An event as a watch delivers it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Delivered {
    #[serde(flatten)]
    pub event: Event,
    /// For an item event, its item's state now (not as of the event).
    pub state: Option<ItemState>,
    pub why: Vec<Why>,
}

/// One poll's reply.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Page {
    pub events: Vec<Delivered>,
    /// Where the next poll goes on from: the head when the reply was made,
    /// or the last event's id when the page is full.
    pub cursor: i64,
}

/// What an agent reads at startup.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Brief {
    /// The head, taken first: the cursor to watch from.
    pub head: i64,
    /// Every container on the paths, root first.
    pub containers: Vec<BriefContainer>,
    /// Open items in queues the name owns, anywhere.
    pub owned: Vec<Item>,
    /// Open items the name asked, other than those in `owned`.
    pub asked: Vec<Item>,
    /// The paths to watch: the containers, then the paths given.
    pub watch: Vec<String>,
}

/// A container on a brief's paths: its listing, and the current version of
/// every note directly in it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BriefContainer {
    pub path: String,
    pub children: Vec<Child>,
    pub notes: Vec<NoteVersion>,
}

/// The structural event types: what a container's watcher hears of itself
/// and of its children (and, of a child, also a note's new version).
const STRUCTURAL: [EventType; 3] = [EventType::NodeCreated, EventType::NodeArchived, EventType::NodeUnarchived];

/// The subscriptions in the order of [`Filter`]'s flag columns.
const REASONS: [Why; 6] = [Why::Path, Why::Child, Why::Mention, Why::Asked, Why::Owner, Why::Item];

/// A [`Watch`] resolved against the tree, ready to run as one query.
struct Filter {
    sql: String,
    /// The names in the query, `?2` on; `since` is `?1`.
    names: Vec<Value>,
}

fn type_list(types: &[EventType]) -> String {
    types.iter().map(|t| format!("'{}'", t.as_str())).collect::<Vec<_>>().join(",")
}

/// Numbered parameters for `values`, which go into `names` in order.
/// Numbered, since the names stand in the text before and after `since`,
/// which is `?1`.
fn placeholders(values: &[String], names: &mut Vec<Value>) -> String {
    let mut out = Vec::with_capacity(values.len());
    for value in values {
        names.push(Value::Text(value.clone()));
        out.push(format!("?{}", names.len().saturating_add(1)));
    }
    out.join(",")
}

fn id_list(ids: &[i64]) -> String {
    ids.iter().map(i64::to_string).collect::<Vec<_>>().join(",")
}

impl Filter {
    /// Resolve the watch's paths to node ids (a missing one is an error:
    /// to hear a leaf's creation, watch its container) and build the query.
    ///
    /// Each subscription is one flag column, in the order of [`REASONS`],
    /// so that a row says which matched; the outer query keeps the rows
    /// where one did. Ids go into the text (they are the board's own
    /// integers), names are parameters. An empty `IN ()` is false.
    fn new(conn: &Connection, watch: &Watch) -> Result<Self, Error> {
        let unfiltered = watch.paths.is_empty()
            && watch.mentions.is_empty()
            && watch.asked_by.is_empty()
            && watch.owners.is_empty()
            && watch.items.is_empty();
        if unfiltered {
            return Err(Error::BadRequest(
                "a watch needs a path or a filter (mention, asked-by, owner, item): there is no board-wide stream"
                    .to_owned(),
            ));
        }
        let (mut leaves, mut containers, mut root) = (Vec::new(), Vec::new(), false);
        for path in &watch.paths {
            if path == ROOT {
                root = true;
                continue;
            }
            let node = nodes::require(conn, path)?;
            if node.kind == NodeKind::Container { &mut containers } else { &mut leaves }.push(node.id);
        }
        let (leaves, containers) = (id_list(&leaves), id_list(&containers));
        let structural = type_list(&STRUCTURAL);
        let child_types = type_list(&[&STRUCTURAL[..], &[EventType::NoteVersion]].concat());
        // An archive event of a message or item (it has a `ref`) is its
        // leaf's, not the container's: a container's watcher heard of no message.
        let node_level = "(e.ref IS NULL OR e.type NOT IN ('node_archived','node_unarchived'))";
        let mut names = Vec::new();
        let mut flags = vec![
            format!("e.node IN ({leaves}) OR (e.node IN ({containers}) AND e.type IN ({structural}) AND {node_level})"),
            format!(
                "(n.parent IN ({containers}) AND e.type IN ({child_types}) AND {node_level}){}",
                if root {
                    format!(" OR (n.parent IS NULL AND e.type IN ({structural}) AND {node_level})")
                } else {
                    String::new()
                }
            ),
        ];
        flags.push(if watch.mentions.is_empty() {
            "0".to_owned()
        } else {
            let mentions = placeholders(&watch.mentions, &mut names);
            format!("EXISTS (SELECT 1 FROM mentions m WHERE m.event = e.id AND m.name IN ({mentions}))")
        });
        flags.push(if watch.asked_by.is_empty() {
            "0".to_owned()
        } else {
            format!("e.item IS NOT NULL AND i.asker IN ({})", placeholders(&watch.asked_by, &mut names))
        });
        flags.push(if watch.owners.is_empty() {
            "0".to_owned()
        } else {
            format!("e.item IS NOT NULL AND n.owner IN ({})", placeholders(&watch.owners, &mut names))
        });
        flags.push(format!("e.item IN ({})", id_list(&watch.items)));
        // A NULL (a top-level node has no parent) counts as no match.
        let columns: Vec<String> =
            flags.iter().enumerate().map(|(at, flag)| format!("coalesce(({flag}), 0) AS w{at}")).collect();
        let any = (0..flags.len()).map(|at| format!("w{at}")).collect::<Vec<_>>().join(" OR ");
        let not_from = if watch.not_from.is_empty() {
            String::new()
        } else {
            // a registration has no actor, and NULL NOT IN (...) is not true
            format!(" AND (actor IS NULL OR actor NOT IN ({}))", placeholders(&watch.not_from, &mut names))
        };
        let sql = format!(
            "SELECT * FROM (SELECT {EVENT_FIELDS}, i.state AS item_state, {columns}
                              FROM {EVENT_FROM} LEFT JOIN items i ON i.id = e.item
                             WHERE e.id > ?1 AND NOT {archived})
              WHERE ({any}){not_from}
              ORDER BY id LIMIT {POLL_LIMIT}",
            columns = columns.join(", "),
            archived = event_archived!()
        );
        Ok(Self { sql, names })
    }

    /// The matching events after `since`, and whether the page is full.
    fn run(&self, conn: &Connection, since: i64) -> rusqlite::Result<(Vec<Delivered>, bool)> {
        let args = std::iter::once(Value::Integer(since)).chain(self.names.iter().cloned());
        let mut statement = conn.prepare_cached(&self.sql)?;
        let mut rows = statement.query(rusqlite::params_from_iter(args))?;
        let mut found = Vec::new();
        let mut bytes = 0_usize;
        while let Some(row) = rows.next()? {
            let event = event_row(row)?;
            let state = match row.get_ref(EVENT_COLUMNS)? {
                rusqlite::types::ValueRef::Null => None,
                _ => Some(parsed(row, EVENT_COLUMNS)?),
            };
            let mut why = Vec::new();
            for (at, reason) in REASONS.into_iter().enumerate() {
                if row.get::<_, bool>(EVENT_COLUMNS.saturating_add(1).saturating_add(at))? {
                    why.push(reason);
                }
            }
            bytes = bytes.saturating_add(event.body.len()).saturating_add(event.path.len());
            found.push(Delivered { event, state, why });
            if bytes >= POLL_BYTES {
                return Ok((found, true));
            }
        }
        let full = i64::try_from(found.len()).is_ok_and(|n| n >= POLL_LIMIT);
        Ok((found, full))
    }
}

impl Board {
    /// The long poll: the events after `since` that `watch` matches (at
    /// most [`POLL_LIMIT`], and none after the one that reaches
    /// [`POLL_BYTES`]), waiting up to `timeout` for one to happen. While it
    /// waits it asks `wanted` every second or so, and gives up with `None`
    /// once that says no: a client that has gone must not hold its thread
    /// and socket for the rest of the timeout.
    ///
    /// # Errors
    /// [`Error::BadRequest`] for an empty watch, [`Error::NotFound`] for a
    /// path that does not exist, and on a database fault.
    pub fn poll(
        &self,
        watch: &Watch,
        since: i64,
        timeout: Duration,
        wanted: impl Fn() -> bool,
    ) -> Result<Option<Page>, Error> {
        let deadline = Instant::now().checked_add(timeout);
        // Query and wait under the same lock: a write cannot land between
        // the two, so no wakeup is lost.
        let mut guard = self.lock()?;
        let filter = Filter::new(guard.as_ref().ok_or(DbError::Closed)?, watch)?;
        let mut scan_from = since;
        loop {
            let conn = guard.as_ref().ok_or(DbError::Closed)?;
            let (events, full) = filter.run(conn, scan_from)?;
            let head = head(conn)?;
            let remaining = deadline.and_then(|d| d.checked_duration_since(Instant::now()));
            match remaining {
                Some(left) if events.is_empty() && !left.is_zero() => {
                    if !wanted() {
                        return Ok(None);
                    }
                    // Nothing up to the head matched: the next scan starts
                    // there, not at `since` again -- but never before
                    // `since`, which a client may hold ahead of the head.
                    scan_from = head.max(scan_from);
                    guard = self.changed.wait_timeout(guard, left.min(POLL_CHECK)).map_err(DbError::from)?.0;
                }
                _ => {
                    let cursor = match events.last() {
                        Some(last) if full => last.event.id,
                        _ => head.max(since),
                    };
                    return Ok(Some(Page { events, cursor }));
                }
            }
        }
    }

    /// The startup read for an agent whose home is `paths`: for every
    /// container on them, root first, its listing and the notes directly in
    /// it; then, for `name`, the open items of the queues it owns and those
    /// it asked. Threads are never part of it. Without a name (a key not
    /// bound yet) there are no items.
    ///
    /// # Errors
    /// [`Error::BadRequest`] without paths, [`Error::NotFound`] for a path
    /// that does not exist, and on a database fault.
    pub fn brief(&self, paths: &[&str], name: Option<&str>) -> Result<Brief, Error> {
        if paths.is_empty() {
            return Err(Error::BadRequest("a brief needs the paths the agent works on".to_owned()));
        }
        self.read(|conn| {
            let head = head(conn)?;
            let mut containers: Vec<String> = Vec::new();
            for path in paths {
                let node = nodes::require(conn, path)?;
                let mut on_path: Vec<&str> =
                    path.match_indices('/').map(|(at, _)| path.get(..at).unwrap_or_default()).collect();
                if node.kind == NodeKind::Container {
                    on_path.push(path);
                }
                for container in on_path {
                    if !containers.iter().any(|c| c == container) {
                        containers.push(container.to_owned());
                    }
                }
            }
            // root first: a parent's path is shorter than its children's
            containers.sort_by_key(|path| path.matches('/').count());
            let mut sections = Vec::with_capacity(containers.len());
            for path in &containers {
                let node = nodes::require(conn, path)?;
                sections.push(BriefContainer {
                    path: path.clone(),
                    children: nodes::children(conn, Some(node.id))?,
                    notes: nodes::notes_in(conn, node.id)?,
                });
            }
            let (owned, asked) = match name {
                None => (Vec::new(), Vec::new()),
                Some(name) => {
                    let open = |query: ItemQuery<'_>| items::list(conn, &query, None).map(|page| page.items);
                    let owned =
                        open(ItemQuery { owner: Some(name), state: StateFilter::Open, ..ItemQuery::default() })?;
                    let seen: BTreeSet<i64> = owned.iter().map(|item| item.id).collect();
                    let asked =
                        open(ItemQuery { asker: Some(name), state: StateFilter::Open, ..ItemQuery::default() })?
                            .into_iter()
                            .filter(|item| !seen.contains(&item.id))
                            .collect();
                    (owned, asked)
                }
            };
            let mut watch = containers.clone();
            for path in paths {
                if !watch.iter().any(|w| w == path) {
                    watch.push((*path).to_owned());
                }
            }
            Ok(Brief { head, containers: sections, owned, asked, watch })
        })
    }
}
