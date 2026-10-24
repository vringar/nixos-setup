//! Queue items: filed by anyone trusted, resolved by the queue's owner
//! (answer, decline), the owner key (decline) or the asker (cancel). Each
//! item is open until one of those, and then stays as it ended.

use std::fmt;
use std::str::FromStr;

use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension as _, Row, params, params_from_iter};
use serde::{Deserialize, Serialize};

use super::actions::{File, Filed, Resolve, Resolved, Signed, Stamp};
use super::blobs;
use super::events::{self, EVENT_FIELDS, EVENT_FROM, Event, EventType, NewEvent, event_row, parsed};
use super::keys::{self, Author};
use super::nodes::{self, NodeKind};
use super::{Board, DbError, Error, REGISTER_QUEUE, opt_text, text};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ItemState {
    Open,
    Answered,
    Declined,
    Cancelled,
    /// Phase 2: moved to another queue as a new item.
    Reassigned,
}

impl ItemState {
    pub const ALL: [Self; 5] = [Self::Open, Self::Answered, Self::Declined, Self::Cancelled, Self::Reassigned];

    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Open => "open",
            Self::Answered => "answered",
            Self::Declined => "declined",
            Self::Cancelled => "cancelled",
            Self::Reassigned => "reassigned",
        }
    }

    /// The event that moves an item into this state.
    const fn event(self) -> EventType {
        match self {
            Self::Open => EventType::ItemFiled,
            Self::Answered => EventType::ItemAnswered,
            Self::Declined => EventType::ItemDeclined,
            Self::Cancelled => EventType::ItemCancelled,
            Self::Reassigned => EventType::ItemReassigned,
        }
    }

    /// The state an item event leaves its item in.
    fn after(event: EventType) -> Option<Self> {
        Self::ALL.into_iter().find(|state| state.event() == event)
    }
}

impl fmt::Display for ItemState {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for ItemState {
    type Err = String;
    fn from_str(s: &str) -> Result<Self, String> {
        Self::ALL.into_iter().find(|state| state.as_str() == s).ok_or_else(|| format!("no item state {s:?}"))
    }
}

/// Which items a listing shows.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum StateFilter {
    #[default]
    Open,
    All,
}

/// An item, as it was filed and as it stands.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Item {
    /// Its `item_filed` event.
    pub id: i64,
    pub ts: String,
    /// The queue.
    pub path: String,
    /// The queue's owner.
    pub owner: String,
    /// `None` for a registration: its key has no name yet.
    pub asker: Option<String>,
    pub state: ItemState,
    pub body: String,
    /// The owner archived it (its resolution with it). Only in reads that
    /// ask for archived items.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub archived: bool,
    /// The event that resolved it, once one has.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<ItemResolution>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ItemResolution {
    pub id: i64,
    pub event: EventType,
    pub from: Option<String>,
    pub body: String,
    /// An answer's message.
    #[serde(rename = "ref")]
    pub reference: Option<i64>,
}

/// An item and every event about it, oldest first.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ItemDetail {
    pub item: Item,
    pub events: Vec<Event>,
}

/// Items across queues.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct ItemQuery<'a> {
    /// Only in queues this name owns.
    pub owner: Option<&'a str>,
    /// Only those this name asked.
    pub asker: Option<&'a str>,
    pub state: StateFilter,
    /// Only those with an id above this: the next page after a truncated one.
    pub after: i64,
    /// The archived ones too.
    pub archived: bool,
}

/// Items, oldest first, at most [`super::READ_ROWS_MAX`] of them.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ItemPage {
    pub items: Vec<Item>,
    /// There are more: ask again with `after` the last id.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub truncated: bool,
}

const ITEM_SELECT: &str = "SELECT i.id, f.ts, n.path, n.owner, i.asker, i.state, f.body,
            r.id, r.type, r.actor, r.body, r.ref, f.archived IS NOT NULL
       FROM items i JOIN events f ON f.id = i.id JOIN nodes n ON n.id = i.queue
       LEFT JOIN events r ON r.id = i.resolved";

fn item_row(r: &Row<'_>) -> rusqlite::Result<Item> {
    let resolution = match r.get::<_, Option<i64>>(7)? {
        None => None,
        Some(id) => Some(ItemResolution {
            id,
            event: parsed(r, 8)?,
            from: opt_text(r, 9)?,
            body: text(r, 10)?,
            reference: r.get(11)?,
        }),
    };
    Ok(Item {
        id: r.get(0)?,
        ts: text(r, 1)?,
        path: text(r, 2)?,
        owner: text(r, 3)?,
        asker: opt_text(r, 4)?,
        state: parsed(r, 5)?,
        body: text(r, 6)?,
        archived: r.get(12)?,
        resolution,
    })
}

pub(super) fn get(conn: &Connection, id: i64) -> rusqlite::Result<Option<Item>> {
    conn.query_row(&format!("{ITEM_SELECT} WHERE i.id=?"), [id], item_row).optional()
}

/// Items matching `query`, oldest first; `queue` narrows them to one.
pub(super) fn list(conn: &Connection, query: &ItemQuery<'_>, queue: Option<i64>) -> rusqlite::Result<ItemPage> {
    let mut sql = format!("{ITEM_SELECT} WHERE i.id>?");
    let mut args = vec![Value::Integer(query.after)];
    if let Some(queue) = queue {
        sql.push_str(" AND i.queue=?");
        args.push(Value::Integer(queue));
    }
    if let Some(owner) = query.owner {
        sql.push_str(" AND n.owner=?");
        args.push(Value::Text(owner.to_owned()));
    }
    if let Some(asker) = query.asker {
        sql.push_str(" AND i.asker=?");
        args.push(Value::Text(asker.to_owned()));
    }
    if query.state == StateFilter::Open {
        sql.push_str(" AND i.state='open'");
    }
    if !query.archived {
        sql.push_str(" AND f.archived IS NULL");
    }
    sql.push_str(" ORDER BY i.id");
    let mut statement = conn.prepare(&sql)?;
    let rows = statement.query_map(params_from_iter(args), item_row)?;
    let (items, truncated) = super::capped(rows, |item| {
        item.body.len().saturating_add(item.resolution.as_ref().map_or(0, |r| r.body.len()))
    })?;
    Ok(ItemPage { items, truncated })
}

/// File an item in queue `queue`; returns its id, which is its
/// `item_filed` event's.
pub(super) fn insert(
    conn: &Connection,
    stamp: &Stamp<'_>,
    queue: i64,
    body: &str,
    asker: Option<&str>,
) -> rusqlite::Result<i64> {
    // The event names its item and the item is its event's id: the event
    // goes first, and names the item once that exists.
    let id = events::insert(conn, stamp, &NewEvent::new(EventType::ItemFiled, queue, body))?;
    conn.execute(
        "INSERT INTO items(id, queue, asker, asker_fp, state) VALUES(?,?,?,?,'open')",
        params![id, queue, asker, stamp.actor_fp],
    )?;
    conn.execute("UPDATE events SET item=?1 WHERE id=?1", [id])?;
    Ok(id)
}

/// Move open item `item` (in queue `queue`) to `state` by a new event;
/// returns the event's id.
pub(super) fn close(
    conn: &Connection,
    stamp: &Stamp<'_>,
    item: &Item,
    queue: i64,
    state: ItemState,
    body: &str,
    reference: Option<i64>,
) -> rusqlite::Result<i64> {
    let event = NewEvent { item: Some(item.id), reference, ..NewEvent::new(state.event(), queue, body) };
    let id = events::insert(conn, stamp, &event)?;
    conn.execute("UPDATE items SET state=?, resolved=? WHERE id=?", params![state.as_str(), id, item.id])?;
    Ok(id)
}

/// Refuse `state` for an item that is not open.
pub(super) fn check_open(item: &Item) -> Result<(), Error> {
    if item.state != ItemState::Open {
        return Err(Error::ItemNotOpen {
            item: item.id,
            state: item.state,
            resolved: item.resolution.as_ref().map(|r| r.id),
        });
    }
    Ok(())
}

/// Refuse `author` resolving `item` as `state` (answered, declined or
/// cancelled): the queue's owner answers, the owner or the owner key
/// declines, the asker cancels.
fn check_resolver(item: &Item, state: ItemState, author: &Author) -> Result<(), Error> {
    let (allowed, who) = match state {
        ItemState::Answered => (item.owner == author.name, format!("{}, the queue's owner, answers", item.owner)),
        ItemState::Declined => (
            item.owner == author.name || author.human,
            format!("{}, the queue's owner, or the board's owner declines", item.owner),
        ),
        // resolve() lets only the three resolutions through
        _ => match &item.asker {
            Some(asker) => (*asker == author.name, format!("{asker}, who asked, cancels")),
            None => (false, "a registration is not cancelled".to_owned()),
        },
    };
    if !allowed {
        return Err(Error::Forbidden(format!("#{}: only {who}", item.id)));
    }
    Ok(())
}

impl Board {
    /// The items of the queue at `path`, oldest first, in a page of at most
    /// [`READ_ROWS_MAX`](super::READ_ROWS_MAX).
    ///
    /// # Errors
    /// As [`Board::node`], and [`Error::BadRequest`] for another kind.
    pub fn queue(&self, path: &str, state: StateFilter, after: i64) -> Result<ItemPage, Error> {
        self.queue_with(path, state, after, false)
    }

    /// [`Board::queue`], with the archived items too (marked) if `archived`.
    ///
    /// # Errors
    /// As [`Board::queue`].
    pub fn queue_with(&self, path: &str, state: StateFilter, after: i64, archived: bool) -> Result<ItemPage, Error> {
        self.read(|conn| {
            let queue = nodes::require_kind(conn, path, NodeKind::Queue)?;
            Ok(list(conn, &ItemQuery { state, after, archived, ..ItemQuery::default() }, Some(queue.id))?)
        })
    }

    /// One item and its events, archived or not.
    ///
    /// # Errors
    /// On a database fault.
    pub fn item(&self, id: i64) -> Result<Option<ItemDetail>, DbError> {
        self.read_db(|conn| {
            let Some(item) = get(conn, id)? else {
                return Ok(None);
            };
            let events = conn
                .prepare_cached(&format!("SELECT {EVENT_FIELDS} FROM {EVENT_FROM} WHERE e.item=? ORDER BY e.id"))?
                .query_map([id], event_row)?
                .collect::<rusqlite::Result<_>>()?;
            Ok(Some(ItemDetail { item, events }))
        })
    }

    /// Items across every queue, oldest first.
    ///
    /// # Errors
    /// On a database fault.
    pub fn items(&self, query: &ItemQuery<'_>) -> Result<ItemPage, DbError> {
        self.read_db(|conn| list(conn, query, None))
    }

    /// File an item in the queue at `path`.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::BadRequest`] for an
    /// empty body or a node that is not a queue; [`Error::NotFound`];
    /// [`Error::Forbidden`] for the register queue, which only `register`
    /// files into.
    pub fn file(&self, signed: &Signed<'_, File<'_>>) -> Result<Filed, Error> {
        let file = &signed.action;
        if file.body.trim().is_empty() {
            return Err(Error::BadRequest("an item needs a body".to_owned()));
        }
        self.signed(
            signed,
            keys::admit,
            |conn, stamp, author| {
                let queue = nodes::require_kind(conn, file.path, NodeKind::Queue)?;
                nodes::check_writable(&queue)?;
                let id = insert(conn, stamp, queue.id, file.body, Some(&author.name))?;
                events::record_mentions(conn, id, file.body)?;
                blobs::record_refs(conn, id, file.body)?;
                Ok(id)
            },
            |conn, id| Ok(Filed { item: id, path: nodes::event_of(conn, id)?.path }),
        )
    }

    /// Answer, decline or cancel an open item. A registration is declined
    /// here (its key is declined with it) but approved by
    /// [`Board::approve`].
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::NotFound`];
    /// [`Error::Archived`] for an archived item or `ref`;
    /// [`Error::ItemNotOpen`] before [`Error::Forbidden`] as [`ItemState`]'s
    /// rules say; [`Error::BadRequest`] for another state, a
    /// missing body, or a `ref` that is not a message or not on an answer.
    pub fn resolve(&self, signed: &Signed<'_, Resolve<'_>>) -> Result<Resolved, Error> {
        let resolve = &signed.action;
        if !matches!(resolve.state, ItemState::Answered | ItemState::Declined | ItemState::Cancelled) {
            return Err(Error::BadRequest(format!(
                "an item is resolved as answered, declined or cancelled, not {}",
                resolve.state
            )));
        }
        if resolve.state != ItemState::Cancelled && resolve.body.trim().is_empty() {
            return Err(Error::BadRequest(format!("to resolve an item as {}, say why", resolve.state)));
        }
        if resolve.reference.is_some() && resolve.state != ItemState::Answered {
            return Err(Error::BadRequest("only an answer refers to a message".to_owned()));
        }
        self.signed(
            signed,
            keys::admit,
            |conn, stamp, author| {
                let item =
                    get(conn, resolve.item)?.ok_or_else(|| Error::NotFound(format!("no item #{}", resolve.item)))?;
                if item.archived {
                    return Err(Error::Archived { id: item.id });
                }
                // Open first: whoever it is, an item that is resolved already
                // is not open, and says what became of it.
                check_open(&item)?;
                check_resolver(&item, resolve.state, author)?;
                let registration = item.path == REGISTER_QUEUE;
                if registration && resolve.state == ItemState::Answered {
                    return Err(Error::BadRequest(format!(
                        "#{} is a registration: approve it (board approve <name>), which binds its name to its key",
                        item.id
                    )));
                }
                if let Some(reference) = resolve.reference
                    && events::type_of(conn, reference)? != Some(EventType::Message)
                {
                    return Err(Error::BadRequest(format!("ref #{reference} is not a message")));
                }
                if let Some(reference) = resolve.reference
                    && events::is_archived(conn, reference)?
                {
                    return Err(Error::Archived { id: reference });
                }
                let queue = nodes::require(conn, &item.path)?;
                let id = close(conn, stamp, &item, queue.id, resolve.state, resolve.body, resolve.reference)?;
                if registration {
                    keys::decline_registration(conn, item.id)?;
                } else {
                    // The register queue's bodies carry what an unknown key
                    // wrote; nothing in it wakes anyone by name.
                    events::record_mentions(conn, id, resolve.body)?;
                    blobs::record_refs(conn, id, resolve.body)?;
                }
                Ok(id)
            },
            resolved,
        )
    }
}

/// The outcome of an item event.
pub(super) fn resolved(conn: &Connection, id: i64) -> Result<Resolved, Error> {
    let event = nodes::event_of(conn, id)?;
    let (Some(item), Some(state)) = (event.item, ItemState::after(event.event)) else {
        return Err(Error::NotFound(format!("event #{id} is no item's")));
    };
    Ok(Resolved { item, state, id })
}
