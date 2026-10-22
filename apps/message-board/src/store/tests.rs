//! The store against real SQLite files in a temporary directory.

use std::error::Error as StdError;
use std::os::unix::fs::PermissionsExt as _;
use std::sync::Arc;
use std::time::{Duration, Instant};
use std::{fs, thread};

use super::*;

type TestResult = Result<(), Box<dyn StdError>>;

struct Fixture {
    _dir: tempfile::TempDir,
    path: std::path::PathBuf,
    board: Board,
}

fn fixture() -> Result<Fixture, Box<dyn StdError>> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("board.db");
    let board = Board::open(&path)?.board;
    Ok(Fixture { _dir: dir, path, board })
}

fn post(board: &Board, area: &str, topic: &str, sender: &str, body: &str) -> Result<i64, Error> {
    board.post(&NewPost { area, topic, sender, body, key: None }, None)
}

fn signer(board: &Board, fingerprint: &str) -> Result<Signer, DbError> {
    Ok(Signer {
        pubkey: format!("ssh-ed25519 KEY-{fingerprint}"),
        fingerprint: fingerprint.to_owned(),
        signature: format!("sig-{fingerprint}"),
        nonce: board.head_id()?.to_string(),
    })
}

fn signed(board: &Board, area: &str, sender: &str, body: &str, fingerprint: &str) -> Result<i64, Error> {
    let signer = signer(board, fingerprint)?;
    board.post(&NewPost { area, topic: "t", sender, body, key: None }, Some(&signer))
}

fn pin(board: &Board, area: &str, topic: &str, editor: &str, body: &str) -> Result<i64, Error> {
    board.set_pin(&PinSave { area, topic, editor, body, base_version: None }, None)
}

fn bodies(messages: &[Message]) -> Vec<&str> {
    messages.iter().map(|m| m.body.as_str()).collect()
}

fn scope<'a>(area: &'a str, topic: &'a str) -> Scope<'a> {
    Scope { since: 0, area, topic, include_archived: true }
}

#[test]
fn ids_are_monotonic_and_new_areas_are_announced() -> TestResult {
    let f = fixture()?;
    assert_eq!(post(&f.board, "reversing", "ghidra", "g", "one")?, 2);
    assert_eq!(post(&f.board, "reversing", "ghidra", "g", "two")?, 3);
    let notes = f.board.query(&scope(SYSTEM_AREA, "areas"), 10)?;
    assert_eq!(bodies(&notes), ["new area: **reversing**"]);
    assert_eq!((notes.first().map(|m| m.sender.as_str())), Some(SERVER_NAME));
    Ok(())
}

#[test]
fn a_post_key_makes_resending_harmless() -> TestResult {
    let f = fixture()?;
    let once = NewPost { area: "x", topic: "t", sender: "a", body: "once", key: Some("k1") };
    let first = f.board.post(&once, None)?;
    assert_eq!(f.board.post(&once, None)?, first);
    let other = NewPost { body: "other", ..once };
    assert!(matches!(f.board.post(&other, None), Err(Error::Conflict(m)) if m.contains("already used")));
    Ok(())
}

#[test]
fn first_signed_post_claims_and_locks_the_name() -> TestResult {
    let f = fixture()?;
    post(&f.board, "x", "t", "agent", "free")?;
    signed(&f.board, "x", "agent", "claims it", "SHA256:a")?;
    let ident = f.board.get_identity("agent")?.ok_or("no claim")?;
    assert_eq!((ident.fingerprint.as_str(), ident.source.as_str()), ("SHA256:a", "tofu"));
    let unsigned = post(&f.board, "x", "t", "agent", "impostor").err().ok_or("accepted")?;
    assert_eq!(unsigned.to_string(), "name \"agent\" is claimed; sign the post");
    let other = signed(&f.board, "x", "agent", "impostor", "SHA256:b").err().ok_or("accepted")?;
    assert_eq!(other.to_string(), "name \"agent\" is claimed by key SHA256:a, not SHA256:b");
    let head = f.board.head_id()?;
    assert!(post(&f.board, "new-area", "t", "agent", "refused").is_err());
    assert_eq!(f.board.head_id()?, head); // a refusal leaves no trace, no new area either
    assert!(f.board.list_areas(true)?.iter().all(|a| a.name != "new-area"));
    Ok(())
}

#[test]
fn look_alikes_of_claims_and_of_the_board_are_refused() -> TestResult {
    let f = fixture()?;
    signed(&f.board, "x", "Stefan", "claim", "SHA256:s")?;
    for near in ["stefan", "STEFAN", "Stefan_", "Stefn", "Stephan"] {
        let err = post(&f.board, "x", "t", near, "hi").err().ok_or("accepted")?;
        assert!(err.to_string().starts_with(&format!("name \"{near}\" is too close to the claimed name \"Stefan\"")));
    }
    signed(&f.board, "x", "Stefan-db", "three edits away", "SHA256:d")?;
    let err = post(&f.board, "x", "t", "board", "hi").err().ok_or("accepted")?;
    assert_eq!(err.to_string(), "name \"board\" is the board itself; write as your own name");
    let err = pin(&f.board, "x", "", "b0ard", "hi").err().ok_or("accepted")?;
    assert!(err.to_string().contains("too close to the reserved name \"board\""));
    Ok(())
}

#[test]
fn areas_and_topics_must_be_addressable() -> TestResult {
    let f = fixture()?;
    for (area, topic) in [("", "t"), ("x", ""), ("a\nb", "t"), ("x", "t\r")] {
        assert!(matches!(post(&f.board, area, topic, "a", "lost"), Err(Error::BadPlace(_))), "{area:?} {topic:?}");
    }
    assert!(matches!(post(&f.board, "x", PIN_TOPIC, "a", "a fake doorbell"), Err(Error::Forbidden(_))));
    assert!(matches!(pin(&f.board, "x", PIN_TOPIC, "a", "a pin"), Err(Error::Forbidden(_))));
    assert!(matches!(pin(&f.board, "", "", "a", "a pin"), Err(Error::BadPlace(_))));
    assert!(matches!(f.board.create_area("", "nameless"), Err(Error::BadPlace(_))));
    assert_eq!(f.board.head_id()?, 0); // nothing was written
    assert_eq!(pin(&f.board, "x", "", "a", "the area pin")?, 1); // an area pin has no topic
    Ok(())
}

#[test]
fn names_follow_the_rule() {
    for good in ["Stefan", "board", "me", "psp_db", "007", &"x".repeat(64)] {
        assert!(check_name(good).is_ok(), "{good}");
    }
    for bad in ["", "Stefan ", "a.b", "Stef\u{430}n", "st\u{e9}fan", &"x".repeat(65)] {
        let err = check_name(bad).err().map(|e| e.to_string()).unwrap_or_default();
        assert_eq!(err, format!("sender name {} is not allowed: names are {NAME_RULE}", quoted(bad)));
    }
}

#[test]
fn a_look_alike_letter_shows_in_the_refusal() {
    let err = check_name("Stef\u{430}n").err().map(|e| e.to_string()).unwrap_or_default();
    assert!(err.starts_with("sender name \"Stef\\u{430}n\" is not allowed"), "{err}");
}

#[test]
fn distance_counts_edits() {
    assert_eq!(distance("", "abc"), 3);
    assert_eq!(distance("kitten", "sitting"), 3);
    assert_eq!(distance("stefan", "stefn"), 1);
    // the closest pairs of the names in use on the board are exactly far enough apart
    for (a, b) in [("psp-db", "psp-etl"), ("psp-etl", "psptool"), ("psptool", "psptool-rs")] {
        assert_eq!(distance(a, b), MIN_DISTANCE, "{a} {b}");
    }
}

#[test]
fn a_signature_is_good_for_one_write_even_after_its_post_is_deleted() -> TestResult {
    let f = fixture()?;
    post(&f.board, "x", "t", "a", "one")?;
    let s = signer(&f.board, "SHA256:a")?;
    let new = NewPost { area: "x", topic: "t", sender: "agent", body: "signed", key: None };
    let id = f.board.post(&new, Some(&s))?;
    assert!(f.board.delete_message(id)?);
    assert_eq!(f.board.head_id()?, id); // the highest id issued, not the highest left
    assert!(matches!(f.board.post(&new, Some(&s)), Err(Error::StaleNonce { head }) if head == id));
    Ok(())
}

#[test]
fn pins_version_announce_and_refuse_stale_edits() -> TestResult {
    let f = fixture()?;
    assert_eq!(pin(&f.board, "rev", "", "s", "v1")?, 1);
    assert_eq!(pin(&f.board, "rev", "", "s", "v2")?, 2);
    let stale = PinSave { area: "rev", topic: "", editor: "s", body: "late", base_version: Some(1) };
    assert!(matches!(f.board.set_pin(&stale, None), Err(Error::StalePin { version: 2 })));
    assert_eq!(pin(&f.board, "rev", "ghidra", "s", "g")?, 1);
    assert_eq!(pin(&f.board, "rev", "", "s", " \u{a0}\t\n")?, 2); // blank removes
    assert!(f.board.get_pin("rev", "")?.is_none());
    let log = f.board.query(&scope("rev", ""), 10)?;
    assert_eq!(
        bodies(&log),
        [
            "pin updated: rev v1 — see `board pin rev`",
            "pin updated: rev v2 — see `board pin rev`",
            "pin updated: rev/ghidra v1 — see `board pin rev ghidra`",
            "pin removed: rev",
        ]
    );
    assert_eq!(f.board.list_topics("rev")?, [PIN_TOPIC, "ghidra"]);
    Ok(())
}

#[test]
fn a_signed_pin_answers_to_its_key_or_the_owners() -> TestResult {
    let f = fixture()?;
    f.board.set_owner("Stefan", "ssh-ed25519 OWNER", "SHA256:owner")?;
    let save = |editor, body, fp: Option<&str>| -> Result<i64, Error> {
        let s = fp.map(|fp| signer(&f.board, fp)).transpose()?;
        f.board.set_pin(&PinSave { area: "rev", topic: "", editor, body, base_version: None }, s.as_ref())
    };
    assert_eq!(save("author", "v1", Some("SHA256:a"))?, 1);
    let err = save("intruder", "takeover", Some("SHA256:b")).err().ok_or("accepted")?;
    assert_eq!(
        err.to_string(),
        "pin rev v1 is signed by key SHA256:a (as author); only that key or the owner's can change it"
    );
    let err = save("anon", "takeover", None).err().ok_or("accepted")?;
    assert!(err.to_string().ends_with("can change it, and only signed"));
    assert_eq!(save("other-name", "v2", Some("SHA256:a"))?, 2);
    assert_eq!(save("Stefan", "v3", Some("SHA256:owner"))?, 3);
    Ok(())
}

#[test]
fn archiving_hides_and_activity_revives() -> TestResult {
    let f = fixture()?;
    post(&f.board, "rev", "t", "g", "x")?;
    assert!(f.board.set_archived("rev", true)?);
    assert!(!f.board.set_archived("rev", true)?);
    assert!(f.board.list_areas(false)?.iter().all(|a| a.name != "rev"));
    post(&f.board, "rev", "t", "late", "still here")?;
    assert!(f.board.list_areas(false)?.iter().any(|a| a.name == "rev"));
    let notes = bodies(&f.board.query(&scope(SYSTEM_AREA, ""), 10)?).into_iter().map(str::to_owned).collect::<Vec<_>>();
    assert_eq!(notes, ["new area: **rev**", "area archived: **rev**", "area unarchived: **rev**"]);
    assert!(matches!(f.board.set_archived(SYSTEM_AREA, true), Err(Error::SystemArea)));
    let missing = f.board.set_archived("nowhere", true).err().ok_or("archived nothing")?;
    assert_eq!(missing.to_string(), "no such area \"nowhere\"");
    Ok(())
}

#[test]
fn a_poll_wakes_for_a_post_made_while_it_waits() -> TestResult {
    let f = fixture()?;
    let board = Arc::new(f.board);
    let writer = Arc::clone(&board);
    let started = Instant::now();
    let poster = thread::spawn(move || {
        thread::sleep(Duration::from_millis(300));
        post(&writer, "x", "t", "a", "woke you")
    });
    let rows =
        board.poll(&Scope { since: 0, area: "x", topic: "", include_archived: false }, Duration::from_secs(60))?;
    poster.join().map_err(|_panicked| "poster thread panicked")??;
    assert_eq!(bodies(&rows), ["woke you"]);
    assert!(started.elapsed() < Duration::from_secs(60));
    let empty = board.poll(&Scope { since: 99, area: "x", topic: "", include_archived: false }, Duration::ZERO)?;
    assert_eq!(empty, Vec::<Message>::new());
    Ok(())
}

#[test]
fn recent_is_the_newest_messages_oldest_first() -> TestResult {
    let f = fixture()?;
    for i in 0..5 {
        post(&f.board, "x", "t", "a", &format!("m{i}"))?;
    }
    assert_eq!(bodies(&f.board.recent("x", "", 3)?), ["m2", "m3", "m4"]);
    Ok(())
}

fn old_board(path: &std::path::Path, script: &str) -> rusqlite::Result<()> {
    let conn = Connection::open(path)?;
    conn.execute_batch(script)?;
    conn.close().map_err(|(_conn, err)| err)
}

#[test]
fn a_board_from_before_signing_and_archiving_is_migrated_in_place() -> TestResult {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("old.db");
    old_board(
        &path,
        "CREATE TABLE areas(name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
                            created_ts TEXT NOT NULL);
         CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                               area TEXT NOT NULL, topic TEXT NOT NULL,
                               sender TEXT NOT NULL, body TEXT NOT NULL);
         CREATE TABLE pins(area TEXT NOT NULL, topic TEXT NOT NULL, body TEXT NOT NULL,
                           version INTEGER NOT NULL, editor TEXT NOT NULL, ts TEXT NOT NULL,
                           PRIMARY KEY(area, topic));
         INSERT INTO areas VALUES('old', '', '2026-09-01T00:00:00Z');
         INSERT INTO messages(ts, area, topic, sender, body)
             VALUES('2026-09-01T00:00:00Z', 'old', 't', 'Stefan ', 'from before');
         INSERT INTO pins VALUES('old', '', 'old pin', 1, 'Stef an', '2026-09-01T00:00:00Z');",
    )?;
    let board = Board::open(&path)?.board;
    let [old] = board.query(&scope("old", ""), 10)?.try_into().map_err(|_rows: Vec<Message>| "not one row")?;
    assert_eq!((old.sender.as_str(), old.verified, old.signed_by), ("Stefan ", false, None));
    assert!(!board.get_pin("old", "")?.ok_or("lost the pin")?.verified);
    let keyed = NewPost { area: "old", topic: "t", sender: "a", body: "x", key: Some("k") };
    board.post(&keyed, None)?;
    board.close()?;
    let board = Board::open(&path)?.board; // a second open must not add the columns again
    let other = NewPost { body: "y", ..keyed };
    assert!(matches!(board.post(&other, None), Err(Error::Conflict(_)))); // the key index exists
    assert!(board.set_archived("old", true)?);
    Ok(())
}

#[test]
fn a_read_only_file_serves_reads_and_refuses_writes() -> TestResult {
    let f = fixture()?;
    post(&f.board, "x", "t", "a", "before")?;
    f.board.close()?;
    fs::set_permissions(&f.path, fs::Permissions::from_mode(0o444))?;
    let board = Board::open(&f.path)?.board; // up to date: opening writes nothing
    let read = board.query(&scope("x", ""), 10);
    let write = post(&board, "x", "t", "a", "after");
    assert_eq!(bodies(&read?), ["before"]);
    let err = write.err().ok_or("wrote to a read-only file")?;
    assert_eq!(err.to_string(), "attempt to write a readonly database");
    assert!(matches!(&err, Error::Db(db) if !db.is_busy()));
    Ok(())
}

#[test]
fn a_lock_held_by_another_program_is_busy_after_the_timeout() -> TestResult {
    let f = fixture()?;
    post(&f.board, "x", "t", "a", "before")?;
    let holder = Connection::open(&f.path)?;
    holder.execute_batch("BEGIN EXCLUSIVE")?;
    let started = Instant::now();
    let write = post(&f.board, "x", "t", "a", "locked out");
    let waited = started.elapsed();
    let read = f.board.query(&scope("x", ""), 10);
    holder.execute_batch("ROLLBACK")?;
    let err = write.err().ok_or("wrote through a lock")?;
    assert!(matches!(&err, Error::Db(db) if db.is_busy()), "{err}");
    assert!(waited >= BUSY_TIMEOUT);
    assert_eq!(bodies(&read?), ["before"]); // readers are not blocked
    post(&f.board, "x", "t", "a", "after")?; // nor is the next write left in a transaction
    Ok(())
}
