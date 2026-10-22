//! The signed writes, read from what a request carries. The JSON API's
//! bodies and the web view's forms bring the same fields under the same
//! names (each action's `named()`), so one reading serves both: what the
//! client signed is rebuilt as the store's action, and the store checks
//! the signature over exactly that.

use std::borrow::Cow;
use std::str::FromStr;

use serde_json::{Map, Value};

use message_board::store::{
    Approve, Board, Create, Error as StoreError, File, Post, Register, Resolve, Revoke, SetNote, Signed,
};

use crate::routes::{Failure, Params};

/// A write, by the endpoint it is posted to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Write {
    Create,
    Post,
    Note,
    File,
    Resolve,
    Register,
    Approve,
    Revoke,
}

impl Write {
    /// The write posted to `/<name>`.
    pub fn named(name: &str) -> Option<Self> {
        Some(match name {
            "create" => Self::Create,
            "post" => Self::Post,
            "note" => Self::Note,
            "file" => Self::File,
            "resolve" => Self::Resolve,
            "register" => Self::Register,
            "approve" => Self::Approve,
            "revoke" => Self::Revoke,
            _ => return None,
        })
    }
}

/// A request's fields, by name. A field that is missing reads as empty,
/// as an absent field is signed.
pub trait Fields {
    /// A text field; `""` if it is missing.
    ///
    /// # Errors
    /// [`StoreError::BadRequest`] for a value that is not text.
    fn text(&self, name: &str) -> Result<&str, StoreError>;

    /// A number field; `None` if it is missing or empty.
    ///
    /// # Errors
    /// [`StoreError::BadRequest`] for a value that is not an integer.
    fn number(&self, name: &str) -> Result<Option<i64>, StoreError>;

    /// The nonce, as signed.
    ///
    /// # Errors
    /// As [`Fields::text`].
    fn nonce(&self) -> Result<Cow<'_, str>, StoreError> {
        self.text("nonce").map(Cow::Borrowed)
    }
}

fn not_a(name: &str, what: &str) -> StoreError {
    StoreError::BadRequest(format!("field {name:?} must be {what}"))
}

/// A decimal integer, as a number field's text.
fn integer(name: &str, text: &str) -> Result<Option<i64>, StoreError> {
    if text.is_empty() {
        return Ok(None);
    }
    text.parse().map(Some).map_err(|err| StoreError::BadRequest(format!("field {name:?} is not an integer: {err}")))
}

impl Fields for Params {
    fn text(&self, name: &str) -> Result<&str, StoreError> {
        Ok(self.get(name).unwrap_or_default())
    }

    fn number(&self, name: &str) -> Result<Option<i64>, StoreError> {
        integer(name, self.get(name).unwrap_or_default())
    }
}

/// A JSON request body's fields; `null` counts as missing.
///
/// A number field may come as a string of digits or as a JSON integer:
/// the signature covers its decimal text either way.
pub struct Json(Map<String, Value>);

impl Json {
    /// The fields of a JSON object; an empty body has none.
    ///
    /// # Errors
    /// [`StoreError::BadRequest`] for a body that is not a JSON object.
    pub fn parse(body: &[u8]) -> Result<Self, StoreError> {
        if body.is_empty() {
            return Ok(Self(Map::new()));
        }
        match serde_json::from_slice(body) {
            Ok(Value::Object(fields)) => Ok(Self(fields)),
            Ok(_) => Err(StoreError::BadRequest("the body must be a JSON object".to_owned())),
            Err(err) => Err(StoreError::BadRequest(format!("the body is not JSON: {err}"))),
        }
    }

    fn get(&self, name: &str) -> Option<&Value> {
        self.0.get(name).filter(|value| !value.is_null())
    }
}

impl Fields for Json {
    fn text(&self, name: &str) -> Result<&str, StoreError> {
        match self.get(name) {
            None => Ok(""),
            Some(Value::String(text)) => Ok(text),
            Some(_) => Err(not_a(name, "a string")),
        }
    }

    fn number(&self, name: &str) -> Result<Option<i64>, StoreError> {
        match self.get(name) {
            None => Ok(None),
            Some(Value::String(text)) => integer(name, text),
            Some(Value::Number(n)) => n.as_i64().map(Some).ok_or_else(|| not_a(name, "an integer")),
            Some(_) => Err(not_a(name, "an integer")),
        }
    }

    fn nonce(&self) -> Result<Cow<'_, str>, StoreError> {
        match self.get("nonce") {
            Some(Value::Number(n)) if n.is_i64() => Ok(Cow::Owned(n.to_string())),
            _ => self.text("nonce").map(Cow::Borrowed),
        }
    }
}

/// What every signed write carries besides its action's own fields.
struct Envelope<'a> {
    key: &'a str,
    nonce: Cow<'a, str>,
    signature: &'a str,
}

impl<'a> Envelope<'a> {
    /// The envelope of `fields`; [`Failure::Unsigned`] without a signature,
    /// before anything else is looked at: no write is taken unsigned.
    fn read(fields: &'a impl Fields) -> Result<Self, Failure> {
        let signature = fields.text("signature")?;
        if signature.trim().is_empty() {
            return Err(Failure::Unsigned);
        }
        Ok(Self { key: fields.text("key")?, nonce: fields.nonce()?, signature })
    }

    fn with<A>(&self, action: A) -> Signed<'_, A> {
        Signed { action, key: self.key, nonce: &self.nonce, signature: self.signature }
    }
}

/// A field that names one of the store's enums.
fn parsed<T: FromStr<Err = String>>(fields: &impl Fields, name: &str) -> Result<T, StoreError> {
    fields.text(name)?.parse().map_err(|err| StoreError::BadRequest(format!("field {name:?}: {err}")))
}

fn required(fields: &impl Fields, name: &str) -> Result<i64, StoreError> {
    fields.number(name)?.ok_or_else(|| StoreError::BadRequest(format!("field {name:?} is needed")))
}

/// Do `write` as `fields` say; what the client is told, as JSON.
///
/// # Errors
/// [`Failure::Unsigned`] without a signature, the store's refusal, and an
/// outcome that does not encode.
pub fn apply(board: &Board, write: Write, fields: &impl Fields) -> Result<Value, Failure> {
    let envelope = Envelope::read(fields)?;
    let text = |name| fields.text(name);
    let outcome = match write {
        Write::Create => {
            let create = Create {
                path: text("path")?,
                kind: parsed(fields, "kind")?,
                description: text("description")?,
                owner: text("owner")?,
                body: text("body")?,
            };
            serde_json::to_value(board.create(&envelope.with(create))?)
        }
        Write::Post => {
            let post = Post { path: text("path")?, body: text("body")? };
            serde_json::to_value(board.post(&envelope.with(post))?)
        }
        Write::Note => {
            let save = SetNote {
                path: text("path")?,
                body: text("body")?,
                base_version: required(fields, "base_version")?,
                through: fields.number("through")?,
            };
            serde_json::to_value(board.set_note(&envelope.with(save))?)
        }
        Write::File => {
            let file = File { path: text("path")?, body: text("body")? };
            serde_json::to_value(board.file(&envelope.with(file))?)
        }
        Write::Resolve => {
            let resolve = Resolve {
                item: required(fields, "item")?,
                state: parsed(fields, "state")?,
                body: text("body")?,
                reference: fields.number("ref")?,
            };
            serde_json::to_value(board.resolve(&envelope.with(resolve))?)
        }
        Write::Register => {
            let register = Register { name: text("name")?, note: text("note")?, repo: text("repo")? };
            serde_json::to_value(board.register(&envelope.with(register))?)
        }
        Write::Approve => {
            let approve =
                Approve { item: required(fields, "item")?, name: text("name")?, fingerprint: text("fingerprint")? };
            serde_json::to_value(board.approve(&envelope.with(approve))?)
        }
        Write::Revoke => {
            let revoke = Revoke { fingerprint: text("fingerprint")?, reason: text("reason")? };
            serde_json::to_value(board.revoke(&envelope.with(revoke))?)
        }
    };
    Ok(outcome?)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn json(text: &str) -> Result<Json, StoreError> {
        Json::parse(text.as_bytes())
    }

    #[test]
    fn a_number_field_is_digits_or_an_integer_and_empty_is_none() -> Result<(), StoreError> {
        let fields = json(r#"{"item": "10022", "ref": 10023, "through": "", "base_version": null}"#)?;
        assert_eq!(fields.number("item")?, Some(10_022));
        assert_eq!(fields.number("ref")?, Some(10_023));
        assert_eq!(fields.number("through")?, None);
        assert_eq!(fields.number("base_version")?, None);
        assert_eq!(fields.number("missing")?, None);
        for bad in [r#"{"item": "ten"}"#, r#"{"item": 1.5}"#, r#"{"item": true}"#] {
            assert!(json(bad)?.number("item").is_err(), "{bad}");
        }
        let form = Params::parse(b"item=7&ref=");
        assert_eq!((form.number("item")?, form.number("ref")?), (Some(7), None));
        Ok(())
    }

    #[test]
    fn text_is_a_string_and_the_nonce_may_be_a_number() -> Result<(), StoreError> {
        let fields = json(r#"{"body": "hi", "path": 3, "nonce": 10041}"#)?;
        assert_eq!(fields.text("body")?, "hi");
        assert_eq!(fields.text("missing")?, "");
        assert!(fields.text("path").is_err());
        assert_eq!(fields.nonce()?, "10041");
        assert_eq!(json(r#"{"nonce": "10041"}"#)?.nonce()?, "10041");
        assert!(json("[1]").is_err() && json("{").is_err());
        assert_eq!(json("")?.text("signature")?, "");
        Ok(())
    }
}
