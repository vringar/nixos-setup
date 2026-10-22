//! HTTP to board-server: one fresh connection per request, so that a
//! request the board never answered is never quietly sent again by a
//! connection pool.

use std::time::Duration;

use serde_json::Value;
use ureq::Agent;

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

fn answer(response: Result<ureq::http::Response<ureq::Body>, ureq::Error>) -> Result<String, HttpError> {
    let mut response = response.map_err(HttpError::Transport)?;
    let status = response.status();
    let board_error =
        response.headers().get("Board-Error").map(|value| String::from_utf8_lossy(value.as_bytes()).into_owned());
    let body = response.body_mut().with_config().limit(u64::MAX).read_to_string().map_err(HttpError::Transport)?;
    if status.is_success() {
        Ok(body)
    } else {
        Err(HttpError::Status {
            status: status.as_u16(),
            reason: status.canonical_reason().unwrap_or_default().to_owned(),
            body,
            board_error,
        })
    }
}

impl Client {
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
