//! The schema, the check that a file is a v2 board before anything is
//! written to it, and the bootstrap of a fresh one.
//!
//! The v2 board starts from an empty file: the v1 database is not migrated
//! but kept, untouched, as a read-only archive. Opening it by mistake (an
//! old `BOARD_DB`) must not write v2 tables into it, so a file is probed
//! read-only first.

use std::os::unix::ffi::OsStrExt as _;
use std::path::Path;

use rusqlite::{Connection, OpenFlags, OptionalExtension as _};

use super::actions::Stamp;
use super::nodes::{self, NewNode, NodeKind};
use super::{BOARD_NODE, DbError, FIRST_ID, REGISTER_QUEUE, SERVER_NAME, TRUST_NOTE};

/// The `meta` row that marks a v2 board.
pub(super) const SCHEMA_NAME: &str = "board-v2";

/// The tables, as the spec has them, with a few more constraints: a check
/// on every enum column, keys that carry their item and revocation as
/// their state says, and a partial index to find what points at an event.
const SCHEMA: &str = "
    CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);

    CREATE TABLE nodes(
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,
        parent      INTEGER REFERENCES nodes(id),      -- NULL: top level
        kind        TEXT NOT NULL CHECK(kind IN ('container','note','thread','queue')),
        description TEXT NOT NULL DEFAULT '',          -- one line, shown in listings
        creator     TEXT NOT NULL,                     -- a name
        owner       TEXT,                              -- queues: the name that resolves
        generated   INTEGER NOT NULL DEFAULT 0,        -- 1: only the board writes it
        created     INTEGER NOT NULL,                  -- event id
        archived    INTEGER,                           -- event id; NULL = live (phase 2)
        CHECK ((kind = 'queue') = (owner IS NOT NULL)));
    CREATE INDEX nodes_parent ON nodes(parent);

    CREATE TABLE events(
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,
        type         TEXT NOT NULL CHECK(type IN ('node_created','node_archived','node_unarchived','message',
                         'note_version','item_filed','item_answered','item_declined','item_cancelled',
                         'item_reassigned')),
        node         INTEGER NOT NULL REFERENCES nodes(id),
        item         INTEGER REFERENCES items(id),     -- item_* events
        actor        TEXT,                             -- name; NULL only for a registration
        actor_fp     TEXT,                             -- NULL only for the board's own
        body         TEXT NOT NULL DEFAULT '',
        version      INTEGER,                          -- note_version
        ref          INTEGER REFERENCES events(id),    -- through / answer ref
        client_key   TEXT UNIQUE,
        request_hash TEXT,                             -- sha256(signer, request without nonce)
        signature    TEXT);
    CREATE INDEX events_node ON events(node, id);
    CREATE INDEX events_item ON events(item) WHERE item IS NOT NULL;
    CREATE INDEX events_ref ON events(ref) WHERE ref IS NOT NULL;

    CREATE TABLE items(
        id        INTEGER PRIMARY KEY,                 -- = its item_filed event id
        queue     INTEGER NOT NULL REFERENCES nodes(id),
        asker     TEXT,                                -- name; NULL for a registration
        asker_fp  TEXT NOT NULL,
        state     TEXT NOT NULL CHECK(state IN ('open','answered','declined','cancelled','reassigned')),
        resolved  INTEGER REFERENCES events(id),
        from_item INTEGER REFERENCES items(id));       -- set on the item a reassign created
    CREATE INDEX items_open  ON items(queue) WHERE state = 'open';
    CREATE INDEX items_asker ON items(asker) WHERE state = 'open';

    CREATE TABLE keys(
        fingerprint TEXT PRIMARY KEY,                  -- SHA256:...
        pubkey      TEXT NOT NULL,
        name        TEXT NOT NULL,                     -- requested, then bound
        state       TEXT NOT NULL CHECK(state IN ('owner','pending','trusted','declined','revoked')),
        repo        TEXT NOT NULL DEFAULT '',
        note        TEXT NOT NULL DEFAULT '',
        item        INTEGER REFERENCES items(id),      -- latest registration
        approved    INTEGER REFERENCES events(id),
        revoked     INTEGER REFERENCES events(id),
        CHECK (state NOT IN ('pending','declined','trusted') OR item IS NOT NULL),
        CHECK ((state = 'revoked') = (revoked IS NOT NULL)));
    CREATE UNIQUE INDEX keys_bound_name ON keys(name) WHERE state IN ('owner','trusted');

    CREATE TABLE mentions(name TEXT NOT NULL, event INTEGER NOT NULL REFERENCES events(id),
                          PRIMARY KEY(name, event)) WITHOUT ROWID;

    -- phase 2
    CREATE TABLE note_editors(note INTEGER NOT NULL REFERENCES nodes(id), name TEXT NOT NULL,
                              PRIMARY KEY(note, name)) WITHOUT ROWID;
";

/// Whether `path` is a new board (no file, or an empty database), checked
/// on a read-only connection.
///
/// # Errors
/// [`DbError::NotABoard`] for a file that holds any other database, the v1
/// board's above all; its bytes are as they were.
pub(super) fn probe(path: &Path) -> Result<bool, DbError> {
    if !path.try_exists().map_err(|err| DbError::NotABoard(format!("cannot look at {}: {err}", path.display())))? {
        return Ok(true);
    }
    let conn = read_only(path)?;
    let verdict = verdict(&conn, path);
    conn.close().map_err(|(_unclosed, err)| DbError::Sqlite(err))?;
    verdict
}

/// A read-only connection that leaves no file beside `path`.
///
/// A plain read-only connection to a WAL database creates its `-wal` and
/// `-shm` files if they are missing, so probing a checkpointed v1 board
/// would leave an empty `board.db-wal` beside the archive. Without a WAL
/// the file holds the whole database, and SQLite can read it as immutable:
/// no lock, no files. With one, its program may be running, so it is read
/// as any live WAL database; its files exist already.
fn read_only(path: &Path) -> rusqlite::Result<Connection> {
    let mut wal = path.as_os_str().to_owned();
    wal.push("-wal");
    let flags = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    if Path::new(&wal).exists() {
        return Connection::open_with_flags(path, flags);
    }
    // A URI, so every byte of the path that is not plain is %-encoded.
    let mut uri = String::from("file:");
    for byte in path.as_os_str().as_bytes() {
        if byte.is_ascii_alphanumeric() || b"/-._~".contains(byte) {
            uri.push(char::from(*byte));
        } else {
            uri.push('%');
            // a nibble is always a hex digit
            uri.extend([byte >> 4, byte & 0xf].map(|nibble| char::from_digit(u32::from(nibble), 16).unwrap_or('0')));
        }
    }
    uri.push_str("?immutable=1");
    Connection::open_with_flags(uri, flags | OpenFlags::SQLITE_OPEN_URI)
}

fn verdict(conn: &Connection, path: &Path) -> Result<bool, DbError> {
    let has = |table: &str| -> rusqlite::Result<bool> {
        conn.query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", [table], |r| r.get(0))
    };
    let tables: i64 = conn.query_row("SELECT count(*) FROM sqlite_master WHERE type='table'", [], |r| r.get(0))?;
    if tables == 0 {
        return Ok(true);
    }
    let shown = path.display();
    if has("meta")? {
        let schema: Option<String> =
            conn.query_row("SELECT v FROM meta WHERE k='schema'", [], |r| r.get(0)).optional()?;
        return match schema.as_deref() {
            Some(SCHEMA_NAME) => Ok(false),
            other => Err(DbError::NotABoard(format!(
                "{shown}: this is not a v2 board database (its schema is {other:?}); point BOARD_DB at a new file"
            ))),
        };
    }
    if has("messages")? {
        return Err(DbError::NotABoard(format!("{shown}: this is a v1 board database; point BOARD_DB at a new file")));
    }
    Err(DbError::NotABoard(format!("{shown}: this database is not a message board's; point BOARD_DB at a new file")))
}

/// Make the schema and the board's own nodes in a fresh database. The
/// counter is set so that the first event is [`FIRST_ID`].
pub(super) fn bootstrap(conn: &Connection, now: &str) -> rusqlite::Result<()> {
    conn.execute_batch(SCHEMA)?;
    conn.execute("INSERT INTO meta(k, v) VALUES('schema', ?)", [SCHEMA_NAME])?;
    // sqlite_sequence is an ordinary table that AUTOINCREMENT reads the
    // next id from; creating `events` made it.
    conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('events', ?)", [FIRST_ID - 1])?;
    let stamp = Stamp::board(now);
    let own = |path, parent, kind, description, owner| NewNode {
        path,
        parent,
        kind,
        description,
        creator: SERVER_NAME,
        owner,
        generated: true,
    };
    let (root, _) = nodes::insert(conn, &stamp, &own(BOARD_NODE, None, NodeKind::Container, "the board's own", None))?;
    // Owned by the board until an owner is configured (set_owner): nobody
    // signs as `board`, so until then nothing can be approved.
    let register = own(
        REGISTER_QUEUE,
        Some(root),
        NodeKind::Queue,
        "registrations: the owner approves or declines each",
        Some(SERVER_NAME),
    );
    nodes::insert(conn, &stamp, &register)?;
    let trust = own(TRUST_NOTE, Some(root), NodeKind::Note, "the keys the board trusts", None);
    nodes::insert(conn, &stamp, &trust)?;
    Ok(())
}
