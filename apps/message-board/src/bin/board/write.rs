//! Signed writes: how one is sent, and each command that makes one.
//!
//! A signature covers the board's head as its nonce, so it is good for one
//! write: when another write lands between reading the head and this one
//! arriving, the board answers `stale-nonce`, and `submit` reads the head
//! again and re-signs. Every write also carries a client key, which the
//! board checks before the nonce: a resend of a write that landed returns
//! the first result instead of writing twice, so a write that got no
//! answer is simply sent again.

use std::io::{self, Read};
use std::thread;
use std::time::Duration;

use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

use message_board::store::{
    Action, Approve, Archive, Create, File, ItemState, KeyState, NodeKind, Post, Resolve, Revoke, SetNote,
};

use crate::api::{Api, TIMEOUT, error_code, node_url, parse};
use crate::client::HttpError;
use crate::identity::{Signer, signer};
use crate::wire::{
    Approved, Archived, Created, Filed, Health, Key, Line, NodeRead, NoteSaved, Posted, Registration, Resolved, Revoked,
};
use crate::{Failure, emit, io_failure, warn};

/// How long a write waits for its answer. Resending is harmless (see the
/// module docs), so a slow or flapping board gets time, then a resend.
const WRITE_TIMEOUT: Duration = Duration::from_secs(30);
/// Resends after no answer, waiting 1, 2, then 4 s before each.
const WRITE_RETRIES: u32 = 3;
/// Signatures in a row that may go stale before giving up.
const NONCE_TRIES: u32 = 8;
/// A stale signature waits a random time between these (in ms), times the
/// number of signatures gone stale so far, before reading the head again:
/// writers that collided once do not read it again in step.
const BACKOFF_MS: (u64, u64) = (10, 100);

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
                thread::sleep(backoff(stale)?);
            }
            Err(Attempt::Http(HttpError::Transport(cause))) if dropped < WRITE_RETRIES => {
                warn(&format!("board: no answer to {route} ({cause}); retrying"))?;
                thread::sleep(Duration::from_secs(1 << dropped));
                dropped += 1;
            }
            Err(Attempt::Http(err)) => return Err(api.failure(err)),
            Err(Attempt::Failed(err)) => return Err(err),
        }
    }
}

/// How long to wait after `stale` signatures went stale in a row: a random
/// time in [`BACKOFF_MS`], times `stale`.
fn backoff(stale: u32) -> Result<Duration, Failure> {
    let mut bytes = [0_u8; 2];
    getrandom::fill(&mut bytes).map_err(|err| Failure::Exit(format!("no random bytes for a backoff: {err}")))?;
    let (low, high) = BACKOFF_MS;
    let ms = low.saturating_add(u64::from(u16::from_le_bytes(bytes)) % (high - low + 1));
    Ok(Duration::from_millis(ms.saturating_mul(u64::from(stale))))
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

/// A body that starts with `--from` is v1's `post <path> --from NAME <body>`
/// habit: the key names the author now. Refused before anything is sent,
/// rather than posted as the text "--from alice hi".
pub fn refuse_from(words: &[String]) -> Result<(), Failure> {
    match words.first() {
        Some(first) if first == "--from" || first.starts_with("--from=") => {
            Err(Failure::V1Form("--from (the signing key names the author)"))
        }
        _ => Ok(()),
    }
}

/// A new note's first version: `--body TEXT`, or all of `stdin` for
/// `--body -`; nothing otherwise, and stdin is not touched (a command that
/// reads it unasked hangs when nothing is piped in).
pub fn create_body(kind: NodeKind, body: Option<&str>, stdin: impl Read) -> Result<String, Failure> {
    match (kind, body) {
        (_, None) => Ok(String::new()),
        (NodeKind::Note, Some("-")) => read_all(stdin, "the note"),
        (NodeKind::Note, Some(text)) => Ok(text.to_owned()),
        (other, Some(_)) => Err(Failure::Arguments(format!("--body is for notes, and this is a {other}"))),
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

/// How the node `create -p` found differs from what was asked for, if it
/// does: a description or owner given must be the existing node's, or the
/// caller would believe it had made what it asked for.
fn existing_mismatch(new: &NewNode<'_>, description: &str, owner: Option<&str>) -> Option<String> {
    let mut differs = Vec::new();
    if !new.description.is_empty() && new.description != description {
        differs.push(format!("its description is {description:?}, not {:?}", new.description));
    }
    if !new.owner.is_empty() && Some(new.owner) != owner {
        differs.push(format!(
            "its owner is {}, not {:?}",
            owner.map_or_else(|| "none".to_owned(), |o| format!("{o:?}")),
            new.owner
        ));
    }
    if differs.is_empty() { None } else { Some(format!("{} exists already, but {}", new.path, differs.join(" and "))) }
}

/// What `create` makes.
pub struct NewNode<'a> {
    pub kind: NodeKind,
    pub path: &'a str,
    pub description: &'a str,
    /// A queue's owner; empty for the signer's own name.
    pub owner: &'a str,
    /// A note's first version; empty for none.
    pub body: &'a str,
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
            let read: NodeRead = api.get(&node_url(new.path))?;
            let node = read
                .node
                .ok_or_else(|| Failure::BadAnswer { what: node_url(new.path), cause: "no node".to_owned() })?;
            if let Some(mismatch) = existing_mismatch(new, &node.description, node.owner.as_deref()) {
                return Err(Failure::Exit(mismatch));
            }
            let created = Created { path: new.path.to_owned(), kind: new.kind, id, existed: true };
            return emit(&Line::Created(&created));
        }
        Plan::Make(containers) => containers,
    };
    let body = new.body;
    let make = |path: &str, kind, description, owner, body| -> Result<(), Failure> {
        let action = Create { path, kind, description, owner, body };
        let created: Created = submit(api, &signer, "/create", &action)?;
        emit(&Line::Created(&created))
    };
    for container in &containers {
        make(container, NodeKind::Container, "", "", "")?;
    }
    make(new.path, new.kind, new.description, new.owner, body)
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
pub fn note_set(api: &Api, path: &str, base: i64, thread: &str, through: Option<i64>) -> Result<(), Failure> {
    let signer = signer()?;
    let body = read_all(io::stdin().lock(), "the note")?;
    let action = SetNote { path, body: &body, base_version: base, thread, through };
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

/// Why a target named no one registration, and the registrations it could
/// have meant.
#[derive(Debug)]
struct Miss<'a> {
    reason: String,
    candidates: Vec<&'a Registration>,
}

/// The pending registration `target` names: a key fingerprint (`SHA256:...`)
/// or an item number, each exactly, else a name, if one pending key has it.
fn pick<'a>(target: &str, pending: &'a [Registration]) -> Result<&'a Registration, Miss<'a>> {
    let (what, hits): (&str, Vec<&Registration>) = if target.starts_with("SHA256:") {
        ("key", pending.iter().filter(|r| r.fingerprint == target).collect())
    } else if !target.is_empty() && target.bytes().all(|b| b.is_ascii_digit()) {
        ("item", pending.iter().filter(|r| target.parse() == Ok(r.item)).collect())
    } else {
        ("name", pending.iter().filter(|r| r.name == target).collect())
    };
    match hits.as_slice() {
        [one] => Ok(one),
        [] => Err(Miss {
            reason: format!("no pending registration has the {what} {target}; the pending ones are listed above"),
            candidates: pending.iter().collect(),
        }),
        _ => Err(Miss {
            reason: format!(
                "{} pending keys are named {target}: approve one by fingerprint or item number",
                hits.len()
            ),
            candidates: hits,
        }),
    }
}

/// A registration on a line, for choosing among them.
fn candidate(r: &Registration) -> String {
    format!("  #{} {} {} {} {}", r.item, r.fingerprint, r.name, r.repo, r.note.lines().next().unwrap_or_default())
}

/// Approve a registration: signed over the key's fingerprint and the name,
/// both as the board has them, so what the owner signs is that key.
pub fn approve(api: &Api, target: &str) -> Result<(), Failure> {
    let signer = signer()?;
    let pending: Vec<Registration> = api.get("/registrations")?;
    let registration = match pick(target, &pending) {
        Ok(registration) => registration,
        Err(miss) => {
            for each in &miss.candidates {
                warn(&candidate(each))?;
            }
            return Err(Failure::Exit(miss.reason));
        }
    };
    let (item, name, fingerprint) = (registration.item, &registration.name, &registration.fingerprint);
    let resolved: Resolved = submit(api, &signer, "/approve", &Approve { item, name, fingerprint })?;
    emit(&Line::Approved(&Approved { item, name: name.clone(), fingerprint: fingerprint.clone(), id: resolved.id }))
}

/// Archive (or unarchive) message or item `id`; the owner key only.
pub fn archive(api: &Api, id: i64, archived: bool, reason: &str) -> Result<(), Failure> {
    let signer = signer()?;
    let action = Archive { event: id, archived, reason };
    let route = if archived { "/archive" } else { "/unarchive" };
    let done: Archived = submit(api, &signer, route, &action)?;
    emit(&Line::Archived(&done))
}

/// The fingerprint `target` names: itself, or the key bound to that name.
fn revoke_target(api: &Api, target: &str) -> Result<String, Failure> {
    if target.starts_with("SHA256:") {
        return Ok(target.to_owned());
    }
    let keys: Vec<Key> = api.get("/keys")?;
    bound_key(target, &keys).map(|key| key.fingerprint.clone())
}

/// The one key bound to `name` (owner or trusted).
fn bound_key<'a>(name: &str, keys: &'a [Key]) -> Result<&'a Key, Failure> {
    let bound: Vec<&Key> =
        keys.iter().filter(|k| k.name == name && matches!(k.state, KeyState::Owner | KeyState::Trusted)).collect();
    match bound.as_slice() {
        [one] => Ok(one),
        [] => Err(Failure::Exit(format!("no key is bound to the name {name}; give a key fingerprint (SHA256:...)"))),
        _ => Err(Failure::Exit(format!("several keys are bound to {name}; give a key fingerprint (SHA256:...)"))),
    }
}

pub fn revoke(api: &Api, target: &str, reason: &str) -> Result<(), Failure> {
    let signer = signer()?;
    let fingerprint = revoke_target(api, target)?;
    let revoked: Revoked = submit(api, &signer, "/revoke", &Revoke { fingerprint: &fingerprint, reason })?;
    emit(&Line::Revoked(&revoked))
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;

    fn registration(item: i64, name: &str, fingerprint: &str) -> Registration {
        Registration {
            item,
            name: name.to_owned(),
            fingerprint: fingerprint.to_owned(),
            repo: "r".to_owned(),
            note: "first\nsecond".to_owned(),
            replaces: None,
            look_alikes: Vec::new(),
        }
    }

    fn pending() -> Vec<Registration> {
        vec![
            registration(10_001, "alice", "SHA256:a"),
            registration(10_002, "bob", "SHA256:b"),
            registration(10_003, "bob", "SHA256:c"),
        ]
    }

    #[test]
    fn a_target_names_a_registration_by_name_fingerprint_or_item() {
        let pending = pending();
        assert_eq!(pick("alice", &pending).map(|r| r.item).ok(), Some(10_001));
        assert_eq!(pick("SHA256:c", &pending).map(|r| r.item).ok(), Some(10_003));
        assert_eq!(pick("10002", &pending).map(|r| r.item).ok(), Some(10_002));
    }

    #[test]
    fn a_target_that_names_no_one_registration_lists_the_candidates() {
        let pending = pending();
        let two = pick("bob", &pending).err();
        assert!(two.as_ref().is_some_and(|m| m.reason.contains("2 pending keys") && m.candidates.len() == 2));
        for target in ["carol", "SHA256:zzz", "99999", "SHA256", "ali"] {
            let miss = pick(target, &pending).err();
            assert!(miss.as_ref().is_some_and(|m| m.candidates.len() == 3), "{target}");
        }
        assert_eq!(pending.first().map(candidate).as_deref(), Some("  #10001 SHA256:a alice r first"));
    }

    #[test]
    fn a_name_revokes_only_the_one_key_bound_to_it() {
        let key = |fingerprint: &str, name: &str, state| Key {
            fingerprint: fingerprint.to_owned(),
            name: name.to_owned(),
            state,
            item: None,
            revoked: None,
        };
        let keys = [
            key("SHA256:old", "alice", KeyState::Revoked),
            key("SHA256:new", "alice", KeyState::Trusted),
            key("SHA256:p", "bob", KeyState::Pending),
        ];
        assert_eq!(bound_key("alice", &keys).map(|k| k.fingerprint.as_str()).ok(), Some("SHA256:new"));
        assert!(bound_key("bob", &keys).is_err());
        assert!(bound_key("nobody", &keys).is_err());
    }

    #[test]
    fn a_backoff_is_random_within_its_bounds_and_grows_with_the_tries() -> Result<(), Failure> {
        for stale in 1..=NONCE_TRIES {
            let ms = backoff(stale)?.as_millis();
            let (low, high) = (u128::from(BACKOFF_MS.0), u128::from(BACKOFF_MS.1));
            assert!((low * u128::from(stale)..=high * u128::from(stale)).contains(&ms), "{stale}: {ms}");
        }
        let spread: std::collections::BTreeSet<_> = (0..40).map(|_| backoff(1)).collect::<Result<_, _>>()?;
        assert!(spread.len() > 5, "{spread:?}");
        Ok(())
    }

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

    #[test]
    fn a_v1_from_is_refused_before_anything_is_sent() {
        let words = |words: &[&str]| words.iter().map(|w| (*w).to_owned()).collect::<Vec<_>>();
        for v1 in [&["--from", "alice", "hi"][..], &["--from=alice", "hi"]] {
            assert!(matches!(refuse_from(&words(v1)), Err(Failure::V1Form(_))), "{v1:?}");
        }
        for body in [&["hi", "--from", "alice"][..], &["from", "alice"], &["--fromage"], &[]] {
            assert!(refuse_from(&words(body)).is_ok(), "{body:?}");
        }
    }

    #[test]
    fn a_note_body_comes_from_stdin_only_when_asked() -> Result<(), Failure> {
        let unread = || -> &[u8] { b"unread" };
        assert_eq!(create_body(NodeKind::Note, None, unread())?, "");
        assert_eq!(create_body(NodeKind::Note, Some("-"), "piped".as_bytes())?, "piped");
        assert_eq!(create_body(NodeKind::Note, Some("text"), unread())?, "text");
        assert!(matches!(create_body(NodeKind::Thread, Some("x"), unread()), Err(Failure::Arguments(_))));
        assert_eq!(create_body(NodeKind::Thread, None, unread())?, "");
        Ok(())
    }

    #[test]
    fn create_p_on_an_existing_node_names_what_differs() {
        let new = |description, owner| NewNode {
            kind: NodeKind::Queue,
            path: "r/q",
            description,
            owner,
            body: "",
            parents: true,
        };
        assert_eq!(existing_mismatch(&new("", ""), "whatever", Some("alice")), None);
        assert_eq!(existing_mismatch(&new("same", "alice"), "same", Some("alice")), None);
        let owner = existing_mismatch(&new("", "bob"), "d", Some("alice")).unwrap_or_default();
        assert!(owner.contains("r/q exists already") && owner.contains(r#"owner is "alice", not "bob""#), "{owner}");
        let both = existing_mismatch(&new("new", "bob"), "old", Some("alice")).unwrap_or_default();
        assert!(both.contains(r#"description is "old", not "new""#) && both.contains(" and its owner"), "{both}");
        let none = existing_mismatch(&new("", "bob"), "", None).unwrap_or_default();
        assert!(none.contains("owner is none"), "{none}");
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
