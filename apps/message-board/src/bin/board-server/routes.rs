//! What the server answers, and how: the JSON API that `board` and agents
//! use (`api`), the web view (`web`), and the web view's static files.
//!
//! Every way a request can fail ends here as an answer, in the form its
//! asker reads: JSON for the API, a page for a browser's navigation, an
//! escaped fragment for what htmx swaps in, and plain text for the web
//! view's signed forms (board-sign.js shows it by the form). Each carries
//! the refusal's code in a `Board-Error` header as well.

use std::borrow::Cow;
use std::fs;
use std::net::SocketAddr;
use std::path::PathBuf;

use serde_json::{Value, json};

use message_board::store::{Board, DbError, Error as StoreError};

use crate::blobs::{self, Blobs};
use crate::html;
use crate::http::{Body, Request, Response};
use crate::log;
use crate::writes::{Json, Write};
use crate::{api, web};

pub struct App {
    pub board: Board,
    /// htmx.min.js and board-sign.js.
    pub static_dir: PathBuf,
    /// The blob store's files and limits.
    pub blobs: Blobs,
}

/// Why a request was not answered as asked.
#[derive(Debug)]
pub enum Failure {
    /// The board refused it, or its database failed (logged, and the end of
    /// the server if the database cannot be used any more).
    Board(StoreError),
    /// A write without a signature: the board takes none.
    Unsigned,
    /// A method the server does not serve.
    Method(String),
    /// An answer that would not encode as JSON (logged).
    Encoding(serde_json::Error),
    /// The client left while its long poll waited: no one to answer.
    Gone,
}

impl From<StoreError> for Failure {
    fn from(err: StoreError) -> Self {
        Self::Board(err)
    }
}

impl From<DbError> for Failure {
    fn from(err: DbError) -> Self {
        Self::Board(StoreError::Db(err))
    }
}

impl From<serde_json::Error> for Failure {
    fn from(err: serde_json::Error) -> Self {
        Self::Encoding(err)
    }
}

pub type Answer = Result<Response, Failure>;

/// A database fault's message: busy (another program held the file past
/// the busy timeout) is temporary and the client's to retry; anything else
/// is the board's own fault.
fn db_message(err: &DbError) -> String {
    if err.is_busy() {
        format!("database busy: {err}; nothing was written, try again")
    } else {
        format!("database error: {err}")
    }
}

/// A failure as it is told: its status, its code, and the JSON object
/// `{"error": code, "message": ..., <the code's own fields>}`.
struct Told {
    status: u16,
    code: &'static str,
    json: Value,
}

impl Told {
    fn new(failure: &Failure) -> Self {
        let plain =
            |status, code, message: String| Self { status, code, json: json!({ "error": code, "message": message }) };
        match failure {
            Failure::Board(err) => {
                let mut json = err.to_json();
                if let (StoreError::Db(db), Some(fields)) = (err, json.as_object_mut()) {
                    fields.insert("message".to_owned(), Value::String(db_message(db)));
                }
                Self { status: err.status(), code: err.code(), json }
            }
            Failure::Unsigned => plain(
                401,
                "unsigned",
                "the board takes signed writes only: sign with a key the owner approved (board register)".to_owned(),
            ),
            Failure::Method(method) => plain(501, "not-implemented", format!("unsupported method {method:?}")),
            Failure::Encoding(err) => plain(500, "internal", format!("cannot encode the answer: {err}")),
            Failure::Gone => plain(500, "internal", "the client has gone".to_owned()),
        }
    }

    fn message(&self) -> &str {
        self.json.get("message").and_then(Value::as_str).unwrap_or(self.code)
    }
}

/// The form a route's failures are answered in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Reply {
    Json,
    /// A whole page, for a browser that navigated here.
    Page,
    /// A piece of page that htmx swaps in, as it swaps a refusal too.
    Fragment,
    /// The reason alone: board-sign.js puts it by the form that was sent.
    Text,
}

impl Reply {
    fn of(method: &str, path: &str) -> Self {
        match (method, path.strip_prefix("/ui/")) {
            ("POST", Some(_)) => Self::Text,
            (_, Some("messages" | "note" | "live/queue" | "live/inbox" | "live/keys")) => Self::Fragment,
            (_, Some(_)) => Self::Page,
            _ if path == "/" || path.starts_with("/view/") => Self::Page,
            _ => Self::Json,
        }
    }

    fn answer(self, told: &Told) -> Response {
        let response = match self {
            Self::Json => match serde_json::to_string(&told.json) {
                Ok(body) => Response::json(told.status, body),
                Err(err) => {
                    log::line(&format!("board: cannot encode an error as JSON: {err}"));
                    Response::text(told.status, told.message())
                }
            },
            Self::Page => Response::html(told.status, html::error_page(told.status, told.message())),
            Self::Fragment => Response::html(told.status, html::error_fragment(told.message())),
            Self::Text => Response::text(told.status, told.message()),
        };
        let response = response.with_header("Board-Error", told.code);
        // a busy board, or one with every upload slot taken, is worth asking again shortly
        if told.status == 503 { response.with_header("Retry-After", "5") } else { response }
    }
}

/// The request, for a log line: method, path and client; and whether the
/// client still waits for the answer.
pub struct Context<'a> {
    method: &'a str,
    path: &'a str,
    peer: SocketAddr,
    pub waiting: &'a dyn Fn() -> bool,
}

impl Context<'_> {
    pub fn log(&self, what: &str) {
        log::line(&format!("board: {} {} from {}: {what}", self.method, self.path, self.peer));
    }

    /// Log what the board's operator must hear of: a database fault, which
    /// ends the server, with status 1, for systemd to restart it if it
    /// would fail every request from now on; and an answer that would not
    /// encode. A refusal is the client's business, and not logged.
    fn note(&self, failure: &Failure) {
        match failure {
            Failure::Board(StoreError::Db(err)) => {
                self.log(&db_message(err));
                if err.is_fatal() {
                    log::line("board: the database cannot be used any more; exiting");
                    std::process::exit(1);
                }
            }
            Failure::Encoding(err) => self.log(&format!("cannot encode the answer: {err}")),
            _ => {}
        }
    }
}

/// A query string or a form body, decoded the standard
/// (`application/x-www-form-urlencoded`) way.
pub struct Params(Vec<(String, String)>);

impl Params {
    pub fn parse(input: &[u8]) -> Self {
        Self(form_urlencoded::parse(input).into_owned().collect())
    }

    /// The first value given for `name`, if any.
    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
    }

    /// Every value given for `name` that is not empty, in order.
    pub fn all<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a str> {
        self.0.iter().filter(move |(n, v)| n == name && !v.is_empty()).map(|(_, v)| v.as_str())
    }

    /// Whether `name` is given as `1`.
    pub fn flag(&self, name: &str) -> bool {
        self.get(name) == Some("1")
    }
}

/// Whether a request's body is left for its handler to stream rather than
/// read whole: only a blob upload's.
pub fn streamed(method: &str, target: &str) -> bool {
    blobs::streamed(method, split_target(target).0)
}

/// Answer a request. `None` only when the client left before there was an
/// answer, as `waiting` (asked during a long poll, or an upload's body) found.
pub fn handle(
    app: &App,
    request: &Request,
    peer: SocketAddr,
    waiting: &dyn Fn() -> bool,
    body: &mut Body<'_, '_>,
) -> Option<Response> {
    let (path, query) = split_target(&request.target);
    let ctx = Context { method: &request.method, path, peer, waiting };
    let answer = match foreign(request) {
        Some(why) => {
            ctx.log(&why);
            Err(StoreError::Forbidden(why).into())
        }
        None => route(app, request, path, query, &ctx, body),
    };
    match answer {
        Ok(response) => Some(response),
        Err(Failure::Gone) => None,
        Err(failure) => {
            ctx.note(&failure);
            Some(Reply::of(&request.method, path).answer(&Told::new(&failure)))
        }
    }
}

/// Why a request comes from somewhere the board does not answer, if it
/// does: a Host that is not one of the board's own names, or a write from
/// another site's page.
fn foreign(request: &Request) -> Option<String> {
    match (&request.host, &request.origin) {
        (Some(host), _) if !own_host(host) => Some(format!("host {host:?} is not a name this board answers to")),
        (host, Some(origin)) if request.method != "GET" && !same_origin(origin, host.as_deref()) => {
            Some(format!("a write from {origin:?}, another site, is refused"))
        }
        _ => None,
    }
}

fn route(app: &App, request: &Request, path: &str, query: &str, ctx: &Context<'_>, body: &mut Body<'_, '_>) -> Answer {
    match request.method.as_str() {
        "GET" => get(app, request, path, &Params::parse(query.as_bytes()), ctx),
        "POST" => post(app, path, &request.body),
        "PUT" if request.streamed => blobs::put(app, request, body, ctx),
        other => Err(Failure::Method(other.to_owned())),
    }
}

fn not_found() -> Failure {
    StoreError::NotFound("nothing here: see GET /n/ for the tree, or / for the web view".to_owned()).into()
}

fn get(app: &App, request: &Request, path: &str, q: &Params, ctx: &Context<'_>) -> Answer {
    let board = &app.board;
    match path {
        "/healthz" => api::healthz(board),
        "/items" => api::items(board, q),
        "/keys" => api::keys(board),
        "/registrations" => api::registrations(board),
        "/brief" => api::brief(board, q),
        "/blobs" => blobs::list(app, q),
        "/blob-refs" => blobs::refs(board, q),
        "/poll" => api::poll(board, q, ctx.waiting),
        "/static/htmx.min.js" | "/static/board-sign.js" => Ok(static_file(app, path, ctx)),
        "/" => web::view(board, "", q),
        "/ui/inbox" => web::inbox(board, q),
        "/ui/keys" => web::keys(board),
        "/ui/messages" => web::messages(board, q),
        "/ui/live/queue" => web::live_queue(board, q),
        "/ui/live/inbox" => web::live_inbox(board, q),
        "/ui/live/keys" => web::live_keys(board),
        "/ui/note" => web::note(board, q),
        _ => {
            let (prefix, rest) = path.get(1..).and_then(|p| p.split_once('/')).ok_or_else(not_found)?;
            let rest = unescaped(rest)?;
            match prefix {
                "n" => api::node(board, &rest, q),
                "view" => web::view(board, &rest, q),
                "event" => api::event(board, &rest),
                "item" => api::item(board, &rest),
                "keys" => api::key(board, &rest),
                "blobs" => blobs::one(board, &rest),
                "sha256" => blobs::download(app, &rest, request),
                _ => Err(not_found()),
            }
        }
    }
}

fn post(app: &App, path: &str, body: &[u8]) -> Answer {
    let board = &app.board;
    if let Some(ui) = path.strip_prefix("/ui/") {
        return web::write(board, web::Form::named(ui).ok_or_else(not_found)?, &Params::parse(body));
    }
    if path == "/blob-delete" {
        return blobs::delete(app, &Json::parse(body)?);
    }
    let write = path.strip_prefix('/').and_then(Write::named).ok_or_else(not_found)?;
    api::write(board, write, &Json::parse(body)?)
}

/// A path's remainder, its `%XX` escapes decoded: a client may well send
/// a fingerprint (`SHA256:...+.../...`) percent-encoded. A `+` stays a
/// `+`; only a query string has it for a space.
fn unescaped(text: &str) -> Result<Cow<'_, str>, StoreError> {
    if !text.contains('%') {
        return Ok(Cow::Borrowed(text));
    }
    let hex = |pair: &[u8]| {
        let digits = std::str::from_utf8(pair).ok().filter(|d| d.bytes().all(|b| b.is_ascii_hexdigit()))?;
        u8::from_str_radix(digits, 16).ok()
    };
    let mut bytes = Vec::with_capacity(text.len());
    let mut rest = text.as_bytes();
    while let Some((&first, tail)) = rest.split_first() {
        if let (b'%', Some(byte)) = (first, tail.get(..2).and_then(hex)) {
            bytes.push(byte);
            rest = tail.get(2..).unwrap_or_default();
        } else {
            bytes.push(first);
            rest = tail;
        }
    }
    String::from_utf8(bytes)
        .map(Cow::Owned)
        .map_err(|err| StoreError::BadRequest(format!("the path {text:?} is not UTF-8 once decoded: {err}")))
}

/// Whether a Host header names the board as only its own clients reach
/// it: by an IP address, as localhost, by a name without dots (a tailnet
/// short name) or by a tailnet name under ts.net. A page on any other name
/// may be an attacker's, rebound in DNS to this address, so that the
/// browser lets it read and write the board as its own origin.
fn own_host(host: &str) -> bool {
    if host.starts_with('[') {
        return true; // an IPv6 address
    }
    let name = match host.rsplit_once(':') {
        Some((name, port)) if port.bytes().all(|b| b.is_ascii_digit()) => name,
        _ => host,
    };
    let name = name.trim_end_matches('.').to_ascii_lowercase();
    name.parse::<std::net::Ipv4Addr>().is_ok()
        || name == "localhost"
        || name.ends_with(".localhost")
        || !name.contains('.')
        || name.ends_with(".ts.net")
}

/// Whether a browser's Origin is the board's own: the host and port it
/// was reached by. Another site's page may make a browser post a form or
/// a plain-text "JSON" body here without asking; `Origin: null` is no
/// origin of ours either.
fn same_origin(origin: &str, host: Option<&str>) -> bool {
    let authority = origin.split_once("://").map(|(_, authority)| authority);
    authority.zip(host).is_some_and(|(authority, host)| authority.eq_ignore_ascii_case(host))
}

/// The path and query string of a request target. An absolute-form target
/// (`http://host/path?query`, which an HTTP/1.1 server must take) loses its
/// scheme and host; a fragment is part of neither.
fn split_target(target: &str) -> (&str, &str) {
    let target = target.split_once('#').map_or(target, |(before, _)| before);
    let target = match target.split_once("://") {
        Some((_, after)) if !target.starts_with('/') => {
            after.find(['/', '?']).and_then(|at| after.get(at..)).unwrap_or("")
        }
        _ => target,
    };
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    (if path.is_empty() { "/" } else { path }, query)
}

/// One of the files in the static directory, or 404 (logged: the package
/// installs both, so a missing one is the operator's to know of).
fn static_file(app: &App, path: &str, ctx: &Context<'_>) -> Response {
    let name = path.rsplit('/').next().unwrap_or(path);
    let file = app.static_dir.join(name);
    match fs::read(&file) {
        Ok(bytes) => Response::bytes(200, "text/javascript; charset=utf-8", bytes),
        Err(err) => {
            ctx.log(&format!("cannot read {}: {err}", file.display()));
            let told = Told::new(&StoreError::NotFound(format!("{name} is not installed")).into());
            Reply::Json.answer(&told)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::writes::Fields as _;

    #[test]
    fn only_the_boards_own_names_and_origins_pass() {
        for host in ["127.0.0.1:8777", "localhost:8777", "sz1:8777", "SZ1.tail1234.ts.net:8777", "[fd7a::1]:8777", "x"]
        {
            assert!(own_host(host), "{host}");
        }
        for host in ["evil.example:8777", "127.0.0.1.evil.example", "ts.net.evil.example:8777"] {
            assert!(!own_host(host), "{host}");
        }
        assert!(same_origin("http://sz1:8777", Some("sz1:8777")));
        assert!(!same_origin("https://evil.example", Some("sz1:8777")));
        assert!(!same_origin("null", Some("sz1:8777")));
        assert!(!same_origin("http://sz1:8777", None));
    }

    #[test]
    fn targets_split_into_path_and_query() {
        assert_eq!(split_target("/poll?since=3&path=a/b"), ("/poll", "since=3&path=a/b"));
        assert_eq!(split_target("http://host:8777/event/5?x#f"), ("/event/5", "x"));
        assert_eq!(split_target("http://host:8777?x"), ("/", "x"));
        assert_eq!(split_target("/x#frag?no"), ("/x", ""));
    }

    #[test]
    fn queries_decode_the_standard_way() {
        let q = Params::parse(b"path=a+b%2Fc&path=&last=7&path=second");
        assert_eq!(q.get("path"), Some("a b/c"));
        assert_eq!(q.all("path").collect::<Vec<_>>(), ["a b/c", "second"]);
        assert!(matches!(q.number("last"), Ok(Some(7))));
        assert!(matches!(q.number("missing"), Ok(None)));
        assert!(matches!(Params::parse(b"since=x").number("since"), Err(StoreError::BadRequest(_))));
    }

    #[test]
    fn a_path_is_percent_decoded_but_plus_stays_plus() -> Result<(), StoreError> {
        assert_eq!(unescaped("SHA256%3Aab+c%2Fd")?, "SHA256:ab+c/d");
        assert_eq!(unescaped("SHA256:ab+c/d")?, "SHA256:ab+c/d");
        assert_eq!(unescaped("100%")?, "100%"); // not an escape: as it is
        assert_eq!(unescaped("%+1x")?, "%+1x");
        assert!(unescaped("%ff").is_err());
        Ok(())
    }

    #[test]
    fn each_route_fails_in_the_form_its_asker_reads() {
        assert_eq!(Reply::of("POST", "/post"), Reply::Json);
        assert_eq!(Reply::of("GET", "/n/rev"), Reply::Json);
        assert_eq!(Reply::of("POST", "/ui/approve"), Reply::Text);
        assert_eq!(Reply::of("GET", "/ui/inbox"), Reply::Page);
        assert_eq!(Reply::of("GET", "/view/rev/log"), Reply::Page);
        assert_eq!(Reply::of("GET", "/ui/messages"), Reply::Fragment);
        assert_eq!(Reply::of("GET", "/ui/note"), Reply::Fragment);
        assert_eq!(Reply::of("GET", "/ui/live/inbox"), Reply::Fragment);
        assert_eq!(Reply::of("POST", "/ui/note"), Reply::Text);
    }

    #[test]
    fn a_database_fault_keeps_its_code_and_says_whether_anything_was_written() {
        let told = Told::new(&Failure::from(DbError::Closed));
        assert_eq!((told.status, told.code), (500, "internal"));
        assert_eq!(told.message(), "database error: the database is closed");
        let told = Told::new(&Failure::from(StoreError::StaleNonce { head: 10_007 }));
        assert_eq!((told.status, told.code, told.json.get("head")), (409, "stale-nonce", Some(&json!(10_007))));
        let told = Told::new(&Failure::Unsigned);
        assert_eq!((told.status, told.code), (401, "unsigned"));
    }
}
