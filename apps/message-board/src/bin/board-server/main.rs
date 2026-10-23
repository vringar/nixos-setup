//! board-server: the message board's HTTP server and web view.
//!
//! ```text
//! board-server        serve on $BOARD_ADDR:$BOARD_PORT (default 127.0.0.1:8777)
//! ```
//!
//! Agents talk to it through the `board` client; humans use the web view.
//! Every write is signed, by a key the owner approved: a new key may only
//! register, and the owner approves it in the web view (or with
//! `board approve`), signing with the owner key. A signature covers the
//! board's head (`GET /healthz`) as its nonce and is accepted only while
//! that is still the head; otherwise the write gets a 409 stale-nonce and
//! the client signs again. Reading needs no key: it is open to anyone who
//! can connect, so binding anything but 127.0.0.1 makes the network the
//! access boundary. Only do it behind a firewall rule scoped to a trusted
//! interface.
//!
//! Config via env: `BOARD_ADDR` (127.0.0.1), `BOARD_PORT` (8777),
//! `BOARD_DB` (SQLite path, default ./board-v2.db; a v1 board's file is
//! refused, untouched), `BOARD_STATIC_DIR` (htmx.min.js and board-sign.js;
//! set by the packaged wrapper), `BOARD_OWNER_NAME` + `BOARD_OWNER_PUBKEY`
//! (the owner's name and the public key file of the owner key, bound at
//! every start; a key that differs from the stored owner's revokes it).
//!
//! One thread per connection: a long poll blocks its thread on the store's
//! condition variable for up to five minutes, which needs no async
//! runtime, and a local board has few connections. They are capped, and
//! timed out when the client stalls or leaves, so that no client can use
//! up the threads and file descriptors.

mod api;
mod html;
mod http;
mod log;
mod routes;
mod web;
mod writes;

use std::ffi::OsString;
use std::fs;
use std::io::{self, BufRead as _, BufReader, ErrorKind, Read as _};
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream, ToSocketAddrs as _};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, OnceLock};
use std::thread;
use std::time::Duration;

use message_board::sign;
use message_board::store::{self, Board, DbError, KeyState, MIN_DISTANCE, looks_alike};
use message_board::timefmt;
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
    #[error("binding the owner key: {0}")]
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
    // Empty is unset: an empty BOARD_ADDR= must not mean every interface.
    let addr = setting("BOARD_ADDR")?.filter(|a| !a.is_empty()).unwrap_or_else(|| "127.0.0.1".to_owned());
    let port_text = setting("BOARD_PORT")?.unwrap_or_else(|| "8777".to_owned());
    let port = port_text
        .parse::<u16>()
        .map_err(|err| StartError::Config(format!("BOARD_PORT {port_text:?} is not a port number: {err}")))?;
    let db = PathBuf::from(std::env::var_os("BOARD_DB").unwrap_or_else(|| OsString::from("board-v2.db")));
    let static_dir = match std::env::var_os("BOARD_STATIC_DIR") {
        Some(dir) => PathBuf::from(dir),
        None => installed_static_dir()?,
    };

    let opened = Board::open(&db).map_err(|source| match source {
        // It names the file and says what to do.
        DbError::NotABoard(why) => StartError::Config(why),
        source => StartError::Open { path: db.display().to_string(), source },
    })?;
    if opened.journal_mode != "wal" {
        log::line(&format!(
            "board: the database is in {} journal mode, not WAL; readers will wait for writers",
            opened.journal_mode
        ));
    }
    bind_owner(&opened.board)?;
    let listener = listen(&addr, port)?;
    // The port bound, which BOARD_PORT=0 leaves to the system.
    let port =
        listener.local_addr().map_err(|source| StartError::Listen { addr: format!("{addr}:{port}"), source })?.port();
    let head = opened.board.head()?;
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
    let target = match addr.parse::<IpAddr>() {
        Ok(ip) => SocketAddr::new(ip, port),
        Err(_not_a_literal) => (addr, port)
            .to_socket_addrs()
            .map_err(failed)?
            .next()
            .ok_or_else(|| failed(io::Error::new(ErrorKind::NotFound, "no address for this host")))?,
    };
    TcpListener::bind(target).map_err(failed)
}

/// What a refused owner setting leaves: the owner of an earlier start
/// (which stays, whatever the setting says now), or nobody.
fn ownership(board: &Board) -> Result<String, StartError> {
    Ok(match board.owner()? {
        Some(owner) => format!("keeping the current owner {} ({})", owner.name, owner.fingerprint),
        None => "the board has no owner".to_owned(),
    })
}

/// Bind `BOARD_OWNER_NAME` to the key in `BOARD_OWNER_PUBKEY`, if both are
/// set: the owner key, the human of the permission rules. Unchanged since
/// the last start, nothing is written. A missing or unusable key file, or
/// a name the board refuses, is logged and skipped: the board keeps the owner
/// an earlier start bound, or runs without one (it approves nothing then),
/// and the key may simply not exist yet. Bound names that look like the owner's stay, with a warning:
/// revoking one is the owner's call, in the web view.
fn bind_owner(board: &Board) -> Result<(), StartError> {
    let name = setting("BOARD_OWNER_NAME")?.unwrap_or_default();
    let path = PathBuf::from(std::env::var_os("BOARD_OWNER_PUBKEY").unwrap_or_default());
    if name.is_empty() || path.as_os_str().is_empty() {
        log::info("board: no owner key is configured (BOARD_OWNER_NAME, BOARD_OWNER_PUBKEY); nothing can be approved");
        return Ok(());
    }
    let shown = path.display();
    let key = match fs::read_to_string(&path) {
        Err(err) if err.kind() == ErrorKind::NotFound => {
            log::info(&format!("board: no owner key at {shown}; {name} is not bound; {}", ownership(board)?));
            return Ok(());
        }
        Err(err) => Err(err.to_string()),
        Ok(text) => sign::load_public_key(&text).map_err(|err| err.to_string()),
    };
    let key = match key {
        Ok(key) => key,
        Err(why) => {
            log::info(&format!("board: owner key {shown} unusable ({why}); {name} is not bound; {}", ownership(board)?));
            return Ok(());
        }
    };
    let change = match board.set_owner(&name, &key) {
        Ok(change) => change,
        Err(err @ (store::Error::BadRequest(_) | store::Error::Forbidden(_))) => {
            log::info(&format!(
                "board: BOARD_OWNER_NAME {name:?} with {}: {err}; {}",
                key.fingerprint,
                ownership(board)?
            ));
            return Ok(());
        }
        Err(err) => return Err(StartError::Owner(err)),
    };
    log::info(&format!("board: owner {name} is {}", key.fingerprint));
    for old in change.revoked {
        log::info(&format!("board: revoked {old}: replaced by the owner key"));
    }
    for near in board.keys()?.into_iter().filter(|k| k.state == KeyState::Trusted && looks_alike(&k.name, &name)) {
        log::info(&format!(
            "board: warning: the bound name {:?} ({}) is fewer than {MIN_DISTANCE} edits from the owner's {name:?}; \
             if it is a look-alike, revoke it in the web view (/ui/keys)",
            near.name, near.fingerprint
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

/// The most connections served at once. Each holds a thread and a file
/// descriptor; past this a new one is answered 503 and closed, so the
/// process never runs out of either (systemd's default soft limit is 1024
/// descriptors).
const MAX_CONNECTIONS: usize = 256;

/// The answer past [`MAX_CONNECTIONS`], as every refusal: JSON with a code.
const FULL: &str = r#"{"error":"busy","message":"the board has too many connections open; try again shortly"}"#;

/// The connections open, and how many were turned away at the limit.
#[derive(Default)]
struct Connections {
    open: AtomicUsize,
    refused: AtomicUsize,
}

/// One connection's place under [`MAX_CONNECTIONS`], given back on drop.
struct Slot(Arc<Connections>);

impl Connections {
    fn take(this: &Arc<Self>) -> Option<Slot> {
        let below = this
            .open
            .try_update(Ordering::SeqCst, Ordering::SeqCst, |n| (n < MAX_CONNECTIONS).then(|| n.saturating_add(1)));
        below.is_ok().then(|| Slot(Arc::clone(this)))
    }

    /// Answer a connection over the limit 503, and close it.
    fn refuse(&self, mut stream: TcpStream, peer: SocketAddr) {
        if self.refused.fetch_add(1, Ordering::SeqCst) == 0 {
            log::line(&format!("board: {MAX_CONNECTIONS} connections open; answering new ones 503 until one closes"));
        }
        let answer = Response::json(503, FULL.to_owned()).with_header("Board-Error", "busy");
        // A new socket's send buffer is empty, so this does not block; the
        // timeout is only a backstop.
        let sent = stream
            .set_write_timeout(Some(Duration::from_secs(1)))
            .and_then(|()| answer.write_to(&mut stream, date().as_deref()));
        if let Err(err) = sent {
            log::line(&format!("board: cannot tell {peer} the board is full: {err}"));
        }
    }
}

impl Drop for Slot {
    fn drop(&mut self) {
        self.0.open.fetch_sub(1, Ordering::SeqCst);
        let refused = self.0.refused.swap(0, Ordering::SeqCst);
        if refused > 0 {
            log::line(&format!("board: below {MAX_CONNECTIONS} connections again; {refused} were turned away"));
        }
    }
}

fn serve(listener: &TcpListener, app: &Arc<App>) {
    let connections = Arc::new(Connections::default());
    loop {
        match listener.accept() {
            Ok((stream, peer)) => {
                let Some(slot) = Connections::take(&connections) else {
                    connections.refuse(stream, peer);
                    continue;
                };
                let app = Arc::clone(app);
                let spawned = thread::Builder::new().name(format!("board {peer}")).spawn(move || {
                    connection(&app, &stream, peer);
                    drop(slot);
                });
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

/// Whether a long poll's client still waits for the answer: once it has
/// gone, its end of the connection reads as closed.
fn client_waiting(stream: &TcpStream, peer: SocketAddr) -> bool {
    if let Err(err) = stream.set_nonblocking(true) {
        log::line(&format!("board: cannot check whether {peer} still waits: {err}"));
        return true;
    }
    let mut probe = [0_u8; 1];
    let waiting = match stream.peek(&mut probe) {
        Ok(0) => false,
        // Nothing to read yet, or its next request already sent.
        Ok(_) => true,
        Err(err) if err.kind() == ErrorKind::WouldBlock => true,
        Err(err) => {
            log::line(&format!("board: connection from {peer} lost during a poll: {err}"));
            false
        }
    };
    match stream.set_nonblocking(false) {
        Ok(()) => waiting,
        Err(err) => {
            log::line(&format!("board: cannot serve {peer} further: {err}"));
            false
        }
    }
}

/// Serve one connection's requests, in order, until either side ends it,
/// or the client stalls for [`http::CLIENT_TIMEOUT`].
fn connection(app: &App, stream: &TcpStream, peer: SocketAddr) {
    // Head and body are written separately, and Nagle's algorithm would
    // hold the body back until the client acknowledged the head.
    let set_up = stream.set_nodelay(true).and_then(|()| stream.set_write_timeout(Some(http::CLIENT_TIMEOUT)));
    if let Err(err) = set_up {
        log::line(&format!("board: cannot set up the connection from {peer}: {err}"));
        return;
    }
    let mut reader = BufReader::new(http::Timed::new(stream));
    let mut writer = stream;
    let waiting = || client_waiting(stream, peer);
    loop {
        // An idle keep-alive connection is closed after the timeout...
        reader.get_mut().allow(http::CLIENT_TIMEOUT);
        match reader.fill_buf() {
            Ok([]) => return,
            Ok(_) => {}
            Err(err) if err.kind() == ErrorKind::TimedOut => return,
            Err(err) => {
                log::line(&format!("board: connection from {peer} lost: {err}"));
                return;
            }
        }
        // ...and from its first byte, a request has as long to arrive whole.
        reader.get_mut().allow(http::CLIENT_TIMEOUT);
        let request = match http::read_request(&mut reader, &mut writer) {
            Ok(Some(request)) => request,
            Ok(None) => return,
            Err(err) => {
                let Some(status) = err.status() else {
                    log::line(&format!("board: connection from {peer} lost: {err}"));
                    return;
                };
                log::line(&format!("board: bad request from {peer}: {err}"));
                let refusal = serde_json::json!({ "error": err.code(), "message": err.to_string() });
                let answer = serde_json::to_string(&refusal)
                    .map(|body| Response::json(status, body).with_header("Board-Error", err.code()));
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
        let Some(response) = routes::handle(app, &request, peer, &waiting) else {
            return; // left during its long poll
        };
        if let Err(err) = response.write_to(&mut writer, date().as_deref()) {
            log::line(&format!("board: client {peer} went away mid-response: {err}"));
            return;
        }
        if request.close {
            return;
        }
    }
}
