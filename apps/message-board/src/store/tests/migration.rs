//! Opening a database an older binary made: the schema is migrated in
//! place, with the data as it was.

use std::fs;
use std::path::Path;

use rusqlite::Connection;

use super::*;

/// The schema as the first pushed baseline made it (`user_version` 0, no
/// `events.thread`), copied from that version's `store/schema.rs`.
const BASELINE: &str = "
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

fn pragma(path: &Path, name: &str) -> Result<i64, Box<dyn StdError>> {
    Ok(Connection::open(path)?.query_row(&format!("PRAGMA {name}"), [], |r| r.get(0))?)
}

fn has_event_column(path: &Path, column: &str) -> Result<bool, Box<dyn StdError>> {
    let conn = Connection::open(path)?;
    let mut columns = conn.prepare("SELECT name FROM pragma_table_info('events')")?;
    let names = columns.query_map([], |r| r.get::<_, String>(0))?.collect::<Result<Vec<_>, _>>()?;
    Ok(names.iter().any(|n| n == column))
}

fn has_table(path: &Path, table: &str) -> Result<bool, Box<dyn StdError>> {
    let conn = Connection::open(path)?;
    Ok(conn.query_row("SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", [table], |r| r.get::<_, i64>(0))? > 0)
}

fn has_thread_column(path: &Path) -> Result<bool, Box<dyn StdError>> {
    has_event_column(path, "thread")
}

/// A board with some of everything (made by this code), and its data copied
/// into a database with the baseline's schema: the file an older binary left.
fn old_database() -> Result<(Fixture, std::path::PathBuf), Box<dyn StdError>> {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    f.queue(&agent, "rev/q", "")?;
    let first = f.post(&agent, "rev/log", "hello @Stefan")?.id;
    f.set_note_on(&agent, "rev/state", "summary", 0, "rev/log", Some(first))?;
    let item = f.file(&agent, "rev/q", "a question")?.item;
    f.resolve(&agent, item, ItemState::Answered, "an answer")?;
    f.board.close()?;

    let old = f.path.with_file_name("old.db");
    let conn = Connection::open(&old)?;
    conn.execute_batch(BASELINE)?;
    conn.execute("ATTACH ? AS new", [f.path.to_string_lossy()])?;
    conn.execute_batch(
        "INSERT INTO meta SELECT * FROM new.meta;
         INSERT INTO nodes SELECT * FROM new.nodes;
         INSERT INTO events(id, ts, type, node, item, actor, actor_fp, body, version, ref, client_key,
                            request_hash, signature)
             SELECT id, ts, type, node, item, actor, actor_fp, body, version, ref, client_key,
                    request_hash, signature FROM new.events;
         INSERT INTO items SELECT * FROM new.items;
         INSERT INTO keys SELECT * FROM new.keys;
         INSERT INTO mentions SELECT * FROM new.mentions;
         DELETE FROM sqlite_sequence;
         INSERT INTO sqlite_sequence SELECT * FROM new.sqlite_sequence;
         DETACH new;",
    )?;
    drop(conn);
    assert_eq!((pragma(&old, "user_version")?, has_thread_column(&old)?), (0, false));
    Ok((f, old))
}

#[test]
fn an_older_database_is_migrated_on_open_with_its_data_intact() -> TestResult {
    let (_f, old) = old_database()?;
    let migrated = Board::open(&old)?.board;
    assert!(has_thread_column(&old)? && has_event_column(&old, "archived")? && pragma(&old, "user_version")? == 4);
    assert!(has_table(&old, "blobs")? && has_table(&old, "blob_refs")?);

    // the data is as it was
    let messages = migrated.thread("rev/log", Range::All)?.messages;
    assert_eq!(messages.len(), 1);
    let note = migrated.note("rev/state", None)?.ok_or("no note")?;
    assert_eq!((note.body.as_str(), note.thread.as_deref()), ("summary", None));
    assert_eq!(migrated.queue("rev/q", StateFilter::All, 0)?.items.len(), 1);
    assert_eq!(migrated.keys()?.len(), 2);
    // nothing is archived in what was there, and the owner can archive it
    assert!(messages.iter().all(|m| !m.archived));
    let first_id = messages.first().map(|m| m.id).ok_or("no message")?;
    let archived = submit!(migrated, &key(1)?, archive, Archive { event: first_id, archived: true, reason: "" })?;
    assert!(archived.hidden);
    assert_eq!(migrated.thread("rev/log", Range::All)?.messages.len(), 0);

    // and the new thing works, signed by a key the old data holds
    let first = messages.first().map(|m| m.id).ok_or("no message")?;
    let set = SetNote { path: "rev/state", body: "v2", base_version: 1, thread: "rev/log", through: Some(first) };
    submit!(migrated, &key(2)?, set_note, set)?;
    let note = migrated.note("rev/state", None)?.ok_or("no note")?;
    assert_eq!(note.thread.as_deref(), Some("rev/log"));
    let history = migrated.note_history("rev/state")?;
    assert_eq!(history.iter().map(|v| v.thread.is_some()).collect::<Vec<_>>(), [false, true]);
    migrated.close()?;
    Ok(())
}

#[test]
fn opening_again_changes_nothing() -> TestResult {
    let (_f, old) = old_database()?;
    Board::open(&old)?.board.close()?;
    let schema = pragma(&old, "schema_version")?;
    let bytes = fs::metadata(&old)?.len();
    Board::open(&old)?.board.close()?;
    assert_eq!((pragma(&old, "schema_version")?, pragma(&old, "user_version")?), (schema, 4));
    assert_eq!(fs::metadata(&old)?.len(), bytes);
    Ok(())
}

#[test]
fn a_fresh_database_starts_at_the_latest_version() -> TestResult {
    let f = fixture()?;
    f.board.close()?;
    assert_eq!((pragma(&f.path, "user_version")?, has_thread_column(&f.path)?), (4, true));
    assert!(has_event_column(&f.path, "archived")?);
    assert!(has_table(&f.path, "blobs")? && has_table(&f.path, "blob_refs")?);
    Ok(())
}

/// Version 3 (the archive column) gains the blob tables, with its data intact.
#[test]
fn a_version_3_database_gains_the_blob_tables() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/log", NodeKind::Thread)?;
    f.post(&agent, "rev/log", "kept")?;
    f.board.close()?;
    Connection::open(&f.path)?
        .execute_batch("DROP TABLE blob_refs; DROP TABLE blobs; PRAGMA user_version = 3")?;
    assert!(!has_table(&f.path, "blobs")?);
    let migrated = Board::open(&f.path)?.board;
    assert!(has_table(&f.path, "blobs")? && has_table(&f.path, "blob_refs")?);
    assert_eq!(pragma(&f.path, "user_version")?, 4);
    assert_eq!(migrated.thread("rev/log", Range::All)?.messages.len(), 1);
    assert_eq!(migrated.blob_stats()?.count, 0);
    migrated.close()?;
    Ok(())
}

#[test]
fn a_database_from_a_newer_board_is_refused_untouched() -> TestResult {
    let f = fixture()?;
    f.board.close()?;
    Connection::open(&f.path)?.execute_batch("PRAGMA user_version = 99")?;
    let schema = pragma(&f.path, "schema_version")?;
    let err = Board::open(&f.path).err().ok_or("opened a newer database")?;
    assert!(err.to_string().contains("schema version 99") && err.to_string().contains("upgrade"), "{err}");
    assert_eq!((pragma(&f.path, "user_version")?, pragma(&f.path, "schema_version")?), (99, schema));
    Ok(())
}
