//! The board's storage: a tree of nodes, the events every write appends,
//! the items of queues, and the keys allowed to write.
//!
//! Shared by board-server (which exposes it over HTTP) and board-admin
//! (which opens the database directly). The state is one SQLite database
//! in WAL mode (the schema is in `schema.rs`).
//!
//! # Mechanics
//!
//! One connection serves the whole process, behind a mutex. Every
//! operation holds the mutex for its whole length and runs in one
//! transaction, so it is atomic, and writes are serialized. Long-poll
//! delivery is a condition variable on the same mutex: a poller queries
//! and waits without letting go of it, and a writer notifies once it has
//! committed, so a write can never land between a poller's query and its
//! wait (no lost wakeup).
//!
//! # Events and the counter
//!
//! Every write appends one or more rows to `events`, and `events.id` is the
//! board's only counter: the `#N` in text, the nonce, the watch cursor, the
//! ids of items and note versions. The head is the highest id ever issued
//! (see [`Board::head`]); it starts at [`FIRST_ID`] - 1.
//!
//! # Writes are signed
//!
//! Every write but the board's own arrives as a [`Signed`] action: the
//! action's fields, a client key, the nonce and an SSHSIG over all of them.
//! The store checks the signature itself, against the very fields it then
//! stores, so what is signed and what is written cannot drift apart. In
//! order, a write is:
//!
//! 1. verified (outside the lock: it is the slow part);
//! 2. looked up by client key: a resend of a write that landed returns the
//!    first result, and the same key on another request is a conflict;
//! 3. admitted: the key must be trusted (or, for [`Board::register`], may
//!    be anyone's);
//! 4. checked for a current nonce: the head it was signed against must
//!    still be the head, which makes a signature good for one write;
//! 5. done, or refused with nothing written.
//!
//! Names are bound to keys by the owner's approval ([`Board::approve`]);
//! the server derives who wrote from the key, never from the request.

mod actions;
mod events;
mod items;
mod keys;
mod nodes;
mod poll;
mod schema;

use std::path::Path;
use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, SystemTimeError};

use rusqlite::types::ValueRef;
use rusqlite::{Connection, ErrorCode, OptionalExtension as _, Row};
use serde_json::{Value, json};

use crate::sign::SignatureError;
use crate::timefmt;

pub use actions::{Action, Approve, Create, File, Post, Register, Resolve, Revoke, SetNote, Signed};
pub use events::{Event, EventType, Message};
pub use items::{Item, ItemDetail, ItemQuery, ItemResolution, ItemState, StateFilter};
pub use keys::{Key, KeyState, Owner, OwnerChange, REGISTER_TEXT_MAX, Registration};
pub use nodes::{
    Child, DESCRIPTION_MAX, Deleted, Node, NodeKind, NoteVersion, PATH_RULE, Range, SEGMENT_MAX, Status, ThreadPage,
    Version,
};
pub use poll::{Brief, BriefContainer, Delivered, Page, Watch, Why};

/// The outcome of each write, as the client is told it.
pub use actions::{Created, Filed, NoteSaved, Posted, Registered, Resolved, Revoked};

/// The first event id of a fresh board. The v1 board's ids end near 1100:
/// text that cites `#1074` stays unambiguous, and no v2 id can be taken
/// for a v1 one.
pub const FIRST_ID: i64 = 10_001;
pub const NAME_MAX: usize = 64;
pub const NAME_RULE: &str = "1-64 ASCII letters, digits, '_' and '-', not starting with '-' or '_'";
/// How many edits (ignoring case) a name must be from every bound one.
pub const MIN_DISTANCE: usize = 3;
/// The board's own name: the actor of its own events. Nobody registers it
/// or a name that looks like it.
pub const SERVER_NAME: &str = "board";
/// The root in a watch: what happens to the top-level nodes. It is no
/// node, and nothing else takes it.
pub const ROOT: &str = "/";
/// The board's own container, and the two nodes in it.
pub const BOARD_NODE: &str = "_board";
/// Where registrations are filed; the owner's queue.
pub const REGISTER_QUEUE: &str = "_board/register";
/// The trust list, a note the board rewrites at every approve and revoke.
pub const TRUST_NOTE: &str = "_board/trust";
/// The most events one poll returns.
pub const POLL_LIMIT: i64 = 500;
/// About the most event text one poll returns: it stops after the event
/// that reaches this, and the cursor is that event's id.
pub const POLL_BYTES: usize = 1024 * 1024;
/// How many messages a thread read returns unless asked otherwise.
pub const THREAD_TAIL: i64 = 50;
/// How often a waiting long poll asks whether its client still wants it.
const POLL_CHECK: Duration = Duration::from_secs(1);
/// How long a write waits for another program's lock on the file.
pub const BUSY_TIMEOUT: Duration = Duration::from_secs(5);
/// What a key with no registration is told to run.
pub const REGISTER_COMMAND: &str = "board register --name <name> --note \"<repo, purpose>\"";

/// A fault in the database itself, as opposed to a refusal.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("{0}")]
    Sqlite(#[from] rusqlite::Error),
    /// The connection is closed after this: see [`DbError::is_fatal`].
    #[error("{cause}; rolling back failed too: {rollback}; the database is closed now{}",
            .closing.as_ref().map(|err| format!(", and closing it failed: {err}")).unwrap_or_default())]
    Rollback { cause: Box<Error>, rollback: rusqlite::Error, closing: Option<rusqlite::Error> },
    #[error("the database is closed")]
    Closed,
    #[error("the database lock is poisoned: {0}")]
    Poisoned(String),
    #[error("the system clock is before 1970: {0}")]
    Clock(#[from] SystemTimeError),
    #[error("event ids have run out")]
    Exhausted,
    /// The file holds a database that is not a v2 board: opening it would
    /// write v2 tables into it. Nothing was written.
    #[error("{0}")]
    NotABoard(String),
}

impl DbError {
    /// Another program holds the database file: worth retrying.
    #[must_use]
    pub fn is_busy(&self) -> bool {
        match self {
            Self::Sqlite(err) => {
                matches!(err.sqlite_error_code(), Some(ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked))
            }
            Self::Rollback { cause, .. } => matches!(cause.as_ref(), Error::Db(db) if db.is_busy()),
            _ => false,
        }
    }

    /// The board cannot go on: a transaction that would not roll back
    /// (its connection is closed now), or a lock a panic left poisoned.
    /// Every later operation would fail; a program serving the board
    /// should exit so that it is restarted.
    #[must_use]
    pub fn is_fatal(&self) -> bool {
        matches!(self, Self::Rollback { .. } | Self::Poisoned(_))
    }
}

impl<T> From<PoisonError<T>> for DbError {
    fn from(err: PoisonError<T>) -> Self {
        Self::Poisoned(err.to_string())
    }
}

/// Why the board did not do something. Each refusal has a code and an HTTP
/// status ([`Error::code`], [`Error::status`]) and is sent as
/// [`Error::to_json`]; its `Display` is the message, without the code.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// A request the board cannot make sense of: a malformed path or name,
    /// the wrong kind of node, a missing body.
    #[error("{0}")]
    BadRequest(String),
    /// A note version with nothing in it: a failed pipe must not wipe a note.
    #[error("a note version cannot be empty")]
    EmptyNote,
    /// The signature does not verify over the request's fields.
    #[error("bad signature: {0}")]
    BadSignature(#[source] SignatureError),
    #[error("key {fingerprint} is not registered; register it: {REGISTER_COMMAND}")]
    NotRegistered { fingerprint: String },
    #[error("this key's registration #{item} waits for the owner's approval")]
    RegistrationPending { item: i64 },
    #[error("this key's registration #{item} was declined; stop and tell the human; do not register again unless asked to")]
    RegistrationDeclined { item: i64 },
    #[error("this key was revoked at #{revoked_at}; stop and tell the human; do not make a new key on your own")]
    KeyRevoked { revoked_at: i64 },
    /// A trusted key that may not do this.
    #[error("{0}")]
    Forbidden(String),
    #[error("{0}")]
    NotFound(String),
    /// The nonce is not the head: a replay, or another write landed between
    /// the client reading the head and this one arriving.
    #[error("the head is now {head}; sign again with it as the nonce")]
    StaleNonce { head: i64 },
    /// The note is no longer at the version the editor started from.
    #[error("the note is at v{version} now; read it and edit that version")]
    NoteChanged { version: i64 },
    #[error("item #{item} is {state}, not open{}", .resolved.map(|id| format!(" (#{id})")).unwrap_or_default())]
    ItemNotOpen { item: i64, state: ItemState, resolved: Option<i64> },
    /// A client key already used for a different request, or a node that
    /// already exists.
    #[error("{0}")]
    Conflict(String),
    #[error("this key is already registered as {name:?}")]
    AlreadyRegistered { name: String },
    #[error(
        "{} registrations are waiting already; try again once the owner has gone through them",
        keys::REGISTER_OPEN_MAX
    )]
    RegisterFull,
    #[error(transparent)]
    Db(#[from] DbError),
}

impl From<rusqlite::Error> for Error {
    fn from(err: rusqlite::Error) -> Self {
        Self::Db(DbError::Sqlite(err))
    }
}

impl From<SystemTimeError> for Error {
    fn from(err: SystemTimeError) -> Self {
        Self::Db(DbError::Clock(err))
    }
}

impl Error {
    /// The refusal's code: the `error` field and the `Board-Error` header.
    #[must_use]
    pub fn code(&self) -> &'static str {
        match self {
            Self::BadRequest(_) | Self::BadSignature(_) => "bad-request",
            Self::EmptyNote => "empty-note",
            Self::NotRegistered { .. } => "not-registered",
            Self::RegistrationPending { .. } => "registration-pending",
            Self::RegistrationDeclined { .. } => "registration-declined",
            Self::KeyRevoked { .. } => "key-revoked",
            Self::Forbidden(_) => "forbidden",
            Self::NotFound(_) => "not-found",
            Self::StaleNonce { .. } => "stale-nonce",
            Self::NoteChanged { .. } => "note-changed",
            Self::ItemNotOpen { .. } => "item-not-open",
            Self::Conflict(_) => "conflict",
            Self::AlreadyRegistered { .. } => "already-registered",
            Self::RegisterFull => "register-full",
            Self::Db(db) if db.is_busy() => "busy",
            Self::Db(_) => "internal",
        }
    }

    /// The HTTP status the refusal is sent with.
    #[must_use]
    pub fn status(&self) -> u16 {
        match self {
            Self::BadRequest(_) | Self::BadSignature(_) | Self::EmptyNote => 400,
            Self::NotRegistered { .. }
            | Self::RegistrationPending { .. }
            | Self::RegistrationDeclined { .. }
            | Self::KeyRevoked { .. }
            | Self::Forbidden(_) => 403,
            Self::NotFound(_) => 404,
            Self::StaleNonce { .. }
            | Self::NoteChanged { .. }
            | Self::ItemNotOpen { .. }
            | Self::Conflict(_)
            | Self::AlreadyRegistered { .. } => 409,
            Self::RegisterFull => 429,
            Self::Db(db) if db.is_busy() => 503,
            Self::Db(_) => 500,
        }
    }

    /// A refusal of a key's identity (not registered, pending, declined,
    /// revoked): the CLI exits 3 on these, so a launcher can tell them
    /// apart.
    #[must_use]
    pub fn is_identity(&self) -> bool {
        matches!(
            self,
            Self::NotRegistered { .. }
                | Self::RegistrationPending { .. }
                | Self::RegistrationDeclined { .. }
                | Self::KeyRevoked { .. }
        )
    }

    /// The refusal as the HTTP API sends it:
    /// `{"error": code, "message": ..., <the code's own fields>}`.
    #[must_use]
    pub fn to_json(&self) -> Value {
        let mut out = json!({ "error": self.code(), "message": self.to_string() });
        let fields = match self {
            Self::NotRegistered { fingerprint } => json!({ "fingerprint": fingerprint, "command": REGISTER_COMMAND }),
            Self::RegistrationPending { item } | Self::RegistrationDeclined { item } => json!({ "item": item }),
            Self::KeyRevoked { revoked_at } => json!({ "revoked_at": revoked_at }),
            Self::StaleNonce { head } => json!({ "head": head }),
            Self::NoteChanged { version } => json!({ "version": version }),
            Self::ItemNotOpen { item, state, resolved } => {
                json!({ "item": item, "state": state, "resolved": resolved })
            }
            Self::AlreadyRegistered { name } => json!({ "name": name }),
            _ => Value::Null,
        };
        if let (Some(out), Value::Object(fields)) = (out.as_object_mut(), fields) {
            out.extend(fields);
        }
        out
    }
}

/// A name in a message: quoted, with anything but printable ASCII escaped,
/// so that a look-alike letter (a Cyrillic "a") shows as what it is.
fn quoted(name: &str) -> String {
    format!("\"{}\"", name.escape_default())
}

/// Refuse a name outside [`NAME_RULE`].
///
/// # Errors
/// [`Error::BadRequest`], quoting the name.
pub fn check_name(name: &str) -> Result<(), Error> {
    let allowed = |b: u8| b.is_ascii_alphanumeric() || b == b'_' || b == b'-';
    // A leading '-' would pass for an option on a command line, and '_' is
    // the board's own (`_board`).
    if (1..=NAME_MAX).contains(&name.len()) && name.bytes().all(allowed) && !name.starts_with(['-', '_']) {
        Ok(())
    } else {
        Err(Error::BadRequest(format!("name {} is not allowed: names are {NAME_RULE}", quoted(name))))
    }
}

/// Levenshtein distance: the fewest insertions, deletions and
/// substitutions that turn `a` into `b`.
#[must_use]
pub fn distance(a: &str, b: &str) -> usize {
    let b: Vec<char> = b.chars().collect();
    let mut previous: Vec<usize> = (0..=b.len()).collect();
    let mut result = b.len();
    for (i, ca) in a.chars().enumerate() {
        let mut left = i.saturating_add(1);
        let mut row = Vec::with_capacity(previous.len());
        row.push(left);
        for ((diagonal, up), cb) in previous.iter().zip(previous.iter().skip(1)).zip(&b) {
            let substitution = diagonal.saturating_add(usize::from(ca != *cb));
            left = up.saturating_add(1).min(left.saturating_add(1)).min(substitution);
            row.push(left);
        }
        result = left;
        previous = row;
    }
    result
}

/// Whether two names are fewer than [`MIN_DISTANCE`] edits apart, ignoring
/// case: `Stefan`, `stefan` and `Stefn` all look alike.
#[must_use]
pub fn looks_alike(a: &str, b: &str) -> bool {
    distance(&a.to_lowercase(), &b.to_lowercase()) < MIN_DISTANCE
}

/// Refuse [`SERVER_NAME`] and names that look like it.
fn check_reserved(name: &str) -> Result<(), Error> {
    if looks_alike(name, SERVER_NAME) {
        return Err(Error::Forbidden(format!(
            "name {} is the board's own or too close to it; use one at least {MIN_DISTANCE} edits from {}",
            quoted(name),
            quoted(SERVER_NAME)
        )));
    }
    Ok(())
}

/// A text column as text, whatever another program left in it: bytes
/// that are not UTF-8 as U+FFFD, a number as its digits, NULL as empty.
/// Read strictly, one such row failed every read that covered it -- every
/// poll past it, for every watcher, for ever.
fn text(row: &Row<'_>, column: usize) -> rusqlite::Result<String> {
    Ok(match row.get_ref(column)? {
        ValueRef::Null => String::new(),
        ValueRef::Integer(n) => n.to_string(),
        ValueRef::Real(x) => x.to_string(),
        ValueRef::Text(bytes) | ValueRef::Blob(bytes) => String::from_utf8_lossy(bytes).into_owned(),
    })
}

/// A nullable text column, leniently as [`text`]: NULL is `None`.
fn opt_text(row: &Row<'_>, column: usize) -> rusqlite::Result<Option<String>> {
    match row.get_ref(column)? {
        ValueRef::Null => Ok(None),
        _ => text(row, column).map(Some),
    }
}

/// The highest event id ever issued.
fn head(conn: &Connection) -> rusqlite::Result<i64> {
    // The highest id ever issued, not the highest still present:
    // AUTOINCREMENT never hands an id out twice even after the newest row
    // is deleted (board-admin can do that), and every cursor watchers hold
    // is derived from it. sqlite_sequence holds that high-water mark;
    // MAX(id) would fall back below ids that watchers have already seen.
    Ok(conn
        .query_row("SELECT seq FROM sqlite_sequence WHERE name='events'", [], |r| r.get(0))
        .optional()?
        .unwrap_or(FIRST_ID - 1))
}

/// The board's database, open.
pub struct Board {
    db: Mutex<Option<Connection>>,
    changed: Condvar,
}

/// A freshly opened board, and the journal mode the file ended up in
/// (`"wal"` unless the file system cannot do WAL).
pub struct Opened {
    pub board: Board,
    pub journal_mode: String,
}

impl Board {
    /// Open the board database at `path`, creating and bootstrapping it if
    /// the file does not exist or is empty.
    ///
    /// A file that holds any other database -- the v1 board's above all --
    /// is refused before anything is written to it, so that its bytes stay
    /// as they were.
    ///
    /// # Errors
    /// [`DbError::NotABoard`] for another database, and if the file cannot
    /// be opened or bootstrapped.
    pub fn open(path: &Path) -> Result<Opened, DbError> {
        let fresh = schema::probe(path)?;
        let conn = Connection::open(path)?;
        conn.busy_timeout(BUSY_TIMEOUT)?;
        let journal_mode: String = conn.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
        conn.execute_batch("PRAGMA foreign_keys=ON")?;
        let board = Self { db: Mutex::new(Some(conn)), changed: Condvar::new() };
        if fresh {
            board.write(|conn, now| Ok(schema::bootstrap(conn, now)?)).map_err(|err| match err {
                Error::Db(db) => db,
                // bootstrap fails only in SQLite, but the type cannot say so
                other => DbError::NotABoard(format!("bootstrapping the board failed: {other}")),
            })?;
        }
        Ok(Opened { board, journal_mode })
    }

    fn lock(&self) -> Result<MutexGuard<'_, Option<Connection>>, DbError> {
        Ok(self.db.lock()?)
    }

    /// Close the database: wait for the operation in flight, then close
    /// the connection (which checkpoints the WAL). Every later operation
    /// fails with [`DbError::Closed`].
    ///
    /// # Errors
    /// If closing fails; the connection is closed regardless.
    pub fn close(&self) -> Result<(), DbError> {
        let taken = self.lock()?.take();
        match taken {
            Some(conn) => conn.close().map_err(|(_unclosed, err)| DbError::Sqlite(err)),
            None => Ok(()),
        }
    }

    /// Run `op` on the connection. Reads take no transaction of their own,
    /// as the lock already keeps this process's writes out; only another
    /// program (board-admin) could write between two of their statements.
    fn read<T>(&self, op: impl FnOnce(&Connection) -> Result<T, Error>) -> Result<T, Error> {
        let guard = self.lock()?;
        let conn = guard.as_ref().ok_or(DbError::Closed)?;
        op(conn)
    }

    /// [`Board::read`] for a read that cannot be refused, only fail.
    fn read_db<T>(&self, op: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Result<T, DbError> {
        let guard = self.lock()?;
        let conn = guard.as_ref().ok_or(DbError::Closed)?;
        Ok(op(conn)?)
    }

    /// Run `op` in one transaction: commit if it succeeds, roll back if it
    /// fails, and wake every poller after a commit.
    ///
    /// The transaction takes the write lock up front (BEGIN IMMEDIATE), so
    /// while another program holds the file the busy timeout applies: a
    /// deferred transaction that has already read gets `SQLITE_BUSY` at once
    /// when it tries to write, without waiting. The price is that a refusal
    /// or a no-op also needs the lock, so on a locked or read-only file it
    /// fails as a write would.
    fn write<T>(&self, op: impl FnOnce(&Connection, &str) -> Result<T, Error>) -> Result<T, Error> {
        let mut guard = self.lock()?;
        let conn = guard.as_ref().ok_or(DbError::Closed)?;
        // Taken with the lock held, so timestamps follow the order of ids.
        let now = timefmt::now()?;
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let outcome = op(conn, &now).and_then(|value| {
            conn.execute_batch("COMMIT")?;
            Ok(value)
        });
        match outcome {
            Ok(value) => {
                self.changed.notify_all();
                Ok(value)
            }
            Err(err) if conn.is_autocommit() => Err(err),
            Err(err) => match conn.execute_batch("ROLLBACK") {
                Ok(()) => Err(err),
                Err(rollback) => {
                    // Still in the transaction, every later write would fail
                    // and the file would stay locked to every other program.
                    // Closing the connection rolls it back.
                    let closing = guard.take().and_then(|conn| conn.close().err()).map(|(_unclosed, err)| err);
                    Err(Error::Db(DbError::Rollback { cause: Box::new(err), rollback, closing }))
                }
            },
        }
    }

    /// The highest event id ever issued: the nonce a write is signed with.
    ///
    /// # Errors
    /// On a database fault.
    pub fn head(&self) -> Result<i64, DbError> {
        self.read_db(head)
    }
}

#[cfg(test)]
mod tests;
