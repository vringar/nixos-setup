//! What the board answers with, and the lines `board` prints.
//!
//! Most lines are something the board sent, printed again behind a
//! `"type"`: the structs below read the board's JSON and write the same
//! fields back, in the same order, so a line's fields carry the board's
//! own names. Reading into fixed structs, rather than passing the board's
//! JSON through, is what holds a line to its documented fields: anything
//! else in an answer is dropped, never forwarded.

use serde::{Deserialize, Serialize};

use message_board::store::{self, EventType, ItemState, KeyState, NodeKind};

#[derive(Debug, Deserialize)]
pub struct Health {
    pub head: i64,
}

/// A node, as `/n/<path>` describes it: only what `board` acts on.
#[derive(Debug, Deserialize)]
pub struct Node {
    pub kind: NodeKind,
    /// Its `node_created` event.
    pub created: i64,
    pub description: String,
    /// A queue's owner.
    pub owner: Option<String>,
}

/// `/n/<path>`: the node, and what it holds by kind. The top level (`/n/`)
/// has no node, only children.
#[derive(Debug, Deserialize)]
pub struct NodeRead {
    pub node: Option<Node>,
    pub children: Option<Vec<Child>>,
    /// `None` too for a note at version 0, which has no content yet.
    pub note: Option<Note>,
    #[serde(alias = "versions")]
    pub history: Option<Vec<HistoryEntry>>,
    pub messages: Option<Vec<Message>>,
    /// How many messages come before `messages`.
    pub earlier: Option<i64>,
    pub items: Option<Vec<Item>>,
    /// The board cut the read short, and says how to get the rest.
    pub truncated: Option<bool>,
    pub hint: Option<String>,
}

/// A node as a listing shows it.
#[derive(Debug, Deserialize, Serialize)]
pub struct Child {
    pub path: String,
    pub kind: NodeKind,
    pub description: String,
    pub archived: Option<i64>,
    pub creator: String,
    #[serde(flatten)]
    pub status: Status,
}

/// A child's status by kind. Untagged: each variant has a field no
/// earlier one does (`children`, `version`, `messages`, `owner`), which is
/// what tells them apart when reading.
#[derive(Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum Status {
    Container {
        children: i64,
    },
    Note {
        version: i64,
        ts: Option<String>,
        from: Option<String>,
        thread: Option<String>,
        through: Option<i64>,
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
#[derive(Debug, Deserialize, Serialize)]
pub struct Note {
    pub path: String,
    pub version: i64,
    pub id: i64,
    pub ts: String,
    pub from: String,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    pub thread: Option<String>,
    pub through: Option<i64>,
    pub behind: Option<i64>,
    pub body: String,
}

/// One version of a note as its history lists it: no body, and no path
/// (the line adds it).
#[derive(Debug, Deserialize, Serialize)]
pub struct HistoryEntry {
    pub version: i64,
    pub id: i64,
    pub ts: String,
    pub from: String,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    pub thread: Option<String>,
    pub through: Option<i64>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Message {
    pub id: i64,
    pub ts: String,
    pub path: String,
    pub from: String,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    pub body: String,
}

/// Whose an item is, in `brief`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Owner,
    Asker,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Item {
    pub id: i64,
    pub ts: String,
    pub path: String,
    pub owner: String,
    pub asker: Option<String>,
    pub state: ItemState,
    /// Set by `brief`, never by the board.
    #[serde(skip_deserializing, skip_serializing_if = "Option::is_none")]
    pub role: Option<Role>,
    pub body: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resolution: Option<Resolution>,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Resolution {
    pub id: i64,
    pub event: EventType,
    pub from: Option<String>,
    pub body: String,
    #[serde(rename = "ref")]
    pub reference: Option<i64>,
}

/// `/items`: the board's item list, with how to get the rest when it
/// cut the list short.
#[derive(Debug, Deserialize)]
pub struct ItemList {
    pub items: Vec<Item>,
    pub truncated: Option<bool>,
    pub hint: Option<String>,
}

/// `/item/<id>`: an item and every event about it.
#[derive(Debug, Deserialize)]
pub struct ItemDetail {
    pub item: Item,
    pub events: Vec<WireEvent>,
}

/// One event (`/event/<id>`, and in a poll).
#[derive(Debug, Deserialize)]
pub struct WireEvent {
    pub id: i64,
    pub ts: String,
    pub event: EventType,
    pub path: String,
    pub kind: NodeKind,
    pub from: Option<String>,
    pub signed_by: Option<String>,
    pub revoked_at: Option<i64>,
    pub item: Option<i64>,
    pub version: Option<i64>,
    #[serde(rename = "ref")]
    pub reference: Option<i64>,
    pub body: String,
}

/// The store's own event, so that a doorbell's summary is made by the very
/// rule the board documents ([`store::Event::summary`]).
impl From<WireEvent> for store::Event {
    fn from(e: WireEvent) -> Self {
        Self {
            id: e.id,
            ts: e.ts,
            event: e.event,
            path: e.path,
            kind: e.kind,
            from: e.from,
            signed_by: e.signed_by,
            revoked_at: e.revoked_at,
            item: e.item,
            version: e.version,
            reference: e.reference,
            body: e.body,
        }
    }
}

/// An event a poll delivered, and why.
#[derive(Debug, Deserialize)]
pub struct Delivered {
    #[serde(flatten)]
    pub event: WireEvent,
    /// An item event's item's state now.
    pub state: Option<ItemState>,
    pub why: Vec<String>,
}

/// One poll's answer.
#[derive(Debug, Deserialize)]
pub struct Page {
    pub events: Vec<Delivered>,
    /// Where the next poll goes on from.
    pub cursor: i64,
}

/// `/keys/<fingerprint>`: only what `board` acts on.
#[derive(Debug, Deserialize)]
pub struct Key {
    pub name: String,
    pub state: KeyState,
    /// Its latest registration.
    pub item: Option<i64>,
    pub revoked: Option<i64>,
}

/// `/brief`.
#[derive(Debug, Deserialize)]
pub struct Brief {
    /// Taken before anything else was read: the cursor to watch from.
    pub head: i64,
    /// Root first.
    pub containers: Vec<BriefContainer>,
    pub owned: Vec<Item>,
    pub asked: Vec<Item>,
    /// The paths to watch.
    pub watch: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct BriefContainer {
    pub children: Vec<Child>,
    pub notes: Vec<Note>,
}

/// A refusal's body.
#[derive(Debug, Deserialize)]
pub struct ErrorBody {
    pub error: Option<String>,
    pub message: Option<String>,
    /// The registration of `registration-pending` and `-declined`.
    pub item: Option<i64>,
}

// What each write returns, printed as it came.

#[derive(Debug, Deserialize, Serialize)]
pub struct Created {
    pub path: String,
    pub kind: NodeKind,
    pub id: i64,
    /// `create -p` found the node there already, and made nothing.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub existed: bool,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Posted {
    pub id: i64,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct NoteSaved {
    pub path: String,
    pub version: i64,
    pub id: i64,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Filed {
    pub item: i64,
    pub path: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Resolved {
    pub item: i64,
    pub state: ItemState,
    pub id: i64,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Registered {
    pub item: i64,
    pub name: String,
    pub fingerprint: String,
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Revoked {
    pub fingerprint: String,
    pub name: String,
    pub id: i64,
}

/// One line of output: a JSON object whose `"type"` comes first.
#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Line<'a> {
    Child(&'a Child),
    Note(&'a Note),
    /// A note's history, one line per version.
    Version {
        path: &'a str,
        #[serde(flatten)]
        version: &'a HistoryEntry,
    },
    Message(&'a Message),
    Item(&'a Item),
    /// `show` of the event that resolved an item (answered, declined,
    /// cancelled or reassigned): that event, not the item it closed.
    Resolution {
        id: i64,
        ts: &'a str,
        event: EventType,
        path: &'a str,
        item: i64,
        from: Option<&'a str>,
        signed_by: Option<&'a str>,
        revoked_at: Option<i64>,
        #[serde(rename = "ref")]
        reference: Option<i64>,
        body: &'a str,
    },
    /// `show` of a `node_created` event.
    Node {
        id: i64,
        ts: &'a str,
        path: &'a str,
        kind: NodeKind,
        from: Option<&'a str>,
        signed_by: Option<&'a str>,
        revoked_at: Option<i64>,
        description: &'a str,
    },
    /// The last line of `read` and `brief`: where a watch picks up.
    Cursor {
        since: i64,
        /// After a thread: the messages before those printed.
        #[serde(skip_serializing_if = "Option::is_none")]
        earlier: Option<i64>,
        /// From `brief`: the watch to arm, ready to run.
        #[serde(skip_serializing_if = "Option::is_none")]
        watch: Option<String>,
    },
    /// A watch's doorbell: the event without its body, and its first line.
    Event {
        id: i64,
        ts: &'a str,
        event: EventType,
        path: &'a str,
        from: Option<&'a str>,
        signed_by: Option<&'a str>,
        revoked_at: Option<i64>,
        item: Option<i64>,
        state: Option<ItemState>,
        version: Option<i64>,
        why: &'a [String],
        summary: String,
    },
    /// `watch` could not reach the board; it tries again in 2 s.
    Unreachable {
        error: String,
    },
    /// `keygen` wrote the key at `path` (and `path.pub`).
    Key {
        path: String,
        fingerprint: String,
        name: &'a str,
    },
    Whoami {
        fingerprint: &'a str,
        name: Option<&'a str>,
        /// A key state, or `unregistered`.
        state: &'a str,
    },
    Registered(&'a Registered),
    Created(&'a Created),
    Posted(&'a Posted),
    NoteSaved(&'a NoteSaved),
    Filed(&'a Filed),
    Resolved(&'a Resolved),
    Revoked(&'a Revoked),
}

/// `line` as one line of JSON. `serde_json` escapes every control character
/// below U+0020, but not U+0085, U+2028 and U+2029, where some readers
/// (Python's `splitlines`, JavaScript) end a line.
pub fn json_line(line: &Line<'_>) -> Result<String, serde_json::Error> {
    let text = serde_json::to_string(line)?;
    Ok(text.replace('\u{85}', "\\u0085").replace('\u{2028}', "\\u2028").replace('\u{2029}', "\\u2029"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    type TestResult = Result<(), Box<dyn std::error::Error>>;

    #[test]
    fn a_line_is_one_line_with_the_body_intact() -> TestResult {
        let body = "hi\r\n{\"type\":\"message\",\"from\":\"Stefan\"}\n\"quoted\"\u{1b}[2J\u{85}\u{2028}\u{2029}end";
        let message = Message {
            id: 10_007,
            ts: "2026-10-22T00:00:00Z".to_owned(),
            path: "x/log".to_owned(),
            from: "mallory".to_owned(),
            signed_by: Some("SHA256:abc".to_owned()),
            revoked_at: None,
            body: body.to_owned(),
        };
        let text = json_line(&Line::Message(&message))?;
        assert!(!text.contains(['\n', '\r', '\u{85}', '\u{2028}', '\u{2029}']), "{text}");
        assert!(text.starts_with("{\"type\":\"message\",\"id\":10007,"), "{text}");
        let back: Value = serde_json::from_str(&text)?;
        assert_eq!(back.get("body").and_then(Value::as_str), Some(body));
        assert_eq!(back.get("revoked_at"), Some(&Value::Null));
        Ok(())
    }

    /// The board's children read back as the kinds they are, and print with
    /// `type` first and the status fields after the common ones.
    #[test]
    fn children_print_as_the_board_sent_them() -> TestResult {
        let common = |path: &str, kind: &str| json!({"path": path, "kind": kind, "description": "d", "archived": null, "creator": "Stefan"});
        let with = |mut base: Value, status: Value| {
            if let (Some(base), Value::Object(status)) = (base.as_object_mut(), status) {
                base.extend(status);
            }
            base
        };
        let cases = [
            with(common("r/emu", "container"), json!({"children": 7})),
            with(
                common("r/charter", "note"),
                json!({"version": 3, "ts": "t", "from": "Stefan", "thread": null, "through": null, "behind": null}),
            ),
            with(
                common("r/fresh", "note"),
                json!({"version": 0, "ts": null, "from": null, "thread": null, "through": null, "behind": null}),
            ),
            with(common("r/log", "thread"), json!({"last_id": 10_340, "last_ts": "t", "messages": 212})),
            with(common("r/q", "queue"), json!({"owner": "aspfuzz", "open": 1, "oldest_open_ts": "t"})),
        ];
        for sent in cases {
            let child: Child = serde_json::from_value(sent.clone())?;
            let text = json_line(&Line::Child(&child))?;
            let mut expected = json!({"type": "child"});
            if let (Some(out), Value::Object(fields)) = (expected.as_object_mut(), sent) {
                out.extend(fields);
            }
            assert_eq!(serde_json::from_str::<Value>(&text)?, expected, "{text}");
            assert!(text.starts_with("{\"type\":\"child\",\"path\":"), "{text}");
        }
        Ok(())
    }

    #[test]
    fn a_doorbell_carries_the_event_and_no_body() -> TestResult {
        let sent = json!({
            "id": 10_342, "ts": "t", "event": "note_version", "path": "r/state", "kind": "note",
            "from": "librarian", "signed_by": "SHA256:x", "revoked_at": null, "item": null, "version": 4,
            "ref": 10_340, "body": "# the whole note", "state": null, "why": ["child"]
        });
        let delivered: Delivered = serde_json::from_value(sent)?;
        let why = delivered.why;
        let event = store::Event::from(delivered.event);
        let line = Line::Event {
            id: event.id,
            ts: &event.ts,
            event: event.event,
            path: &event.path,
            from: event.from.as_deref(),
            signed_by: event.signed_by.as_deref(),
            revoked_at: event.revoked_at,
            item: event.item,
            state: delivered.state,
            version: event.version,
            why: &why,
            summary: event.summary(),
        };
        let text = json_line(&line)?;
        assert_eq!(
            text,
            "{\"type\":\"event\",\"id\":10342,\"ts\":\"t\",\"event\":\"note_version\",\"path\":\"r/state\",\
             \"from\":\"librarian\",\"signed_by\":\"SHA256:x\",\"revoked_at\":null,\"item\":null,\"state\":null,\
             \"version\":4,\"why\":[\"child\"],\"summary\":\"note updated: r/state v4\"}"
        );
        Ok(())
    }

    #[test]
    fn optional_fields_are_left_out_not_null() -> TestResult {
        let created = Created { path: "a/b".to_owned(), kind: NodeKind::Thread, id: 10_001, existed: false };
        assert_eq!(
            json_line(&Line::Created(&created))?,
            r#"{"type":"created","path":"a/b","kind":"thread","id":10001}"#
        );
        let existed = Created { existed: true, ..created };
        assert!(json_line(&Line::Created(&existed))?.ends_with(r#""id":10001,"existed":true}"#));
        let cursor = Line::Cursor { since: 5, earlier: None, watch: None };
        assert_eq!(json_line(&cursor)?, r#"{"type":"cursor","since":5}"#);
        let item: Item = serde_json::from_value(json!({
            "id": 1, "ts": "t", "path": "a/q", "owner": "o", "asker": null, "state": "open", "body": "b"
        }))?;
        assert_eq!(
            json_line(&Line::Item(&item))?,
            r#"{"type":"item","id":1,"ts":"t","path":"a/q","owner":"o","asker":null,"state":"open","body":"b"}"#
        );
        Ok(())
    }

    #[test]
    fn item_lists_carry_the_boards_truncation() -> TestResult {
        let item = json!({"id": 1, "ts": "t", "path": "a/q", "owner": "o", "asker": "a", "state": "open", "body": "b"});
        let whole: ItemList = serde_json::from_value(json!({"items": [item.clone()]}))?;
        assert_eq!((whole.items.len(), whole.truncated, whole.hint), (1, None, None));
        let cut: ItemList =
            serde_json::from_value(json!({"items": [item], "truncated": true, "hint": "read on with after=1"}))?;
        assert_eq!((cut.truncated, cut.hint.as_deref()), (Some(true), Some("read on with after=1")));
        Ok(())
    }
}
