//! HTTP to board-server: one fresh connection per request, so that a
//! request the board never answered is never quietly sent again by a
//! connection pool.

use std::io::Read;
use std::time::Duration;

use serde_json::Value;
use ureq::{Agent, SendBody};

/// A request that did not get a 2xx answer.
#[derive(Debug)]
pub enum HttpError {
    /// The board answered, with this status.
    Status { status: u16, reason: String, body: String, board_error: Option<String> },
    /// No answer: the board is down, the connection broke, it timed out.
    Transport(ureq::Error),
}

pub struct Client {
    pub base: String,
}

/// An agent whose every connect, read and write may take `timeout`.
fn agent(timeout: Duration) -> Agent {
    Agent::config_builder()
        .http_status_as_error(false)
        .max_idle_connections(0)
        .max_idle_connections_per_host(0)
        .timeout_connect(Some(timeout))
        .timeout_per_read(Some(timeout))
        .timeout_per_write(Some(timeout))
        .build()
        .new_agent()
}

/// A non-2xx answer, its body read whole.
fn refusal(mut response: ureq::http::Response<ureq::Body>) -> HttpError {
    let status = response.status();
    let board_error =
        response.headers().get("Board-Error").map(|value| String::from_utf8_lossy(value.as_bytes()).into_owned());
    match response.body_mut().with_config().limit(u64::MAX).read_to_string() {
        Ok(body) => HttpError::Status {
            status: status.as_u16(),
            reason: status.canonical_reason().unwrap_or_default().to_owned(),
            body,
            board_error,
        },
        Err(err) => HttpError::Transport(err),
    }
}

fn answer(response: Result<ureq::http::Response<ureq::Body>, ureq::Error>) -> Result<String, HttpError> {
    let mut response = response.map_err(HttpError::Transport)?;
    if !response.status().is_success() {
        return Err(refusal(response));
    }
    response.body_mut().with_config().limit(u64::MAX).read_to_string().map_err(HttpError::Transport)
}

/// A download that is on its way: the body as it arrives.
pub struct Download {
    pub reader: Box<dyn Read>,
}

impl Client {
    /// PUT `body` to `path` with `headers`; the board's answer. The caller
    /// says `Content-Length` and `Expect: 100-continue` among the headers:
    /// a refusal that comes before the body is then read from the answer
    /// instead of from a connection reset mid-upload.
    pub fn put(
        &self,
        path: &str,
        headers: &[(&str, String)],
        body: impl Read,
        timeout: Duration,
    ) -> Result<String, HttpError> {
        let mut request = agent(timeout).put(format!("{}{path}", self.base));
        for (name, value) in headers {
            request = request.header(*name, value);
        }
        let mut body = body;
        answer(request.send(SendBody::from_reader(&mut body)))
    }

    /// GET `path`, the body left to the caller to read as it arrives.
    pub fn open(&self, path: &str, timeout: Duration) -> Result<Download, HttpError> {
        let response = agent(timeout).get(format!("{}{path}", self.base)).call().map_err(HttpError::Transport)?;
        if !response.status().is_success() {
            return Err(refusal(response));
        }
        let reader = response.into_body().into_with_config().limit(u64::MAX).reader();
        Ok(Download { reader: Box::new(reader) })
    }

    pub fn get(&self, path: &str, timeout: Duration) -> Result<String, HttpError> {
        answer(agent(timeout).get(format!("{}{path}", self.base)).call())
    }

    pub fn post(&self, path: &str, data: &Value, timeout: Duration) -> Result<String, HttpError> {
        let body = serde_json::to_vec(data).map_err(|err| HttpError::Transport(ureq::Error::Other(Box::new(err))))?;
        answer(
            agent(timeout)
                .post(format!("{}{path}", self.base))
                .header("content-type", "application/json")
                .send(&body[..]),
        )
    }
}
