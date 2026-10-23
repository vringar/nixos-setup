//! Archiving a message or an item: the owner key hides it from reads and
//! watches, and can show it again. Nothing is deleted and nothing is
//! renumbered: the target keeps its id, and the archive and unarchive are
//! events of their own.
//!
//! The target's `archived` column holds the archive event while it is
//! archived. An item is archived by its id (its filing's), which hides the
//! filing and its resolution; a note's versions are never archived (a newer
//! one supersedes, and the history stays as it is), nor are registrations.
//!
//! The events are `node_archived` and `node_unarchived`, with `ref` the
//! target: a node's own archiving (phase 2) has none.

use rusqlite::{Connection, OptionalExtension as _};

use super::actions::{ARCHIVE_REASON_MAX, Archive, Archived, Signed};
use super::events::{self, EventType, NewEvent, parsed};
use super::keys;
use super::nodes;
use super::{Board, Error, REGISTER_QUEUE, text};

/// The latest archive or unarchive event of `target`, if any.
fn latest(conn: &Connection, target: i64) -> rusqlite::Result<Option<i64>> {
    conn.query_row(
        "SELECT id FROM events WHERE ref=? AND type IN ('node_archived','node_unarchived')
          ORDER BY id DESC LIMIT 1",
        [target],
        |r| r.get(0),
    )
    .optional()
}

/// What an archive event, or an untouched target, tells the client.
fn outcome(conn: &Connection, id: i64) -> Result<Archived, Error> {
    let event = nodes::event_of(conn, id)?;
    Ok(match (event.event, event.reference) {
        (EventType::NodeArchived, Some(target)) => Archived { id: Some(id), event: target, hidden: true },
        (EventType::NodeUnarchived, Some(target)) => Archived { id: Some(id), event: target, hidden: false },
        // nothing was written: `id` is the target, which was never archived
        _ => Archived { id: None, event: id, hidden: false },
    })
}

impl Board {
    /// The owner archives (or unarchives) message or item `action.event`.
    /// Already as asked is success with nothing written.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::Forbidden`] unless the
    /// owner key signed; [`Error::NotFound`]; [`Error::BadRequest`] for an
    /// event that is no message or item (the message names the item of a
    /// resolution), for a registration, and for a reason over
    /// [`ARCHIVE_REASON_MAX`] bytes.
    pub fn archive(&self, signed: &Signed<'_, Archive<'_>>) -> Result<Archived, Error> {
        let archive = &signed.action;
        if archive.reason.len() > ARCHIVE_REASON_MAX {
            return Err(Error::BadRequest(format!("a reason is at most {ARCHIVE_REASON_MAX} bytes")));
        }
        self.signed(
            signed,
            keys::admit,
            |conn, stamp, author| {
                if !author.human {
                    return Err(Error::Forbidden("only the owner key archives and unarchives".to_owned()));
                }
                let target = archive.event;
                let (kind, node, item, archived, path) = conn
                    .query_row(
                        "SELECT e.type, e.node, e.item, e.archived, n.path
                           FROM events e JOIN nodes n ON n.id = e.node WHERE e.id=?",
                        [target],
                        |r| Ok((parsed::<EventType>(r, 0)?, r.get::<_, i64>(1)?, r.get::<_, Option<i64>>(2)?, r.get::<_, Option<i64>>(3)?, text(r, 4)?)),
                    )
                    .optional()?
                    .ok_or_else(|| Error::NotFound(format!("no message or item #{target}")))?;
                match (kind, item) {
                    (EventType::ItemFiled, _) if path == REGISTER_QUEUE => {
                        return Err(Error::BadRequest(format!(
                            "#{target} is a registration: approve or decline it; registrations are not archived"
                        )));
                    }
                    (EventType::Message | EventType::ItemFiled, _) => {}
                    (EventType::ItemAnswered | EventType::ItemDeclined | EventType::ItemCancelled, Some(item)) => {
                        return Err(Error::BadRequest(format!(
                            "#{target} resolved item #{item}: archive the item (#{item}), which hides its resolution too"
                        )));
                    }
                    (other, _) => {
                        return Err(Error::BadRequest(format!(
                            "#{target} is a {other}: only messages and queue items are archived"
                        )));
                    }
                }
                if archived.is_some() == archive.archived {
                    // as asked already: nothing to write
                    return Ok(latest(conn, target)?.unwrap_or(target));
                }
                let kind = if archive.archived { EventType::NodeArchived } else { EventType::NodeUnarchived };
                let event = NewEvent {
                    item: (item == Some(target)).then_some(target),
                    reference: Some(target),
                    ..NewEvent::new(kind, node, archive.reason.trim())
                };
                let id = events::insert(conn, stamp, &event)?;
                conn.execute("UPDATE events SET archived=? WHERE id=?", rusqlite::params![archive.archived.then_some(id), target])?;
                Ok(id)
            },
            outcome,
        )
    }
}
