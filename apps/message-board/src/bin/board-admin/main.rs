//! board-admin: the TUI that browses the board's tree and permanently
//! deletes from it: a node with everything in it (a branch), or one thread
//! message.
//!
//! This is deliberately not part of `board` (no shared subcommand, no
//! mention in its help) and deliberately not an HTTP endpoint. It opens the
//! SQLite file directly and issues real DELETEs: the one operation the
//! board itself refuses to offer (archive, not delete).
//!
//! The boundary this relies on is the interaction model, not secrecy or
//! permissions: a flagged command (`board-admin delete --id N`) would be
//! exactly as agent-invokable as an HTTP DELETE route, while a terminal UI
//! that needs a pty to navigate and an interactive confirmation to act is
//! not something an agent in this harness can drive. It is not a hard
//! boundary either: anything with shell access could still open the file
//! and write SQL. What this buys is "not reachable through any normal
//! path", not "impossible".
//!
//! What is never deleted is what something else points to: a message a
//! note summarizes up to (its `through`) or an answer refers to (its
//! `ref`), and a branch such a pointer from outside it leads into. The
//! store refuses those, as it does the board's own nodes (`_board`). Keys
//! are not this tool's business: names are bound and revoked by the
//! owner's signed actions in the web view.
//!
//! Every deletion is appended to a local audit log (not the board: this
//! never touches the board's own API), so there is a record of what was
//! removed and when, since unlike archive this cannot be undone.
//!
//! The file is checked before anything else is done with it: a database
//! that is not a v2 board (the v1 board's above all, kept as an archive)
//! is refused, read-only, before the service is touched.
//!
//! It stops the message-board systemd user service for the session (only
//! if it was running, and only through systemctl: a board-server it did
//! not stop is never touched), and starts it again however the session
//! ends: a quit, an error, Ctrl-C, a kill or the terminal closing.
//!
//! The blobs have a screen of their own (`b` on the top level): every blob
//! with its size, age and what may become of it (pinned, in its grace,
//! evictable), one blob with every event that references it, and its
//! deletion, by the store's rule: nothing a live message, open item or
//! current note pins is deleted, by anyone. Deleting a blob's last holder
//! (a message here) is what frees it.
//!
//! Usage: `board-admin` for the session above, or `board-admin fsck [--deep]
//! [--quarantine]` to check the blob directory against the index: read-only,
//! so a flagged command is no hazard, and it leaves the service running (see
//! `message_board::fsck` for what it finds). It prints one line per
//! finding and a summary, and exits 0 when the store is clean, 1 when it
//! found something. `--deep` also hashes every file (minutes for tens of
//! gigabytes; it runs niced). `--quarantine` then repairs what is wrong with
//! blob files and rows (bad bytes, a wrong size, a row without a file): the
//! file is moved to `quarantine/` in the blob directory, never deleted, and
//! the row dropped. That writes the index, so it refuses while the service
//! (or anything else) is serving the board, as deleting does; each blob
//! dealt with is a line in the audit log, and the exit status is that of a
//! check run again afterwards.
//!
//! `board-admin usage` prints what each key uploaded as blobs (count, bytes,
//! of which pinned): read-only, no quota.
//!
//! Config via env, as board-server: `BOARD_DB`, `BOARD_BLOBS` and
//! `BOARD_BLOB_CAP` and `BOARD_BLOB_GRACE` (as the server reads them),
//! `BOARD_URL` (checked, not written to); `BOARD_ADMIN_SERVICE` (the unit,
//! default message-board; empty for none) and `BOARD_ADMIN_LOG` (the audit
//! log).
//!
//! # Reading the code
//!
//! As a ratatui program, in the order it runs:
//!
//! - `main.rs` (here): finding the database, the service, and the order
//!   things are done and undone in;
//! - `tui.rs`: the terminal (raw mode, the alternate screen, giving it
//!   back) and the event loop;
//! - `app.rs`: the update, what each key does, and the deletions;
//! - `screens.rs`: the model, each screen as data read from the board,
//!   with `picker.rs` (a list's selection) and `pager.rs` (a text's
//!   scrolling) as the state screens share;
//! - `ui.rs`: the view, drawing a screen.

mod app;
mod audit;
#[cfg(test)]
mod fixture;
mod pager;
mod picker;
mod screens;
mod service;
mod tui;
mod ui;

use std::ffi::OsString;
use std::fs;
use std::io::{self, Write as _};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::thread;
use std::time::{Duration, SystemTimeError};

use message_board::blob;
use message_board::fsck;
use message_board::store::{self, Board, DbError};

use crate::app::{App, BlobSettings};
use crate::audit::Audit;
use crate::service::{Service, note};
use crate::tui::{Ending, Signals};

/// Why board-admin stopped short, as it says so.
#[derive(Debug, thiserror::Error)]
pub enum Failure {
    /// A refusal, said as it is.
    #[error("{0}")]
    Exit(String),
    #[error("{what}: {cause}")]
    Io { what: String, cause: io::Error },
    #[error("the terminal: {cause}{}", .restoring.as_ref().map(|r| format!("; restoring it failed too: {r}")).unwrap_or_default())]
    Terminal { cause: io::Error, restoring: Option<io::Error> },
    #[error("database: {0}")]
    Db(#[from] DbError),
    #[error("database: {0}")]
    Store(#[from] store::Error),
    #[error("the system clock is before 1970: {0}")]
    Clock(#[from] SystemTimeError),
    #[error("{0}; and then: {1}")]
    Both(Box<Failure>, Box<Failure>),
}

fn main() -> ExitCode {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let outcome = match args.iter().map(|a| a.to_str()).collect::<Option<Vec<_>>>().as_deref() {
        Some([]) => run(),
        Some(["usage"]) => usage(),
        Some(["fsck", flags @ ..]) if fsck_flags(flags).is_some() => {
            let (deep, quarantine) = fsck_flags(flags).unwrap_or_default();
            check(deep, quarantine)
        }
        _ => {
            let usage = "usage: board-admin            browse the board and delete from it\n       board-admin fsck [--deep] [--quarantine]\n                                  check the blob directory against the index (read-only);\n                                  --quarantine moves bad files aside and drops their rows (service stopped)\n       board-admin usage          what each key uploaded as blobs";
            return match note(usage) {
                Ok(()) => ExitCode::from(2),
                Err(_unwritable) => ExitCode::from(74),
            };
        }
    };
    match outcome {
        Ok(code) => code,
        Err(err) => {
            // If stderr is gone too, the exit status is the only report left.
            match note(&format!("board-admin: {err}")) {
                Ok(()) => ExitCode::FAILURE,
                Err(_unwritable) => ExitCode::from(74),
            }
        }
    }
}

fn text_var(name: &str) -> Result<Option<String>, Failure> {
    match std::env::var_os(name).map(OsString::into_string) {
        None => Ok(None),
        Some(Ok(value)) => Ok(Some(value)),
        Some(Err(raw)) => Err(Failure::Exit(format!("board-admin: {name} is not valid UTF-8: {}", raw.display()))),
    }
}

fn nonempty_var(name: &str) -> Option<OsString> {
    std::env::var_os(name).filter(|value| !value.is_empty())
}

/// The file `board-server` opens under systemd (`BOARD_DB` in the unit is
/// %S/message-board/board-v2.db). Unlike board-server's own default, a
/// bare ./board-v2.db, which is right for a development run and wrong
/// here: run from the wrong directory, this would look for a database
/// that is not there.
fn db_path(board_db: Option<OsString>, state_home: Option<OsString>, home: Option<PathBuf>) -> Option<PathBuf> {
    if let Some(explicit) = board_db {
        return Some(PathBuf::from(explicit));
    }
    let state = state_home.map(PathBuf::from).or_else(|| home.map(|h| h.join(".local/state")))?;
    Some(state.join("message-board").join("board-v2.db"))
}

/// The audit log: `BOARD_ADMIN_LOG`, else beside the service's state.
fn audit_path(home: Option<PathBuf>) -> Option<PathBuf> {
    nonempty_var("BOARD_ADMIN_LOG")
        .map(PathBuf::from)
        .or_else(|| home.map(|h| h.join(".local/state/message-board/admin-deletions.log")))
}

/// Refuse a database that is missing or empty: opening it would make a board
/// of it, and an admin tool must not.
fn check_database(db: &Path) -> Result<(), Failure> {
    let size = match fs::metadata(db) {
        Ok(metadata) => metadata.len(),
        Err(cause) if cause.kind() == io::ErrorKind::NotFound => {
            // Opening it would create it; an admin tool must not.
            return Err(Failure::Exit(format!("no database at {} (set BOARD_DB to point elsewhere).", db.display())));
        }
        Err(cause) => return Err(Failure::Io { what: format!("cannot look at {}", db.display()), cause }),
    };
    if size == 0 {
        // Board::open would make a fresh board of it, as of a missing file.
        return Err(Failure::Exit(format!(
            "{} is empty, not a board (set BOARD_DB to point elsewhere).",
            db.display()
        )));
    }
    Ok(())
}

/// The number in environment variable `name`, or `default`.
fn number_var(name: &str, default: u64) -> Result<u64, Failure> {
    match text_var(name)?.filter(|value| !value.is_empty()) {
        None => Ok(default),
        Some(value) => value
            .parse()
            .map_err(|err| Failure::Exit(format!("board-admin: {name} is not a number: {value:?} ({err})"))),
    }
}

/// The flags of `fsck` as (deep, quarantine), if they are only those, each at most once.
fn fsck_flags(flags: &[&str]) -> Option<(bool, bool)> {
    let (mut deep, mut quarantine) = (false, false);
    for flag in flags {
        let seen = match *flag {
            "--deep" => &mut deep,
            "--quarantine" => &mut quarantine,
            _ => return None,
        };
        if std::mem::replace(seen, true) {
            return None;
        }
    }
    Some((deep, quarantine))
}

/// The board's address and its systemd unit, as the session reads them.
fn endpoint() -> Result<(String, Service), Failure> {
    let url = match text_var("BOARD_URL")? {
        Some(url) => url.trim_end_matches('/').to_owned(),
        None => format!("http://127.0.0.1:{}", text_var("BOARD_PORT")?.unwrap_or_else(|| "8777".to_owned())),
    };
    let unit = text_var("BOARD_ADMIN_SERVICE")?.unwrap_or_else(|| "message-board".to_owned());
    Ok((url, Service { unit: (!unit.is_empty()).then_some(unit) }))
}

/// `board-admin usage`: see the module docs.
fn usage() -> Result<ExitCode, Failure> {
    let home = std::env::home_dir();
    let no_home = || Failure::Exit("board-admin: cannot tell where the home directory is".to_owned());
    let db = db_path(nonempty_var("BOARD_DB"), nonempty_var("XDG_STATE_HOME"), home).ok_or_else(no_home)?;
    check_database(&db)?;
    let board = Board::open(&db)?.board;
    let usage = board.blob_usage();
    let closed = board.close().map_err(Failure::from);
    let usage = usage?;
    closed?;
    let mut out = io::stdout().lock();
    let wrote = |cause| Failure::Io { what: "cannot write the usage".to_owned(), cause };
    let (mut blobs, mut bytes) = (0_u64, 0_u64);
    for u in &usage {
        writeln!(
            out,
            "{:<16} {}  {:>5} blobs  {:>9}  {:>9} pinned  last upload {}",
            u.name,
            u.fingerprint,
            u.blobs,
            blob::human(u.bytes),
            blob::human(u.pinned_bytes),
            u.last_upload
        )
        .map_err(wrote)?;
        blobs = blobs.saturating_add(u.blobs);
        bytes = bytes.saturating_add(u.bytes);
    }
    writeln!(out, "usage: {} keys, {blobs} blobs, {}", usage.len(), blob::human(bytes)).map_err(wrote)?;
    Ok(ExitCode::SUCCESS)
}

/// `board-admin fsck`: see the module docs.
fn check(deep: bool, quarantine: bool) -> Result<ExitCode, Failure> {
    let home = std::env::home_dir();
    let no_home = || Failure::Exit("board-admin: cannot tell where the home directory is".to_owned());
    let db = db_path(nonempty_var("BOARD_DB"), nonempty_var("XDG_STATE_HOME"), home.clone()).ok_or_else(no_home)?;
    check_database(&db)?;
    if quarantine {
        let (url, service) = endpoint()?;
        if service.is_active()? || service::is_live(&url)? {
            return Err(Failure::Exit(format!(
                "board-admin: --quarantine writes the index, which the board serving {url} keeps: stop it first \
                 (systemctl --user stop {})",
                service.unit.as_deref().unwrap_or("message-board")
            )));
        }
    }
    let root = fsck::blob_root(nonempty_var("BOARD_BLOBS"), &db);
    let board = Board::open(&db)?.board;
    if deep {
        // Hashing tens of gigabytes must not slow the board down.
        if let Err(err) = rustix::process::nice(10) {
            note(&format!("board-admin: cannot lower its priority: {err}"))?;
        }
    }
    let options =
        fsck::Options { deep, cap: number_var("BOARD_BLOB_CAP", blob::CAP_DEFAULT)?, tmp_idle: fsck::TMP_IDLE };
    let checked = fsck::check(&board, &root, &options);
    let (checked, quarantined) = match checked {
        Ok(first) if quarantine => {
            // what is left once the bad files are aside, checked again (shallow: they were hashed already)
            let again = fsck::quarantine(&board, &root, &first)
                .and_then(|done| Ok((done, fsck::check(&board, &root, &fsck::Options { deep: false, ..options })?)));
            match again {
                Ok((done, after)) => (Ok(after), done),
                Err(err) => (Err(err), Vec::new()),
            }
        }
        other => (other, Vec::new()),
    };
    let closed = board.close().map_err(Failure::from);
    let report = checked.map_err(|err| Failure::Exit(format!("board-admin: fsck: {err}")))?;
    closed?;
    if !quarantined.is_empty() {
        let audit = Audit { path: audit_path(home).ok_or_else(no_home)? };
        for q in &quarantined {
            audit.record("quarantine_blob", &q.to_string())?;
        }
    }
    let mut out = io::stdout().lock();
    let wrote = |cause| Failure::Io { what: "cannot write the report".to_owned(), cause };
    for q in &quarantined {
        writeln!(out, "{q}").map_err(wrote)?;
    }
    for finding in &report.findings {
        writeln!(out, "{finding}").map_err(wrote)?;
    }
    let looked = if deep { format!("{} files hashed", report.hashed) } else { "sizes only; --deep hashes".to_owned() };
    let verdict =
        if report.findings.is_empty() { "clean".to_owned() } else { format!("{} findings", report.findings.len()) };
    writeln!(
        out,
        "fsck: {} blobs in the index ({} bytes), {} files ({} bytes), {looked}: {verdict}",
        report.blobs, report.indexed_bytes, report.files, report.file_bytes
    )
    .map_err(wrote)?;
    Ok(if report.findings.is_empty() { ExitCode::SUCCESS } else { ExitCode::FAILURE })
}

fn run() -> Result<ExitCode, Failure> {
    // First, so that from here on a kill ends the session like a quit.
    let signals = Signals::install()?;
    let (url, service) = endpoint()?;
    // HOME, else the passwd entry's home.
    let home = std::env::home_dir();
    let no_home = || Failure::Exit("board-admin: cannot tell where the home directory is".to_owned());
    let db = db_path(nonempty_var("BOARD_DB"), nonempty_var("XDG_STATE_HOME"), home.clone()).ok_or_else(no_home)?;
    let audit = Audit { path: audit_path(home).ok_or_else(no_home)? };

    check_database(&db)?;
    // Board::open looks at the file read-only first and refuses one that
    // is not a v2 board with nothing written to it. That is done before
    // the service is touched: a wrong BOARD_DB bounces nothing. Opening a
    // board the server has open too is safe; SQLite has two programs share
    // a file. It is only written to once the server is gone.
    let board = Board::open(&db)?.board;

    let blobs = BlobSettings {
        root: fsck::blob_root(nonempty_var("BOARD_BLOBS"), &db),
        grace: number_var("BOARD_BLOB_GRACE", blob::GRACE_DEFAULT)?,
    };
    let mut stopped = false;
    let session = administer(&service, &url, &board, audit, blobs, &signals, &mut stopped);
    // Closed before the service comes back, so that the server is the only
    // program with the file open again; closing also checkpoints the WAL.
    let closed = board.close().map_err(Failure::from);
    // Everything after the stop is inside `administer`, so any way out of
    // it -- refusal, error, Ctrl-C, a kill, a quit -- brings the board back.
    if stopped {
        note(&format!("board-admin: restarting {}...", service.unit.as_deref().unwrap_or_default()))?;
        if let Err(err) = service.start() {
            let unit = service.unit.as_deref().unwrap_or_default();
            note(&format!(
                "board-admin: WARNING -- failed to restart {unit} ({err}). Run `systemctl --user start {unit}` yourself."
            ))?;
        }
    }
    match with_cleanup(session, closed)? {
        Ending::Quit => Ok(ExitCode::SUCCESS),
        Ending::Interrupted => Ok(ExitCode::from(130)),
        Ending::Signalled(signal) => Ok(ExitCode::from(u8::try_from(128 + signal).unwrap_or(1))),
    }
}

/// The outcome of a step and of the cleanup that ran after it whatever the
/// step did: the step's error first, and a failed cleanup never dropped.
fn with_cleanup<T>(step: Result<T, Failure>, cleanup: Result<(), Failure>) -> Result<T, Failure> {
    match (step, cleanup) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(err), Ok(())) | (Ok(_), Err(err)) => Err(err),
        (Err(err), Err(cleaning)) => Err(Failure::Both(Box::new(err), Box::new(cleaning))),
    }
}

/// Take the service down if it runs, make sure nothing else serves the
/// database, then run the TUI on it.
fn administer(
    service: &Service,
    url: &str,
    board: &Board,
    audit: Audit,
    blobs: BlobSettings,
    signals: &Signals,
    stopped: &mut bool,
) -> Result<Ending, Failure> {
    let unit = service.unit.as_deref().unwrap_or_default();
    if service.is_active()? {
        note(&format!("board-admin: stopping {unit}..."))?;
        // Before the stop, not after: systemctl can fail once systemd has
        // begun it (a Ctrl-C, its wait timing out), and systemd finishes the
        // stop anyway. Starting a unit that still runs does nothing.
        *stopped = true;
        service.stop()?;
        // systemctl stop returns once the unit reports stopped, but give the
        // port a moment to actually free up before trusting it.
        let mut freed = false;
        for _ in 0..20 {
            if let Some(signal) = signals.received() {
                return Ok(Ending::Signalled(signal));
            }
            if !service::is_live(url)? {
                freed = true;
                break;
            }
            thread::sleep(Duration::from_millis(250));
        }
        if !freed {
            return Err(Failure::Exit(format!(
                "stopped {unit} but {url} is still answering -- something else is serving this port too. Not touching the DB."
            )));
        }
    } else if service::is_live(url)? {
        // systemctl thinks it is down but the port answers anyway: an
        // unmanaged board-server is running (someone's own shell, a stale
        // process). Only the managed instance is ours to stop.
        return Err(Failure::Exit(format!(
            "{url} is answering but systemctl says {unit} is not active -- an unmanaged board-server is running \
             somewhere. Stop it yourself, this tool only manages the systemd service."
        )));
    }
    if let Some(signal) = signals.received() {
        return Ok(Ending::Signalled(signal));
    }
    let mut app = App::new(board, audit, blobs)?;
    tui::session(&mut app, signals)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_database_is_where_the_service_keeps_it() {
        let os = |s: &str| Some(OsString::from(s));
        assert_eq!(db_path(os("/x/y.db"), os("/st"), None), Some(PathBuf::from("/x/y.db")));
        assert_eq!(db_path(None, os("/st"), None), Some(PathBuf::from("/st/message-board/board-v2.db")));
        // without XDG_STATE_HOME, under the home directory -- HOME's, or the
        // passwd entry's when HOME is unset (std::env::home_dir looks it up)
        assert_eq!(
            db_path(None, None, Some(PathBuf::from("/home/u"))),
            Some(PathBuf::from("/home/u/.local/state/message-board/board-v2.db"))
        );
        assert_eq!(db_path(None, None, None), None);
    }
}
