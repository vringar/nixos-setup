//! The JSON API: the reads (open to anyone who can connect, as in v1) and
//! the signed writes. Each answer is the store's own type, serialized; the
//! shapes are the spec's HTTP API table.

use std::time::Duration;

use serde::Serialize;

use message_board::store::{
    Board, Child, Error as StoreError, Item, ItemQuery, Message, Node, NodeKind, NoteVersion, Owner, Range,
    StateFilter, THREAD_TAIL, Version, Watch, check_name,
};

use crate::http::Response;
use crate::routes::{Answer, Failure, Params};
use crate::writes::{self, Fields, Write};

/// The longest a poll may wait, in seconds.
const MAX_POLL: f64 = 300.0;
/// How long a poll waits unless told, in seconds.
const DEFAULT_POLL: f64 = 50.0;

fn json(value: &impl Serialize) -> Answer {
    Ok(Response::json(200, serde_json::to_string(value)?))
}

#[derive(Serialize)]
struct Health {
    head: i64,
    /// `None` until an owner key is configured: then nothing can be approved.
    owner: Option<Owner>,
}

pub fn healthz(board: &Board) -> Answer {
    json(&Health { head: board.head()?, owner: board.owner()? })
}

/// What `GET /n/<path>` answers, by the node's kind.
#[derive(Serialize)]
#[serde(untagged)]
enum Read {
    /// A container, or the top level (no node).
    Children {
        node: Option<Node>,
        children: Vec<Child>,
    },
    /// `None` for a note without a version yet.
    Note {
        node: Node,
        note: Option<NoteVersion>,
    },
    History {
        node: Node,
        history: Vec<Version>,
    },
    Thread {
        node: Node,
        messages: Vec<Message>,
        earlier: i64,
    },
    Queue {
        node: Node,
        items: Vec<Item>,
    },
}

/// Which messages of a thread a query asks for: `last=N` (the default
/// tail), `since=ID` or `all=1`, at most one of them.
pub fn range(q: &Params) -> Result<Range, StoreError> {
    match (q.number("last")?, q.number("since")?, q.flag("all")) {
        (None, None, false) => Ok(Range::Last(THREAD_TAIL)),
        (Some(last), None, false) => Ok(Range::Last(last)),
        (None, Some(since), false) => Ok(Range::Since(since)),
        (None, None, true) => Ok(Range::All),
        _ => Err(StoreError::BadRequest("ask for one of last, since and all".to_owned())),
    }
}

/// Which items a query asks for: `state=open` (the default) or `all`.
pub fn state_filter(q: &Params) -> Result<StateFilter, StoreError> {
    match q.get("state").unwrap_or_default() {
        "" | "open" => Ok(StateFilter::Open),
        "all" => Ok(StateFilter::All),
        other => Err(StoreError::BadRequest(format!("state {other:?} is neither open nor all"))),
    }
}

/// A node, read by its kind; the top level for an empty path.
pub fn node(board: &Board, path: &str, q: &Params) -> Answer {
    if path.is_empty() {
        return json(&Read::Children { node: None, children: board.children("")? });
    }
    let node = board.node(path)?;
    let read = match node.kind {
        NodeKind::Container => Read::Children { children: board.children(path)?, node: Some(node) },
        NodeKind::Note if q.flag("history") => Read::History { history: board.note_history(path)?, node },
        NodeKind::Note => Read::Note { note: board.note(path, q.number("version")?)?, node },
        NodeKind::Thread => {
            let page = board.thread(path, range(q)?)?;
            Read::Thread { node, messages: page.messages, earlier: page.earlier }
        }
        NodeKind::Queue => Read::Queue { items: board.queue(path, state_filter(q)?)?, node },
    };
    json(&read)
}

fn id(text: &str) -> Result<i64, StoreError> {
    text.parse().map_err(|err| StoreError::BadRequest(format!("{text:?} is not an id: {err}")))
}

pub fn event(board: &Board, text: &str) -> Answer {
    let id = id(text)?;
    json(&board.event(id)?.ok_or_else(|| StoreError::NotFound(format!("no event #{id}")))?)
}

pub fn item(board: &Board, text: &str) -> Answer {
    let id = id(text)?;
    json(&board.item(id)?.ok_or_else(|| StoreError::NotFound(format!("no item #{id}")))?)
}

/// A name a query filters by, refused if it could be no one's: a filter
/// with a typo in it would quietly match nothing.
fn name<'a>(q: &'a Params, field: &str) -> Result<Option<&'a str>, StoreError> {
    q.get(field).filter(|n| !n.is_empty()).map(|n| check_name(n).map(|()| n)).transpose()
}

fn names(q: &Params, field: &str) -> Result<Vec<String>, StoreError> {
    q.all(field).map(|n| check_name(n).map(|()| n.to_owned())).collect()
}

pub fn items(board: &Board, q: &Params) -> Answer {
    let query = ItemQuery { owner: name(q, "owner")?, asker: name(q, "asker")?, state: state_filter(q)? };
    json(&board.items(&query)?)
}

pub fn keys(board: &Board) -> Answer {
    json(&board.keys()?)
}

pub fn key(board: &Board, fingerprint: &str) -> Answer {
    json(&board.key(fingerprint)?.ok_or_else(|| StoreError::NotFound(format!("no key {fingerprint:?}")))?)
}

/// The startup read for `path`s, with the items of the `as` name.
pub fn brief(board: &Board, q: &Params) -> Answer {
    let paths: Vec<&str> = q.all("path").collect();
    json(&board.brief(&paths, name(q, "as")?)?)
}

/// The long poll: `since`, `timeout` (seconds, at most [`MAX_POLL`]) and
/// the watch's subscriptions, each repeatable: `path` (`/` is the top
/// level), `mention`, `not_from`, `asked_by`, `owner`, `item`.
pub fn poll(board: &Board, q: &Params, waiting: &dyn Fn() -> bool) -> Answer {
    let since = q.number("since")?.unwrap_or(0);
    let timeout = match q.get("timeout").filter(|t| !t.is_empty()) {
        None => DEFAULT_POLL,
        Some(text) => text
            .parse::<f64>()
            .ok()
            .filter(|t| !t.is_nan())
            .ok_or_else(|| StoreError::BadRequest(format!("timeout {text:?} is not a number of seconds")))?,
    };
    // In 0..=300 and not NaN: always a duration.
    let wait = Duration::try_from_secs_f64(timeout.clamp(0.0, MAX_POLL)).unwrap_or(Duration::ZERO);
    let items = q.all("item").map(id).collect::<Result<_, _>>()?;
    let watch = Watch {
        paths: q.all("path").map(str::to_owned).collect(),
        mentions: names(q, "mention")?,
        not_from: names(q, "not_from")?,
        asked_by: names(q, "asked_by")?,
        owners: names(q, "owner")?,
        items,
    };
    json(&board.poll(&watch, since, wait, waiting)?.ok_or(Failure::Gone)?)
}

/// A signed write from a JSON body; its outcome.
pub fn write(board: &Board, write: Write, fields: &impl Fields) -> Answer {
    json(&writes::apply(board, write, fields)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_thread_read_asks_for_one_range() {
        let range = |q: &[u8]| range(&Params::parse(q));
        assert!(matches!(range(b""), Ok(Range::Last(THREAD_TAIL))));
        assert!(matches!(range(b"last=5"), Ok(Range::Last(5))));
        assert!(matches!(range(b"since=10042"), Ok(Range::Since(10_042))));
        assert!(matches!(range(b"all=1"), Ok(Range::All)));
        assert!(range(b"last=5&since=7").is_err() && range(b"all=1&last=5").is_err());
        assert!(matches!(state_filter(&Params::parse(b"state=all")), Ok(StateFilter::All)));
        assert!(state_filter(&Params::parse(b"state=closed")).is_err());
    }
}
