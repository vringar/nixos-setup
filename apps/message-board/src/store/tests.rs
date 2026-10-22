//! The store against real SQLite files in a temporary directory, with real
//! keys and signatures.

use std::error::Error as StdError;
use std::sync::atomic::{AtomicU64, Ordering};

use super::*;
use crate::sign::{self, PrivateKey};

type TestResult = Result<(), Box<dyn StdError>>;

struct Fixture {
    _dir: tempfile::TempDir,
    path: std::path::PathBuf,
    board: Board,
    owner: PrivateKey,
}

/// A key from a fixed seed: the same in every run.
fn key(seed: u8) -> Result<PrivateKey, Box<dyn StdError>> {
    Ok(PrivateKey::from_seed(&[seed; 32])?)
}

fn verified(key: &PrivateKey) -> sign::Verified {
    sign::Verified { public_line: key.public_line(), fingerprint: key.fingerprint() }
}

/// A fresh board with `Stefan` as the owner (key seed 1).
fn fixture() -> Result<Fixture, Box<dyn StdError>> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("board-v2.db");
    let board = Board::open(&path)?.board;
    let owner = key(1)?;
    board.set_owner("Stefan", &verified(&owner))?;
    Ok(Fixture { _dir: dir, path, board, owner })
}

/// A client key no other request in this test run uses.
fn client_key() -> String {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    format!("k-{}", NEXT.fetch_add(1, Ordering::Relaxed))
}

/// A request signed by `private` against the board's head, as a client
/// makes it.
struct Request<A> {
    action: A,
    key: String,
    nonce: String,
    signature: String,
}

impl<A: Action + Copy> Request<A> {
    fn new(board: &Board, private: &PrivateKey, action: A) -> Result<Self, Box<dyn StdError>> {
        Self::keyed(board, private, action, &client_key())
    }

    fn keyed(board: &Board, private: &PrivateKey, action: A, key: &str) -> Result<Self, Box<dyn StdError>> {
        let nonce = board.head()?.to_string();
        let signature = action.sign(private, key, &nonce)?;
        Ok(Self { action, key: key.to_owned(), nonce, signature })
    }

    fn signed(&self) -> Signed<'_, A> {
        Signed { action: self.action, key: &self.key, nonce: &self.nonce, signature: &self.signature }
    }
}

/// Sign `action` with `$key` and run it as `$board.$method`.
macro_rules! submit {
    ($board:expr, $key:expr, $method:ident, $action:expr) => {
        Request::new(&$board, $key, $action).map(|request| $board.$method(&request.signed()))
    };
}

/// Register `name` with `private` and have the owner approve it.
fn trusted(f: &Fixture, private: &PrivateKey, name: &str) -> Result<i64, Box<dyn StdError>> {
    let registered = submit!(f.board, private, register, Register { name, note: "test", repo: "" })??;
    let approve = Approve { item: registered.item, name, fingerprint: &private.fingerprint() };
    Ok(submit!(f.board, &f.owner, approve, approve)??.id)
}

fn create(path: &str, kind: NodeKind) -> Create<'_> {
    Create { path, kind, description: "", owner: "", body: "" }
}

#[test]
fn a_round_trip_through_the_main_flows() -> TestResult {
    let f = fixture()?;
    let agent = key(2)?;
    // unknown, then pending, then trusted
    let early = submit!(f.board, &agent, post, Post { path: "rev/log", body: "hi" })?.err().ok_or("accepted")?;
    assert!(matches!(&early, Error::NotRegistered { fingerprint } if *fingerprint == agent.fingerprint()));
    let registered = submit!(f.board, &agent, register, Register { name: "agent", note: "n", repo: "/r" })??;
    let pending = submit!(f.board, &agent, post, Post { path: "rev/log", body: "hi" })?.err().ok_or("accepted")?;
    assert!(matches!(pending, Error::RegistrationPending { item } if item == registered.item));
    let approve = Approve { item: registered.item, name: "agent", fingerprint: &agent.fingerprint() };
    let approved = submit!(f.board, &f.owner, approve, approve)??;
    assert_eq!(approved.state, ItemState::Answered);

    submit!(f.board, &f.owner, create, create("rev", NodeKind::Container))??;
    submit!(f.board, &agent, create, create("rev/log", NodeKind::Thread))??;
    let since = f.board.head()?;
    let posted = submit!(f.board, &agent, post, Post { path: "rev/log", body: "hello @Stefan" })??;
    let watch = Watch { paths: vec!["rev/log".to_owned()], mentions: vec!["Stefan".to_owned()], ..Watch::default() };
    let page = f.board.poll(&watch, since, std::time::Duration::ZERO, || true)?.ok_or("gave up")?;
    let [delivered] = page.events.as_slice() else { return Err(format!("not one event: {page:?}").into()) };
    assert_eq!(delivered.event.id, posted.id);
    assert_eq!(delivered.event.from.as_deref(), Some("agent"));
    assert_eq!(delivered.why, [Why::Path, Why::Mention]);
    assert_eq!(page.cursor, f.board.head()?);
    assert!(f.path.exists());
    Ok(())
}

#[test]
fn trusted_helper_works() -> TestResult {
    let f = fixture()?;
    trusted(&f, &key(3)?, "helper")?;
    assert_eq!(f.board.key(&key(3)?.fingerprint())?.map(|k| k.state), Some(KeyState::Trusted));
    Ok(())
}
