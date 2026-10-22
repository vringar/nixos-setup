//! Long polls: `watch`, a stream of doorbells for a monitor, and `wait`,
//! which blocks until one item is resolved.
//!
//! The board filters: a poll names what it is subscribed to, and gets the
//! matching events after `since` and a cursor to go on from. The cursor is
//! the head, so a poll moves past every event it did not want without
//! fetching it.

use std::thread;
use std::time::{Duration, Instant};

use message_board::store::{self, EventType, ItemState};

use crate::api::{Api, TIMEOUT, parse, query, transient};
use crate::client::HttpError;
use crate::identity::{bound_name, signer};
use crate::wire::{Delivered, Health, ItemDetail, Line, Page, Resolved};
use crate::{Failure, emit, warn};

/// How long to wait before asking a board that did not answer again.
const RETRY: Duration = Duration::from_secs(2);
/// The longest single poll `wait` makes; it polls again until resolved.
const WAIT_POLL: u64 = 50;
/// A poll's answer may take this much longer than the poll itself.
const POLL_MARGIN: u64 = 10;

/// `--since`: an event id, or `head` for "only what happens next".
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Since {
    Head,
    Id(i64),
}

pub fn since_arg(value: &str) -> Result<Since, String> {
    if value == "head" {
        return Ok(Since::Head);
    }
    value.parse().map(Since::Id).map_err(|err| format!("expected an event id or 'head' ({err})"))
}

/// What a watch hears: every event on these leaves, the structural events
/// of these containers, and the events matching any of the filters, minus
/// those by `not_from`.
#[derive(Debug, Default)]
pub struct Subscriptions {
    pub paths: Vec<String>,
    pub mentions: Vec<String>,
    pub not_from: Vec<String>,
    pub asked_by: Vec<String>,
    pub owners: Vec<String>,
    pub items: Vec<i64>,
}

impl Subscriptions {
    /// Subscribed to nothing: `not_from` only takes events away.
    fn is_empty(&self) -> bool {
        self.paths.is_empty()
            && self.mentions.is_empty()
            && self.asked_by.is_empty()
            && self.owners.is_empty()
            && self.items.is_empty()
    }

    fn names(&self) -> impl Iterator<Item = &String> {
        self.mentions.iter().chain(&self.not_from).chain(&self.asked_by).chain(&self.owners)
    }

    /// Add `name`'s own: what mentions it, what it asked, its queues, but
    /// never its own writes, so that they do not wake it.
    fn add_me(&mut self, name: &str) {
        for list in [&mut self.mentions, &mut self.asked_by, &mut self.owners, &mut self.not_from] {
            list.push(name.to_owned());
        }
    }

    /// `/poll` for the events after `since`, waiting up to `timeout` s.
    fn url(&self, since: i64, timeout: u64) -> String {
        let mut pairs = vec![("since", since.to_string()), ("timeout", timeout.to_string())];
        let lists = [
            ("path", &self.paths),
            ("mention", &self.mentions),
            ("not_from", &self.not_from),
            ("asked_by", &self.asked_by),
            ("owner", &self.owners),
        ];
        for (name, values) in lists {
            pairs.extend(values.iter().map(|value| (name, value.clone())));
        }
        pairs.extend(self.items.iter().map(|item| ("item", item.to_string())));
        format!("/poll{}", query(&pairs))
    }
}

/// Say the board is down, on stdout (silence must never pass for "no
/// news"), and wait before asking again.
fn unreachable(api: &Api, err: HttpError) -> Result<(), Failure> {
    emit(&Line::Unreachable { error: api.failure(err).to_string() })?;
    thread::sleep(RETRY);
    Ok(())
}

/// The head, asking until the board answers: a persistent watch is
/// routinely started before the board is up, and dying there would look
/// like "no news".
fn head_retrying(api: &Api) -> Result<i64, Failure> {
    loop {
        match api.fetch("/healthz", TIMEOUT) {
            Ok(text) => return Ok(parse::<Health>("/healthz", &text)?.head),
            Err(err) if transient(&err) => unreachable(api, err)?,
            Err(err) => return Err(api.failure(err)),
        }
    }
}

/// An event as a doorbell: what it is and where, its first line, no body.
fn doorbell(delivered: Delivered) -> Result<(), Failure> {
    let Delivered { event, state, why } = delivered;
    let event = store::Event::from(event);
    emit(&Line::Event {
        id: event.id,
        ts: &event.ts,
        event: event.event,
        path: &event.path,
        from: event.from.as_deref(),
        signed_by: event.signed_by.as_deref(),
        revoked_at: event.revoked_at,
        item: event.item,
        state,
        version: event.version,
        why: &why,
        summary: event.summary(),
    })
}

/// Long-poll and print one line per matching event, or per failed poll.
/// Never ends on its own: `timeout` is the length of one poll.
pub fn watch(api: &Api, mut subscriptions: Subscriptions, me: bool, since: Since, timeout: u64) -> Result<(), Failure> {
    if subscriptions.is_empty() && !me {
        return Err(Failure::Arguments(
            "a watch needs a path or a filter (--mention, --asked-by, --owner, --item, --me): there is no \
             board-wide stream"
                .to_owned(),
        ));
    }
    for name in subscriptions.names() {
        store::check_name(name).map_err(|err| Failure::Exit(err.to_string()))?;
    }
    if me {
        subscriptions.add_me(&bound_name(api, &signer()?)?);
    }
    let mut since = match since {
        Since::Id(id) => id,
        Since::Head => head_retrying(api)?,
    };
    let answer_within = Duration::from_secs(timeout.saturating_add(POLL_MARGIN));
    loop {
        let text = match api.fetch(&subscriptions.url(since, timeout), answer_within) {
            Ok(text) => text,
            Err(err) if transient(&err) => {
                unreachable(api, err)?;
                continue;
            }
            // A refusal (a path that does not exist, a bad name) would be
            // refused again: say so and stop.
            Err(err) => return Err(api.failure(err)),
        };
        let page: Page = parse("/poll", &text)?;
        for delivered in page.events {
            doorbell(delivered)?;
        }
        // Never backwards, whatever the answer says: that would repeat events.
        since = since.max(page.cursor);
    }
}

/// The state an item event leaves its item in; `None` for its filing.
fn resolution_state(event: EventType) -> Option<ItemState> {
    match event {
        EventType::ItemAnswered => Some(ItemState::Answered),
        EventType::ItemDeclined => Some(ItemState::Declined),
        EventType::ItemCancelled => Some(ItemState::Cancelled),
        EventType::ItemReassigned => Some(ItemState::Reassigned),
        _ => None,
    }
}

/// Seconds for the next poll of a wait with `left` to go: rounded up, so a
/// wait never ends early, and at most [`WAIT_POLL`].
fn poll_seconds(left: Option<Duration>) -> u64 {
    left.map_or(WAIT_POLL, |left| left.as_secs().saturating_add(u64::from(left.subsec_nanos() > 0)).clamp(1, WAIT_POLL))
}

/// Block until `item` is no longer open, then print how it was resolved;
/// with `timeout` (seconds), give up after that long (exit 124).
pub fn wait(api: &Api, item: i64, timeout: Option<u64>) -> Result<(), Failure> {
    // A deadline past what the clock can say is no deadline at all.
    let deadline = timeout.and_then(|seconds| Instant::now().checked_add(Duration::from_secs(seconds)));
    let detail: ItemDetail = api.get(&format!("/item/{item}"))?;
    if detail.item.state != ItemState::Open {
        let id = detail.item.resolution.map(|r| r.id).ok_or_else(|| Failure::BadAnswer {
            what: format!("/item/{item}"),
            cause: format!("a {} item without its resolution", detail.item.state),
        })?;
        return emit(&Line::Resolved(&Resolved { item, state: detail.item.state, id }));
    }
    let subscriptions = Subscriptions { items: vec![item], ..Subscriptions::default() };
    // From the item's own id: its resolution, whenever it came, is after
    // it, so one that landed since the read above is not missed.
    let mut since = item;
    let mut down = false;
    loop {
        let left = match deadline {
            None => None,
            Some(deadline) => match deadline.checked_duration_since(Instant::now()) {
                Some(left) if !left.is_zero() => Some(left),
                _ => return Err(Failure::TimedOut),
            },
        };
        let seconds = poll_seconds(left);
        let answer_within = Duration::from_secs(seconds.saturating_add(POLL_MARGIN));
        match api.fetch(&subscriptions.url(since, seconds), answer_within) {
            Ok(text) => {
                down = false;
                let page: Page = parse("/poll", &text)?;
                let resolved = page.events.iter().find_map(|delivered| {
                    let event = &delivered.event;
                    let state = resolution_state(event.event).filter(|_| event.item == Some(item))?;
                    Some(Resolved { item, state, id: event.id })
                });
                if let Some(resolved) = resolved {
                    return emit(&Line::Resolved(&resolved));
                }
                since = since.max(page.cursor);
            }
            // A board restart must not end a wait that may last hours: say
            // so once, and keep asking.
            Err(err) if transient(&err) => {
                if !down {
                    warn(&format!("board: {}; trying again every {} s", api.failure(err), RETRY.as_secs()))?;
                    down = true;
                }
                thread::sleep(left.map_or(RETRY, |left| left.min(RETRY)));
            }
            Err(err) => return Err(api.failure(err)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn since_is_an_id_or_head() {
        assert_eq!(since_arg("head"), Ok(Since::Head));
        assert_eq!(since_arg("10342"), Ok(Since::Id(10_342)));
        assert!(since_arg("banana").is_err_and(|why| why.starts_with("expected an event id or 'head'")));
    }

    #[test]
    fn me_is_mention_asked_by_owner_and_not_from() {
        let mut subscriptions = Subscriptions { paths: vec!["r/log".to_owned()], ..Subscriptions::default() };
        subscriptions.add_me("aspfuzz");
        assert_eq!(
            subscriptions.url(10_350, 50),
            "/poll?since=10350&timeout=50&path=r%2Flog&mention=aspfuzz&not_from=aspfuzz&asked_by=aspfuzz&owner=aspfuzz"
        );
        assert!(Subscriptions { not_from: vec!["x".to_owned()], ..Subscriptions::default() }.is_empty());
        assert!(!Subscriptions { items: vec![7], ..Subscriptions::default() }.is_empty());
        // the root is a path like any other
        assert_eq!(
            Subscriptions { paths: vec!["/".to_owned()], ..Subscriptions::default() }.url(1, 5),
            "/poll?since=1&timeout=5&path=%2F"
        );
    }

    #[test]
    fn a_wait_polls_no_longer_than_it_has_left() {
        assert_eq!(poll_seconds(None), WAIT_POLL);
        assert_eq!(poll_seconds(Some(Duration::from_millis(300))), 1);
        assert_eq!(poll_seconds(Some(Duration::from_millis(2_100))), 3);
        assert_eq!(poll_seconds(Some(Duration::from_secs(2))), 2);
        assert_eq!(poll_seconds(Some(Duration::from_secs(540))), WAIT_POLL);
    }

    #[test]
    fn only_a_resolving_event_ends_a_wait() {
        assert_eq!(resolution_state(EventType::ItemFiled), None);
        assert_eq!(resolution_state(EventType::Message), None);
        assert_eq!(resolution_state(EventType::ItemAnswered), Some(ItemState::Answered));
        assert_eq!(resolution_state(EventType::ItemCancelled), Some(ItemState::Cancelled));
    }
}
