//! SSH signatures for board writes.
//!
//! `board` signs, board-server verifies. A signature is an OpenSSH SSHSIG
//! (PROTOCOL.sshsig: sha512, ed25519 keys only) over a canonical encoding of
//! the write's fields, so `ssh-keygen -Y verify` can check any signature the
//! board stores. It travels and is stored as the armored body's base64,
//! without the BEGIN/END lines.
//!
//! Canonical encoding: the fields in a fixed order, each as
//! `<name>:<utf-8 byte length>:<value>\n`. The length prefix is what makes
//! it unambiguous: a `name=value` line format cannot tell a body containing
//! "\nkey=x" from a real `key` field. A field the write leaves out is
//! signed as the empty string. static/board-sign.js implements the same
//! encoding and must stay byte-identical with this one.

use std::error::Error as StdError;
use std::fmt;
use std::fs;
use std::io::{self, Read as _};
use std::os::unix::fs::PermissionsExt as _;
use std::path::Path;

use base64::Engine as _;
use base64::engine::general_purpose::{STANDARD, STANDARD_NO_PAD};
use ed25519_dalek::{Signature, Signer as _, SigningKey, VerifyingKey};
use sha2::{Digest as _, Sha256, Sha512};

/// What a signature signs: its SSHSIG namespace, and its fields in signing
/// order. The namespace keeps a signature for one action from being
/// replayed as another; `nonce`, the board's head when signing, keeps it
/// from being replayed at all.
///
/// The namespaces start with `board2-`: the v2 board's event counter
/// started afresh, so a nonce from the v1 board could be current again,
/// and a v1 signature must never verify here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Kind {
    pub namespace: &'static str,
    pub fields: &'static [&'static str],
}

pub const CREATE: Kind =
    Kind { namespace: "board2-create", fields: &["path", "kind", "description", "owner", "body", "key", "nonce"] };
pub const POST: Kind = Kind { namespace: "board2-post", fields: &["path", "body", "key", "nonce"] };
pub const NOTE: Kind =
    Kind { namespace: "board2-note", fields: &["path", "body", "base_version", "thread", "through", "key", "nonce"] };
pub const FILE: Kind = Kind { namespace: "board2-file", fields: &["path", "body", "key", "nonce"] };
pub const RESOLVE: Kind =
    Kind { namespace: "board2-resolve", fields: &["item", "state", "body", "ref", "key", "nonce"] };
pub const REGISTER: Kind = Kind { namespace: "board2-register", fields: &["name", "note", "repo", "key", "nonce"] };
pub const APPROVE: Kind =
    Kind { namespace: "board2-approve", fields: &["item", "name", "fingerprint", "key", "nonce"] };
pub const REVOKE: Kind = Kind { namespace: "board2-revoke", fields: &["fingerprint", "reason", "key", "nonce"] };
/// Archive (`archived` is `1`) or unarchive (`0`) the message or item `event`.
pub const ARCHIVE: Kind =
    Kind { namespace: "board2-archive", fields: &["event", "archived", "reason", "key", "nonce"] };

/// Upload a blob: its address, size, media type and original file name.
pub const BLOB: Kind =
    Kind { namespace: "board2-blob", fields: &["sha256", "size", "media_type", "filename", "key", "nonce"] };
/// Delete a blob.
pub const BLOBDEL: Kind = Kind { namespace: "board2-blobdel", fields: &["sha256", "key", "nonce"] };

/// Every kind a board write can be signed as.
pub const KINDS: [Kind; 11] = [CREATE, POST, NOTE, FILE, RESOLVE, REGISTER, APPROVE, REVOKE, ARCHIVE, BLOB, BLOBDEL];

const MAGIC: &[u8] = b"SSHSIG";
const HASH: &str = "sha512";
const KEY_TYPE: &[u8] = b"ssh-ed25519";
const PRIVATE_MAGIC: &[u8] = b"openssh-key-v1\0";
const PEM_BEGIN: &str = "-----BEGIN OPENSSH PRIVATE KEY-----";
const PEM_END: &str = "-----END OPENSSH PRIVATE KEY-----";

/// A key or signature the board cannot use, and why. The message is what
/// clients see; the cause, when there is one, stays attached for the log.
#[derive(Debug)]
pub struct SignatureError {
    message: String,
    cause: Option<Box<dyn StdError + Send + Sync>>,
}

impl SignatureError {
    fn new(message: impl Into<String>) -> Self {
        Self { message: message.into(), cause: None }
    }

    fn caused(message: impl Into<String>, cause: impl StdError + Send + Sync + 'static) -> Self {
        Self { message: message.into(), cause: Some(Box::new(cause)) }
    }
}

impl fmt::Display for SignatureError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl StdError for SignatureError {
    fn source(&self) -> Option<&(dyn StdError + 'static)> {
        self.cause.as_deref().map(|cause| cause as &(dyn StdError + 'static))
    }
}

/// The canonical encoding of `(name, value)` pairs, in the order given.
pub(crate) fn encode<'a>(pairs: impl IntoIterator<Item = (&'a str, &'a str)>) -> Vec<u8> {
    let mut out = Vec::new();
    for (name, value) in pairs {
        out.extend_from_slice(format!("{name}:{}:", value.len()).as_bytes());
        out.extend_from_slice(value.as_bytes());
        out.push(b'\n');
    }
    out
}

/// The bytes a signature covers: `values` in the order of `kind.fields`.
///
/// # Errors
/// If there is not exactly one value per field: a value too few or too
/// many would silently shift what each one is signed as.
pub fn canonical(kind: &Kind, values: &[&str]) -> Result<Vec<u8>, SignatureError> {
    if values.len() != kind.fields.len() {
        return Err(SignatureError::new(format!(
            "{} signs {} fields, not {}",
            kind.namespace,
            kind.fields.len(),
            values.len()
        )));
    }
    Ok(encode(kind.fields.iter().copied().zip(values.iter().copied())))
}

/// Append an SSH wire-format string: a big-endian u32 length, then the bytes.
fn put_string(out: &mut Vec<u8>, data: &[u8]) -> Result<(), SignatureError> {
    let len = u32::try_from(data.len()).map_err(|err| SignatureError::caused("too long to sign", err))?;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(data);
    Ok(())
}

fn string(data: &[u8]) -> Result<Vec<u8>, SignatureError> {
    let mut out = Vec::new();
    put_string(&mut out, data)?;
    Ok(out)
}

/// Reads SSH wire-format fields off the front of a byte string.
struct Reader<'a> {
    rest: &'a [u8],
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { rest: data }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], SignatureError> {
        let (head, tail) =
            self.rest.split_at_checked(n).ok_or_else(|| SignatureError::new("truncated key or signature"))?;
        self.rest = tail;
        Ok(head)
    }

    fn u32(&mut self) -> Result<u32, SignatureError> {
        let bytes: [u8; 4] =
            self.take(4)?.try_into().map_err(|err| SignatureError::caused("truncated key or signature", err))?;
        Ok(u32::from_be_bytes(bytes))
    }

    fn string(&mut self) -> Result<&'a [u8], SignatureError> {
        let len =
            usize::try_from(self.u32()?).map_err(|err| SignatureError::caused("truncated key or signature", err))?;
        self.take(len)
    }

    fn is_empty(&self) -> bool {
        self.rest.is_empty()
    }
}

/// The ed25519 key in an OpenSSH public key blob. A key of low order is
/// refused: with it, a signature verifies for almost any data.
fn ed25519_from_blob(blob: &[u8]) -> Result<VerifyingKey, SignatureError> {
    let mut r = Reader::new(blob);
    let kind = r.string()?;
    if kind != KEY_TYPE {
        return Err(SignatureError::new(format!(
            "only ed25519 keys are supported, not {}",
            String::from_utf8_lossy(kind)
        )));
    }
    let raw = r.string()?;
    if !r.is_empty() {
        return Err(SignatureError::new("malformed ed25519 public key"));
    }
    let raw: [u8; 32] = raw.try_into().map_err(|err| SignatureError::caused("malformed ed25519 public key", err))?;
    let key =
        VerifyingKey::from_bytes(&raw).map_err(|err| SignatureError::caused("malformed ed25519 public key", err))?;
    if key.is_weak() {
        return Err(SignatureError::new("weak ed25519 public key (of low order): it would verify any signature"));
    }
    Ok(key)
}

fn public_blob(raw: &[u8; 32]) -> Result<Vec<u8>, SignatureError> {
    let mut blob = string(KEY_TYPE)?;
    put_string(&mut blob, raw)?;
    Ok(blob)
}

/// OpenSSH's SHA256 fingerprint of a public key blob, as `ssh-keygen -l`
/// prints it.
#[must_use]
pub fn fingerprint(blob: &[u8]) -> String {
    format!("SHA256:{}", STANDARD_NO_PAD.encode(Sha256::digest(blob)))
}

/// The key as an `authorized_keys` line, without a comment.
#[must_use]
pub fn public_line(blob: &[u8]) -> String {
    format!("ssh-ed25519 {}", STANDARD.encode(blob))
}

/// What SSHSIG actually signs for `data` in `namespace`.
fn signed_data(namespace: &str, data: &[u8]) -> Result<Vec<u8>, SignatureError> {
    let mut out = MAGIC.to_vec();
    put_string(&mut out, namespace.as_bytes())?;
    put_string(&mut out, b"")?;
    put_string(&mut out, HASH.as_bytes())?;
    put_string(&mut out, Sha512::digest(data).as_slice())?;
    Ok(out)
}

/// A signing key and the public key blob that goes with it.
pub struct PrivateKey {
    key: SigningKey,
    blob: Vec<u8>,
}

impl PrivateKey {
    pub(crate) fn from_seed(seed: &[u8; 32]) -> Result<Self, SignatureError> {
        let key = SigningKey::from_bytes(seed);
        let blob = public_blob(key.verifying_key().as_bytes())?;
        Ok(Self { key, blob })
    }

    /// This key's fingerprint.
    #[must_use]
    pub fn fingerprint(&self) -> String {
        fingerprint(&self.blob)
    }

    /// This key's public half as an `authorized_keys` line, without a comment.
    #[must_use]
    pub fn public_line(&self) -> String {
        public_line(&self.blob)
    }

    /// An SSHSIG over `data` in `namespace`, as single-line base64.
    ///
    /// # Errors
    /// Only if `data` is too long for the wire format (4 GiB).
    pub fn sign_data(&self, namespace: &str, data: &[u8]) -> Result<String, SignatureError> {
        let signature: Signature = self
            .key
            .try_sign(&signed_data(namespace, data)?)
            .map_err(|err| SignatureError::caused("signing failed", err))?;
        armored(&self.blob, namespace, &signature.to_bytes())
    }

    /// Sign `values` as a write of `kind`.
    ///
    /// # Errors
    /// If `values` does not fit `kind` (see [`canonical`]), or is too long
    /// for the wire format.
    pub fn sign(&self, kind: &Kind, values: &[&str]) -> Result<String, SignatureError> {
        self.sign_data(kind.namespace, &canonical(kind, values)?)
    }
}

/// An SSHSIG by the key in `key_blob`, as single-line base64.
fn armored(key_blob: &[u8], namespace: &str, signature: &[u8; 64]) -> Result<String, SignatureError> {
    let mut inner = string(KEY_TYPE)?;
    put_string(&mut inner, signature)?;
    let mut blob = MAGIC.to_vec();
    blob.extend_from_slice(&1_u32.to_be_bytes());
    put_string(&mut blob, key_blob)?;
    put_string(&mut blob, namespace.as_bytes())?;
    put_string(&mut blob, b"")?;
    put_string(&mut blob, HASH.as_bytes())?;
    put_string(&mut blob, &inner)?;
    Ok(STANDARD.encode(blob))
}

/// Who signed: the key as a public key line, and its fingerprint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Verified {
    pub public_line: String,
    pub fingerprint: String,
}

/// Check an SSHSIG over `data`, and say whose it is.
///
/// Takes the base64 with or without line breaks.
///
/// # Errors
/// For anything that is not a valid sha512 ed25519 signature over `data`
/// in `namespace`, saying which way it is not.
pub fn verify_data(namespace: &str, data: &[u8], signature: &str) -> Result<Verified, SignatureError> {
    let joined: String = signature.split_whitespace().collect();
    let blob = STANDARD.decode(joined).map_err(|err| SignatureError::caused("signature is not base64", err))?;
    let mut r = Reader::new(&blob);
    if r.take(MAGIC.len())? != MAGIC || r.u32()? != 1 {
        return Err(SignatureError::new("not an SSHSIG version 1 signature"));
    }
    let key_blob = r.string()?;
    let key = ed25519_from_blob(key_blob)?;
    let signed_namespace = String::from_utf8_lossy(r.string()?).into_owned();
    r.string()?; // reserved; ignored, as ssh-keygen does
    let hash = String::from_utf8_lossy(r.string()?).into_owned();
    let mut sig = Reader::new(r.string()?);
    if !r.is_empty() {
        return Err(SignatureError::new("trailing bytes after the signature"));
    }
    if signed_namespace != namespace {
        return Err(SignatureError::new(format!("signature is for namespace {signed_namespace:?}, not {namespace:?}")));
    }
    if hash != HASH {
        return Err(SignatureError::new(format!("signature uses {hash}; only {HASH} is accepted")));
    }
    if sig.string()? != KEY_TYPE {
        return Err(SignatureError::new("only ed25519 signatures are supported"));
    }
    let raw_signature = sig.string()?;
    let mismatch = "signature does not match the signed fields";
    let signature = Signature::from_slice(raw_signature).map_err(|err| SignatureError::caused(mismatch, err))?;
    // Strict: also refuses a signature whose R is of low order.
    key.verify_strict(&signed_data(namespace, data)?, &signature)
        .map_err(|err| SignatureError::caused(mismatch, err))?;
    Ok(Verified { public_line: public_line(key_blob), fingerprint: fingerprint(key_blob) })
}

/// Verify a signature over `values` as a write of `kind`.
///
/// # Errors
/// As [`canonical`] and [`verify_data`].
pub fn verify(kind: &Kind, values: &[&str], signature: &str) -> Result<Verified, SignatureError> {
    verify_data(kind.namespace, &canonical(kind, values)?, signature)
}

/// An OpenSSH ed25519 public key line's (public line, fingerprint).
///
/// # Errors
/// If `text` is not an OpenSSH public key line, or not an ed25519 one.
pub fn load_public_key(text: &str) -> Result<Verified, SignatureError> {
    let not_a_key = "not an OpenSSH public key line";
    let encoded = text.split_whitespace().nth(1).ok_or_else(|| SignatureError::new(not_a_key))?;
    let blob = STANDARD.decode(encoded).map_err(|err| SignatureError::caused(not_a_key, err))?;
    ed25519_from_blob(&blob)?;
    Ok(Verified { public_line: public_line(&blob), fingerprint: fingerprint(&blob) })
}

/// What `strerror` says for an I/O error: Rust's message without the
/// " (os error N)" it appends.
#[must_use]
pub fn strerror(err: &io::Error) -> String {
    let text = err.to_string();
    match err.raw_os_error() {
        Some(code) => text.strip_suffix(&format!(" (os error {code})")).unwrap_or(&text).to_owned(),
        None => text,
    }
}

/// Why an openssh-key-v1 file did not parse.
#[derive(Debug)]
enum KeyFileError {
    Format(&'static str, Option<SignatureError>),
    Encrypted,
    NotEd25519,
}

impl From<SignatureError> for KeyFileError {
    fn from(truncated: SignatureError) -> Self {
        Self::Format("Invalid data", Some(truncated))
    }
}

const PADDING: &[u8] = &[1, 2, 3, 4, 5, 6, 7];
const NOT_OPENSSH: &str = "Not OpenSSH private key format";

/// The seed of the one unencrypted ed25519 key in an openssh-key-v1 file.
fn parse_private(data: &[u8]) -> Result<[u8; 32], KeyFileError> {
    let text = String::from_utf8_lossy(data);
    let (_, after_begin) = text.split_once(PEM_BEGIN).ok_or(KeyFileError::Format(NOT_OPENSSH, None))?;
    let (armored, _) = after_begin.split_once(PEM_END).ok_or(KeyFileError::Format(NOT_OPENSSH, None))?;
    // The armored body is base64 broken into lines: skip all that is not base64.
    let armored: String =
        armored.chars().filter(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '=')).collect();
    let raw = STANDARD
        .decode(armored)
        .map_err(|err| KeyFileError::Format(NOT_OPENSSH, Some(SignatureError::caused("bad base64", err))))?;
    let rest = raw.strip_prefix(PRIVATE_MAGIC).ok_or(KeyFileError::Format(NOT_OPENSSH, None))?;
    let mut r = Reader::new(rest);
    let cipher = r.string()?;
    let kdf = r.string()?;
    r.string()?; // kdf options
    if r.u32()? != 1 {
        return Err(KeyFileError::Format("Only one key supported", None));
    }
    let mut public = Reader::new(r.string()?);
    let key_type = public.string()?;
    if (cipher, kdf) != (&b"none"[..], &b"none"[..]) {
        return Err(KeyFileError::Encrypted);
    }
    if key_type != KEY_TYPE {
        return Err(KeyFileError::NotEd25519);
    }
    let public_raw = public.string()?;
    let mut private = Reader::new(r.string()?);
    if !public.is_empty() || !r.is_empty() {
        return Err(KeyFileError::Format("Invalid data", None));
    }
    if private.u32()? != private.u32()? {
        return Err(KeyFileError::Format("Corrupt data: broken checksum", None));
    }
    if private.string()? != key_type {
        return Err(KeyFileError::Format("Corrupt data: key type mismatch", None));
    }
    let point = private.string()?;
    let mut keypair = Reader::new(private.string()?); // seed || public key
    let seed = keypair.take(32)?;
    if point != keypair.rest || point != public_raw {
        return Err(KeyFileError::Format("Corrupt data: ed25519 field mismatch", None));
    }
    private.string()?; // comment
    if !PADDING.starts_with(private.rest) {
        return Err(KeyFileError::Format("Corrupt data: invalid padding", None));
    }
    let seed: [u8; 32] = seed
        .try_into()
        .map_err(|err| KeyFileError::Format("Invalid data", Some(SignatureError::caused("short seed", err))))?;
    if SigningKey::from_bytes(&seed).verifying_key().as_bytes().as_slice() != point {
        return Err(KeyFileError::Format("Corrupt data: ed25519 field mismatch", None));
    }
    Ok(seed)
}

/// An unencrypted OpenSSH ed25519 private key from `path`.
///
/// # Errors
/// If the file cannot be read, is readable by group or others (as ssh
/// refuses a key like that), is not an OpenSSH private key, is
/// passphrase-protected, or holds another kind of key.
pub fn load_private_key(path: &Path) -> Result<PrivateKey, SignatureError> {
    let shown = path.display();
    let cannot = |err: &std::io::Error| SignatureError::new(format!("cannot read key {shown}: {}", strerror(err)));
    let mut file = fs::File::open(path).map_err(|err| cannot(&err))?;
    // The open file's mode, not the path's: nothing can swap it in between.
    let mode = file.metadata().map_err(|err| cannot(&err))?.permissions().mode();
    if mode & 0o077 != 0 {
        return Err(SignatureError::new(format!(
            "key {shown} can be read by others (mode {:03o}); make it private first: chmod 600 {shown}",
            mode & 0o777
        )));
    }
    let mut data = Vec::new();
    file.read_to_end(&mut data).map_err(|err| cannot(&err))?;
    match parse_private(&data) {
        Ok(seed) => PrivateKey::from_seed(&seed),
        Err(KeyFileError::Format(why, cause)) => {
            let message = format!("{shown} is not an OpenSSH private key: {why}");
            Err(match cause {
                Some(cause) => SignatureError::caused(message, cause),
                None => SignatureError::new(message),
            })
        }
        Err(KeyFileError::Encrypted) => Err(SignatureError::new(format!(
            "{shown} is passphrase-protected; use an unencrypted dedicated key (`board keygen`)"
        ))),
        Err(KeyFileError::NotEd25519) => Err(SignatureError::new(format!("{shown}: only ed25519 keys are supported"))),
    }
}

/// A fresh key pair.
pub struct Generated {
    /// The private key file, unencrypted openssh-key-v1.
    pub private: String,
    /// The public key line, with `comment`, and a final newline.
    pub public: String,
    pub fingerprint: String,
}

/// Make a fresh ed25519 key pair.
///
/// # Errors
/// If the system cannot supply random bytes.
pub fn generate(comment: &str) -> Result<Generated, SignatureError> {
    let mut seed = [0_u8; 32];
    getrandom::fill(&mut seed).map_err(|err| SignatureError::new(format!("no random bytes: {err}")))?;
    let check = getrandom::u32().map_err(|err| SignatureError::new(format!("no random bytes: {err}")))?;
    let key = PrivateKey::from_seed(&seed)?;
    let public_raw = key.key.verifying_key().to_bytes();

    let mut private = Vec::new();
    private.extend_from_slice(&check.to_be_bytes());
    private.extend_from_slice(&check.to_be_bytes());
    put_string(&mut private, KEY_TYPE)?;
    put_string(&mut private, &public_raw)?;
    let mut keypair = seed.to_vec();
    keypair.extend_from_slice(&public_raw);
    put_string(&mut private, &keypair)?;
    put_string(&mut private, b"")?; // the comment lives in the .pub line
    for pad in PADDING.iter().take((8 - private.len() % 8) % 8) {
        private.push(*pad);
    }

    let mut file = PRIVATE_MAGIC.to_vec();
    put_string(&mut file, b"none")?;
    put_string(&mut file, b"none")?;
    put_string(&mut file, b"")?;
    file.extend_from_slice(&1_u32.to_be_bytes());
    put_string(&mut file, &key.blob)?;
    put_string(&mut file, &private)?;

    let encoded = STANDARD.encode(&file);
    let lines: Vec<String> =
        encoded.as_bytes().chunks(70).map(|line| String::from_utf8_lossy(line).into_owned()).collect();
    Ok(Generated {
        private: format!("{PEM_BEGIN}\n{}\n{PEM_END}\n", lines.join("\n")),
        public: format!("{} {comment}\n", public_line(&key.blob)),
        fingerprint: key.fingerprint(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::error::Error;

    use serde::Deserialize;

    #[derive(Deserialize)]
    struct Vectors {
        key: VectorKey,
        cases: Vec<Case>,
        v2_cases: Vec<V2Case>,
        refusals: Vec<Refusal>,
        accepted: Vec<Accepted>,
    }

    #[derive(Deserialize)]
    struct Accepted {
        why: String,
        namespace: String,
        data_hex: String,
        signature: String,
        fingerprint: String,
    }

    #[derive(Deserialize)]
    struct VectorKey {
        private: String,
        public: String,
        fingerprint: String,
    }

    /// A v1 board write: kept to hold the encoding to what it always was,
    /// and as signatures v2 must refuse.
    #[derive(Deserialize)]
    struct Case {
        kind: String,
        values: [String; 6],
        canonical_hex: String,
        python: String,
        js: String,
    }

    #[derive(Deserialize)]
    struct V2Case {
        kind: String,
        values: Vec<String>,
        canonical_hex: String,
        ssh_keygen: String,
        js: String,
    }

    #[derive(Deserialize)]
    struct Refusal {
        why: String,
        namespace: String,
        data_hex: String,
        signature: String,
        python_error: String,
    }

    /// The v1 board's kinds, which this board no longer signs or accepts.
    const V1_MESSAGE: Kind =
        Kind { namespace: "board-message", fields: &["area", "topic", "from", "body", "key", "nonce"] };
    const V1_PIN: Kind =
        Kind { namespace: "board-pin", fields: &["area", "topic", "from", "body", "base_version", "nonce"] };

    fn vectors() -> Result<Vectors, Box<dyn Error>> {
        Ok(serde_json::from_str(include_str!("../tests/sign-vectors.json"))?)
    }

    fn hex(bytes: &[u8]) -> String {
        bytes.iter().map(|b| format!("{b:02x}")).collect::<Vec<_>>().concat()
    }

    fn unhex(text: &str) -> Result<Vec<u8>, Box<dyn Error>> {
        text.as_bytes().chunks(2).map(|pair| Ok(u8::from_str_radix(std::str::from_utf8(pair)?, 16)?)).collect()
    }

    fn v1_kind(name: &str) -> Result<Kind, Box<dyn Error>> {
        match name {
            "message" => Ok(V1_MESSAGE),
            "pin" => Ok(V1_PIN),
            other => Err(format!("unknown kind {other}").into()),
        }
    }

    fn vector_key(v: &Vectors, dir: &tempfile::TempDir) -> Result<PrivateKey, Box<dyn Error>> {
        let path = dir.path().join("key");
        fs::write(&path, &v.key.private)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        Ok(load_private_key(&path)?)
    }

    /// The v1 vectors were made by `board_sign.py` and `board-sign.js` with
    /// one key: the encoding, the signing and the verifying here must still
    /// produce and accept exactly those bytes.
    #[test]
    fn python_and_browser_vectors_hold() -> Result<(), Box<dyn Error>> {
        let v = vectors()?;
        let dir = tempfile::tempdir()?;
        let key = vector_key(&v, &dir)?;
        assert_eq!(key.fingerprint(), v.key.fingerprint);
        assert_eq!(load_public_key(&v.key.public)?.fingerprint, v.key.fingerprint);
        assert_eq!(key.public_line(), v.key.public.split(' ').take(2).collect::<Vec<_>>().join(" "));
        assert!(!v.cases.is_empty());
        for case in &v.cases {
            let kind = v1_kind(&case.kind)?;
            let values = case.values.each_ref().map(String::as_str);
            assert_eq!(hex(&canonical(&kind, &values)?), case.canonical_hex);
            let signature = key.sign(&kind, &values)?;
            assert_eq!(signature, case.python);
            assert_eq!(signature, case.js);
            let verified = verify(&kind, &values, &case.python)?;
            assert_eq!(verified.fingerprint, v.key.fingerprint);
            assert_eq!(verified.public_line, key.public_line());
        }
        Ok(())
    }

    /// The board2-* vectors were signed by `ssh-keygen -Y sign` over bytes
    /// encoded apart from the board, and by `board-sign.js` under node,
    /// which agreed: Rust must make the same bytes and signatures, and every
    /// kind must be covered.
    #[test]
    fn ssh_keygen_and_browser_v2_vectors_hold() -> Result<(), Box<dyn Error>> {
        let v = vectors()?;
        let dir = tempfile::tempdir()?;
        let key = vector_key(&v, &dir)?;
        for kind in KINDS {
            assert!(v.v2_cases.iter().any(|case| case.kind == kind.namespace), "no vector for {}", kind.namespace);
        }
        for case in &v.v2_cases {
            let kind = KINDS.iter().find(|k| k.namespace == case.kind).ok_or(format!("unknown kind {}", case.kind))?;
            let values: Vec<&str> = case.values.iter().map(String::as_str).collect();
            assert_eq!(hex(&canonical(kind, &values)?), case.canonical_hex, "{values:?}");
            let signature = key.sign(kind, &values)?;
            assert_eq!(signature, case.ssh_keygen, "{values:?}");
            assert_eq!(signature, case.js, "{values:?}");
            assert_eq!(verify(kind, &values, &case.ssh_keygen)?.fingerprint, v.key.fingerprint);
        }
        Ok(())
    }

    /// The v2 counter restarts, so a v1 nonce can be current again: a v1
    /// signature must not verify for any v2 write, not even over the very
    /// bytes it signed.
    #[test]
    fn a_v1_signature_never_verifies_for_a_v2_write() -> Result<(), Box<dyn Error>> {
        let v = vectors()?;
        for case in &v.cases {
            let v1 = v1_kind(&case.kind)?;
            let data = unhex(&case.canonical_hex)?;
            for kind in KINDS {
                let err = verify_data(kind.namespace, &data, &case.python).err().ok_or("a v1 signature verified")?;
                assert_eq!(
                    err.to_string(),
                    format!("signature is for namespace {:?}, not {:?}", v1.namespace, kind.namespace)
                );
                let mut values: Vec<&str> = case.values.iter().map(String::as_str).collect();
                values.resize(kind.fields.len(), "");
                assert!(verify(&kind, &values, &case.python).is_err());
            }
        }
        Ok(())
    }

    #[test]
    fn every_kind_has_its_own_v2_namespace_and_a_nonce() {
        for (i, kind) in KINDS.iter().enumerate() {
            assert!(kind.namespace.starts_with("board2-"), "{}", kind.namespace);
            assert_eq!(kind.fields.last(), Some(&"nonce"), "{}", kind.namespace);
            assert!(KINDS.iter().skip(i + 1).all(|other| other.namespace != kind.namespace));
        }
    }

    /// Signatures the Python verifier refused are refused here too, for the
    /// same reason (its texts quoted names in single quotes, these in double).
    #[test]
    fn python_refusals_hold() -> Result<(), Box<dyn Error>> {
        let v = vectors()?;
        assert!(!v.refusals.is_empty());
        for refusal in &v.refusals {
            let data = unhex(&refusal.data_hex)?;
            match verify_data(&refusal.namespace, &data, &refusal.signature) {
                Ok(_) => return Err(format!("accepted: {}", refusal.why).into()),
                Err(err) => assert_eq!(err.to_string(), refusal.python_error.replace('\'', "\""), "{}", refusal.why),
            }
        }
        Ok(())
    }

    /// Signatures the Python verifier accepted, in forms the board itself
    /// never sends (base64 with line breaks).
    #[test]
    fn python_acceptances_hold() -> Result<(), Box<dyn Error>> {
        let v = vectors()?;
        assert!(!v.accepted.is_empty());
        for accepted in &v.accepted {
            let verified = verify_data(&accepted.namespace, &unhex(&accepted.data_hex)?, &accepted.signature)
                .map_err(|err| format!("{}: {err}", accepted.why))?;
            assert_eq!(verified.fingerprint, accepted.fingerprint, "{}", accepted.why);
        }
        Ok(())
    }

    #[test]
    fn canonical_encoding_is_length_prefixed() -> Result<(), Box<dyn Error>> {
        let one = canonical(&POST, &["x", "a\nkey:1:b", "k", "7"])?;
        let two = canonical(&POST, &["x", "a", "b\nkey:1:k", "7"])?;
        assert_ne!(one, two);
        assert_eq!(one, b"path:1:x\nbody:9:a\nkey:1:b\nkey:1:k\nnonce:1:7\n");
        assert!(canonical(&POST, &["\u{fc}", "", "", ""])?.starts_with("path:2:\u{fc}\n".as_bytes()));
        Ok(())
    }

    #[test]
    fn a_value_too_few_or_too_many_is_refused() {
        for values in [&["x", "body", "k"][..], &["x", "body", "k", "7", "extra"]] {
            let err = canonical(&POST, values).err().map(|e| e.to_string()).unwrap_or_default();
            assert_eq!(err, format!("board2-post signs 4 fields, not {}", values.len()));
        }
    }

    #[test]
    fn generated_keys_load_and_sign() -> Result<(), Box<dyn Error>> {
        let generated = generate("board:test")?;
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("key");
        fs::write(&path, &generated.private)?;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
        let key = load_private_key(&path)?;
        assert_eq!(key.fingerprint(), generated.fingerprint);
        assert!(generated.public.ends_with(" board:test\n"));
        assert_eq!(load_public_key(&generated.public)?.fingerprint, generated.fingerprint);
        let signature = key.sign_data(POST.namespace, b"data")?;
        assert_eq!(verify_data(POST.namespace, b"data", &signature)?.fingerprint, generated.fingerprint);
        let err = verify_data(FILE.namespace, b"data", &signature).err().ok_or("accepted another namespace")?;
        assert_eq!(err.to_string(), "signature is for namespace \"board2-post\", not \"board2-file\"");
        Ok(())
    }

    #[test]
    fn unreadable_and_unusable_key_files_say_why() -> Result<(), Box<dyn Error>> {
        let dir = tempfile::tempdir()?;
        let missing = dir.path().join("missing");
        let err = load_private_key(&missing).err().ok_or("loaded a missing key")?;
        assert_eq!(err.to_string(), format!("cannot read key {}: No such file or directory", missing.display()));
        let junk = dir.path().join("junk");
        fs::write(&junk, "not a key")?;
        fs::set_permissions(&junk, fs::Permissions::from_mode(0o600))?;
        let err = load_private_key(&junk).err().ok_or("loaded junk")?;
        assert_eq!(
            err.to_string(),
            format!("{} is not an OpenSSH private key: Not OpenSSH private key format", junk.display())
        );
        Ok(())
    }

    #[test]
    fn a_key_file_others_can_read_is_refused_like_ssh_does() -> Result<(), Box<dyn Error>> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("key");
        fs::write(&path, generate("board:test")?.private)?;
        for mode in [0o644, 0o640, 0o604, 0o660] {
            fs::set_permissions(&path, fs::Permissions::from_mode(mode))?;
            let err = load_private_key(&path).err().ok_or("loaded a key others can read")?;
            let message = err.to_string();
            assert!(message.contains("can be read by others") && message.contains("chmod 600"), "{message}");
            assert!(message.contains(&format!("mode {mode:03o}")), "{message}");
        }
        for mode in [0o600, 0o400] {
            fs::set_permissions(&path, fs::Permissions::from_mode(mode))?;
            load_private_key(&path)?;
        }
        Ok(())
    }

    #[test]
    fn a_key_of_low_order_signs_nothing() -> Result<(), Box<dyn Error>> {
        // The identity point as the key, and R = identity, S = 0 as the
        // signature: the plain ed25519 equation holds for any data.
        let mut identity = [0_u8; 32];
        identity[0] = 1;
        let weak = public_blob(&identity)?;
        let mut forged = [0_u8; 64];
        forged[0] = 1;
        let signature = armored(&weak, POST.namespace, &forged)?;
        for data in [b"deploy main now".as_slice(), b"anything else"] {
            let err = verify_data(POST.namespace, data, &signature).err().ok_or("a weak key signed")?;
            assert!(err.to_string().contains("weak"), "{err}");
        }
        let err = load_public_key(&public_line(&weak)).err().ok_or("a weak key loaded")?;
        assert!(err.to_string().contains("weak"), "{err}");
        Ok(())
    }
}
