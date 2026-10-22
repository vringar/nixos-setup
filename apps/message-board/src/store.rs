//! The board's storage: schema, migrations, claims, pins, nonces, the
//! archive, and deletion.
//!
//! Shared by board-server (which exposes it over HTTP) and board-admin
//! (which opens the database directly). The state is one SQLite database
//! in WAL mode, so a restart re-derives the board and message ids stay
//! monotonic.
//!
//! One connection serves the whole process, behind a mutex. Every
//! operation holds the mutex for its whole length and runs in one
//! transaction, so it is atomic, and writes are serialized. Long-poll
//! delivery is a condition variable on the same mutex: a poller queries
//! and waits without letting go of it, and a writer notifies once it has
//! committed, so a write can never land between a poller's query and its
//! wait (no lost wakeup).
//!
//! Sender names are 1-64 ASCII letters, digits, '_' and '-'
//! ([`check_name`]). [`SERVER_NAME`] is the board's own, for its notes,
//! and is held as if claimed with no key: no client write may use it or a
//! name that looks like it.
//!
//! Name claims: a sender name is free until a post signed as that name
//! lands, which binds the name to the signing key ("first use, then
//! locked"). After that, posts and pin edits as the name must be signed by
//! that key. A free name fewer than [`MIN_DISTANCE`] edits from a claimed
//! one, ignoring case, cannot be written as, signed or not. A name is only
//! an alias for its key, though: a signed pin answers to the key that
//! signed it (and the owner's), whatever name a later save uses. Checking
//! signatures is the caller's job; this module only gets the verified
//! result as a [`Signer`].
//!
//! Replay: a signed write's nonce is the head it was signed against, and
//! it is accepted only while that is still the head. Every post and pin
//! save appends a message and so moves the head, which makes a signature
//! good for one write.

use std::path::Path;
use std::sync::{Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant, SystemTimeError};

use rusqlite::types::Value;
use rusqlite::{Connection, ErrorCode, OptionalExtension as _, Row, params, params_from_iter};
use serde::Serialize;

use crate::timefmt;

pub const SYSTEM_AREA: &str = "_system";
/// Where area-wide pin updates are announced.
pub const PIN_TOPIC: &str = "_pin";
pub const NAME_MAX: usize = 64;
pub const NAME_RULE: &str = "1-64 ASCII letters, digits, '_' and '-'";
/// How many edits (ignoring case) a free name must be from every claimed one.
pub const MIN_DISTANCE: usize = 3;
/// The sender of the board's own notes (new area, archived, ...): reserved,
/// as if the server held a claim on it.
pub const SERVER_NAME: &str = "board";
/// The most messages one poll returns.
pub const POLL_LIMIT: i64 = 500;
/// About the most message text one poll returns: it stops after the
/// message that reaches this. A client pages on from the last id it got.
pub const POLL_BYTES: usize = 1024 * 1024;
/// How often a waiting long poll asks whether its client still wants it.
const POLL_CHECK: Duration = Duration::from_secs(1);
/// How long a write waits for another program's lock on the file.
pub const BUSY_TIMEOUT: Duration = Duration::from_secs(5);

const SCHEMA: &str = "
    CREATE TABLE IF NOT EXISTS areas(
        name TEXT PRIMARY KEY,
        description TEXT NOT NULL DEFAULT '',
        created_ts TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        area TEXT NOT NULL,
        topic TEXT NOT NULL,
        sender TEXT NOT NULL,
        body TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_messages_area ON messages(area, id);
    CREATE TABLE IF NOT EXISTS pins(
        area TEXT NOT NULL,
        topic TEXT NOT NULL,  -- '' = the area-wide pin
        body TEXT NOT NULL,
        version INTEGER NOT NULL,
        editor TEXT NOT NULL,
        ts TEXT NOT NULL,
        PRIMARY KEY(area, topic));
    CREATE TABLE IF NOT EXISTS identities(
        name TEXT PRIMARY KEY,
        pubkey TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        source TEXT NOT NULL CHECK(source IN ('tofu', 'owner')),
        registered_ts TEXT NOT NULL);
    -- Every pin save, with who signed it: the address of one pin version
    -- (versions restart at 1 after a pin is removed, so `id` is the key).
    CREATE TABLE IF NOT EXISTS pin_versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        area TEXT NOT NULL,
        topic TEXT NOT NULL,
        version INTEGER NOT NULL,
        body TEXT NOT NULL,
        editor TEXT NOT NULL,
        base_version INTEGER,  -- as signed; NULL = saved without a stale check
        signed_by TEXT,
        signature TEXT,
        ts TEXT NOT NULL);
";

/// Columns added after the first boards were created. SQLite cannot add a
/// UNIQUE column, so `client_key`'s uniqueness is an index (NULLs stay
/// distinct).
const ADDED_COLUMNS: [(&str, &str); 5] = [
    ("areas", "archived_ts"),
    ("messages", "client_key"),
    ("messages", "signed_by"),
    ("messages", "signature"),
    ("pins", "signed_by"),
];

/// Only messages in areas that are not archived.
const LIVE: &str = " AND area NOT IN (SELECT name FROM areas WHERE archived_ts IS NOT NULL)";

/// A fault in the database itself, as opposed to a refusal.
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("{0}")]
    Sqlite(#[from] rusqlite::Error),
    #[error("{cause}; rolling back failed too: {rollback}")]
    Rollback { cause: Box<Error>, rollback: rusqlite::Error },
    #[error("the database is closed")]
    Closed,
    #[error("the database lock is poisoned: {0}")]
    Poisoned(String),
    #[error("the system clock is before 1970: {0}")]
    Clock(#[from] SystemTimeError),
    #[error("message ids have run out")]
    Exhausted,
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
}

impl<T> From<PoisonError<T>> for DbError {
    fn from(err: PoisonError<T>) -> Self {
        Self::Poisoned(err.to_string())
    }
}

/// Why the board did not do something.
#[derive(Debug, thiserror::Error)]
pub enum Error {
    /// A sender name outside [`NAME_RULE`].
    #[error("{0}")]
    BadName(String),
    /// An area or topic name messages cannot be addressed by.
    #[error("{0}")]
    BadPlace(String),
    /// A claimed name without its key, a reserved or look-alike name, or a
    /// signed pin that is not the signer's.
    #[error("{0}")]
    Forbidden(String),
    /// A client key already used for a different message.
    #[error("{0}")]
    Conflict(String),
    /// A signed write whose nonce is not the head: a replay, or another
    /// write landed between the client reading the head and this one
    /// arriving.
    #[error("stale-nonce: the head is now {head}; sign again with it as the nonce")]
    StaleNonce { head: i64 },
    /// The pin changed since the editor loaded it.
    #[error("pin changed since base_version")]
    StalePin { version: i64 },
    /// No area by that name.
    #[error("no such area {0:?}")]
    NoSuchArea(String),
    #[error("{SYSTEM_AREA} cannot be archived")]
    SystemArea,
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

/// Who signed a write, as established by the caller's signature check.
#[derive(Debug, Clone)]
pub struct Signer {
    /// "ssh-ed25519 AAAA..."
    pub pubkey: String,
    /// "SHA256:..."
    pub fingerprint: String,
    /// The SSHSIG it was verified from, base64, as it was sent.
    pub signature: String,
    /// The head it was signed against, as signed.
    pub nonce: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Message {
    pub id: i64,
    pub ts: String,
    pub area: String,
    pub topic: String,
    #[serde(rename = "from")]
    pub sender: String,
    pub body: String,
    pub signed_by: Option<String>,
    pub verified: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Pin {
    pub area: String,
    pub topic: String,
    pub body: String,
    pub version: i64,
    pub editor: String,
    pub ts: String,
    pub signed_by: Option<String>,
    pub verified: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Area {
    pub name: String,
    pub description: String,
    pub created_ts: String,
    pub archived_ts: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Identity {
    pub name: String,
    pub pubkey: String,
    pub fingerprint: String,
    pub source: String,
    pub registered_ts: String,
}

/// One message as board-admin lists it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listed {
    pub id: i64,
    pub ts: String,
    pub sender: String,
    pub body: String,
}

/// A message to append.
#[derive(Debug, Clone, Copy)]
pub struct NewPost<'a> {
    pub area: &'a str,
    pub topic: &'a str,
    pub sender: &'a str,
    pub body: &'a str,
    /// Makes the post idempotent: re-sending a key with the same content
    /// returns the original id; with different content it is a conflict.
    pub key: Option<&'a str>,
}

/// A pin save.
#[derive(Debug, Clone, Copy)]
pub struct PinSave<'a> {
    pub area: &'a str,
    pub topic: &'a str,
    pub editor: &'a str,
    /// Empty (or only whitespace) removes the pin.
    pub body: &'a str,
    /// The version the editor started from; `None` saves without a stale check.
    pub base_version: Option<i64>,
}

/// Which messages a read or poll is about.
#[derive(Debug, Clone, Copy)]
pub struct Scope<'a> {
    pub since: i64,
    /// Empty: every area.
    pub area: &'a str,
    /// Empty: every topic. A topic also takes in the area pin's updates.
    pub topic: &'a str,
    pub include_archived: bool,
}

/// A name in a message: quoted, with anything but printable ASCII escaped,
/// so that a look-alike letter (a Cyrillic "a") shows as what it is.
fn quoted(name: &str) -> String {
    format!("\"{}\"", name.escape_default())
}

/// Raise [`Error::BadName`] unless `name` may be written as a sender.
///
/// # Errors
/// If `name` is outside [`NAME_RULE`].
pub fn check_name(name: &str) -> Result<(), Error> {
    let allowed = |b: u8| b.is_ascii_alphanumeric() || b == b'_' || b == b'-';
    if (1..=NAME_MAX).contains(&name.len()) && name.bytes().all(allowed) {
        Ok(())
    } else {
        Err(Error::BadName(format!("sender name {} is not allowed: names are {NAME_RULE}", quoted(name))))
    }
}

/// Raise [`Error::BadPlace`] unless `area` and `topic` can address
/// messages: neither empty, since an empty area or topic means "all of
/// them" in every read, and no control characters. `topic` is `None` for an
/// area's own pin.
///
/// # Errors
/// [`Error::BadPlace`], naming what is wrong.
pub fn check_place(area: &str, topic: Option<&str>) -> Result<(), Error> {
    check_place_name("area", area)?;
    topic.map_or(Ok(()), |topic| check_place_name("topic", topic))
}

fn check_place_name(what: &str, name: &str) -> Result<(), Error> {
    if name.is_empty() {
        return Err(Error::BadPlace(format!("{what} name cannot be empty")));
    }
    if name.chars().any(char::is_control) {
        return Err(Error::BadPlace(format!("{what} name {} has a control character", quoted(name))));
    }
    Ok(())
}

/// Raise [`Error::Forbidden`] for the board's own topic: every
/// topic-scoped read and watch also shows `_pin`, where the board announces
/// area pin updates, so a client message there would reach them all.
fn check_topic_is_not_the_boards(topic: &str) -> Result<(), Error> {
    if topic == PIN_TOPIC {
        return Err(Error::Forbidden(format!(
            "topic {} is the board's own: area pin updates are announced there",
            quoted(PIN_TOPIC)
        )));
    }
    Ok(())
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

fn looks_alike(a: &str, b: &str) -> bool {
    distance(&a.to_lowercase(), &b.to_lowercase()) < MIN_DISTANCE
}

/// Raise [`Error::Forbidden`] if `name` is [`SERVER_NAME`] or looks like it.
fn check_reserved(name: &str) -> Result<(), Error> {
    if name == SERVER_NAME {
        return Err(Error::Forbidden(format!("name {} is the board itself; write as your own name", quoted(name))));
    }
    if looks_alike(name, SERVER_NAME) {
        return Err(Error::Forbidden(format!(
            "name {} is too close to the reserved name {}; use one at least {MIN_DISTANCE} edits from it",
            quoted(name),
            quoted(SERVER_NAME)
        )));
    }
    Ok(())
}

fn message(row: &Row<'_>) -> rusqlite::Result<Message> {
    let signed_by: Option<String> = row.get(6)?;
    Ok(Message {
        id: row.get(0)?,
        ts: row.get(1)?,
        area: row.get(2)?,
        topic: row.get(3)?,
        sender: row.get(4)?,
        body: row.get(5)?,
        verified: signed_by.is_some(),
        signed_by,
    })
}

const MESSAGE_COLUMNS: &str = "id, ts, area, topic, sender, body, signed_by";

fn head_id(conn: &Connection) -> rusqlite::Result<i64> {
    // The highest id ever issued, not the highest still present: AUTOINCREMENT
    // never hands an id out twice even after the newest row is deleted
    // (board-admin can do that), and every cursor watchers hold is derived
    // from it. sqlite_sequence holds that high-water mark; MAX(id) would fall
    // back below ids that watchers have already seen.
    Ok(conn
        .query_row("SELECT seq FROM sqlite_sequence WHERE name='messages'", [], |r| r.get(0))
        .optional()?
        .unwrap_or(0))
}

fn query(conn: &Connection, scope: &Scope<'_>, limit: i64) -> rusqlite::Result<Vec<Message>> {
    let mut sql = format!("SELECT {MESSAGE_COLUMNS} FROM messages WHERE id>?");
    let mut args = vec![Value::Integer(scope.since)];
    if !scope.include_archived {
        sql.push_str(LIVE);
    }
    if !scope.area.is_empty() {
        sql.push_str(" AND area=?");
        args.push(Value::Text(scope.area.to_owned()));
    }
    if !scope.topic.is_empty() {
        // an area pin applies to every topic, so its updates do too
        sql.push_str(" AND topic IN (?, ?)");
        args.push(Value::Text(scope.topic.to_owned()));
        args.push(Value::Text(PIN_TOPIC.to_owned()));
    }
    sql.push_str(" ORDER BY id LIMIT ?");
    args.push(Value::Integer(limit));
    let mut statement = conn.prepare(&sql)?;
    let mut rows = statement.query(params_from_iter(args))?;
    let mut found = Vec::new();
    let mut text = 0_usize;
    while let Some(row) = rows.next()? {
        let m = message(row)?;
        text = text.saturating_add(m.body.len()).saturating_add(m.area.len()).saturating_add(m.topic.len());
        found.push(m);
        if text >= POLL_BYTES {
            break;
        }
    }
    Ok(found)
}

fn get_identity(conn: &Connection, name: &str) -> rusqlite::Result<Option<Identity>> {
    conn.query_row(
        "SELECT name, pubkey, fingerprint, source, registered_ts FROM identities WHERE name=?",
        [name],
        |r| {
            Ok(Identity {
                name: r.get(0)?,
                pubkey: r.get(1)?,
                fingerprint: r.get(2)?,
                source: r.get(3)?,
                registered_ts: r.get(4)?,
            })
        },
    )
    .optional()
}

fn near_claims(conn: &Connection, name: &str) -> rusqlite::Result<Vec<String>> {
    let claimed: Vec<String> = conn
        .prepare("SELECT name FROM identities ORDER BY name")?
        .query_map([], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(claimed.into_iter().filter(|c| c != name && looks_alike(name, c)).collect())
}

fn check_nonce(conn: &Connection, signer: Option<&Signer>) -> Result<(), Error> {
    if let Some(signer) = signer {
        let head = head_id(conn)?;
        if signer.nonce != head.to_string() {
            return Err(Error::StaleNonce { head });
        }
    }
    Ok(())
}

/// Raise [`Error::Forbidden`] unless `sender` may write as signed (or
/// unsigned). A claimed name answers to its key only; a free one must not
/// look like a claimed one, and nobody writes as [`SERVER_NAME`]. Returns
/// whether the write would claim the name.
fn check_claim(conn: &Connection, sender: &str, signer: Option<&Signer>) -> Result<bool, Error> {
    check_reserved(sender)?;
    let Some(ident) = get_identity(conn, sender)? else {
        if let Some(near) = near_claims(conn, sender)?.first() {
            return Err(Error::Forbidden(format!(
                "name {} is too close to the claimed name {}; use one at least {MIN_DISTANCE} edits from every claimed name",
                quoted(sender),
                quoted(near)
            )));
        }
        return Ok(signer.is_some());
    };
    match signer {
        None => Err(Error::Forbidden(format!("name {} is claimed; sign the post", quoted(sender)))),
        Some(signer) if signer.fingerprint != ident.fingerprint => Err(Error::Forbidden(format!(
            "name {} is claimed by key {}, not {}",
            quoted(sender),
            ident.fingerprint,
            signer.fingerprint
        ))),
        Some(_) => Ok(false),
    }
}

/// Bind a free name to its first signer.
fn claim(conn: &Connection, now: &str, sender: &str, signer: &Signer) -> rusqlite::Result<()> {
    conn.execute(
        "INSERT INTO identities VALUES(?,?,?,'tofu',?)",
        params![sender, signer.pubkey, signer.fingerprint, now],
    )?;
    Ok(())
}

/// A row for the messages table.
struct MessageRow<'a> {
    area: &'a str,
    topic: &'a str,
    sender: &'a str,
    body: &'a str,
    key: Option<&'a str>,
    /// Marks `sender` as authenticated.
    signed_by: Option<&'a str>,
    /// Only stored when it signs this row's own fields (a pin doorbell has
    /// a signer, but no signature of its own).
    signature: Option<&'a str>,
}

fn insert(conn: &Connection, now: &str, row: &MessageRow<'_>) -> rusqlite::Result<i64> {
    conn.execute(
        "INSERT INTO messages(ts, area, topic, sender, body, client_key, signed_by, signature)
         VALUES(?,?,?,?,?,?,?,?)",
        params![now, row.area, row.topic, row.sender, row.body, row.key, row.signed_by, row.signature],
    )?;
    Ok(conn.last_insert_rowid())
}

/// A note from the board itself on `_system/areas`.
fn note(conn: &Connection, now: &str, body: &str) -> rusqlite::Result<i64> {
    insert(
        conn,
        now,
        &MessageRow {
            area: SYSTEM_AREA,
            topic: "areas",
            sender: SERVER_NAME,
            body,
            key: None,
            signed_by: None,
            signature: None,
        },
    )
}

/// Flip an existing area's archive state and announce it.
fn set_archived_row(conn: &Connection, now: &str, name: &str, archived: bool) -> rusqlite::Result<()> {
    conn.execute("UPDATE areas SET archived_ts=? WHERE name=?", params![archived.then_some(now), name])?;
    let state = if archived { "archived" } else { "unarchived" };
    note(conn, now, &format!("area {state}: **{name}**"))?;
    Ok(())
}

/// Create the area if new, revive it if archived. Every write goes
/// through here, so any activity in an archived area brings it back: a
/// late agent can never write into an area nobody can see.
fn ensure_area(conn: &Connection, now: &str, name: &str, description: &str) -> rusqlite::Result<()> {
    let row: Option<(String, Option<String>)> = conn
        .query_row("SELECT description, archived_ts FROM areas WHERE name=?", [name], |r| Ok((r.get(0)?, r.get(1)?)))
        .optional()?;
    match row {
        None => {
            conn.execute(
                "INSERT INTO areas(name, description, created_ts) VALUES(?,?,?)",
                params![name, description, now],
            )?;
            if name != SYSTEM_AREA {
                ensure_area(conn, now, SYSTEM_AREA, "")?;
                let about = if description.is_empty() { String::new() } else { format!(" — {description}") };
                note(conn, now, &format!("new area: **{name}**{about}"))?;
            }
        }
        Some((existing, archived_ts)) => {
            if archived_ts.is_some() {
                set_archived_row(conn, now, name, false)?;
            }
            if !description.is_empty() && existing.is_empty() {
                conn.execute("UPDATE areas SET description=? WHERE name=?", params![description, name])?;
            }
        }
    }
    Ok(())
}

fn get_pin(conn: &Connection, area: &str, topic: &str) -> rusqlite::Result<Option<Pin>> {
    conn.query_row(
        "SELECT area, topic, body, version, editor, ts, signed_by FROM pins WHERE area=? AND topic=?",
        [area, topic],
        |r| {
            let signed_by: Option<String> = r.get(6)?;
            Ok(Pin {
                area: r.get(0)?,
                topic: r.get(1)?,
                body: r.get(2)?,
                version: r.get(3)?,
                editor: r.get(4)?,
                ts: r.get(5)?,
                verified: signed_by.is_some(),
                signed_by,
            })
        },
    )
    .optional()
}

fn list_areas(conn: &Connection, include_archived: bool) -> rusqlite::Result<Vec<Area>> {
    let filter = if include_archived { "" } else { " WHERE archived_ts IS NULL" };
    conn.prepare(&format!("SELECT name, description, created_ts, archived_ts FROM areas{filter} ORDER BY created_ts"))?
        .query_map([], |r| {
            Ok(Area { name: r.get(0)?, description: r.get(1)?, created_ts: r.get(2)?, archived_ts: r.get(3)? })
        })?
        .collect()
}

/// The schema: create what is missing, and add the columns that older
/// boards lack. Statements that change nothing need no write lock, so a
/// board on a read-only file still opens (and serves reads) if it is up to
/// date.
fn migrate(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(SCHEMA)?;
    for (table, column) in ADDED_COLUMNS {
        let present: Vec<String> = conn
            .prepare(&format!("PRAGMA table_info({table})"))?
            .query_map([], |r| r.get(1))?
            .collect::<rusqlite::Result<_>>()?;
        if !present.iter().any(|c| c == column) {
            conn.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} TEXT"))?;
        }
    }
    conn.execute_batch("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_key ON messages(client_key)")
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
    /// Open (and migrate) the board database at `path`, creating it if it
    /// does not exist.
    ///
    /// # Errors
    /// If the file cannot be opened or migrated.
    pub fn open(path: &Path) -> Result<Opened, DbError> {
        let conn = Connection::open(path)?;
        conn.busy_timeout(BUSY_TIMEOUT)?;
        let journal_mode: String = conn.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
        migrate(&conn)?;
        Ok(Opened { board: Self { db: Mutex::new(Some(conn)), changed: Condvar::new() }, journal_mode })
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

    fn read<T>(&self, op: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Result<T, DbError> {
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
        let guard = self.lock()?;
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
                Err(rollback) => Err(Error::Db(DbError::Rollback { cause: Box::new(err), rollback })),
            },
        }
    }

    /// The highest message id ever issued.
    ///
    /// # Errors
    /// On a database fault.
    pub fn head_id(&self) -> Result<i64, DbError> {
        self.read(head_id)
    }

    /// Append a message; returns its id. `signer` is the verified signer of
    /// exactly these fields, or `None`. Its nonce is checked after the key,
    /// so a resend of a signed post that did land returns the original id
    /// rather than a stale nonce.
    ///
    /// # Errors
    /// [`Error::BadPlace`], [`Error::BadName`], [`Error::Conflict`],
    /// [`Error::StaleNonce`] or [`Error::Forbidden`] if the post is
    /// refused, and on a database fault.
    pub fn post(&self, new: &NewPost<'_>, signer: Option<&Signer>) -> Result<i64, Error> {
        check_place(new.area, Some(new.topic))?;
        check_topic_is_not_the_boards(new.topic)?;
        check_name(new.sender)?;
        self.write(|conn, now| {
            if let Some(key) = new.key {
                let earlier: Option<(i64, String, String, String, String)> = conn
                    .query_row("SELECT id, area, topic, sender, body FROM messages WHERE client_key=?", [key], |r| {
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
                    })
                    .optional()?;
                if let Some((id, area, topic, sender, body)) = earlier {
                    if (area.as_str(), topic.as_str(), sender.as_str(), body.as_str())
                        == (new.area, new.topic, new.sender, new.body)
                    {
                        return Ok(id);
                    }
                    return Err(Error::Conflict(format!("key {key} was already used for a different message")));
                }
            }
            check_nonce(conn, signer)?;
            let claims = check_claim(conn, new.sender, signer)?;
            ensure_area(conn, now, new.area, "")?;
            if let (true, Some(signer)) = (claims, signer) {
                claim(conn, now, new.sender, signer)?;
            }
            Ok(insert(
                conn,
                now,
                &MessageRow {
                    area: new.area,
                    topic: new.topic,
                    sender: new.sender,
                    body: new.body,
                    key: new.key,
                    signed_by: signer.map(|s| s.fingerprint.as_str()),
                    signature: signer.map(|s| s.signature.as_str()),
                },
            )?)
        })
    }

    /// Create, replace or (empty body) remove a pin, and announce it on the
    /// log; returns the version saved (or removed).
    ///
    /// A signed pin stays signed: only a save signed by the same key, or by
    /// the owner's, may change or remove it, whatever name it is saved as.
    ///
    /// # Errors
    /// [`Error::StalePin`] if `base_version` is not the current version, a
    /// refusal as for [`Board::post`], and on a database fault.
    pub fn set_pin(&self, save: &PinSave<'_>, signer: Option<&Signer>) -> Result<i64, Error> {
        check_place(save.area, (!save.topic.is_empty()).then_some(save.topic))?;
        check_topic_is_not_the_boards(save.topic)?;
        check_name(save.editor)?;
        self.write(|conn, now| {
            check_nonce(conn, signer)?;
            let current = get_pin(conn, save.area, save.topic)?;
            let version = current.as_ref().map_or(0, |pin| pin.version);
            let place =
                if save.topic.is_empty() { save.area.to_owned() } else { format!("{}/{}", save.area, save.topic) };
            let claims = check_claim(conn, save.editor, signer)?;
            if let Some(pin) = current.as_ref().filter(|pin| pin.verified) {
                let owner: Option<String> = conn
                    .query_row("SELECT fingerprint FROM identities WHERE source='owner'", [], |r| r.get(0))
                    .optional()?;
                let allowed = signer.is_some_and(|s| {
                    Some(&s.fingerprint) == pin.signed_by.as_ref() || Some(&s.fingerprint) == owner.as_ref()
                });
                if !allowed {
                    return Err(Error::Forbidden(format!(
                        "pin {place} v{version} is signed by key {} (as {}); only that key or the owner's can change it{}",
                        pin.signed_by.as_deref().unwrap_or_default(),
                        pin.editor,
                        if signer.is_some() { "" } else { ", and only signed" }
                    )));
                }
            }
            if save.base_version.is_some_and(|base| base != version) {
                return Err(Error::StalePin { version });
            }
            if current.is_none() && save.body.trim().is_empty() {
                // Nothing to remove: no doorbell, no new area, version 0
                // as for any place without a pin.
                return Ok(0);
            }
            if let (true, Some(signer)) = (claims, signer) {
                claim(conn, now, save.editor, signer)?;
            }
            let fingerprint = signer.map(|s| s.fingerprint.as_str());
            let (doorbell, saved) = if save.body.trim().is_empty() {
                conn.execute("DELETE FROM pins WHERE area=? AND topic=?", [save.area, save.topic])?;
                (format!("pin removed: {place}"), version)
            } else {
                let saved = version.checked_add(1).ok_or(DbError::Exhausted)?;
                conn.execute(
                    "INSERT OR REPLACE INTO pins(area, topic, body, version, editor, ts, signed_by)
                     VALUES(?,?,?,?,?,?,?)",
                    params![save.area, save.topic, save.body, saved, save.editor, now, fingerprint],
                )?;
                conn.execute(
                    "INSERT INTO pin_versions(area, topic, version, body, editor, base_version,
                     signed_by, signature, ts) VALUES(?,?,?,?,?,?,?,?,?)",
                    params![
                        save.area,
                        save.topic,
                        saved,
                        save.body,
                        save.editor,
                        save.base_version,
                        fingerprint,
                        signer.map(|s| s.signature.as_str()),
                        now
                    ],
                )?;
                // A doorbell, not a copy: an agent scoped to one topic must
                // not see another topic's (or the whole area's) pin text as an
                // ordinary message, out of the context a human reading the
                // pin box has.
                let reference = if save.topic.is_empty() {
                    format!("board pin {}", save.area)
                } else {
                    format!("board pin {} {}", save.area, save.topic)
                };
                (format!("pin updated: {place} v{saved} — see `{reference}`"), saved)
            };
            ensure_area(conn, now, save.area, "")?;
            // The board writes the doorbell: it carries the save's signer, so
            // the editor shows as verified, but no signature of its own text.
            insert(
                conn,
                now,
                &MessageRow {
                    area: save.area,
                    topic: if save.topic.is_empty() { PIN_TOPIC } else { save.topic },
                    sender: save.editor,
                    body: &doorbell,
                    key: None,
                    signed_by: fingerprint,
                    signature: None,
                },
            )?;
            Ok(saved)
        })
    }

    /// Create an area (announcing it), or give an existing one a
    /// description if it has none yet.
    ///
    /// # Errors
    /// On a database fault.
    pub fn create_area(&self, name: &str, description: &str) -> Result<(), Error> {
        check_place(name, None)?;
        self.write(|conn, now| Ok(ensure_area(conn, now, name, description)?))
    }

    /// Archive or unarchive an area; false if it already was. Archiving
    /// hides an area from listings and unscoped reads; nothing is deleted,
    /// and the next post or pin edit in the area unarchives it.
    ///
    /// # Errors
    /// [`Error::SystemArea`] for `_system`, [`Error::NoSuchArea`], and on a
    /// database fault.
    pub fn set_archived(&self, name: &str, archived: bool) -> Result<bool, Error> {
        if name == SYSTEM_AREA {
            return Err(Error::SystemArea);
        }
        self.write(|conn, now| {
            let state: Option<Option<String>> =
                conn.query_row("SELECT archived_ts FROM areas WHERE name=?", [name], |r| r.get(0)).optional()?;
            let Some(archived_ts) = state else {
                return Err(Error::NoSuchArea(name.to_owned()));
            };
            if archived_ts.is_some() == archived {
                return Ok(false);
            }
            set_archived_row(conn, now, name, archived)?;
            Ok(true)
        })
    }

    /// Make `name` the owner identity, bound to this key. Overrides
    /// whatever claim the name had, but never [`SERVER_NAME`]'s. There is
    /// one owner: a previous owner under another name keeps its claim but
    /// becomes ordinary.
    ///
    /// # Errors
    /// [`Error::BadName`] or [`Error::Forbidden`] for a name that cannot be
    /// an owner, and on a database fault.
    pub fn set_owner(&self, name: &str, pubkey: &str, fingerprint: &str) -> Result<(), Error> {
        check_name(name)?;
        check_reserved(name)?;
        // At every start but the first it is so already: nothing to write,
        // and a read-only database still starts.
        let bound = self.read(|conn| {
            let others: i64 =
                conn.query_row("SELECT count(*) FROM identities WHERE source='owner' AND name<>?", [name], |r| {
                    r.get(0)
                })?;
            let current = get_identity(conn, name)?;
            Ok(others == 0 && current.is_some_and(|i| i.source == "owner" && i.fingerprint == fingerprint))
        })?;
        if bound {
            return Ok(());
        }
        self.write(|conn, now| {
            conn.execute("UPDATE identities SET source='tofu' WHERE source='owner' AND name<>?", [name])?;
            let current = get_identity(conn, name)?;
            if !current.is_some_and(|i| i.source == "owner" && i.fingerprint == fingerprint) {
                conn.execute(
                    "INSERT OR REPLACE INTO identities VALUES(?,?,?,'owner',?)",
                    params![name, pubkey, fingerprint, now],
                )?;
            }
            Ok(())
        })
    }

    /// The messages in `scope`, oldest first: at most `limit`, and none
    /// after the one that reaches [`POLL_BYTES`] of text.
    ///
    /// # Errors
    /// On a database fault.
    pub fn query(&self, scope: &Scope<'_>, limit: i64) -> Result<Vec<Message>, DbError> {
        self.read(|conn| query(conn, scope, limit))
    }

    /// The long poll: the next messages in `scope` (as [`Board::query`]
    /// with [`POLL_LIMIT`]), waiting up to `timeout` for one to be posted.
    /// While it waits it asks `wanted` every second or so, and gives up
    /// with `None` once that says no: a client that has gone must not hold
    /// its thread and socket for the rest of the timeout.
    ///
    /// # Errors
    /// On a database fault.
    pub fn poll(
        &self,
        scope: &Scope<'_>,
        timeout: Duration,
        wanted: impl Fn() -> bool,
    ) -> Result<Option<Vec<Message>>, DbError> {
        let deadline = Instant::now().checked_add(timeout);
        // Query and wait under the same lock: a write cannot land between
        // the two, so no wakeup is lost.
        let mut guard = self.lock()?;
        loop {
            let rows = query(guard.as_ref().ok_or(DbError::Closed)?, scope, POLL_LIMIT)?;
            let remaining = deadline.and_then(|d| d.checked_duration_since(Instant::now()));
            match remaining {
                Some(left) if rows.is_empty() && !left.is_zero() => {
                    if !wanted() {
                        return Ok(None);
                    }
                    guard = self.changed.wait_timeout(guard, left.min(POLL_CHECK))?.0;
                }
                _ => return Ok(Some(rows)),
            }
        }
    }

    /// One message by id.
    ///
    /// # Errors
    /// On a database fault.
    pub fn message(&self, id: i64) -> Result<Option<Message>, DbError> {
        self.read(|conn| {
            conn.query_row(&format!("SELECT {MESSAGE_COLUMNS} FROM messages WHERE id=?"), [id], message).optional()
        })
    }

    /// The last `limit` messages of a scope, oldest first (for the web
    /// view). Archived areas only show when opened by name.
    ///
    /// # Errors
    /// On a database fault.
    pub fn recent(&self, area: &str, topic: &str, limit: i64) -> Result<Vec<Message>, DbError> {
        self.read(|conn| {
            let mut sql = format!("SELECT {MESSAGE_COLUMNS} FROM messages WHERE 1");
            let mut args: Vec<Value> = Vec::new();
            if area.is_empty() {
                sql.push_str(LIVE);
            } else {
                sql.push_str(" AND area=?");
                args.push(Value::Text(area.to_owned()));
            }
            if !topic.is_empty() {
                sql.push_str(" AND topic IN (?, ?)");
                args.push(Value::Text(topic.to_owned()));
                args.push(Value::Text(PIN_TOPIC.to_owned()));
            }
            args.push(Value::Integer(limit));
            conn.prepare(&format!("SELECT {MESSAGE_COLUMNS} FROM ({sql} ORDER BY id DESC LIMIT ?) ORDER BY id"))?
                .query_map(params_from_iter(args), message)?
                .collect()
        })
    }

    /// Every area, oldest first; archived ones only if asked for.
    ///
    /// # Errors
    /// On a database fault.
    pub fn list_areas(&self, include_archived: bool) -> Result<Vec<Area>, DbError> {
        self.read(|conn| list_areas(conn, include_archived))
    }

    /// One pin; `topic` empty for the area-wide one.
    ///
    /// # Errors
    /// On a database fault.
    pub fn get_pin(&self, area: &str, topic: &str) -> Result<Option<Pin>, DbError> {
        self.read(|conn| get_pin(conn, area, topic))
    }

    /// The pins a reader of this scope should see: area-wide first, then
    /// the topic's own -- or every topic's, when no topic is given.
    ///
    /// No area means no pins, not "every area's": a pin is a standing
    /// instruction for its own workstream, and dumping every workstream's
    /// into an unscoped read would cross the area isolation boundary the
    /// rest of the board enforces.
    ///
    /// # Errors
    /// On a database fault.
    pub fn pins_for(&self, area: &str, topic: &str, include_archived: bool) -> Result<Vec<Pin>, DbError> {
        if area.is_empty() {
            return Ok(Vec::new());
        }
        self.read(|conn| {
            let mut sql = String::from("SELECT area, topic FROM pins WHERE area=?");
            let mut args = vec![area];
            if !include_archived {
                sql.push_str(LIVE);
            }
            if !topic.is_empty() {
                sql.push_str(" AND topic IN ('', ?)");
                args.push(topic);
            }
            sql.push_str(" ORDER BY area, topic");
            let keys: Vec<(String, String)> = conn
                .prepare(&sql)?
                .query_map(params_from_iter(args), |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            let mut pins = Vec::with_capacity(keys.len());
            for (area, topic) in keys {
                pins.extend(get_pin(conn, &area, &topic)?);
            }
            Ok(pins)
        })
    }

    /// The topics of an area: those with messages, and those with only a pin.
    ///
    /// # Errors
    /// On a database fault.
    pub fn list_topics(&self, area: &str) -> Result<Vec<String>, DbError> {
        self.read(|conn| {
            conn.prepare(
                "SELECT topic FROM messages WHERE area=? UNION
                 SELECT topic FROM pins WHERE area=? AND topic<>'' ORDER BY 1",
            )?
            .query_map([area, area], |r| r.get(0))?
            .collect()
        })
    }

    /// One claimed name.
    ///
    /// # Errors
    /// On a database fault.
    pub fn get_identity(&self, name: &str) -> Result<Option<Identity>, DbError> {
        self.read(|conn| get_identity(conn, name))
    }

    /// The claimed names other than `name` that are fewer than
    /// [`MIN_DISTANCE`] edits from it, ignoring case.
    ///
    /// # Errors
    /// On a database fault.
    pub fn near_claims(&self, name: &str) -> Result<Vec<String>, DbError> {
        self.read(|conn| near_claims(conn, name))
    }

    // Deletion is permanent and has no HTTP route: only board-admin calls these.

    /// (area, archived, message count) for every area, empty ones included.
    ///
    /// # Errors
    /// On a database fault.
    pub fn area_counts(&self) -> Result<Vec<(String, bool, i64)>, DbError> {
        self.read(|conn| {
            let areas = list_areas(conn, true)?;
            let counts: Vec<(String, i64)> = conn
                .prepare("SELECT area, COUNT(*) FROM messages GROUP BY area")?
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect::<rusqlite::Result<_>>()?;
            Ok(areas
                .into_iter()
                .map(|a| {
                    let count = counts.iter().find(|(name, _)| *name == a.name).map_or(0, |(_, n)| *n);
                    (a.name, a.archived_ts.is_some(), count)
                })
                .collect())
        })
    }

    /// (topic, message count) for every topic that has messages in `area`.
    ///
    /// # Errors
    /// On a database fault.
    pub fn topic_counts(&self, area: &str) -> Result<Vec<(String, i64)>, DbError> {
        self.read(|conn| {
            conn.prepare("SELECT topic, COUNT(*) FROM messages WHERE area=? GROUP BY topic ORDER BY topic")?
                .query_map([area], |r| Ok((r.get(0)?, r.get(1)?)))?
                .collect()
        })
    }

    /// The messages of one topic, oldest first.
    ///
    /// # Errors
    /// On a database fault.
    pub fn messages_in(&self, area: &str, topic: &str) -> Result<Vec<Listed>, DbError> {
        self.read(|conn| {
            conn.prepare("SELECT id, ts, sender, body FROM messages WHERE area=? AND topic=? ORDER BY id")?
                .query_map([area, topic], |r| {
                    Ok(Listed { id: r.get(0)?, ts: r.get(1)?, sender: r.get(2)?, body: r.get(3)? })
                })?
                .collect()
        })
    }

    /// Delete one message; true if there was one.
    ///
    /// # Errors
    /// On a database fault.
    pub fn delete_message(&self, id: i64) -> Result<bool, Error> {
        self.write(|conn, _| Ok(conn.execute("DELETE FROM messages WHERE id=?", [id])? > 0))
    }

    /// Delete every message of a topic; returns how many. Its pin (if any)
    /// is a separate row and stays: deletion's scope is messages, and the
    /// archive already covers areas.
    ///
    /// # Errors
    /// On a database fault.
    pub fn delete_topic(&self, area: &str, topic: &str) -> Result<usize, Error> {
        self.write(|conn, _| Ok(conn.execute("DELETE FROM messages WHERE area=? AND topic=?", [area, topic])?))
    }

    /// Every claimed name, by name.
    ///
    /// # Errors
    /// On a database fault.
    pub fn list_identities(&self) -> Result<Vec<Identity>, DbError> {
        self.read(|conn| {
            let names: Vec<String> = conn
                .prepare("SELECT name FROM identities ORDER BY name")?
                .query_map([], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
            let mut identities = Vec::with_capacity(names.len());
            for name in names {
                identities.extend(get_identity(conn, &name)?);
            }
            Ok(identities)
        })
    }

    /// Release a name claim; the next signed post as `name` claims it anew.
    /// True if there was a claim.
    ///
    /// # Errors
    /// On a database fault.
    pub fn delete_identity(&self, name: &str) -> Result<bool, Error> {
        self.write(|conn, _| Ok(conn.execute("DELETE FROM identities WHERE name=?", [name])? > 0))
    }
}

#[cfg(test)]
mod tests;
