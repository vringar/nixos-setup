//! The board's HTTP API as the commands use it: URLs, typed reads, and a
//! refusal turned into the [`Failure`] that says what the board said.

use std::time::Duration;

use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::client::{Client, Download, HttpError};
use crate::wire::{ErrorBody, Health};
use crate::{Failure, pending_hint};

/// A read's timeout. A write's is longer (see `write`), and a long poll's
/// is its own length plus a margin.
pub const TIMEOUT: Duration = Duration::from_secs(10);

/// The refusal codes of a key's identity (`store::Error::is_identity`):
/// `board` exits 3 on these, so that a launcher can tell them apart.
pub const IDENTITY_CODES: [&str; 4] =
    ["not-registered", "registration-pending", "registration-declined", "key-revoked"];

pub struct Api {
    client: Client,
}

impl Api {
    pub fn new(base: String) -> Self {
        Self { client: Client { base } }
    }

    /// GET `path`, as the board answered it.
    pub fn fetch(&self, path: &str, timeout: Duration) -> Result<String, HttpError> {
        self.client.get(path, timeout)
    }

    /// POST `data` to `path`, as the board answered it.
    pub fn send(&self, path: &str, data: &Value, timeout: Duration) -> Result<String, HttpError> {
        self.client.post(path, data, timeout)
    }

    /// PUT `body` to `path`, as the board answered it.
    pub fn put(
        &self,
        path: &str,
        headers: &[(&str, String)],
        body: impl std::io::Read,
        timeout: Duration,
    ) -> Result<String, HttpError> {
        self.client.put(path, headers, body, timeout)
    }

    /// GET `path`, its body read as it arrives.
    pub fn open(&self, path: &str, timeout: Duration) -> Result<Download, HttpError> {
        self.client.open(path, timeout)
    }

    /// The URL of `path` on this board, in full.
    pub fn absolute(&self, path: &str) -> String {
        format!("{}{path}", self.client.base)
    }

    /// GET `path`, read as a `T`.
    pub fn get<T: DeserializeOwned>(&self, path: &str) -> Result<T, Failure> {
        let text = self.fetch(path, TIMEOUT).map_err(|err| self.failure(err))?;
        parse(path, &text)
    }

    /// As [`Api::get`], but `None` where there is nothing at `path` (404).
    pub fn find<T: DeserializeOwned>(&self, path: &str) -> Result<Option<T>, Failure> {
        match self.fetch(path, TIMEOUT) {
            Ok(text) => parse(path, &text).map(Some),
            Err(HttpError::Status { status: 404, .. }) => Ok(None),
            Err(err) => Err(self.failure(err)),
        }
    }

    /// The board's head: the highest event id, and the nonce a write signs.
    pub fn head(&self) -> Result<i64, Failure> {
        Ok(self.get::<Health>("/healthz")?.head)
    }

    /// What a failed request says, as a [`Failure`].
    pub fn failure(&self, err: HttpError) -> Failure {
        match err {
            HttpError::Status { status, reason, body, board_error } => refusal(status, &reason, board_error, &body),
            HttpError::Transport(cause) => {
                Failure::Unreachable { url: self.client.base.clone(), cause: cause.to_string() }
            }
        }
    }
}

/// Whether a failed request is worth sending again unchanged: no answer at
/// all, or the board's own trouble (5xx: busy, too many connections, a
/// database fault) rather than a refusal of the request.
pub fn transient(err: &HttpError) -> bool {
    match err {
        HttpError::Transport(_) => true,
        HttpError::Status { status, .. } => *status >= 500,
    }
}

/// The refusal's code: the `Board-Error` header, else the body's `error`.
pub fn error_code(err: &HttpError) -> Option<String> {
    match err {
        HttpError::Status { board_error: Some(code), .. } => Some(code.clone()),
        HttpError::Status { body, .. } => error_body(body).and_then(|b| b.error),
        HttpError::Transport(_) => None,
    }
}

/// A refusal's JSON body. The board sends one with every refusal; a proxy
/// or a server too old to know this API may not, and then there is only
/// the status line to go by, so no body is not an error here.
fn error_body(body: &str) -> Option<ErrorBody> {
    serde_json::from_str(body).ok()
}

/// A refusal: its code and message, with a hint where the next step is not
/// in the message already.
fn refusal(status: u16, reason: &str, board_error: Option<String>, body: &str) -> Failure {
    let parsed = error_body(body);
    let (error, message, item) = parsed.map(|b| (b.error, b.message, b.item)).unwrap_or_default();
    let code = board_error.or(error).unwrap_or_else(|| status.to_string());
    let mut message = message.unwrap_or_else(|| reason.to_owned());
    if let ("registration-pending", Some(item)) = (code.as_str(), item) {
        message.push_str(&pending_hint(item));
    }
    Failure::Refused { code, message }
}

/// Read the board's answer to `what` as a `T`.
pub fn parse<T: DeserializeOwned>(what: &str, text: &str) -> Result<T, Failure> {
    serde_json::from_str(text).map_err(|err| Failure::BadAnswer { what: what.to_owned(), cause: err.to_string() })
}

/// A query string, `pairs` form-urlencoded, with a leading `?` unless empty.
pub fn query(pairs: &[(&str, String)]) -> String {
    if pairs.is_empty() {
        return String::new();
    }
    let encoded = form_urlencoded::Serializer::new(String::new()).extend_pairs(pairs).finish();
    format!("?{encoded}")
}

/// `/n/<path>`, each segment percent-encoded. A valid path needs none of
/// it; anything else reaches the board as typed, for it to refuse.
pub fn node_url(path: &str) -> String {
    let segments: Vec<String> =
        path.split('/').map(|s| form_urlencoded::byte_serialize(s.as_bytes()).collect()).collect();
    format!("/n/{}", segments.join("/"))
}

/// `/keys/<fingerprint>`: a fingerprint is base64, whose `/` and `+` would
/// otherwise split the path or read as a space.
pub fn key_url(fingerprint: &str) -> String {
    format!("/keys/{}", form_urlencoded::byte_serialize(fingerprint.as_bytes()).collect::<String>())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls_encode_what_would_change_their_meaning() {
        assert_eq!(node_url(""), "/n/");
        assert_eq!(node_url("reversing/emu/log"), "/n/reversing/emu/log");
        assert_eq!(node_url("a b/c?d#e"), "/n/a+b/c%3Fd%23e");
        assert_eq!(key_url("SHA256:ab/c+d"), "/keys/SHA256%3Aab%2Fc%2Bd");
        assert_eq!(query(&[]), "");
        assert_eq!(query(&[("path", "a/b".to_owned()), ("path", "c".to_owned())]), "?path=a%2Fb&path=c");
    }

    #[test]
    fn a_refusal_says_the_boards_code_and_message() {
        let refused = |status, header: Option<&str>, body: &str| {
            refusal(status, "Reason", header.map(str::to_owned), body).to_string()
        };
        assert_eq!(
            refused(
                403,
                Some("registration-pending"),
                r#"{"error":"registration-pending","message":"waits","item":7}"#
            ),
            "registration-pending: waits; `board wait 7` says when the owner has decided"
        );
        assert_eq!(refused(409, None, r#"{"error":"conflict","message":"exists"}"#), "conflict: exists");
        assert_eq!(refused(503, None, "busy, plain text"), "503: Reason");
    }

    /// The codes `board` exits 3 on are exactly the store's identity refusals.
    #[test]
    fn identity_codes_are_the_stores() {
        use message_board::store::Error;
        let identity = [
            Error::NotRegistered { fingerprint: String::new() },
            Error::RegistrationPending { item: 1 },
            Error::RegistrationDeclined { item: 1 },
            Error::KeyRevoked { revoked_at: 1 },
        ];
        for err in &identity {
            assert!(err.is_identity());
        }
        let codes: Vec<&str> = identity.iter().map(Error::code).collect();
        assert_eq!(codes, IDENTITY_CODES);
        assert!(!Error::Forbidden(String::new()).is_identity());
    }
}
