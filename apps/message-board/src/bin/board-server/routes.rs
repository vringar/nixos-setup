//! What the server answers: the JSON API that `board` and agents use, the
//! htmx web view, and its static files.

use std::fs;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{Map, Value, json};

use message_board::sign::{self, Kind, MESSAGE, PIN, SignatureError};
use message_board::store::{Area, Board, DbError, Error as StoreError, NewPost, PinSave, Scope, Signer};
use message_board::text::one_line;

use crate::html::{self, Html, Page, PinForm};
use crate::http::{Request, Response};
use crate::log;

/// The longest a poll may wait.
const MAX_POLL: f64 = 300.0;
/// How many messages the web view shows.
const RECENT: i64 = 100;

pub struct App {
    pub board: Board,
    /// htmx.min.js and board-sign.js.
    pub static_dir: PathBuf,
}

/// Why a request could not be answered as asked.
enum Failure {
    /// The database failed: logged, then answered 503 (busy) or 500.
    Db(DbError),
    /// A request value that cannot be used: logged, then answered 400.
    Bad(String),
    /// The JSON body is not JSON.
    NotJson(serde_json::Error),
    /// The client left while its long poll waited: no one to answer.
    Gone,
}

impl From<DbError> for Failure {
    fn from(err: DbError) -> Self {
        Self::Db(err)
    }
}

impl From<serde_json::Error> for Failure {
    fn from(err: serde_json::Error) -> Self {
        Self::NotJson(err)
    }
}

type Answer = Result<Response, Failure>;

/// A database fault as (status, message): busy -- another program holds
/// the file longer than the busy timeout -- is temporary and the client's
/// to retry; anything else is the board's own fault.
fn db_failure(err: &DbError) -> (u16, String) {
    if err.is_busy() {
        (503, format!("database busy: {err}; nothing was written, try again"))
    } else {
        (500, format!("database error: {err}"))
    }
}

/// The request, for a log line: method, path and client; and whether the
/// client still waits for the answer.
struct Context<'a> {
    method: &'a str,
    path: &'a str,
    peer: SocketAddr,
    waiting: &'a dyn Fn() -> bool,
}

impl Context<'_> {
    fn log(&self, what: &str) {
        log::line(&format!("board: {} {} from {}: {what}", self.method, self.path, self.peer));
    }

    /// Log a database fault and say how to answer it.
    fn db_failed(&self, err: &DbError) -> (u16, String) {
        let (status, message) = db_failure(err);
        self.log(&message);
        (status, message)
    }
}

fn json_error(status: u16, error: &str) -> Result<Response, serde_json::Error> {
    Ok(Response::json(status, serde_json::to_string(&json!({ "error": error }))?))
}

/// A query string or a form body, decoded the standard
/// (`application/x-www-form-urlencoded`) way.
struct Params(Vec<(String, String)>);

impl Params {
    fn parse(input: &[u8]) -> Self {
        Self(form_urlencoded::parse(input).into_owned().collect())
    }

    /// The first value given for `name`, if any.
    fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
    }

    /// The value of `name`, or `default` when it is missing or empty.
    fn or<'a>(&'a self, name: &str, default: &'a str) -> &'a str {
        self.get(name).filter(|v| !v.is_empty()).unwrap_or(default)
    }
}

/// Answer a request. Every way a request can go wrong ends here as a
/// status: nothing is dropped. `None` only when the client left before
/// there was an answer, as `waiting` (asked during a long poll) found.
pub fn handle(app: &App, request: &Request, peer: SocketAddr, waiting: &dyn Fn() -> bool) -> Option<Response> {
    let (path, query) = split_target(&request.target);
    let ctx = Context { method: &request.method, path, peer, waiting };
    let answer = match request.method.as_str() {
        "GET" => get(app, path, &Params::parse(query.as_bytes()), &ctx),
        "POST" => post(app, path, &request.body, &ctx),
        other => {
            ctx.log(&format!("unsupported method {other:?}"));
            json_error(501, &format!("unsupported method {other:?}")).map_err(Failure::NotJson)
        }
    };
    let failed = match answer {
        Ok(response) => return Some(response),
        Err(Failure::Gone) => return None,
        Err(Failure::Db(err)) => ctx.db_failed(&err),
        Err(Failure::Bad(why)) => {
            ctx.log(&why);
            (400, why)
        }
        Err(Failure::NotJson(err)) => {
            ctx.log(&format!("invalid JSON: {err}"));
            (400, "invalid JSON".to_owned())
        }
    };
    Some(match json_error(failed.0, &failed.1) {
        Ok(response) => response,
        Err(err) => {
            ctx.log(&format!("cannot encode an error as JSON: {err}"));
            Response::text(500, "internal error")
        }
    })
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

fn archived_flag(q: &Params) -> bool {
    q.get("archived") == Some("1")
}

/// A number in a query string; `default` when it is missing or empty.
fn number<T>(q: &Params, name: &str, default: T) -> Result<T, Failure>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    match q.get(name).filter(|v| !v.is_empty()) {
        None => Ok(default),
        Some(text) => text.parse().map_err(|err| Failure::Bad(format!("bad {name} {text:?}: {err}"))),
    }
}

fn get(app: &App, path: &str, q: &Params, ctx: &Context<'_>) -> Answer {
    let area = q.or("area", "");
    let topic = q.or("topic", "");
    match path {
        "/poll" => {
            let since: i64 = number(q, "since", 0)?;
            let timeout: f64 = number(q, "timeout", 50.0)?;
            if timeout.is_nan() {
                return Err(Failure::Bad("bad timeout: NaN".to_owned()));
            }
            let wait = Duration::try_from_secs_f64(timeout.clamp(0.0, MAX_POLL)).unwrap_or(Duration::ZERO);
            let scope = Scope { since, area, topic, include_archived: archived_flag(q) };
            let messages = app.board.poll(&scope, wait, ctx.waiting)?.ok_or(Failure::Gone)?;
            if q.get("format") == Some("lines") {
                Ok(Response::text(200, &format_lines(&messages)))
            } else {
                Ok(Response::json(200, serde_json::to_string(&messages)?))
            }
        }
        "/areas" => Ok(Response::json(200, serde_json::to_string(&app.board.list_areas(archived_flag(q))?)?)),
        "/pins" => Ok(Response::json(200, serde_json::to_string(&app.board.pins_for(area, topic, archived_flag(q))?)?)),
        "/pin" | "/ui/pin" | "/ui/pin/edit" if area.is_empty() => Ok(json_error(400, "need an 'area'")?),
        "/ui/pin" => Ok(Response::html(200, html::pin(area, topic, app.board.get_pin(area, topic)?.as_ref()))),
        "/ui/pin/edit" => Ok(Response::html(200, pin_form(app, area, topic, "me", None, None, "")?)),
        "/pin" => match app.board.get_pin(area, topic)? {
            Some(pin) => Ok(Response::json(200, serde_json::to_string(&pin)?)),
            None => Ok(json_error(404, "no pin")?),
        },
        "/ui/messages" => Ok(Response::html(200, html::rows(&app.board.recent(area, topic, RECENT)?))),
        "/static/htmx.min.js" | "/static/board-sign.js" => Ok(static_file(app, path, ctx)?),
        "/healthz" => Ok(Response::json(200, serde_json::to_string(&json!({ "head": app.board.head_id()? }))?)),
        "/" => Ok(Response::html(200, page(app, area, topic, q.get("show") == Some("archived"))?)),
        _ if path.starts_with("/msg/") => {
            let id = path.strip_prefix("/msg/").and_then(|id| id.parse::<i64>().ok());
            match id.map(|id| app.board.message(id)).transpose()?.flatten() {
                Some(message) => Ok(Response::json(200, serde_json::to_string(&message)?)),
                None => Ok(json_error(404, "no such id")?),
            }
        }
        _ => Ok(json_error(404, "not found")?),
    }
}

fn static_file(app: &App, path: &str, ctx: &Context<'_>) -> Result<Response, serde_json::Error> {
    let name = path.rsplit('/').next().unwrap_or(path);
    let file = app.static_dir.join(name);
    match fs::read(&file) {
        Ok(bytes) => Ok(Response::bytes(200, "text/javascript; charset=utf-8", bytes)),
        Err(err) => {
            ctx.log(&format!("cannot read {}: {err}", file.display()));
            json_error(404, &format!("{name} not found"))
        }
    }
}

/// The watch stream: `id\t[area/topic] from: first line`, with ` ✓` after
/// a verified sender. One line per message, whatever the names and the
/// body hold: a watcher reads the id up to the first tab of each line.
fn format_lines(messages: &[message_board::store::Message]) -> String {
    messages
        .iter()
        .map(|m| {
            let mark = if m.verified { " ✓" } else { "" };
            format!(
                "{}\t[{}/{}] {}{mark}: {}\n",
                m.id,
                one_line(&m.area),
                one_line(&m.topic),
                one_line(&m.sender),
                one_line(&summary(&m.body))
            )
        })
        .collect::<Vec<_>>()
        .concat()
}

/// The first line of a body, cut at 200 characters.
fn summary(body: &str) -> String {
    let first = body.trim().lines().next().unwrap_or_default();
    if first.chars().count() <= 200 {
        first.to_owned()
    } else {
        let mut cut: String = first.chars().take(197).collect();
        cut.push_str("...");
        cut
    }
}

fn page(app: &App, area: &str, topic: &str, show_archived: bool) -> Result<Html, DbError> {
    let mut pins = Html::new();
    if !area.is_empty() {
        // the area-wide pin, then the topic's own
        pins.html(&html::pin(area, "", app.board.get_pin(area, "")?.as_ref()));
        if !topic.is_empty() {
            pins.html(&html::pin(area, topic, app.board.get_pin(area, topic)?.as_ref()));
        }
    }
    let (archived, live): (Vec<Area>, Vec<Area>) =
        app.board.list_areas(true)?.into_iter().partition(|a| a.archived_ts.is_some());
    let archived_ts = archived.iter().find(|a| a.name == area).and_then(|a| a.archived_ts.as_deref());
    let topics = if area.is_empty() { Vec::new() } else { app.board.list_topics(area)? };
    Ok(html::page(&Page {
        area,
        topic,
        show_archived,
        live: &live,
        archived: &archived,
        archived_ts,
        topics: &topics,
        pins,
        rows: html::rows(&app.board.recent(area, topic, RECENT)?),
        sender: "me",
    }))
}

/// The pin editor; `body` and `base_version` default to the pin's own.
fn pin_form(
    app: &App,
    area: &str,
    topic: &str,
    sender: &str,
    body: Option<&str>,
    base_version: Option<i64>,
    error: &str,
) -> Result<Html, DbError> {
    let current = if body.is_none() || base_version.is_none() { app.board.get_pin(area, topic)? } else { None };
    let current_body = current.as_ref().map_or("", |pin| pin.body.as_str());
    Ok(html::pin_form(&PinForm {
        area,
        topic,
        sender,
        body: body.unwrap_or(current_body),
        base_version: base_version.unwrap_or_else(|| current.as_ref().map_or(0, |pin| pin.version)),
        error,
    }))
}

/// How a write ended, when it did not land.
enum Refused {
    /// The board will not do it: the status, and why.
    Refusal {
        status: u16,
        message: String,
        stale_head: Option<i64>,
    },
    /// The pin changed since the editor's base version.
    StalePin(i64),
    Fault(DbError),
}

impl From<SignatureError> for Refused {
    fn from(err: SignatureError) -> Self {
        Self::Refusal { status: 400, message: err.to_string(), stale_head: None }
    }
}

impl From<StoreError> for Refused {
    fn from(err: StoreError) -> Self {
        let message = err.to_string();
        match err {
            StoreError::Db(db) => Self::Fault(db),
            StoreError::StalePin { version } => Self::StalePin(version),
            StoreError::StaleNonce { head } => Self::Refusal { status: 409, message, stale_head: Some(head) },
            StoreError::Forbidden(_) => Self::Refusal { status: 403, message, stale_head: None },
            StoreError::Conflict(_) => Self::Refusal { status: 409, message, stale_head: None },
            StoreError::BadName(_) | StoreError::BadPlace(_) | StoreError::NoSuchArea(_) | StoreError::SystemArea => {
                Self::Refusal { status: 400, message, stale_head: None }
            }
        }
    }
}

/// A refusal as JSON. stale-nonce is a code: the client signs again on it.
fn refusal_json(status: u16, message: &str, stale_head: Option<i64>) -> Result<Response, serde_json::Error> {
    let response = match stale_head {
        Some(head) => Response::json(status, serde_json::to_string(&json!({ "error": "stale-nonce", "head": head }))?),
        None => Response::json(status, serde_json::to_string(&json!({ "error": message }))?),
    };
    Ok(with_board_error(response, stale_head))
}

/// Whatever the body is, a client can tell a stale nonce by its header.
fn with_board_error(response: Response, stale_head: Option<i64>) -> Response {
    if stale_head.is_some() { response.with_header("Board-Error", "stale-nonce") } else { response }
}

/// The verified signer of `values`, or `None` for an unsigned write.
fn signer(
    kind: &Kind,
    values: &[&str; 6],
    nonce: &str,
    signature: Option<&str>,
) -> Result<Option<Signer>, SignatureError> {
    let Some(signature) = signature.filter(|s| !s.is_empty()) else {
        return Ok(None);
    };
    let verified = sign::verify(kind, values, signature)?;
    Ok(Some(Signer {
        pubkey: verified.public_line,
        fingerprint: verified.fingerprint,
        signature: signature.to_owned(),
        nonce: nonce.to_owned(),
    }))
}

/// A client's message: verified if signed, then stored.
struct Submission<'a> {
    area: &'a str,
    topic: &'a str,
    sender: &'a str,
    body: &'a str,
    key: Option<&'a str>,
    signature: Option<&'a str>,
    nonce: &'a str,
}

fn submit_post(app: &App, s: &Submission<'_>) -> Result<i64, Refused> {
    let values = [s.area, s.topic, s.sender, s.body, s.key.unwrap_or(""), s.nonce];
    let signer = signer(&MESSAGE, &values, s.nonce, s.signature)?;
    let new = NewPost { area: s.area, topic: s.topic, sender: s.sender, body: s.body, key: s.key };
    Ok(app.board.post(&new, signer.as_ref())?)
}

fn submit_pin(app: &App, save: &PinSave<'_>, signature: Option<&str>, nonce: &str) -> Result<i64, Refused> {
    let base = save.base_version.map(|v| v.to_string()).unwrap_or_default();
    let values = [save.area, save.topic, save.editor, save.body, base.as_str(), nonce];
    let signer = signer(&PIN, &values, nonce, signature)?;
    Ok(app.board.set_pin(save, signer.as_ref())?)
}

fn post(app: &App, path: &str, body: &[u8], ctx: &Context<'_>) -> Answer {
    match path {
        "/ui/archive" => ui_archive(app, &Params::parse(body)),
        "/ui/pin" => ui_pin(app, &Params::parse(body), ctx),
        "/ui/post" => ui_post(app, &Params::parse(body), ctx),
        _ => {
            let data: Value = if body.is_empty() { Value::Object(Map::new()) } else { serde_json::from_slice(body)? };
            let api: fn(&App, &Fields) -> Answer = match path {
                "/post" => api_post,
                "/archive" => api_archive,
                "/pin" => api_pin,
                "/areas" => api_areas,
                _ => return Ok(json_error(404, "not found")?),
            };
            match data {
                Value::Object(fields) => api(app, &Fields(fields)),
                _ => Err(Failure::Bad("expected a JSON object".to_owned())),
            }
        }
    }
}

/// A plain form post: archive or unarchive, then back to a sensible page.
fn ui_archive(app: &App, form: &Params) -> Answer {
    let area = form.get("area").unwrap_or_default();
    let archived = form.get("archived") == Some("1");
    match app.board.set_archived(area, archived) {
        Ok(_) => {
            let location = if archived {
                "/".to_owned()
            } else {
                format!("/?{}", form_urlencoded::Serializer::new(String::new()).append_pair("area", area).finish())
            };
            Ok(Response::redirect(&location))
        }
        Err(StoreError::Db(err)) => Err(Failure::Db(err)),
        Err(err) => Ok(json_error(400, &err.to_string())?),
    }
}

/// The htmx pin editor: the saved pin box, or the form again with why.
fn ui_pin(app: &App, form: &Params, ctx: &Context<'_>) -> Answer {
    let area = form.get("area").unwrap_or_default();
    if area.is_empty() {
        return Ok(json_error(400, "need an 'area'")?);
    }
    let topic = form.get("topic").unwrap_or_default();
    let sender = form.or("from", "anon");
    let body = form.get("body").unwrap_or_default();
    let base_version: i64 = number(form, "base_version", 0)?;
    let save = PinSave { area, topic, editor: sender, body, base_version: Some(base_version) };
    let refill = |base: i64, error: &str| pin_form(app, area, topic, sender, Some(body), Some(base), error);
    match submit_pin(app, &save, form.get("signature"), form.get("nonce").unwrap_or_default()) {
        Ok(_) => Ok(Response::html(200, html::pin(area, topic, app.board.get_pin(area, topic)?.as_ref()))),
        // Keep their text; the next save is a deliberate overwrite.
        Err(Refused::StalePin(version)) => Ok(Response::html(
            200,
            refill(
                version,
                &format!(
                    "Someone saved v{version} while you were editing. Your text is below; save again to overwrite theirs."
                ),
            )?,
        )),
        Err(Refused::Refusal { status, message, stale_head }) => {
            Ok(with_board_error(Response::html(status, refill(base_version, &message)?), stale_head))
        }
        Err(Refused::Fault(err)) => {
            let (status, message) = ctx.db_failed(&err);
            Ok(Response::html(status, refill(base_version, &message)?))
        }
    }
}

/// The htmx post form: the refreshed list, or why not as plain text
/// (board-sign.js shows it by the form, unswapped).
fn ui_post(app: &App, form: &Params, ctx: &Context<'_>) -> Answer {
    if let Some(body) = form.get("body").filter(|b| !b.is_empty()) {
        let submission = Submission {
            area: form.or("area", "general"),
            topic: form.or("topic", "general"),
            sender: form.or("from", "anon"),
            body,
            key: form.get("key").filter(|k| !k.is_empty()),
            signature: form.get("signature"),
            nonce: form.get("nonce").unwrap_or_default(),
        };
        match submit_post(app, &submission) {
            Ok(_) => {}
            Err(Refused::Refusal { status, message, stale_head }) => {
                return Ok(with_board_error(Response::text(status, &message), stale_head));
            }
            Err(Refused::StalePin(_)) => return Ok(Response::text(409, "pin changed since base_version")),
            Err(Refused::Fault(err)) => {
                let (status, message) = ctx.db_failed(&err);
                return Ok(Response::text(status, &message));
            }
        }
    }
    let rows = app.board.recent(form.get("area").unwrap_or_default(), form.get("topic").unwrap_or_default(), RECENT)?;
    Ok(Response::html(200, html::rows(&rows)))
}

/// A JSON request body's fields, with their types checked. A field that is
/// `null` counts as missing.
struct Fields(Map<String, Value>);

fn wrong_type(name: &str, kind: &str) -> Failure {
    Failure::Bad(format!("field {name:?} must be {kind}"))
}

impl Fields {
    fn get(&self, name: &str) -> Option<&Value> {
        self.0.get(name).filter(|value| !value.is_null())
    }

    /// A text field, `default` when it is missing.
    fn text<'a>(&'a self, name: &str, default: &'a str) -> Result<&'a str, Failure> {
        match self.get(name) {
            None => Ok(default),
            Some(Value::String(s)) => Ok(s),
            Some(_) => Err(wrong_type(name, "a string")),
        }
    }

    /// A text field that may be missing or empty.
    fn optional_text(&self, name: &str) -> Result<Option<&str>, Failure> {
        Ok(Some(self.text(name, "")?).filter(|s| !s.is_empty()))
    }

    /// The nonce as signed: text, or a number written out; empty if none.
    fn nonce(&self) -> Result<String, Failure> {
        match self.get("nonce") {
            None => Ok(String::new()),
            Some(Value::String(s)) => Ok(s.clone()),
            Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Ok(n.to_string()),
            Some(_) => Err(wrong_type("nonce", "a string")),
        }
    }

    fn base_version(&self) -> Result<Option<i64>, Failure> {
        match self.get("base_version") {
            None => Ok(None),
            Some(value) => value.as_i64().map(Some).ok_or_else(|| wrong_type("base_version", "an integer")),
        }
    }

    fn flag(&self, name: &str, default: bool) -> Result<bool, Failure> {
        match self.get(name) {
            None => Ok(default),
            Some(value) => value.as_bool().ok_or_else(|| wrong_type(name, "true or false")),
        }
    }
}

fn refused(r: Refused) -> Answer {
    match r {
        Refused::Refusal { status, message, stale_head } => Ok(refusal_json(status, &message, stale_head)?),
        Refused::StalePin(version) => Ok(Response::json(
            409,
            serde_json::to_string(&json!({ "error": "pin changed since base_version", "version": version }))?,
        )),
        Refused::Fault(err) => Err(Failure::Db(err)),
    }
}

fn api_post(app: &App, data: &Fields) -> Answer {
    if data.get("body").is_none() {
        return Ok(json_error(400, "need a 'body'")?);
    }
    let signature = data.optional_text("signature")?;
    // only a signed write carries a nonce that means anything
    let nonce = if signature.is_some() { data.nonce()? } else { String::new() };
    let submission = Submission {
        area: data.text("area", "general")?,
        topic: data.text("topic", "general")?,
        sender: data.text("from", "anon")?,
        body: data.text("body", "")?,
        key: data.optional_text("key")?,
        signature,
        nonce: &nonce,
    };
    match submit_post(app, &submission) {
        Ok(id) => Ok(Response::json(200, serde_json::to_string(&json!({ "id": id }))?)),
        Err(r) => refused(r),
    }
}

fn api_archive(app: &App, data: &Fields) -> Answer {
    let area = data.text("area", "")?;
    let archived = data.flag("archived", true)?;
    match app.board.set_archived(area, archived) {
        Ok(changed) => Ok(Response::json(200, serde_json::to_string(&json!({ "changed": changed }))?)),
        Err(StoreError::NoSuchArea(_)) => Ok(json_error(404, "no such area")?),
        Err(StoreError::Db(err)) => Err(Failure::Db(err)),
        Err(err) => Ok(json_error(400, &err.to_string())?),
    }
}

fn api_pin(app: &App, data: &Fields) -> Answer {
    if data.get("area").is_none() || data.get("body").is_none() {
        return Ok(json_error(400, "need 'area' and 'body'")?);
    }
    let signature = data.optional_text("signature")?;
    let nonce = if signature.is_some() { data.nonce()? } else { String::new() };
    let save = PinSave {
        area: data.text("area", "")?,
        topic: data.text("topic", "")?,
        editor: data.text("from", "anon")?,
        body: data.text("body", "")?,
        base_version: data.base_version()?,
    };
    match submit_pin(app, &save, signature, &nonce) {
        Ok(version) => Ok(Response::json(200, serde_json::to_string(&json!({ "version": version }))?)),
        Err(r) => refused(r),
    }
}

fn api_areas(app: &App, data: &Fields) -> Answer {
    if data.get("name").is_none() {
        return Ok(json_error(400, "need a 'name'")?);
    }
    let name = data.text("name", "")?;
    let description = data.text("description", "")?;
    match app.board.create_area(name, description) {
        Ok(()) => Ok(Response::json(200, serde_json::to_string(&json!({ "ok": true }))?)),
        Err(StoreError::Db(err)) => Err(Failure::Db(err)),
        Err(err) => Ok(json_error(400, &err.to_string())?),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn targets_split_into_path_and_query() {
        assert_eq!(split_target("/poll?since=3&area=x"), ("/poll", "since=3&area=x"));
        assert_eq!(split_target("http://host:8777/msg/5?x#f"), ("/msg/5", "x"));
        assert_eq!(split_target("http://host:8777?x"), ("/", "x"));
        assert_eq!(split_target("/x#frag?no"), ("/x", ""));
    }

    #[test]
    fn queries_decode_the_standard_way() {
        let q = Params::parse(b"area=a+b%2Fc&topic=&since=7&area=second");
        assert_eq!(q.get("area"), Some("a b/c"));
        assert_eq!(q.or("topic", "general"), "general");
        assert!(matches!(number::<i64>(&q, "since", 0), Ok(7)));
        assert!(matches!(number::<i64>(&q, "missing", 3), Ok(3)));
        assert!(matches!(number::<i64>(&Params::parse(b"since=x"), "since", 0), Err(Failure::Bad(_))));
    }

    #[test]
    fn summaries_are_the_first_line_cut_at_200() {
        assert_eq!(summary("  \n first\nsecond"), "first");
        assert_eq!(summary(""), "");
        let long = "é".repeat(250);
        assert_eq!(summary(&long), format!("{}...", "é".repeat(197)));
    }

    #[test]
    fn a_watch_line_is_one_line_whatever_it_holds() {
        let m = message_board::store::Message {
            id: 7,
            ts: "2026-10-22T00:00:00Z".to_owned(),
            area: "x\n8\t[forged".to_owned(),
            topic: "t\r".to_owned(),
            sender: "a".to_owned(),
            body: "first\rhidden\nsecond".to_owned(),
            signed_by: None,
            verified: false,
        };
        assert_eq!(format_lines(&[m]), "7\t[x\\n8\\t[forged/t\\r] a: first\\rhidden\n");
    }
}
