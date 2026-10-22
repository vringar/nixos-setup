//! board-server: the message board's HTTP server and web view.
//!
//! ```text
//! board-server        serve on $BOARD_ADDR:$BOARD_PORT (default 127.0.0.1:8777)
//! ```
//!
//! Agents talk to it through the `board` client; humans use the web view.
//! Posts and pin edits may carry an SSH signature; the first signed post as
//! a name claims it, and from then on writes as that name must be signed by
//! the same key. A signature covers the board's head (`GET /healthz`) as
//! its nonce and is accepted only while that is still the head; otherwise
//! the write gets a 409 stale-nonce and the client signs again. That is the
//! only authentication: reading is open to anyone who can connect, so
//! binding anything but 127.0.0.1 makes the network the access boundary.
//! Only do it behind a firewall rule scoped to a trusted interface.
//!
//! Config via env: `BOARD_ADDR` (127.0.0.1), `BOARD_PORT` (8777),
//! `BOARD_DB` (SQLite path, default ./board.db), `BOARD_STATIC_DIR`
//! (htmx.min.js and board-sign.js; set by the packaged wrapper),
//! `BOARD_OWNER_NAME` + `BOARD_OWNER_PUBKEY` (a name and the public key
//! file it is bound to at startup, overriding any claim).
//!
//! One thread per connection, as Python's `ThreadingHTTPServer` had: a long
//! poll blocks its thread on the store's condition variable for up to five
//! minutes, which needs no async runtime, and a local board has few
//! connections.

mod html;
mod http;
mod log;
mod routes;

use std::ffi::OsString;
use std::fs;
use std::io::{self, BufReader, ErrorKind, Read as _};
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream, ToSocketAddrs as _};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::{Arc, OnceLock};
use std::thread;
use std::time::Duration;

use message_board::sign;
use message_board::store::{self, Board, DbError, MIN_DISTANCE};
use message_board::{py, timefmt};
use signal_hook::consts::{SIGINT, SIGTERM};

use crate::http::Response;
use crate::routes::App;

#[derive(Debug, thiserror::Error)]
enum StartError {
    #[error("{0}")]
    Config(String),
    #[error("cannot open the database {path}: {source}")]
    Open { path: String, source: DbError },
    #[error("database: {0}")]
    Db(#[from] DbError),
    #[error("preseeding the owner: {0}")]
    Owner(store::Error),
    #[error("cannot listen on {addr}: {source}")]
    Listen { addr: String, source: io::Error },
    #[error("cannot set up the shutdown on SIGTERM: {0}")]
    Signals(io::Error),
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            log::line(&format!("board: {err}"));
            ExitCode::FAILURE
        }
    }
}

/// A text setting from the environment, `None` if unset.
fn setting(name: &str) -> Result<Option<String>, StartError> {
    match std::env::var_os(name).map(OsString::into_string) {
        None => Ok(None),
        Some(Ok(value)) => Ok(Some(value)),
        Some(Err(raw)) => Err(StartError::Config(format!("{name} is not valid UTF-8: {}", raw.display()))),
    }
}

fn run() -> Result<(), StartError> {
    // First of all: as PID 1, a SIGTERM that comes before there is a handler
    // is dropped, and opening the database can take a while.
    let shutdown = Shutdown::on_signals().map_err(StartError::Signals)?;
    let addr = setting("BOARD_ADDR")?.unwrap_or_else(|| "127.0.0.1".to_owned());
    let port_text = setting("BOARD_PORT")?.unwrap_or_else(|| "8777".to_owned());
    let port = py::int(&port_text)
        .and_then(|n| u16::try_from(n).ok())
        .ok_or_else(|| StartError::Config(format!("BOARD_PORT {} is not a port number", py::repr(&port_text))))?;
    let db = PathBuf::from(std::env::var_os("BOARD_DB").unwrap_or_else(|| OsString::from("board.db")));
    let static_dir = match std::env::var_os("BOARD_STATIC_DIR") {
        Some(dir) => PathBuf::from(dir),
        None => installed_static_dir()?,
    };

    let opened = Board::open(&db).map_err(|source| StartError::Open { path: db.display().to_string(), source })?;
    if opened.journal_mode != "wal" {
        log::line(&format!(
            "board: the database is in {} journal mode, not WAL; readers will wait for writers",
            opened.journal_mode
        ));
    }
    preseed_owner(&opened.board)?;
    let listener = listen(&addr, port)?;
    let head = opened.board.head_id()?;
    let app = Arc::new(App { board: opened.board, static_dir });
    shutdown.serving(&app);
    log::info(&format!("board on http://{addr}:{port}  db={}  head={head}", db.display()));
    serve(&listener, &app);
    Ok(())
}

/// Where the package installs htmx.min.js and board-sign.js, relative to
/// this program: `share/message-board/static` beside its `bin`. The
/// packaged wrapper sets `BOARD_STATIC_DIR` to it anyway; a development run
/// sets it to the source tree's `static`.
fn installed_static_dir() -> Result<PathBuf, StartError> {
    let exe = std::env::current_exe()
        .map_err(|err| StartError::Config(format!("cannot tell where board-server is installed: {err}")))?;
    let prefix = exe.parent().and_then(Path::parent).unwrap_or(Path::new("/"));
    Ok(prefix.join("share/message-board/static"))
}

fn listen(addr: &str, port: u16) -> Result<TcpListener, StartError> {
    let failed = |source| StartError::Listen { addr: format!("{addr}:{port}"), source };
    // An empty address is every interface, as Python's bind(("", port)) took it.
    let host = if addr.is_empty() { "0.0.0.0" } else { addr };
    let target = match host.parse::<IpAddr>() {
        Ok(ip) => SocketAddr::new(ip, port),
        Err(_not_a_literal) => (host, port)
            .to_socket_addrs()
            .map_err(failed)?
            .next()
            .ok_or_else(|| failed(io::Error::new(ErrorKind::NotFound, "no address for this host")))?,
    };
    TcpListener::bind(target).map_err(failed)
}

/// Bind `BOARD_OWNER_NAME` to the key in `BOARD_OWNER_PUBKEY`, if both are
/// set. A missing or unusable key file, or a name outside the name rule or
/// reserved for the board itself, is logged and skipped: the board runs
/// fine without an owner, and the key may simply not exist yet. Other
/// claims that look like the owner name stay, with a warning: releasing one
/// is a human's call (board-admin).
fn preseed_owner(board: &Board) -> Result<(), StartError> {
    let name = setting("BOARD_OWNER_NAME")?.unwrap_or_default();
    let path = PathBuf::from(std::env::var_os("BOARD_OWNER_PUBKEY").unwrap_or_default());
    if name.is_empty() || path.as_os_str().is_empty() {
        return Ok(());
    }
    let shown = path.display();
    let key = match fs::read_to_string(&path) {
        Err(err) if err.kind() == ErrorKind::NotFound => {
            log::info(&format!("board: no owner key at {shown}; {name} is not preseeded"));
            return Ok(());
        }
        Err(err) => Err(err.to_string()),
        Ok(text) => sign::load_public_key(&text).map_err(|err| err.to_string()),
    };
    let key = match key {
        Ok(key) => key,
        Err(why) => {
            log::info(&format!("board: owner key {shown} unusable ({why}); {name} is not preseeded"));
            return Ok(());
        }
    };
    match board.set_owner(&name, &key.public_line, &key.fingerprint) {
        Ok(()) => {}
        Err(err @ (store::Error::BadName(_) | store::Error::Forbidden(_))) => {
            log::info(&format!("board: BOARD_OWNER_NAME: {err}; no owner is preseeded"));
            return Ok(());
        }
        Err(err) => return Err(StartError::Owner(err)),
    }
    log::info(&format!("board: owner {name} is {}", key.fingerprint));
    for near in board.near_claims(&name)? {
        log::info(&format!(
            "board: warning: the claimed name {} is fewer than {MIN_DISTANCE} edits from the owner's {}; \
             if it is a look-alike, release it with board-admin",
            py::repr(&near),
            py::repr(&name)
        ));
    }
    Ok(())
}

/// Exit on SIGTERM (and Ctrl-C), closing the database first once the
/// server has one open.
///
/// The user unit runs the server as PID 1 of its own PID namespace, and the
/// kernel drops any signal PID 1 has no handler for: without this, a stop
/// would wait for systemd to give up and SIGKILL it. The signals are
/// written to a pipe rather than a socket pair, since the unit allows the
/// server no `AF_UNIX` sockets.
struct Shutdown {
    app: Arc<OnceLock<Arc<App>>>,
}

impl Shutdown {
    fn on_signals() -> io::Result<Self> {
        let (mut wake, signalled) = io::pipe()?;
        signal_hook::low_level::pipe::register(SIGINT, signalled.try_clone()?)?;
        signal_hook::low_level::pipe::register(SIGTERM, signalled)?;
        let app: Arc<OnceLock<Arc<App>>> = Arc::new(OnceLock::new());
        let serving = Arc::clone(&app);
        thread::Builder::new().name("shutdown".to_owned()).spawn(move || {
            let mut byte = [0_u8; 1];
            let status = match wake.read(&mut byte) {
                // Still starting: nothing is open that needs closing. (SQLite
                // keeps the file consistent through an exit at any point.)
                Ok(_) => match serving.get().map(|app| app.board.close()) {
                    None | Some(Ok(())) => 0,
                    Some(Err(err)) => {
                        log::line(&format!("board: closing the database on shutdown: {err}"));
                        1
                    }
                },
                Err(err) => {
                    log::line(&format!("board: waiting for SIGTERM failed: {err}"));
                    1
                }
            };
            std::process::exit(status);
        })?;
        Ok(Self { app })
    }

    /// From now on, a shutdown closes this app's database first.
    fn serving(&self, app: &Arc<App>) {
        // Only ever set here, once; a second call could only repeat it.
        if self.app.set(Arc::clone(app)).is_err() {
            log::line("board: the shutdown was handed a second app; it closes the first");
        }
    }
}

fn serve(listener: &TcpListener, app: &Arc<App>) {
    loop {
        match listener.accept() {
            Ok((stream, peer)) => {
                let app = Arc::clone(app);
                let spawned =
                    thread::Builder::new().name(format!("board {peer}")).spawn(move || connection(&app, stream, peer));
                if let Err(err) = spawned {
                    log::line(&format!("board: no thread for the connection from {peer}: {err}"));
                }
            }
            Err(err) => {
                log::line(&format!("board: accepting a connection failed: {err}"));
                // a full file table frees up as connections close; don't spin meanwhile
                thread::sleep(Duration::from_millis(100));
            }
        }
    }
}

/// The value of a response's Date header now.
fn date() -> Option<String> {
    match timefmt::unix_now() {
        Ok(now) => Some(timefmt::http_date(now)),
        Err(err) => {
            log::line(&format!("board: no Date header: {err}"));
            None
        }
    }
}

/// Serve one connection's requests, in order, until either side ends it.
fn connection(app: &App, stream: TcpStream, peer: SocketAddr) {
    let mut reader = match stream.try_clone() {
        Ok(read_half) => BufReader::new(read_half),
        Err(err) => {
            log::line(&format!("board: cannot read from {peer}: {err}"));
            return;
        }
    };
    let mut writer = stream;
    loop {
        let request = match http::read_request(&mut reader) {
            Ok(Some(request)) => request,
            Ok(None) => return,
            Err(err) => {
                let Some(status) = err.status() else {
                    log::line(&format!("board: connection from {peer} lost: {err}"));
                    return;
                };
                log::line(&format!("board: bad request from {peer}: {err}"));
                let answer = py::dumps(&serde_json::json!({ "error": err.to_string() }))
                    .map(|body| Response::json(status, body));
                match answer {
                    Ok(answer) => {
                        if let Err(gone) = answer.write_to(&mut writer, date().as_deref()) {
                            log::line(&format!("board: client {peer} went away before the answer: {gone}"));
                        }
                    }
                    Err(json) => log::line(&format!("board: cannot encode the answer to {peer}: {json}")),
                }
                return;
            }
        };
        let response = routes::handle(app, &request, peer);
        if let Err(err) = response.write_to(&mut writer, date().as_deref()) {
            log::line(&format!("board: client {peer} went away mid-response: {err}"));
            return;
        }
        if request.close {
            return;
        }
    }
}
