//! Just enough HTTP/1.1 for the board, over a plain `TcpStream`: one
//! request at a time per connection, bodies by Content-Length, keep-alive
//! as HTTP/1.1 and 1.0 define it, and every protocol error named, so that
//! it can be logged and answered.

use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Seek as _, SeekFrom, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

use crate::html::Html;

/// The most a request line and its headers may take.
pub const MAX_HEAD: usize = 64 * 1024;
/// The largest request body taken. The largest message on the board so
/// far is 25 KB.
pub const MAX_BODY: u64 = 1024 * 1024;
const MAX_HEADERS: usize = 100;
/// How long a client may take to send the next request, and then to send
/// all of it: a stalled client must not hold a thread and a socket.
pub const CLIENT_TIMEOUT: Duration = Duration::from_secs(60);

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
    /// The Host and Origin headers, if sent (the first of each).
    pub host: Option<String>,
    pub origin: Option<String>,
    /// The Content-Length, if sent.
    pub length: Option<u64>,
    /// Whether the body of a streamed request is still unread, and the
    /// client waits for a `100 Continue` to send it.
    pub expect_continue: bool,
    /// Whether the body was left unread for the handler to stream
    /// ([`Body`]): `body` is empty then.
    pub streamed: bool,
    /// Every header, its name in lowercase.
    headers: Vec<(String, String)>,
}

impl Request {
    /// The first header called `name` (lowercase), as sent.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ProtocolError {
    #[error("{0}")]
    Io(io::Error),
    #[error("the request took over {} s to arrive", CLIENT_TIMEOUT.as_secs())]
    TimedOut,
    #[error("the connection closed in the middle of a request")]
    Truncated,
    #[error("the request line and headers are over {MAX_HEAD} bytes")]
    HeadTooLarge,
    #[error("malformed request: {0}")]
    Malformed(#[from] httparse::Error),
    #[error("bad Content-Length {0:?}: not a number of bytes")]
    BadLength(String),
    #[error("conflicting Content-Length headers")]
    ConflictingLength,
    #[error("a request body of {0} bytes is over the {MAX_BODY}-byte limit")]
    BodyTooLarge(u64),
    #[error("chunked request bodies are not supported")]
    Chunked,
}

impl From<io::Error> for ProtocolError {
    fn from(err: io::Error) -> Self {
        if err.kind() == io::ErrorKind::TimedOut { Self::TimedOut } else { Self::Io(err) }
    }
}

impl ProtocolError {
    /// The status to answer with, or `None` when there is no one left to
    /// answer.
    pub fn status(&self) -> Option<u16> {
        match self {
            Self::Io(_) | Self::Truncated => None,
            Self::TimedOut => Some(408),
            Self::HeadTooLarge | Self::Malformed(httparse::Error::TooManyHeaders) => Some(431),
            Self::Malformed(_) | Self::BadLength(_) | Self::ConflictingLength => Some(400),
            Self::BodyTooLarge(_) => Some(413),
            Self::Chunked => Some(501),
        }
    }

    /// The code the answer carries, as every refusal's does: in its JSON
    /// and in the `Board-Error` header.
    pub fn code(&self) -> &'static str {
        match self {
            Self::TimedOut => "timeout",
            Self::HeadTooLarge | Self::BodyTooLarge(_) | Self::Malformed(httparse::Error::TooManyHeaders) => {
                "too-large"
            }
            Self::Chunked => "not-implemented",
            Self::Io(_) | Self::Truncated | Self::Malformed(_) | Self::BadLength(_) | Self::ConflictingLength => {
                "bad-request"
            }
        }
    }
}

/// A connection's reading side, with a deadline for each read: a read
/// past it fails with [`io::ErrorKind::TimedOut`].
pub struct Timed<'a> {
    stream: &'a TcpStream,
    deadline: Instant,
}

impl<'a> Timed<'a> {
    pub fn new(stream: &'a TcpStream) -> Self {
        Self { stream, deadline: Instant::now() }
    }

    /// Let reads go on for `time` from now.
    pub fn allow(&mut self, time: Duration) {
        let now = Instant::now();
        self.deadline = now.checked_add(time).unwrap_or(now);
    }
}

impl Read for Timed<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let left = self.deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(io::ErrorKind::TimedOut.into());
        }
        self.stream.set_read_timeout(Some(left))?;
        // A socket read timeout surfaces as WouldBlock on Unix.
        let mut stream = self.stream;
        stream
            .read(buf)
            .map_err(|err| if err.kind() == io::ErrorKind::WouldBlock { io::ErrorKind::TimedOut.into() } else { err })
    }
}

/// The request line and headers, up to and including the empty line;
/// `None` if the client closed the connection before sending any.
fn read_head(reader: &mut impl BufRead) -> Result<Option<Vec<u8>>, ProtocolError> {
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

/// The next request on a connection, or `None` once the client is done;
/// `interim` takes a 100 Continue. A request that `streamed` says is
/// streamed (by method and target) is returned with its head only: no size
/// limit applies, no `100 Continue` is sent and no body is read, for its
/// handler to do after it has checked what it can.
///
/// # Errors
/// If the connection fails, or the client sends what is not a request
/// this server takes.
pub fn read_request(
    reader: &mut impl BufRead,
    interim: &mut impl Write,
    streamed: &dyn Fn(&str, &str) -> bool,
) -> Result<Option<Request>, ProtocolError> {
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
    let (mut host, mut origin) = (None, None);
    let mut headers = Vec::new();
    for header in parsed.headers.iter() {
        let value = String::from_utf8_lossy(header.value);
        let value = value.trim();
        headers.push((header.name.to_ascii_lowercase(), value.to_owned()));
        if header.name.eq_ignore_ascii_case("host") {
            host.get_or_insert_with(|| value.to_owned());
        } else if header.name.eq_ignore_ascii_case("origin") {
            origin.get_or_insert_with(|| value.to_owned());
        }
        if header.name.eq_ignore_ascii_case("content-length") {
            // Digits only: u64's parser also takes a leading '+'.
            let n = Some(value)
                .filter(|v| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit()))
                .and_then(|v| v.parse::<u64>().ok())
                .ok_or_else(|| ProtocolError::BadLength(value.to_owned()))?;
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
    if streamed(&method, &target) {
        let request = Request {
            method,
            target,
            body: Vec::new(),
            close,
            host,
            origin,
            length,
            expect_continue,
            streamed: true,
            headers,
        };
        return Ok(Some(request));
    }
    let length_read = length.unwrap_or(0);
    if length_read > MAX_BODY {
        return Err(ProtocolError::BodyTooLarge(length_read));
    }
    if expect_continue {
        interim.write_all(b"HTTP/1.1 100 Continue\r\n\r\n")?;
        interim.flush()?;
    }
    let mut body = Vec::new();
    reader.by_ref().take(length_read).read_to_end(&mut body)?;
    if u64::try_from(body.len()).ok() != Some(length_read) {
        return Err(ProtocolError::Truncated);
    }
    Ok(Some(Request {
        method,
        target,
        body,
        close,
        host,
        origin,
        length,
        expect_continue: false,
        streamed: false,
        headers,
    }))
}

/// The body of a streamed request, read a piece at a time by its handler.
pub struct Body<'r, 'a> {
    reader: &'r mut BufReader<Timed<'a>>,
    interim: &'r TcpStream,
    remaining: u64,
}

impl<'r, 'a> Body<'r, 'a> {
    /// The body of a request whose `length` bytes are still to be read.
    pub fn new(reader: &'r mut BufReader<Timed<'a>>, interim: &'r TcpStream, length: u64) -> Self {
        Self { reader, interim, remaining: length }
    }

    /// How much of the body is still unread; a handler that leaves any makes
    /// the connection close, since it is never drained.
    pub fn remaining(&self) -> u64 {
        self.remaining
    }

    /// Tell the client to send the body.
    ///
    /// # Errors
    /// If the client has gone.
    pub fn send_continue(&self) -> io::Result<()> {
        let mut out = self.interim;
        out.write_all(b"HTTP/1.1 100 Continue\r\n\r\n")?;
        out.flush()
    }

    /// Read some of the body, never past its end: the next piece must come
    /// within `idle`, and the whole by `deadline`. `Ok(0)` is the end of the
    /// body, or the client leaving before it ([`Body::remaining`] says which).
    ///
    /// # Errors
    /// [`io::ErrorKind::TimedOut`] past either time, or the connection's.
    pub fn read(&mut self, buf: &mut [u8], idle: Duration, deadline: Instant) -> io::Result<usize> {
        let room = usize::try_from(self.remaining).unwrap_or(usize::MAX).min(buf.len());
        let Some(buf) = buf.get_mut(..room).filter(|b| !b.is_empty()) else {
            return Ok(0);
        };
        self.reader.get_mut().allow(idle.min(deadline.saturating_duration_since(Instant::now())));
        let n = self.reader.read(buf)?;
        self.remaining = self.remaining.saturating_sub(u64::try_from(n).unwrap_or(0));
        Ok(n)
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        206 => "Partial Content",
        304 => "Not Modified",
        400 => "Bad Request",
        401 => "Unauthorized",
        403 => "Forbidden",
        404 => "Not Found",
        408 => "Request Timeout",
        409 => "Conflict",
        411 => "Length Required",
        413 => "Content Too Large",
        416 => "Range Not Satisfiable",
        429 => "Too Many Requests",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        501 => "Not Implemented",
        503 => "Service Unavailable",
        507 => "Insufficient Storage",
        _ => "",
    }
}

pub struct Response {
    status: u16,
    /// Headers before Content-Length, and after it.
    head: Vec<(&'static str, String)>,
    tail: Vec<(&'static str, String)>,
    body: Content,
}

/// What a response carries.
enum Content {
    Bytes(Vec<u8>),
    /// `len` bytes of a file from `offset`, copied as they are sent: a blob
    /// can be gigabytes.
    File {
        file: File,
        offset: u64,
        len: u64,
    },
}

impl Content {
    fn len(&self) -> u64 {
        match self {
            Self::Bytes(bytes) => u64::try_from(bytes.len()).unwrap_or(u64::MAX),
            Self::File { len, .. } => *len,
        }
    }
}

impl Response {
    pub fn bytes(status: u16, content_type: &'static str, body: Vec<u8>) -> Self {
        Self {
            status,
            head: vec![("Content-Type", content_type.to_owned())],
            tail: Vec::new(),
            body: Content::Bytes(body),
        }
    }

    /// `len` bytes of `file` from `offset`, as an opaque download.
    pub fn file(status: u16, file: File, offset: u64, len: u64) -> Self {
        let head = vec![("Content-Type", "application/octet-stream".to_owned())];
        Self { status, head, tail: Vec::new(), body: Content::File { file, offset, len } }
    }

    /// The same answer, which ends the connection.
    #[must_use]
    pub fn closing(self) -> Self {
        self.with_header("Connection", "close")
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

    #[must_use]
    pub fn with_header(mut self, name: &'static str, value: &str) -> Self {
        self.tail.push((name, value.to_owned()));
        self
    }

    /// Send it, with a Date header if there is a `date`.
    ///
    /// # Errors
    /// If the client has gone.
    pub fn write_to(&mut self, out: &mut impl Write, date: Option<&str>) -> io::Result<()> {
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
        // Head and body as they are, not copied into one buffer: a poll's
        // body can be megabytes.
        out.write_all(wire.as_bytes())?;
        match &mut self.body {
            Content::Bytes(bytes) => out.write_all(bytes)?,
            Content::File { file, offset, len } => {
                file.seek(SeekFrom::Start(*offset))?;
                let sent = io::copy(&mut file.take(*len), out)?;
                if sent != *len {
                    return Err(io::Error::new(io::ErrorKind::UnexpectedEof, "the file is shorter than its row says"));
                }
            }
        }
        out.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(wire: &[u8]) -> Result<Option<Request>, ProtocolError> {
        read_request(&mut io::BufReader::new(wire), &mut Vec::new(), &|_, _| false)
    }

    #[test]
    fn content_length_is_digits() -> Result<(), ProtocolError> {
        let request = parse(b"POST /post HTTP/1.1\r\nContent-Length: 2\r\n\r\nhi")?;
        assert_eq!(request.map(|r| r.body), Some(b"hi".to_vec()));
        for bad in ["+2", "-2", "2 2", "0x2", "", "99999999999999999999999"] {
            let wire = format!("POST /post HTTP/1.1\r\nContent-Length: {bad}\r\n\r\nhi");
            assert!(matches!(parse(wire.as_bytes()), Err(ProtocolError::BadLength(_))), "{bad:?}");
        }
        Ok(())
    }

    #[test]
    fn a_request_cut_short_is_truncated_and_none_is_none() {
        assert!(matches!(parse(b""), Ok(None)));
        assert!(matches!(parse(b"GET / HTTP/1.1\r\nHost: x"), Err(ProtocolError::Truncated)));
        assert!(matches!(parse(b"POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nhi"), Err(ProtocolError::Truncated)));
    }
}
