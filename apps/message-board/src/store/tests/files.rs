//! The database file: other files refused untouched, other programs'
//! locks and rows, read-only files, and board-admin's deletes.

use std::fs;
use std::os::unix::fs::PermissionsExt as _;
use std::time::Instant;

use rusqlite::Connection;

use super::*;

/// The v1 board's tables, as its last version made them.
const V1_SCHEMA: &str = "
    PRAGMA journal_mode=WAL;
    CREATE TABLE areas(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '', created_ts TEXT NOT NULL,
                       archived_ts TEXT);
    CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, area TEXT NOT NULL,
                          topic TEXT NOT NULL, sender TEXT NOT NULL, body TEXT NOT NULL, client_key TEXT,
                          signed_by TEXT, signature TEXT);
    CREATE TABLE identities(name TEXT PRIMARY KEY, pubkey TEXT NOT NULL, fingerprint TEXT NOT NULL,
                            source TEXT NOT NULL, registered_ts TEXT NOT NULL);
    INSERT INTO areas(name, created_ts) VALUES('reversing', '2026-10-01T00:00:00Z');
    INSERT INTO messages(ts, area, topic, sender, body)
        VALUES('2026-10-01T00:00:00Z', 'reversing', 'ghidra', 'Stefan', 'the archive');
";

/// A database file and its WAL (if it has one), each as its length and
/// SHA-256: what must not change.
type Snapshot = Vec<(String, usize, String)>;

fn bytes(path: &std::path::Path) -> Result<Snapshot, Box<dyn StdError>> {
    use sha2::Digest as _;
    let mut out = Vec::new();
    for suffix in ["", "-wal"] {
        let mut file = path.as_os_str().to_owned();
        file.push(suffix);
        let file = std::path::PathBuf::from(file);
        if file.exists() {
            let data = fs::read(&file)?;
            let digest = sha2::Sha256::digest(&data).iter().map(|b| format!("{b:02x}")).collect::<Vec<_>>().concat();
            out.push((suffix.to_owned(), data.len(), digest));
        }
    }
    Ok(out)
}

#[test]
fn a_v1_database_is_refused_and_left_as_it_was() -> TestResult {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("board.db");
    let v1 = Connection::open(&path)?;
    v1.execute_batch(V1_SCHEMA)?;
    // with v1 still running, its last writes are in the WAL
    let running = bytes(&path)?;
    assert_eq!(running.len(), 2, "the test needs a WAL");
    let err = Board::open(&path).err().ok_or("opened a v1 database")?;
    assert_eq!(
        err.to_string(),
        format!("{}: this is a v1 board database; point BOARD_DB at a new file", path.display())
    );
    assert_eq!(bytes(&path)?, running);
    // and stopped, its WAL checkpointed into the file
    v1.close().map_err(|(_conn, err)| err)?;
    let stopped = bytes(&path)?;
    assert!(Board::open(&path).is_err());
    assert_eq!(bytes(&path)?, stopped);
    assert_eq!(fs::read_dir(dir.path())?.count(), 1, "no file was left beside it");
    Ok(())
}

#[test]
fn other_databases_are_refused_and_empty_ones_bootstrapped() -> TestResult {
    let dir = tempfile::tempdir()?;
    let other = dir.path().join("other.db");
    Connection::open(&other)?.execute_batch("CREATE TABLE notes(x)")?;
    let err = Board::open(&other).err().ok_or("opened another database")?;
    assert!(err.to_string().contains("this database is not a message board's"), "{err}");
    let future = dir.path().join("future.db");
    Connection::open(&future)?
        .execute_batch("CREATE TABLE meta(k, v); INSERT INTO meta VALUES('schema', 'board-v3')")?;
    let err = Board::open(&future).err().ok_or("opened another schema")?;
    assert!(err.to_string().contains("this is not a v2 board database"), "{err}");
    let empty = dir.path().join("empty.db");
    fs::write(&empty, b"")?;
    let board = Board::open(&empty)?.board;
    assert_eq!(board.head()?, FIRST_ID + 2);
    Ok(())
}

#[test]
fn a_read_only_file_serves_reads_and_refuses_writes() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    f.post(&f.owner, "rev/t", "before")?;
    f.board.close()?;
    fs::set_permissions(&f.path, fs::Permissions::from_mode(0o444))?;
    let opened = Board::open(&f.path)?;
    assert_eq!(opened.journal_mode, "wal");
    let board = opened.board;
    // the owner as configured already: opening and binding write nothing
    board.set_owner(OWNER, &verified(&f.owner))?;
    let read = board.thread("rev/t", Range::All)?;
    let write = submit!(board, &f.owner, post, Post { path: "rev/t", body: "after" });
    assert_eq!(read.messages.iter().map(|m| m.body.as_str()).collect::<Vec<_>>(), ["before"]);
    let err = refused(write)?;
    assert_eq!(err.to_string(), "attempt to write a readonly database");
    assert!(matches!(&err, Error::Db(db) if !db.is_busy()));
    assert_eq!((err.code(), err.status()), ("internal", 500));
    Ok(())
}

#[test]
fn a_lock_held_by_another_program_is_busy_after_the_timeout() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    f.post(&f.owner, "rev/t", "before")?;
    let holder = Connection::open(&f.path)?;
    holder.execute_batch("BEGIN EXCLUSIVE")?;
    let started = Instant::now();
    let write = f.post(&f.owner, "rev/t", "locked out");
    let waited = started.elapsed();
    let read = f.board.thread("rev/t", Range::All);
    holder.execute_batch("ROLLBACK")?;
    let err = refused(write)?;
    assert!(matches!(&err, Error::Db(db) if db.is_busy()), "{err}");
    assert_eq!((err.code(), err.status()), ("busy", 503));
    assert!(waited >= BUSY_TIMEOUT);
    assert_eq!(read?.messages.len(), 1, "readers are not blocked");
    f.post(&f.owner, "rev/t", "after")?; // nor is the next write left in a transaction
    Ok(())
}

#[test]
fn rows_another_program_left_odd_still_read() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    f.post(&f.owner, "rev/t", "fine")?;
    let thread = f.board.node("rev/t")?;
    let other = Connection::open(&f.path)?;
    other.execute(
        "INSERT INTO events(ts, type, node, actor, body)
         VALUES('2026-10-22T00:00:00Z', 'message', ?1, CAST(x'ff' AS TEXT), CAST(x'6869ff' AS TEXT)),
               ('2026-10-22T00:00:00Z', 'message', ?1, 7, 8)",
        [thread.id],
    )?;
    let page = f.board.thread("rev/t", Range::All)?;
    let read: Vec<(&str, &str)> = page.messages.iter().map(|m| (m.from.as_str(), m.body.as_str())).collect();
    assert_eq!(read, [(OWNER, "fine"), ("\u{fffd}", "hi\u{fffd}"), ("7", "8")]);
    let watch = Watch { paths: vec!["rev/t".to_owned()], ..Watch::default() };
    // the thread's own creation, and its three messages
    assert_eq!(f.board.poll(&watch, 0, std::time::Duration::ZERO, || true)?.map(|p| p.events.len()), Some(4));
    Ok(())
}

#[test]
fn board_admin_deletes_a_branch_unless_something_points_into_it() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    let agent = f.agent(2, "agent")?;
    f.create(&agent, "rev/emu", NodeKind::Container)?;
    f.create(&agent, "rev/emu/log", NodeKind::Thread)?;
    f.queue(&agent, "rev/emu/q", "")?;
    let message = f.post(&agent, "rev/emu/log", "hello @agent")?.id;
    let item = f.file(&agent, "rev/emu/q", "question")?.item;
    f.resolve(&agent, item, ItemState::Answered, "answer")?;
    f.create(&agent, "rev/state", NodeKind::Note)?;
    f.set_note_on(&agent, "rev/state", "summary", 0, "rev/emu/log", Some(message))?;

    let err = refused(f.board.delete_branch("rev/emu"))?;
    assert!(matches!(&err, Error::Forbidden(m) if m.contains("points into")), "{err}");
    let err = refused(f.board.delete_message(message))?;
    assert!(matches!(&err, Error::Forbidden(m) if m.contains("points to")), "{err}");
    f.set_note(&agent, "rev/state", "summary, through nothing", 1, None)?;
    // the first version still points there
    assert!(refused(f.board.delete_branch("rev/emu")).is_ok());
    f.board.delete_branch("rev/state")?;

    let head = f.board.head()?;
    let deleted = f.board.delete_branch("rev/emu")?;
    assert_eq!(deleted, Deleted { nodes: 3, events: 6 });
    assert!(matches!(refused(f.board.node("rev/emu/q"))?, Error::NotFound(_)));
    assert!(f.board.item(item)?.is_none());
    assert_eq!(f.board.head()?, head);
    assert!(matches!(refused(f.board.delete_branch(BOARD_NODE))?, Error::Forbidden(_)));
    assert!(matches!(refused(f.board.delete_branch(REGISTER_QUEUE))?, Error::Forbidden(_)));
    Ok(())
}

#[test]
fn board_admin_deletes_only_messages_one_by_one() -> TestResult {
    let f = fixture()?;
    f.rev()?;
    f.create(&f.owner, "rev/t", NodeKind::Thread)?;
    let message = f.post(&f.owner, "rev/t", "to go @Stefan")?.id;
    let created = f.board.node("rev/t")?.created;
    assert!(matches!(refused(f.board.delete_message(created))?, Error::BadRequest(_)));
    assert!(matches!(refused(f.board.delete_message(99_999))?, Error::NotFound(_)));
    f.board.delete_message(message)?;
    assert!(f.board.event(message)?.is_none());
    assert_eq!(f.board.thread("rev/t", Range::All)?.messages, Vec::<Message>::new());
    Ok(())
}
