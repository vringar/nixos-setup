//! board-admin: the TUI that permanently deletes board threads and
//! releases name claims.
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
//! The same goes for releasing a name claim (the identity a sender name
//! was bound to by its first signed post): the one way to free a name whose
//! key was lost or that the wrong key claimed. The next signed post as that
//! name claims it again.
//!
//! Every deletion and release is appended to a local audit log (not the
//! board: this never touches the board's own API), so there is a record of
//! what was removed and when, since unlike archive this cannot be undone.
//!
//! It stops the message-board systemd user service for the session (only
//! if it was running, and only through systemctl: a board-server it did
//! not stop is never touched), and starts it again however the session
//! ends: a quit, an error, Ctrl-C, a kill or the terminal closing.
//!
//! Usage: `board-admin`. Config via env, as board-server: `BOARD_DB`,
//! `BOARD_URL` (checked, not written to); `BOARD_ADMIN_SERVICE` (the unit,
//! default message-board; empty for none) and `BOARD_ADMIN_LOG` (the audit
//! log).

mod app;
mod audit;
mod service;
mod tui;
mod ui;

use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::thread;
use std::time::{Duration, SystemTimeError};

use message_board::store::{self, Board, DbError};

use crate::app::App;
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
    match run() {
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
/// %S/message-board/board.db). Unlike board-server's own default, a bare
/// ./board.db, which is right for a development run and wrong here: run
/// from the wrong directory, this would open (and create) an empty
/// database and show a board with nothing on it.
fn db_path(board_db: Option<OsString>, state_home: Option<OsString>, home: Option<PathBuf>) -> Option<PathBuf> {
    if let Some(explicit) = board_db {
        return Some(PathBuf::from(explicit));
    }
    let state = state_home.map(PathBuf::from).or_else(|| home.map(|h| h.join(".local/state")))?;
    Some(state.join("message-board").join("board.db"))
}

/// The audit log: `BOARD_ADMIN_LOG`, else beside the service's state.
fn audit_path(home: Option<PathBuf>) -> Option<PathBuf> {
    nonempty_var("BOARD_ADMIN_LOG")
        .map(PathBuf::from)
        .or_else(|| home.map(|h| h.join(".local/state/message-board/admin-deletions.log")))
}

fn run() -> Result<ExitCode, Failure> {
    // First, so that from here on a kill ends the session like a quit.
    let signals = Signals::install()?;
    let url = match text_var("BOARD_URL")? {
        Some(url) => url.trim_end_matches('/').to_owned(),
        None => format!("http://127.0.0.1:{}", text_var("BOARD_PORT")?.unwrap_or_else(|| "8777".to_owned())),
    };
    let unit = text_var("BOARD_ADMIN_SERVICE")?.unwrap_or_else(|| "message-board".to_owned());
    let service = Service { unit: (!unit.is_empty()).then_some(unit) };
    // HOME, else the passwd entry's home.
    let home = std::env::home_dir();
    let no_home = || Failure::Exit("board-admin: cannot tell where the home directory is".to_owned());
    let db = db_path(nonempty_var("BOARD_DB"), nonempty_var("XDG_STATE_HOME"), home.clone()).ok_or_else(no_home)?;
    let audit = Audit { path: audit_path(home).ok_or_else(no_home)? };

    let exists =
        db.try_exists().map_err(|cause| Failure::Io { what: format!("cannot look for {}", db.display()), cause })?;
    if !exists {
        // Opening it would create it; an admin tool must not.
        return Err(Failure::Exit(format!("no database at {} (set BOARD_DB to point elsewhere).", db.display())));
    }

    let mut stopped = false;
    let session = administer(&service, &url, &db, audit, &signals, &mut stopped);
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
    match session? {
        Ending::Quit => Ok(ExitCode::SUCCESS),
        Ending::Interrupted => Ok(ExitCode::from(130)),
        Ending::Signalled(signal) => Ok(ExitCode::from(u8::try_from(128 + signal).unwrap_or(1))),
    }
}

/// Take the service down if it runs, make sure nothing else holds the
/// database, then run the TUI on it.
fn administer(
    service: &Service,
    url: &str,
    db: &Path,
    audit: Audit,
    signals: &Signals,
    stopped: &mut bool,
) -> Result<Ending, Failure> {
    let unit = service.unit.as_deref().unwrap_or_default();
    if service.is_active()? {
        note(&format!("board-admin: stopping {unit}..."))?;
        service.stop()?;
        *stopped = true;
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
    let opened = Board::open(db)?;
    let mut app = App::new(opened.board, audit)?;
    tui::session(&mut app, signals)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_database_is_where_the_service_keeps_it() {
        let os = |s: &str| Some(OsString::from(s));
        assert_eq!(db_path(os("/x/y.db"), os("/st"), None), Some(PathBuf::from("/x/y.db")));
        assert_eq!(db_path(None, os("/st"), None), Some(PathBuf::from("/st/message-board/board.db")));
        // without XDG_STATE_HOME, under the home directory -- HOME's, or the
        // passwd entry's when HOME is unset (std::env::home_dir looks it up)
        assert_eq!(
            db_path(None, None, Some(PathBuf::from("/home/u"))),
            Some(PathBuf::from("/home/u/.local/state/message-board/board.db"))
        );
        assert_eq!(db_path(None, None, None), None);
    }
}
