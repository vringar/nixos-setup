//! The store against real SQLite files in a temporary directory, with real
//! keys and signatures. The helpers here sign every write as a client
//! would: against the board's head, with a fresh client key.

mod files;
mod identity;
mod items;
mod notes;
mod paths;
mod poll;
mod writes;

use std::error::Error as StdError;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use super::*;
use crate::sign::{self, PrivateKey};

type TestResult = Result<(), Box<dyn StdError>>;

/// The owner's name in every fixture.
const OWNER: &str = "Stefan";

struct Fixture {
    _dir: tempfile::TempDir,
    path: std::path::PathBuf,
    board: Arc<Board>,
    /// `Stefan`'s key, the owner key (seed 1).
    owner: PrivateKey,
}

/// A key from a fixed seed: the same in every run.
fn key(seed: u8) -> Result<PrivateKey, sign::SignatureError> {
    PrivateKey::from_seed(&[seed; 32])
}

fn verified(key: &PrivateKey) -> sign::Verified {
    sign::Verified { public_line: key.public_line(), fingerprint: key.fingerprint() }
}

/// A fresh board with `Stefan` as the owner.
fn fixture() -> Result<Fixture, Box<dyn StdError>> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("board-v2.db");
    let board = Board::open(&path)?.board;
    let owner = key(1)?;
    board.set_owner(OWNER, &verified(&owner))?;
    Ok(Fixture { _dir: dir, path, board: Arc::new(board), owner })
}

/// A client key no other request in this test run uses.
fn client_key() -> String {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    format!("k-{}", NEXT.fetch_add(1, Ordering::Relaxed))
}

/// A request signed by a key against the board's head, as a client makes it.
struct Request<A> {
    action: A,
    key: String,
    nonce: String,
    signature: String,
}

impl<A: Action + Copy> Request<A> {
    fn new(board: &Board, private: &PrivateKey, action: A) -> Result<Self, Error> {
        Self::keyed(board, private, action, &client_key())
    }

    fn keyed(board: &Board, private: &PrivateKey, action: A, key: &str) -> Result<Self, Error> {
        let nonce = board.head()?.to_string();
        let signature = action.sign(private, key, &nonce).map_err(Error::BadSignature)?;
        Ok(Self { action, key: key.to_owned(), nonce, signature })
    }

    fn signed(&self) -> Signed<'_, A> {
        Signed { action: self.action, key: &self.key, nonce: &self.nonce, signature: &self.signature }
    }
}

/// Sign `$action` with `$key` and run it as `$board.$method`.
macro_rules! submit {
    ($board:expr, $key:expr, $method:ident, $action:expr) => {
        Request::new(&$board, $key, $action).and_then(|request| $board.$method(&request.signed()))
    };
}
use submit;

fn create(path: &str, kind: NodeKind) -> Create<'_> {
    Create { path, kind, description: "", owner: "", body: "" }
}

impl Fixture {
    /// A key `seed`, registered as `name` and approved by the owner.
    fn agent(&self, seed: u8, name: &str) -> Result<PrivateKey, Box<dyn StdError>> {
        let private = key(seed)?;
        let registered = self.register(&private, name)?;
        self.approve(registered.item, name, &private)?;
        Ok(private)
    }

    fn register(&self, private: &PrivateKey, name: &str) -> Result<Registered, Error> {
        submit!(self.board, private, register, Register { name, note: "for a test", repo: "/src/test" })
    }

    /// The owner approves registration `item` of `name` by `private`.
    fn approve(&self, item: i64, name: &str, private: &PrivateKey) -> Result<Resolved, Error> {
        submit!(self.board, &self.owner, approve, Approve { item, name, fingerprint: &private.fingerprint() })
    }

    fn revoke(&self, by: &PrivateKey, fingerprint: &str, reason: &str) -> Result<Revoked, Error> {
        submit!(self.board, by, revoke, Revoke { fingerprint, reason })
    }

    fn create(&self, by: &PrivateKey, path: &str, kind: NodeKind) -> Result<Created, Error> {
        submit!(self.board, by, create, create(path, kind))
    }

    fn queue(&self, by: &PrivateKey, path: &str, owner: &str) -> Result<Created, Error> {
        submit!(self.board, by, create, Create { owner, ..create(path, NodeKind::Queue) })
    }

    fn post(&self, by: &PrivateKey, path: &str, body: &str) -> Result<Posted, Error> {
        submit!(self.board, by, post, Post { path, body })
    }

    fn set_note(
        &self,
        by: &PrivateKey,
        path: &str,
        body: &str,
        base_version: i64,
        through: Option<i64>,
    ) -> Result<NoteSaved, Error> {
        submit!(self.board, by, set_note, SetNote { path, body, base_version, through })
    }

    fn file(&self, by: &PrivateKey, path: &str, body: &str) -> Result<Filed, Error> {
        submit!(self.board, by, file, File { path, body })
    }

    fn resolve(&self, by: &PrivateKey, item: i64, state: ItemState, body: &str) -> Result<Resolved, Error> {
        submit!(self.board, by, resolve, Resolve { item, state, body, reference: None })
    }

    /// `rev`, made by the owner: the container most tests work in.
    fn rev(&self) -> Result<(), Error> {
        self.create(&self.owner, "rev", NodeKind::Container).map(drop)
    }
}

/// The refusal a result is, or an error saying it was not one.
fn refused<T: std::fmt::Debug>(result: Result<T, Error>) -> Result<Error, String> {
    match result {
        Ok(value) => Err(format!("accepted: {value:?}")),
        Err(err) => Ok(err),
    }
}
