//! Signed writes: how one is sent, and each command that makes one.
//!
//! A signature covers the board's head as its nonce, so it is good for one
//! write: when another write lands between reading the head and this one
//! arriving, the board answers `stale-nonce`, and `submit` reads the head
//! again and re-signs. Every write also carries a client key, which the
//! board checks before the nonce: a resend of a write that landed returns
//! the first result instead of writing twice, so a write that got no
//! answer is simply sent again.

use std::io::{self, IsTerminal as _, Read};
use std::thread;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

use message_board::store::{
    Action, Approve, Create, EventType, File, ItemState, NodeKind, Post, REGISTER_QUEUE, Resolve, Revoke, SetNote,
};

use crate::api::{Api, TIMEOUT, error_code, node_url, parse};
use crate::client::HttpError;
use crate::identity::{Signer, lookup, signer};
use crate::wire::{Created, Filed, Health, ItemDetail, Line, NodeRead, NoteSaved, Posted, Resolved, Revoked};
use crate::{Failure, emit, io_failure, warn};

/// How long a write waits for its answer. Resending is harmless (see the
/// module docs), so a slow or flapping board gets time, then a resend.
const WRITE_TIMEOUT: Duration = Duration::from_secs(30);
/// Resends after no answer, waiting 1, 2, then 4 s before each.
const WRITE_RETRIES: u32 = 3;
/// Signatures in a row that may go stale before giving up.
const NONCE_TRIES: u32 = 5;

/// Sign `action` and POST it to `route`; the board's answer, read as a `T`.
pub fn submit<A: Action, T: DeserializeOwned>(
    api: &Api,
    signer: &Signer,
    route: &str,
    action: &A,
) -> Result<T, Failure> {
    let key = client_key()?;
    let mut stale = 0;
    let mut dropped = 0;
    loop {
        match send_once(api, signer, route, action, &key) {
            Ok(text) => return parse(route, &text),
            Err(Attempt::Http(err)) if error_code(&err).as_deref() == Some("stale-nonce") => {
                stale += 1;
                if stale == NONCE_TRIES {
                    return Err(Failure::Exit(format!(
                        "gave up after {NONCE_TRIES} tries: each time, another write landed between reading the \
                         board's head and this one arriving (stale-nonce); run it again"
                    )));
                }
            }
            Err(Attempt::Http(HttpError::Transport(cause))) if dropped < WRITE_RETRIES => {
                warn(&format!("board: no answer to {route} ({cause}); sending it again"))?;
                thread::sleep(Duration::from_secs(1 << dropped));
                dropped += 1;
            }
            Err(Attempt::Http(err)) => return Err(api.failure(err)),
            Err(Attempt::Failed(err)) => return Err(err),
        }
    }
}

/// One try at a write: an answer, or why there was none.
enum Attempt {
    Http(HttpError),
    Failed(Failure),
}

/// Read the head, sign against it, send. The body is the action's signed
/// fields by name, every value a string as signed, plus the signature.
fn send_once<A: Action>(api: &Api, signer: &Signer, route: &str, action: &A, key: &str) -> Result<String, Attempt> {
    let health = api.fetch("/healthz", TIMEOUT).map_err(Attempt::Http)?;
    let nonce = parse::<Health>("/healthz", &health).map_err(Attempt::Failed)?.head.to_string();
    let signature = action.sign(&signer.key, key, &nonce).map_err(|err| Attempt::Failed(err.into()))?;
    let mut data: Map<String, Value> =
        action.named(key, &nonce).into_iter().map(|(name, value)| (name.to_owned(), Value::String(value))).collect();
    data.insert("signature".to_owned(), Value::String(signature));
    api.send(route, &Value::Object(data), WRITE_TIMEOUT).map_err(Attempt::Http)
}

/// A fresh client key for a write: a version-4 UUID, as 32 hex digits.
fn client_key() -> Result<String, Failure> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes).map_err(|err| Failure::Exit(format!("no random bytes for a client key: {err}")))?;
    let [b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, b13, b14, b15] = bytes;
    let uuid = [b0, b1, b2, b3, b4, b5, (b6 & 0x0f) | 0x40, b7, (b8 & 0x3f) | 0x80, b9, b10, b11, b12, b13, b14, b15];
    // two lowercase hex digits per byte; a nibble is always a digit
    Ok(uuid.iter().flat_map(|b| [b >> 4, b & 0x0f]).filter_map(|n| char::from_digit(u32::from(n), 16)).collect())
}

fn read_all(mut input: impl Read, what: &str) -> Result<String, Failure> {
    let mut text = String::new();
    input.read_to_string(&mut text).map_err(io_failure(format!("cannot read {what} from stdin")))?;
    Ok(text)
}

/// A body given as arguments: the words joined by spaces, or, for a lone
/// `-`, all of `stdin`.
fn body_text(words: &[String], stdin: impl Read) -> Result<String, Failure> {
    match words {
        [only] if only == "-" => read_all(stdin, "the body"),
        _ => Ok(words.join(" ")),
    }
}

/// What `create -p` has to do.
#[derive(Debug, PartialEq, Eq)]
enum Plan {
    /// Make these containers, root first, then the node.
    Make(Vec<String>),
    /// The node is there already, of the kind asked for; its
    /// `node_created` event.
    Existed(i64),
}

/// Plan `create -p KIND PATH`: which containers along `path` are missing,
/// or that there is nothing to do. `lookup` says what is at a path: its
/// kind and `node_created` event, or `None`.
fn plan_parents(
    path: &str,
    kind: NodeKind,
    mut lookup: impl FnMut(&str) -> Result<Option<(NodeKind, i64)>, Failure>,
) -> Result<Plan, Failure> {
    if let Some((found, created)) = lookup(path)? {
        if found == kind {
            return Ok(Plan::Existed(created));
        }
        return Err(Failure::Exit(format!("{path} is a {found}, not a {kind}")));
    }
    let mut missing = Vec::new();
    for ancestor in path.match_indices('/').filter_map(|(at, _)| path.get(..at)) {
        // Below a missing node nothing can exist: no need to ask.
        if missing.is_empty() {
            match lookup(ancestor)? {
                Some((NodeKind::Container, _)) => continue,
                Some((other, _)) => {
                    return Err(Failure::Exit(format!("{ancestor} is a {other}: nothing goes under a leaf")));
                }
                None => {}
            }
        }
        missing.push(ancestor.to_owned());
    }
    Ok(Plan::Make(missing))
}

/// The node at `path`: its kind and `node_created` event, or `None`.
fn node_at(api: &Api, path: &str) -> Result<Option<(NodeKind, i64)>, Failure> {
    let read: Option<NodeRead> = api.find(&node_url(path))?;
    Ok(read.and_then(|read| read.node).map(|node| (node.kind, node.created)))
}

/// What `create` makes.
pub struct NewNode<'a> {
    pub kind: NodeKind,
    pub path: &'a str,
    pub description: &'a str,
    /// A queue's owner; empty for the signer's own name.
    pub owner: &'a str,
    /// Make the missing containers along the path first, like `mkdir -p`.
    pub parents: bool,
}

/// Make a node, and with `parents` the containers it needs. Not atomic,
/// by design: each node is its own signed create, and a rerun completes a
/// run that stopped half way.
pub fn create(api: &Api, new: &NewNode<'_>) -> Result<(), Failure> {
    let signer = signer()?;
    let plan =
        if new.parents { plan_parents(new.path, new.kind, |path| node_at(api, path))? } else { Plan::Make(vec![]) };
    let containers = match plan {
        Plan::Existed(id) => {
            let created = Created { path: new.path.to_owned(), kind: new.kind, id, existed: true };
            return emit(&Line::Created(&created));
        }
        Plan::Make(containers) => containers,
    };
    // A note's first version comes from stdin, if there is any. A terminal
    // is no input: reading one would sit waiting for typing.
    let body = if new.kind == NodeKind::Note && !io::stdin().is_terminal() {
        read_all(io::stdin().lock(), "the note")?
    } else {
        String::new()
    };
    let make = |path: &str, kind, description, owner, body| -> Result<(), Failure> {
        let action = Create { path, kind, description, owner, body };
        let created: Created = submit(api, &signer, "/create", &action)?;
        emit(&Line::Created(&created))
    };
    for container in &containers {
        make(container, NodeKind::Container, "", "", "")?;
    }
    make(new.path, new.kind, new.description, new.owner, &body)
}

pub fn post(api: &Api, thread: &str, words: &[String]) -> Result<(), Failure> {
    // v1 was `post <area> <topic> <from> <body>`. A thread is never
    // top-level, so a first argument without a '/' is that form.
    if !thread.contains('/') {
        return Err(Failure::V1Form("areas"));
    }
    let signer = signer()?;
    let body = body_text(words, io::stdin().lock())?;
    let posted: Posted = submit(api, &signer, "/post", &Post { path: thread, body: &body })?;
    emit(&Line::Posted(&posted))
}

/// Save stdin as the note's next version. A `note-changed` refusal is never
/// retried: someone saved meanwhile, and only a re-read and merge is safe.
pub fn note_set(api: &Api, path: &str, base: i64, through: Option<i64>) -> Result<(), Failure> {
    let signer = signer()?;
    let body = read_all(io::stdin().lock(), "the note")?;
    let action = SetNote { path, body: &body, base_version: base, through };
    let saved: NoteSaved = submit(api, &signer, "/note", &action)?;
    emit(&Line::NoteSaved(&saved))
}

pub fn ask(api: &Api, queue: &str, words: &[String]) -> Result<(), Failure> {
    let signer = signer()?;
    let body = body_text(words, io::stdin().lock())?;
    let filed: Filed = submit(api, &signer, "/file", &File { path: queue, body: &body })?;
    emit(&Line::Filed(&filed))
}

/// Answer, decline or cancel an item.
pub fn resolve(
    api: &Api,
    item: i64,
    state: ItemState,
    words: &[String],
    reference: Option<i64>,
) -> Result<(), Failure> {
    let signer = signer()?;
    let body = body_text(words, io::stdin().lock())?;
    let action = Resolve { item, state, body: &body, reference };
    let resolved: Resolved = submit(api, &signer, "/resolve", &action)?;
    emit(&Line::Resolved(&resolved))
}

/// Approve a registration: signed over the key's fingerprint and the name,
/// both as the board has them, so what the owner signs is that key.
pub fn approve(api: &Api, item: i64) -> Result<(), Failure> {
    let signer = signer()?;
    let detail: ItemDetail = api.get(&format!("/item/{item}"))?;
    if detail.item.path != REGISTER_QUEUE {
        return Err(Failure::Exit(format!("#{item} is an item in {}, not a registration", detail.item.path)));
    }
    // A registration is filed by the key it registers.
    let fingerprint = detail
        .events
        .into_iter()
        .find(|event| event.event == EventType::ItemFiled)
        .and_then(|event| event.signed_by)
        .ok_or_else(|| Failure::BadAnswer {
            what: format!("/item/{item}"),
            cause: "no signed item_filed event".to_owned(),
        })?;
    let key = lookup(api, &fingerprint)?
        .ok_or_else(|| Failure::Exit(format!("the board does not know registration #{item}'s key {fingerprint}")))?;
    let action = Approve { item, name: &key.name, fingerprint: &fingerprint };
    let resolved: Resolved = submit(api, &signer, "/approve", &action)?;
    emit(&Line::Resolved(&resolved))
}

pub fn revoke(api: &Api, fingerprint: &str, reason: &str) -> Result<(), Failure> {
    let signer = signer()?;
    let revoked: Revoked = submit(api, &signer, "/revoke", &Revoke { fingerprint, reason })?;
    emit(&Line::Revoked(&revoked))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;

    #[test]
    fn client_keys_are_uuid4_hex() -> Result<(), Failure> {
        let key = client_key()?;
        assert_eq!(key.len(), 32);
        assert!(key.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
        assert_eq!(key.get(12..13), Some("4"));
        assert!(key.get(16..17).is_some_and(|variant| "89ab".contains(variant)));
        assert_ne!(client_key()?, key);
        Ok(())
    }

    #[test]
    fn a_lone_dash_reads_the_body_from_stdin() -> Result<(), Failure> {
        let words = |words: &[&str]| words.iter().map(|w| (*w).to_owned()).collect::<Vec<_>>();
        assert_eq!(body_text(&words(&["-"]), "from\nstdin".as_bytes())?, "from\nstdin");
        assert_eq!(body_text(&words(&["fix", "-", "now"]), "unread".as_bytes())?, "fix - now");
        assert_eq!(body_text(&words(&["one"]), "unread".as_bytes())?, "one");
        assert_eq!(body_text(&[], "unread".as_bytes())?, "");
        Ok(())
    }

    /// `plan_parents` against a fixed tree, recording what it asked.
    fn plan(path: &str, kind: NodeKind, tree: &[(&str, NodeKind)]) -> (Result<Plan, String>, Vec<String>) {
        let tree: BTreeMap<&str, NodeKind> = tree.iter().copied().collect();
        let mut asked = Vec::new();
        let plan = plan_parents(path, kind, |at| {
            asked.push(at.to_owned());
            Ok(tree.get(at).map(|&kind| (kind, 10_001)))
        });
        (plan.map_err(|err| err.to_string()), asked)
    }

    #[test]
    fn create_p_makes_the_missing_containers_root_first() {
        use NodeKind::{Container, Note, Queue, Thread};
        let make = |paths: &[&str]| Ok(Plan::Make(paths.iter().map(|p| (*p).to_owned()).collect()));
        // nothing there: every container, and no question below the first gap
        assert_eq!(plan("r/emu/log", Thread, &[]), (make(&["r", "r/emu"]), vec!["r/emu/log".into(), "r".into()]));
        // the root exists
        assert_eq!(plan("r/emu/log", Thread, &[("r", Container)]).0, make(&["r/emu"]));
        assert_eq!(plan("r/emu/log", Thread, &[("r", Container), ("r/emu", Container)]).0, make(&[]));
        assert_eq!(plan("r", Container, &[]).0, make(&[]));
        // there already, of that kind: nothing to do
        assert_eq!(plan("r/emu", Container, &[("r", Container), ("r/emu", Container)]).0, Ok(Plan::Existed(10_001)));
        assert_eq!(
            plan("r/q", Queue, &[("r", Container), ("r/q", Queue)]),
            (Ok(Plan::Existed(10_001)), vec!["r/q".into()])
        );
        // another kind at the path, or a leaf on the way
        assert_eq!(
            plan("r/q", Thread, &[("r", Container), ("r/q", Queue)]).0,
            Err("r/q is a queue, not a thread".to_owned())
        );
        assert_eq!(
            plan("r/notes/x", Note, &[("r", Container), ("r/notes", Note)]).0,
            Err("r/notes is a note: nothing goes under a leaf".to_owned())
        );
    }
}
