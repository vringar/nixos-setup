//! The writes clients sign: each action's fields in signing order, what
//! the client is told when one lands, and the steps every signed write
//! goes through (see the module docs of `store`).

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD_NO_PAD;
use rusqlite::{Connection, OptionalExtension as _};
use serde::Serialize;
use sha2::{Digest as _, Sha256};

use crate::sign::{self, Kind, PrivateKey, SignatureError, Verified};

use super::{Board, Error, ItemState, NodeKind, SERVER_NAME, head, opt_text, quoted};

/// A write a client signs.
///
/// Every kind of write signs its own fields and then `key` and `nonce`
/// ([`sign::KINDS`] all end so), which [`Signed`] carries for all of them.
pub trait Action {
    /// The namespace and field order this write is signed with.
    const KIND: Kind;

    /// This write's own fields, in signing order: `KIND.fields` without
    /// `key` and `nonce`. Numbers are in decimal; a field the write leaves
    /// out is the empty string.
    fn fields(&self) -> Vec<String>;

    /// Every value `KIND` signs, in order.
    fn values(&self, key: &str, nonce: &str) -> Vec<String> {
        let mut values = self.fields();
        values.push(key.to_owned());
        values.push(nonce.to_owned());
        values
    }

    /// Every signed field by name, in order: the JSON object a client
    /// sends, before it adds `signature`.
    fn named(&self, key: &str, nonce: &str) -> Vec<(&'static str, String)> {
        Self::KIND.fields.iter().copied().zip(self.values(key, nonce)).collect()
    }

    /// This write's signature by `private`, with the client key `key`,
    /// signed against the head `nonce`.
    ///
    /// # Errors
    /// Only if the fields are too long for the wire format.
    fn sign(&self, private: &PrivateKey, key: &str, nonce: &str) -> Result<String, SignatureError> {
        private.sign(&Self::KIND, &as_strs(&self.values(key, nonce)))
    }
}

fn as_strs(values: &[String]) -> Vec<&str> {
    values.iter().map(String::as_str).collect()
}

/// A write as it arrives: the action, and what every signed write carries.
#[derive(Debug, Clone, Copy)]
pub struct Signed<'a, A> {
    pub action: A,
    /// The client's key for this write, unique per write (a UUID). A resend
    /// with the same key and the same request returns the first result; with
    /// another request it is a conflict. Empty: no key, so a resend of a write
    /// that landed is refused as stale, or lands twice if signed afresh.
    pub key: &'a str,
    /// The head the client signed against.
    pub nonce: &'a str,
    /// The SSHSIG over [`Action::values`], base64.
    pub signature: &'a str,
}

/// Make a node. A note may get its first version with it.
#[derive(Debug, Clone, Copy)]
pub struct Create<'a> {
    pub path: &'a str,
    pub kind: NodeKind,
    /// One line, shown in listings.
    pub description: &'a str,
    /// The name that resolves a queue's items; empty for the creator's own
    /// name. Only queues have one.
    pub owner: &'a str,
    /// A note's first version; empty for none. Only notes have one.
    pub body: &'a str,
}

impl Action for Create<'_> {
    const KIND: Kind = sign::CREATE;
    fn fields(&self) -> Vec<String> {
        [self.path, self.kind.as_str(), self.description, self.owner, self.body].map(str::to_owned).into()
    }
}

/// Append a message to a thread.
#[derive(Debug, Clone, Copy)]
pub struct Post<'a> {
    pub path: &'a str,
    pub body: &'a str,
}

impl Action for Post<'_> {
    const KIND: Kind = sign::POST;
    fn fields(&self) -> Vec<String> {
        [self.path, self.body].map(str::to_owned).into()
    }
}

/// Save a new version of a note.
#[derive(Debug, Clone, Copy)]
pub struct SetNote<'a> {
    pub path: &'a str,
    pub body: &'a str,
    /// The version the editor started from (0 for a note without one); a
    /// save over any other is refused, so nobody overwrites an edit unseen.
    pub base_version: i64,
    /// The thread this version summarizes (a path; empty for none: not
    /// every note is a summary).
    pub thread: &'a str,
    /// The message of `thread` it summarizes everything up to; needs `thread`.
    pub through: Option<i64>,
}

impl Action for SetNote<'_> {
    const KIND: Kind = sign::NOTE;
    fn fields(&self) -> Vec<String> {
        vec![
            self.path.to_owned(),
            self.body.to_owned(),
            self.base_version.to_string(),
            self.thread.to_owned(),
            decimal(self.through),
        ]
    }
}

/// File an item in a queue.
#[derive(Debug, Clone, Copy)]
pub struct File<'a> {
    pub path: &'a str,
    pub body: &'a str,
}

impl Action for File<'_> {
    const KIND: Kind = sign::FILE;
    fn fields(&self) -> Vec<String> {
        [self.path, self.body].map(str::to_owned).into()
    }
}

/// Answer, decline or cancel an open item.
#[derive(Debug, Clone, Copy)]
pub struct Resolve<'a> {
    pub item: i64,
    /// `Answered`, `Declined` or `Cancelled`.
    pub state: ItemState,
    /// Needed to answer or decline; optional to cancel.
    pub body: &'a str,
    /// An answer's message: an existing `message` event.
    pub reference: Option<i64>,
}

impl Action for Resolve<'_> {
    const KIND: Kind = sign::RESOLVE;
    fn fields(&self) -> Vec<String> {
        vec![self.item.to_string(), self.state.as_str().to_owned(), self.body.to_owned(), decimal(self.reference)]
    }
}

/// Ask the owner to bind `name` to the signing key.
#[derive(Debug, Clone, Copy)]
pub struct Register<'a> {
    pub name: &'a str,
    /// What the key is for, for the owner to judge by.
    pub note: &'a str,
    /// The repository the key was found in; informational.
    pub repo: &'a str,
}

impl Action for Register<'_> {
    const KIND: Kind = sign::REGISTER;
    fn fields(&self) -> Vec<String> {
        [self.name, self.note, self.repo].map(str::to_owned).into()
    }
}

/// The owner binds a registration's name to its key. Signed over the
/// fingerprint: what the owner approves is the key, not just the name.
#[derive(Debug, Clone, Copy)]
pub struct Approve<'a> {
    pub item: i64,
    pub name: &'a str,
    pub fingerprint: &'a str,
}

impl Action for Approve<'_> {
    const KIND: Kind = sign::APPROVE;
    fn fields(&self) -> Vec<String> {
        vec![self.item.to_string(), self.name.to_owned(), self.fingerprint.to_owned()]
    }
}

/// The owner withdraws a key's trust.
#[derive(Debug, Clone, Copy)]
pub struct Revoke<'a> {
    pub fingerprint: &'a str,
    pub reason: &'a str,
}

impl Action for Revoke<'_> {
    const KIND: Kind = sign::REVOKE;
    fn fields(&self) -> Vec<String> {
        [self.fingerprint, self.reason].map(str::to_owned).into()
    }
}

/// The owner hides a message or an item (`archived`), or shows it again.
#[derive(Debug, Clone, Copy)]
pub struct Archive<'a> {
    /// The message, or the item (which is its filing's id).
    pub event: i64,
    /// Signed as `1` or `0`.
    pub archived: bool,
    /// Why; at most [`ARCHIVE_REASON_MAX`] bytes.
    pub reason: &'a str,
}

/// The longest reason for an archive, in bytes.
pub const ARCHIVE_REASON_MAX: usize = 200;

impl Action for Archive<'_> {
    const KIND: Kind = sign::ARCHIVE;
    fn fields(&self) -> Vec<String> {
        vec![self.event.to_string(), u8::from(self.archived).to_string(), self.reason.to_owned()]
    }
}

/// Upload a blob: the fields the uploader signs before the first byte.
#[derive(Debug, Clone, Copy)]
pub struct PutBlob<'a> {
    /// The address: 64 lowercase hex digits.
    pub sha256: &'a str,
    pub size: u64,
    /// As [`crate::blob::media_type`] makes it.
    pub media_type: &'a str,
    /// The original file name, decoded; may be empty.
    pub filename: &'a str,
}

impl Action for PutBlob<'_> {
    const KIND: Kind = sign::BLOB;
    fn fields(&self) -> Vec<String> {
        vec![self.sha256.to_owned(), self.size.to_string(), self.media_type.to_owned(), self.filename.to_owned()]
    }
}

/// Delete a blob that nothing pins.
#[derive(Debug, Clone, Copy)]
pub struct DeleteBlob<'a> {
    pub sha256: &'a str,
}

impl Action for DeleteBlob<'_> {
    const KIND: Kind = sign::BLOBDEL;
    fn fields(&self) -> Vec<String> {
        vec![self.sha256.to_owned()]
    }
}

/// A number as signed: decimal, or empty for none.
fn decimal(n: Option<i64>) -> String {
    n.map(|n| n.to_string()).unwrap_or_default()
}

/// A node made: the id is its `node_created` event's.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Created {
    pub path: String,
    pub kind: NodeKind,
    pub id: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Posted {
    pub id: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct NoteSaved {
    pub path: String,
    pub version: i64,
    pub id: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Filed {
    pub item: i64,
    pub path: String,
}

/// An item resolved; the id is the resolving event's.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Resolved {
    pub item: i64,
    pub state: ItemState,
    pub id: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Registered {
    pub item: i64,
    pub name: String,
    pub fingerprint: String,
}

/// An archive or unarchive done: `id` is the event that did it, or `None`
/// when the target already was as asked and nothing was written.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Archived {
    pub id: Option<i64>,
    /// The message or item.
    pub event: i64,
    /// Whether it is archived now (`archived` on the wire).
    #[serde(rename = "archived")]
    pub hidden: bool,
}

/// A key revoked.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Revoked {
    pub fingerprint: String,
    pub name: String,
    /// The trust list version that revoked it: the `revoked_at` every event
    /// the key signed shows from then on.
    pub id: i64,
}

/// Who a write is from, as each event of it records.
pub(super) trait Writer {
    /// The name, or `None` for a key that has none yet (a registration).
    fn actor(&self) -> Option<&str>;
    fn fingerprint(&self) -> &str;
}

impl Writer for Verified {
    fn actor(&self) -> Option<&str> {
        None
    }
    fn fingerprint(&self) -> &str {
        &self.fingerprint
    }
}

/// What every event of one write is stamped with.
#[derive(Debug, Clone, Copy)]
pub(super) struct Stamp<'a> {
    pub now: &'a str,
    pub actor: Option<&'a str>,
    pub actor_fp: Option<&'a str>,
    pub signature: Option<&'a str>,
    /// The client key and request hash. Only the write's first event gets
    /// them: it is the one a resend is matched to, and its id the one the
    /// outcome is rebuilt from.
    pub keyed: Option<(&'a str, &'a str)>,
}

impl<'a> Stamp<'a> {
    /// The board's own events: actor `board`, no key, no signature.
    pub fn board(now: &'a str) -> Self {
        Self { now, actor: Some(SERVER_NAME), actor_fp: None, signature: None, keyed: None }
    }

    /// The stamp for the write's further events.
    pub fn later(&self) -> Self {
        Self { keyed: None, ..*self }
    }
}

/// The hash a resend is recognized by: the request without its nonce, which
/// a resend may have re-signed with, and with its signer, so that another
/// key reusing a client key is a conflict, not told of a write it did not
/// make.
fn request_hash(kind: &Kind, values: &[String], fingerprint: &str) -> String {
    let fields =
        kind.fields.iter().copied().zip(values.iter().map(String::as_str)).filter(|(name, _)| *name != "nonce");
    let mut hasher = Sha256::new();
    hasher.update(sign::encode([("namespace", kind.namespace), ("signer", fingerprint)]));
    hasher.update(sign::encode(fields));
    STANDARD_NO_PAD.encode(hasher.finalize())
}

/// The first event of the write that used `key`, if it was this request.
fn replay(conn: &Connection, key: &str, hash: &str) -> Result<Option<i64>, Error> {
    let earlier: Option<(i64, Option<String>)> = conn
        .query_row("SELECT id, request_hash FROM events WHERE client_key=?", [key], |r| {
            Ok((r.get(0)?, opt_text(r, 1)?))
        })
        .optional()?;
    match earlier {
        None => Ok(None),
        Some((id, Some(earlier))) if earlier == hash => Ok(Some(id)),
        Some(_) => Err(Error::Conflict(format!("key {} was already used for a different request", quoted(key)))),
    }
}

pub(super) fn check_nonce(conn: &Connection, nonce: &str) -> Result<(), Error> {
    let head = head(conn)?;
    if nonce != head.to_string() {
        return Err(Error::StaleNonce { head });
    }
    Ok(())
}

/// Verify the signature of `signed` over the very fields it carries.
pub(super) fn verify<A: Action>(signed: &Signed<'_, A>) -> Result<Verified, Error> {
    let values = signed.action.values(signed.key, signed.nonce);
    sign::verify(&A::KIND, &as_strs(&values), signed.signature).map_err(Error::BadSignature)
}

impl Board {
    /// A signed write, through the steps in the `store` docs: verify,
    /// replay, `admit`, nonce, then `apply`, which returns the id of the
    /// write's first event. `outcome` turns that id into what the client is
    /// told, for a fresh write and a resend alike, so both get the same.
    pub(super) fn signed<A: Action, W: Writer, T>(
        &self,
        signed: &Signed<'_, A>,
        admit: impl FnOnce(&Connection, Verified) -> Result<W, Error>,
        apply: impl FnOnce(&Connection, &Stamp<'_>, &W) -> Result<i64, Error>,
        outcome: impl FnOnce(&Connection, i64) -> Result<T, Error>,
    ) -> Result<T, Error> {
        let values = signed.action.values(signed.key, signed.nonce);
        let verified = verify(signed)?;
        let hash = request_hash(&A::KIND, &values, &verified.fingerprint);
        let key = Some(signed.key).filter(|key| !key.is_empty());
        self.write(|conn, now| {
            if let Some(key) = key
                && let Some(id) = replay(conn, key, &hash)?
            {
                return outcome(conn, id);
            }
            let writer = admit(conn, verified)?;
            check_nonce(conn, signed.nonce)?;
            let stamp = Stamp {
                now,
                actor: writer.actor(),
                actor_fp: Some(writer.fingerprint()),
                signature: Some(signed.signature),
                keyed: key.map(|key| (key, hash.as_str())),
            };
            let id = apply(conn, &stamp, &writer)?;
            outcome(conn, id)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// [`Action::values`] appends key and nonce: true only while every
    /// kind's fields end with them.
    #[test]
    fn every_kind_ends_with_key_and_nonce() {
        for kind in sign::KINDS {
            assert!(kind.fields.ends_with(&["key", "nonce"]), "{}", kind.namespace);
        }
    }

    #[test]
    fn every_action_has_one_value_per_field() {
        let create = Create { path: "a", kind: NodeKind::Note, description: "", owner: "", body: "" };
        let note = SetNote { path: "a/n", body: "", base_version: 0, thread: "a/t", through: Some(7) };
        let resolve = Resolve { item: 1, state: ItemState::Answered, body: "", reference: None };
        let counts = [
            (create.values("k", "1").len(), Create::KIND),
            (Post { path: "a/t", body: "" }.values("k", "1").len(), Post::KIND),
            (note.values("k", "1").len(), SetNote::KIND),
            (File { path: "a/q", body: "" }.values("k", "1").len(), File::KIND),
            (resolve.values("k", "1").len(), Resolve::KIND),
            (Register { name: "n", note: "", repo: "" }.values("k", "1").len(), Register::KIND),
            (Approve { item: 1, name: "n", fingerprint: "f" }.values("k", "1").len(), Approve::KIND),
            (Revoke { fingerprint: "f", reason: "" }.values("k", "1").len(), Revoke::KIND),
            (Archive { event: 1, archived: true, reason: "" }.values("k", "1").len(), Archive::KIND),
            (PutBlob { sha256: "h", size: 1, media_type: "a/b", filename: "" }.values("k", "1").len(), PutBlob::KIND),
            (DeleteBlob { sha256: "h" }.values("k", "1").len(), DeleteBlob::KIND),
        ];
        for (count, kind) in counts {
            assert_eq!(count, kind.fields.len(), "{}", kind.namespace);
        }
        assert_eq!(note.fields(), ["a/n", "", "0", "a/t", "7"]);
        let named = note.named("k", "9");
        assert_eq!(named.first(), Some(&("path", "a/n".to_owned())));
        assert_eq!(named.last(), Some(&("nonce", "9".to_owned())));
        assert_eq!(resolve.fields(), ["1", "answered", "", ""]);
        assert_eq!(Archive { event: 10_360, archived: true, reason: "noise" }.fields(), ["10360", "1", "noise"]);
        assert_eq!(Archive { event: 10_360, archived: false, reason: "" }.fields(), ["10360", "0", ""]);
    }

    #[test]
    fn a_request_hash_leaves_out_the_nonce_only() {
        let values = |nonce: &str| Post { path: "a/t", body: "hi" }.values("k", nonce);
        let hash = |values: &[String], fp| request_hash(&sign::POST, values, fp);
        assert_eq!(hash(&values("1"), "fp"), hash(&values("2"), "fp"));
        assert_ne!(hash(&values("1"), "fp"), hash(&values("1"), "other"));
        assert_ne!(hash(&values("1"), "fp"), hash(&Post { path: "a/t", body: "ho" }.values("k", "1"), "fp"));
        assert_ne!(hash(&values("1"), "fp"), request_hash(&sign::FILE, &values("1"), "fp"));
    }
}
