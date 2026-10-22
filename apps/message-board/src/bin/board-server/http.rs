//! Just enough HTTP/1.1 for the board, over a plain `TcpStream`: one
//! request at a time per connection, bodies by Content-Length, keep-alive
//! as HTTP/1.1 and 1.0 define it, and every protocol error named, so that
//! it can be logged and answered.

use std::io::{self, BufRead as _, BufReader, Read as _, Write};
use std::net::TcpStream;
use std::num::ParseIntError;

use crate::html::Html;

/// The most a request line and its headers may take.
pub const MAX_HEAD: usize = 64 * 1024;
/// The largest request body taken; a pin or post is far smaller.
pub const MAX_BODY: u64 = 16 * 1024 * 1024;
const MAX_HEADERS: usize = 100;

/// Every HTML response's policy. No inline script: board-sign.js holds a
/// private key in memory, so nothing a message body could smuggle into the
/// page may run. Inline styles stay allowed for the page's own <style>.
const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; \
                   object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

pub struct Request {
    pub method: String,
    /// The request target, as sent.
    pub target: String,
    pub body: Vec<u8>,
    /// Whether the connection ends after this request's response.
    pub close: bool,
}

#[derive(Debug, thiserror::Error)]
pub enum ProtocolError {
    #[error("{0}")]
    Io(#[from] io::Error),
    #[error("the connection closed in the middle of a request")]
    Truncated,
    #[error("the request line and headers are over {MAX_HEAD} bytes")]
    HeadTooLarge,
    #[error("malformed request: {0}")]
    Malformed(#[from] httparse::Error),
    #[error("bad Content-Length {value:?}: {cause}")]
    BadLength { value: String, cause: ParseIntError },
    #[error("conflicting Content-Length headers")]
    ConflictingLength,
    #[error("a request body of {0} bytes is over the {MAX_BODY}-byte limit")]
    BodyTooLarge(u64),
    #[error("chunked request bodies are not supported")]
    Chunked,
}

impl ProtocolError {
    /// The status to answer with, or `None` when there is no one left to
    /// answer.
    pub fn status(&self) -> Option<u16> {
        match self {
            Self::Io(_) | Self::Truncated => None,
            Self::HeadTooLarge | Self::Malformed(httparse::Error::TooManyHeaders) => Some(431),
            Self::Malformed(_) | Self::BadLength { .. } | Self::ConflictingLength => Some(400),
            Self::BodyTooLarge(_) => Some(413),
            Self::Chunked => Some(501),
        }
    }
}

/// The request line and headers, up to and including the empty line;
/// `None` if the client closed the connection before sending any.
fn read_head(reader: &mut BufReader<TcpStream>) -> Result<Option<Vec<u8>>, ProtocolError> {
    let mut head = Vec::new();
    loop {
        let start = head.len();
        let room = u64::try_from(MAX_HEAD.saturating_add(1).saturating_sub(start)).unwrap_or(0);
        let read = reader.by_ref().take(room).read_until(b'\n', &mut head)?;
        if read == 0 {
            return if head.is_empty() { Ok(None) } else { Err(ProtocolError::Truncated) };
        }
        if head.len() > MAX_HEAD {
            return Err(ProtocolError::HeadTooLarge);
        }
        match head.get(start..) {
            Some(b"\r\n" | b"\n") if start == 0 => head.clear(), // blank lines before a request
            Some(b"\r\n" | b"\n") => return Ok(Some(head)),
            Some(line) if !line.ends_with(b"\n") => return Err(ProtocolError::Truncated),
            _ => {}
        }
    }
}

/// The next request on a connection, or `None` once the client is done.
///
/// # Errors
/// If the connection fails, or the client sends what is not a request
/// this server takes.
pub fn read_request(reader: &mut BufReader<TcpStream>) -> Result<Option<Request>, ProtocolError> {
    let Some(head) = read_head(reader)? else {
        return Ok(None);
    };
    let mut headers = [httparse::EMPTY_HEADER; MAX_HEADERS];
    let mut parsed = httparse::Request::new(&mut headers);
    if parsed.parse(&head)?.is_partial() {
        return Err(ProtocolError::Truncated);
    }
    let method = parsed.method.unwrap_or_default().to_owned();
    let target = parsed.path.unwrap_or_default().to_owned();
    let http11 = parsed.version.unwrap_or(1) >= 1;

    let mut length: Option<u64> = None;
    let mut close = !http11;
    let mut expect_continue = false;
    for header in parsed.headers.iter() {
        let value = String::from_utf8_lossy(header.value);
        let value = value.trim();
        if header.name.eq_ignore_ascii_case("content-length") {
            let n =
                value.parse::<u64>().map_err(|cause| ProtocolError::BadLength { value: value.to_owned(), cause })?;
            if length.is_some_and(|earlier| earlier != n) {
                return Err(ProtocolError::ConflictingLength);
            }
            length = Some(n);
        } else if header.name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(ProtocolError::Chunked);
        } else if header.name.eq_ignore_ascii_case("connection") {
            if value.eq_ignore_ascii_case("close") {
                close = true;
            } else if value.eq_ignore_ascii_case("keep-alive") {
                close = false;
            }
        } else if header.name.eq_ignore_ascii_case("expect") {
            expect_continue = http11 && value.eq_ignore_ascii_case("100-continue");
        }
    }
    let length = length.unwrap_or(0);
    if length > MAX_BODY {
        return Err(ProtocolError::BodyTooLarge(length));
    }
    if expect_continue {
        let stream = reader.get_mut();
        stream.write_all(b"HTTP/1.1 100 Continue\r\n\r\n")?;
        stream.flush()?;
    }
    let mut body = Vec::new();
    reader.by_ref().take(length).read_to_end(&mut body)?;
    if u64::try_from(body.len()).ok() != Some(length) {
        return Err(ProtocolError::Truncated);
    }
    Ok(Some(Request { method, target, body, close }))
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        303 => "See Other",
        400 => "Bad Request",
        403 => "Forbidden",
        404 => "Not Found",
        409 => "Conflict",
        413 => "Content Too Large",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        503 => "Service Unavailable",
        _ => "",
    }
}

pub struct Response {
    status: u16,
    /// Headers before Content-Length, and after it.
    head: Vec<(&'static str, String)>,
    tail: Vec<(&'static str, String)>,
    body: Vec<u8>,
}

impl Response {
    pub fn bytes(status: u16, content_type: &'static str, body: Vec<u8>) -> Self {
        Self { status, head: vec![("Content-Type", content_type.to_owned())], tail: Vec::new(), body }
    }

    pub fn json(status: u16, body: String) -> Self {
        Self::bytes(status, "application/json", body.into_bytes())
    }

    pub fn text(status: u16, body: &str) -> Self {
        Self::bytes(status, "text/plain; charset=utf-8", body.as_bytes().to_vec())
    }

    pub fn html(status: u16, html: Html) -> Self {
        let mut response = Self::bytes(status, "text/html; charset=utf-8", html.into_bytes());
        response.head.push(("Content-Security-Policy", CSP.to_owned()));
        response
    }

    pub fn redirect(location: &str) -> Self {
        Self { status: 303, head: vec![("Location", location.to_owned())], tail: Vec::new(), body: Vec::new() }
    }

    #[must_use]
    pub fn with_header(mut self, name: &'static str, value: &str) -> Self {
        self.tail.push((name, value.to_owned()));
        self
    }

    /// Send it, with a Date header if there is a `date`.
    ///
    /// # Errors
    /// If the client has gone.
    pub fn write_to(&self, out: &mut impl Write, date: Option<&str>) -> io::Result<()> {
        let mut wire = format!("HTTP/1.1 {} {}\r\n", self.status, reason(self.status));
        if let Some(date) = date {
            wire.push_str("Date: ");
            wire.push_str(date);
            wire.push_str("\r\n");
        }
        let length = self.body.len().to_string();
        let headers = self.head.iter().map(|(n, v)| (*n, v.as_str())).chain([("Content-Length", length.as_str())]);
        for (name, value) in headers.chain(self.tail.iter().map(|(n, v)| (*n, v.as_str()))) {
            wire.push_str(name);
            wire.push_str(": ");
            wire.push_str(value);
            wire.push_str("\r\n");
        }
        wire.push_str("\r\n");
        let mut bytes = wire.into_bytes();
        bytes.extend_from_slice(&self.body);
        out.write_all(&bytes)?;
        out.flush()
    }
}
