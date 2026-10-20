"""SSH signatures for board posts and pin edits.

Shared by `board` (signs) and board-server (verifies). A signature is an
OpenSSH SSHSIG (PROTOCOL.sshsig, sha512, ed25519 keys only) over a canonical
encoding of the post's fields, so `ssh-keygen -Y verify` can check any
signature the board stores. It is stored and sent as the armored body's
base64, without the BEGIN/END lines.

Canonical encoding: the fields in a fixed order, each as
`<name>:<utf-8 byte length>:<value>\\n`. The length prefix is what makes it
unambiguous; a `name=value` line format cannot tell a body containing
"\\nfrom=x" from a real `from` field. static/board-sign.js implements the same
encoding and must stay byte-identical with this one.
"""
import base64
import hashlib
import struct
from pathlib import Path

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

# (namespace, fields in signing order). The namespace keeps a message
# signature from being replayed as a pin edit and vice versa; `nonce`, the
# board's head when signing, keeps either from being replayed at all.
MESSAGE = ("board-message", ("area", "topic", "from", "body", "key", "nonce"))
PIN = ("board-pin", ("area", "topic", "from", "body", "base_version", "nonce"))

_MAGIC = b"SSHSIG"
_HASH = "sha512"
_KEY_TYPE = b"ssh-ed25519"


class SignatureError(ValueError):
    """A key or signature the board cannot use, with the reason why."""


def canonical(fields, values):
    """The bytes a signature covers: `values[name]` for each name in `fields`."""
    out = []
    for name in fields:
        value = values[name]
        if not isinstance(value, str):
            raise SignatureError(f"field {name!r} must be a string to be signed")
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate, e.g. from a JSON "\ud800"
            raise SignatureError(f"field {name!r} is not valid unicode")
        out.append(f"{name}:{len(raw)}:".encode() + raw + b"\n")
    return b"".join(out)


def _string(data):
    return struct.pack(">I", len(data)) + data


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        if self.pos + n > len(self.data):
            raise SignatureError("truncated key or signature")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def string(self):
        return self.take(self.u32())


def _public_blob(public_key):
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _string(_KEY_TYPE) + _string(raw)


def _ed25519_from_blob(blob):
    r = _Reader(blob)
    kind = r.string()
    if kind != _KEY_TYPE:
        raise SignatureError(f"only ed25519 keys are supported, not {kind.decode(errors='replace')}")
    raw = r.string()
    if len(raw) != 32 or r.pos != len(blob):
        raise SignatureError("malformed ed25519 public key")
    return Ed25519PublicKey.from_public_bytes(raw)


def fingerprint(blob):
    """OpenSSH's SHA256 fingerprint, as `ssh-keygen -l` prints it."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def public_line(blob):
    """The key as an authorized_keys line, without a comment."""
    return f"{_KEY_TYPE.decode()} {base64.b64encode(blob).decode()}"


def _signed_data(namespace, data):
    return (_MAGIC + _string(namespace.encode()) + _string(b"")
            + _string(_HASH.encode()) + _string(hashlib.sha512(data).digest()))


def load_private_key(path):
    """An unencrypted OpenSSH ed25519 private key from `path`."""
    try:
        key = serialization.load_ssh_private_key(Path(path).read_bytes(), password=None)
    except OSError as exc:
        raise SignatureError(f"cannot read key {path}: {exc.strerror}")
    except TypeError:  # cryptography's "password was not provided"
        raise SignatureError(
            f"{path} is passphrase-protected; use an unencrypted dedicated key (`board keygen`)")
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise SignatureError(f"{path} is not an OpenSSH private key: {exc}")
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError(f"{path}: only ed25519 keys are supported")
    return key


def load_public_key(text):
    """(public_line, fingerprint) of an OpenSSH ed25519 public key line."""
    parts = text.split()
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (IndexError, ValueError):
        raise SignatureError("not an OpenSSH public key line")
    _ed25519_from_blob(blob)
    return public_line(blob), fingerprint(blob)


def generate(comment):
    """A fresh key pair: (private key file bytes, public key line, fingerprint)."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
    blob = _public_blob(key.public_key())
    return private, f"{public_line(blob)} {comment}\n", fingerprint(blob)


def sign_data(private_key, namespace, data):
    """An SSHSIG over `data`, as single-line base64."""
    signature = private_key.sign(_signed_data(namespace, data))
    blob = (_MAGIC + struct.pack(">I", 1) + _string(_public_blob(private_key.public_key()))
            + _string(namespace.encode()) + _string(b"") + _string(_HASH.encode())
            + _string(_string(_KEY_TYPE) + _string(signature)))
    return base64.b64encode(blob).decode()


def verify_data(namespace, data, signature):
    """Check an SSHSIG over `data`; returns the signer's (public_line, fingerprint).

    Accepts the base64 with or without line breaks. Raises SignatureError for
    anything that is not a valid sha512 ed25519 signature in `namespace`.
    """
    try:
        blob = base64.b64decode("".join(signature.split()), validate=True)
    except (ValueError, AttributeError):
        raise SignatureError("signature is not base64")
    r = _Reader(blob)
    if r.take(len(_MAGIC)) != _MAGIC or r.u32() != 1:
        raise SignatureError("not an SSHSIG version 1 signature")
    key_blob = r.string()
    public_key = _ed25519_from_blob(key_blob)
    signed_ns = r.string().decode(errors="replace")
    r.string()  # reserved; ignored, as ssh-keygen does
    hash_alg = r.string().decode(errors="replace")
    sig = _Reader(r.string())
    if r.pos != len(blob):
        raise SignatureError("trailing bytes after the signature")
    if signed_ns != namespace:
        raise SignatureError(f"signature is for namespace {signed_ns!r}, not {namespace!r}")
    if hash_alg != _HASH:
        raise SignatureError(f"signature uses {hash_alg}; only {_HASH} is accepted")
    if sig.string() != _KEY_TYPE:
        raise SignatureError("only ed25519 signatures are supported")
    raw = sig.string()
    try:
        public_key.verify(raw, _signed_data(namespace, data))
    except InvalidSignature:
        raise SignatureError("signature does not match the signed fields")
    return public_line(key_blob), fingerprint(key_blob)


def sign(private_key, kind, values):
    """Sign `values` (field name -> str) as a MESSAGE or PIN."""
    namespace, fields = kind
    return sign_data(private_key, namespace, canonical(fields, values))


def verify(kind, values, signature):
    """Verify a MESSAGE or PIN signature; returns (public_line, fingerprint)."""
    namespace, fields = kind
    return verify_data(namespace, canonical(fields, values), signature)

