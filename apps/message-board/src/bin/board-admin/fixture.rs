//! A board with a little of everything on it, for the tests, written
//! through the store's own signed writes as the server would make them:
//! board-admin only reads and deletes, so it has no way to fill one itself.

use std::cell::Cell;
use std::error::Error;
use std::fs;
use std::os::unix::fs::PermissionsExt as _;

use message_board::sign::{self, PrivateKey, Verified};
use message_board::store::{self, Action, Board, Create, File, ItemState, NodeKind, Post, Resolve, SetNote, Signed};

use crate::audit::Audit;

type Fallible<T> = Result<T, Box<dyn Error>>;

/// The ids the tests refer to.
pub struct Ids {
    /// The three messages of `rev/log`, oldest first.
    pub first: i64,
    pub second: i64,
    pub third: i64,
}

pub struct Fixture {
    dir: tempfile::TempDir,
    pub board: Board,
    pub ids: Ids,
    owner: PrivateKey,
    /// The next client key: each write needs its own.
    next: Cell<u32>,
}

impl Fixture {
    /// A board in a temporary directory, with `stefan` as its owner, the
    /// only key that writes here, and:
    ///
    /// ```text
    /// rev          container
    /// rev/asks     queue: one item, answered with a reference to the third message
    /// rev/empty    container, nothing in it
    /// rev/log      thread: three messages
    /// rev/state    note: v1, summarizing rev/log through the second message
    /// ```
    pub fn populated() -> Fallible<Self> {
        let dir = tempfile::tempdir()?;
        let board = Board::open(&dir.path().join("board-v2.db"))?.board;
        // The key file goes through the same parser as a real one.
        let key_file = dir.path().join("owner_ed25519");
        fs::write(&key_file, sign::generate("board-admin test")?.private)?;
        fs::set_permissions(&key_file, fs::Permissions::from_mode(0o600))?;
        let owner = sign::load_private_key(&key_file)?;
        board.set_owner("stefan", &Verified { public_line: owner.public_line(), fingerprint: owner.fingerprint() })?;
        let mut fixture = Self { dir, board, ids: Ids { first: 0, second: 0, third: 0 }, owner, next: Cell::new(1) };

        for (path, kind) in [
            ("rev", NodeKind::Container),
            ("rev/asks", NodeKind::Queue),
            ("rev/empty", NodeKind::Container),
            ("rev/log", NodeKind::Thread),
            ("rev/state", NodeKind::Note),
        ] {
            fixture.write(Create { path, kind, description: "", owner: "", body: "" }, Board::create)?;
        }
        let [first, second, third] =
            ["first", "second", "third"].map(|body| fixture.write(Post { path: "rev/log", body }, Board::post));
        fixture.ids = Ids { first: first?.id, second: second?.id, third: third?.id };
        let note = SetNote {
            path: "rev/state",
            body: "the state of things\nall fine",
            base_version: 0,
            through: Some(fixture.ids.second),
        };
        fixture.write(note, Board::set_note)?;
        let item = fixture.write(File { path: "rev/asks", body: "please look at it" }, Board::file)?.item;
        let answer = Resolve { item, state: ItemState::Answered, body: "done", reference: Some(fixture.ids.third) };
        fixture.write(answer, Board::resolve)?;
        Ok(fixture)
    }

    /// Sign `action` with the owner's key against the current head, and
    /// have the board do it with `write`.
    fn write<A: Action + Copy, T>(
        &self,
        action: A,
        write: impl FnOnce(&Board, &Signed<'_, A>) -> Result<T, store::Error>,
    ) -> Fallible<T> {
        let key = format!("admin-test-{}", self.next.replace(self.next.get().saturating_add(1)));
        let nonce = self.board.head()?.to_string();
        let signature = action.sign(&self.owner, &key, &nonce)?;
        Ok(write(&self.board, &Signed { action, key: &key, nonce: &nonce, signature: &signature })?)
    }

    pub fn audit(&self) -> Audit {
        Audit { path: self.dir.path().join("audit.log") }
    }

    /// What the audit log holds: nothing, if nothing was ever written to it.
    pub fn audit_log(&self) -> Fallible<String> {
        let path = self.audit().path;
        Ok(if path.exists() { fs::read_to_string(path)? } else { String::new() })
    }
}
