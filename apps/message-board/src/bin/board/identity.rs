//! Who `board` writes as: the signing key it finds, the key it makes, and
//! the name the board's owner bound to it.
//!
//! A key is found as `$BOARD_KEY`, else the nearest `scratch/board_ed25519`
//! above the current directory: the directory decides who an agent is.
//! The board knows a key by its fingerprint; the name is whatever the
//! owner bound to it on approval, so no command takes a sender name.

use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Write as _};
use std::os::unix::fs::OpenOptionsExt as _;
use std::path::{Path, PathBuf};

use message_board::sign::{self, PrivateKey};
use message_board::store::{self, KeyState, Register};

use crate::api::{Api, key_url};
use crate::wire::{Key, Line, Registered};
use crate::{Failure, emit, io_failure, warn, write};

/// Where `board keygen` puts the key, below the repository root.
pub const KEY_FILE: &str = "scratch/board_ed25519";

/// The key `board` signs with, and where it was found.
pub struct Signer {
    pub key: PrivateKey,
    pub path: PathBuf,
}

/// The signing key to use: `$BOARD_KEY`, else the nearest
/// `scratch/board_ed25519` above the current directory, else none.
pub fn find_key() -> Result<Option<PathBuf>, Failure> {
    if let Some(key) = std::env::var_os("BOARD_KEY").filter(|k| !k.is_empty()) {
        return Ok(Some(PathBuf::from(key)));
    }
    let here = current_dir()?;
    for dir in here.ancestors() {
        let candidate = dir.join(KEY_FILE);
        match fs::metadata(&candidate) {
            Ok(meta) if meta.is_file() => return Ok(Some(candidate)),
            Ok(_) => {}
            Err(err) if matches!(err.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory) => {}
            // e.g. a scratch/ it may not look into: say so rather than go on
            // as if there were no key
            Err(err) => {
                return Err(Failure::Io {
                    what: format!("cannot look for a key at {}", candidate.display()),
                    cause: err,
                });
            }
        }
    }
    Ok(None)
}

/// The key a write is signed with; none is an identity refusal (exit 3).
pub fn signer() -> Result<Signer, Failure> {
    let path = find_key()?.ok_or(Failure::NoKey)?;
    Ok(Signer { key: sign::load_private_key(&path)?, path })
}

fn current_dir() -> Result<PathBuf, Failure> {
    std::env::current_dir().and_then(fs::canonicalize).map_err(io_failure("cannot find the current directory"))
}

/// The nearest directory from `start` up with a `.jj` or `.git`: the
/// repository root, as `keygen` and `register` see it.
fn repo_root(start: &Path) -> Result<Option<&Path>, Failure> {
    for dir in start.ancestors() {
        for marker in [".jj", ".git"] {
            let path = dir.join(marker);
            if path.try_exists().map_err(io_failure(format!("cannot check {}", path.display())))? {
                return Ok(Some(dir));
            }
        }
    }
    Ok(None)
}

/// Create the repo's signing key under `<repo root>/scratch/`.
pub fn keygen(name: &str) -> Result<(), Failure> {
    store::check_name(name).map_err(|err| Failure::Exit(err.to_string()))?;
    let here = current_dir()?;
    let Some(root) = repo_root(&here)? else {
        return Err(Failure::Exit(
            "not inside a repository (no .jj or .git above the current directory); make a key with ssh-keygen \
             and point BOARD_KEY at it"
                .to_owned(),
        ));
    };
    let path = root.join(KEY_FILE);
    let public = root.join(format!("{KEY_FILE}.pub"));
    for existing in [&path, &public] {
        if existing.try_exists().map_err(io_failure(format!("cannot check {}", existing.display())))? {
            return Err(Failure::Exit(format!("{} already exists; not overwriting it", existing.display())));
        }
    }
    let scratch = root.join("scratch");
    match fs::create_dir(&scratch) {
        Ok(()) => {}
        Err(err) if err.kind() == ErrorKind::AlreadyExists && scratch.is_dir() => {}
        Err(err) => return Err(Failure::Io { what: format!("cannot create {}", scratch.display()), cause: err }),
    }
    keep_out_of_version_control(&scratch.join(".gitignore"))?;
    let generated = sign::generate(&format!("board:{name}"))?;
    write_new(&path, generated.private.as_bytes())?;
    write_new(&public, generated.public.as_bytes())?;
    emit(&Line::Key { path: path.display().to_string(), fingerprint: generated.fingerprint, name })
}

/// The lines of a `.gitignore` that, with nothing after them taking it
/// back, keep `board_ed25519` and its `.pub` out of a commit.
const COVERING: [&str; 5] = ["*", "board_ed25519*", "/board_ed25519*", "board_ed25519", "/board_ed25519"];

/// Whether `ignore`'s text keeps the key out of a commit: a line from
/// [`COVERING`] and no later `!` pattern that could take it back (one that
/// names the key or starts with a wildcard).
fn covers_the_key(ignore: &str) -> bool {
    let mut covered = false;
    for line in ignore.lines().map(str::trim) {
        if COVERING.contains(&line) {
            covered = true;
        } else if line
            .strip_prefix('!')
            .is_some_and(|re| re.contains("board_ed25519") || re.starts_with(['*', '?', '[']))
        {
            covered = false;
        }
    }
    covered
}

/// Make sure `scratch/.gitignore` keeps the key out of a commit: write it
/// if there is none, and add a line to one that does not cover the key
/// (saying so), since a private key must never be committable.
fn keep_out_of_version_control(ignore: &Path) -> Result<(), Failure> {
    let what = || format!("{}", ignore.display());
    let existing = match fs::read(ignore) {
        Ok(bytes) => Some(String::from_utf8_lossy(&bytes).into_owned()),
        Err(err) if err.kind() == ErrorKind::NotFound => None,
        Err(err) => return Err(Failure::Io { what: format!("cannot read {}", what()), cause: err }),
    };
    match existing {
        None => fs::write(ignore, "*\n").map_err(io_failure(format!("cannot write {}", what()))),
        Some(text) if covers_the_key(&text) => Ok(()),
        Some(text) => {
            let separator = if text.is_empty() || text.ends_with('\n') { "" } else { "\n" };
            let mut file =
                OpenOptions::new().append(true).open(ignore).map_err(io_failure(format!("cannot open {}", what())))?;
            file.write_all(format!("{separator}board_ed25519*\n").as_bytes())
                .map_err(io_failure(format!("cannot write {}", what())))?;
            warn(&format!("board: {} did not keep the key out of a commit; added the line board_ed25519*", what()))
        }
    }
}

/// Write a file that must not exist yet, readable by its owner only.
fn write_new(path: &Path, data: &[u8]) -> Result<(), Failure> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(io_failure(format!("cannot create {}", path.display())))?;
    file.write_all(data).map_err(io_failure(format!("cannot write {}", path.display())))
}

/// What the board knows of the key with `fingerprint`; `None` if nothing.
pub fn lookup(api: &Api, fingerprint: &str) -> Result<Option<Key>, Failure> {
    api.find(&key_url(fingerprint))
}

/// The name `key` may write as, or the refusal the board would give a
/// write signed with it.
fn standing<'a>(key: Option<&'a Key>, fingerprint: &str) -> Result<&'a str, store::Error> {
    let Some(key) = key else {
        return Err(store::Error::NotRegistered { fingerprint: fingerprint.to_owned() });
    };
    // The board gives every pending and declined key its item and every
    // revoked key its event, so the defaults are never taken.
    match key.state {
        KeyState::Owner | KeyState::Trusted => Ok(&key.name),
        KeyState::Pending => Err(store::Error::RegistrationPending { item: key.item.unwrap_or_default() }),
        KeyState::Declined => Err(store::Error::RegistrationDeclined { item: key.item.unwrap_or_default() }),
        KeyState::Revoked => Err(store::Error::KeyRevoked { revoked_at: key.revoked.unwrap_or_default() }),
    }
}

/// The name bound to the signing key, for `--me`, `items --mine` and
/// `brief`; a key without one is refused as the board would refuse it.
pub fn bound_name(api: &Api, signer: &Signer) -> Result<String, Failure> {
    let fingerprint = signer.key.fingerprint();
    let key = lookup(api, &fingerprint)?;
    standing(key.as_ref(), &fingerprint).map(str::to_owned).map_err(|err| Failure::from_store(&err))
}

pub fn whoami(api: &Api) -> Result<(), Failure> {
    let fingerprint = signer()?.key.fingerprint();
    let key = lookup(api, &fingerprint)?;
    let (name, state) = match &key {
        Some(key) => (Some(key.name.as_str()), key.state.as_str()),
        None => (None, "unregistered"),
    };
    emit(&Line::Whoami { fingerprint: &fingerprint, name, state })
}

/// File a registration for the signing key in the owner's queue.
pub fn register(api: &Api, name: &str, note: &str) -> Result<(), Failure> {
    store::check_name(name).map_err(|err| Failure::Exit(err.to_string()))?;
    let signer = signer()?;
    // Informational, for the owner to judge by: the repository the key was
    // found in, as keygen finds it.
    let key_path =
        fs::canonicalize(&signer.path).map_err(io_failure(format!("cannot resolve {}", signer.path.display())))?;
    let root = match key_path.parent() {
        Some(dir) => repo_root(dir)?,
        None => None,
    };
    let repo = root.map(|root| root.display().to_string()).unwrap_or_default();
    let registered: Registered = write::submit(api, &signer, "/register", &Register { name, note, repo: &repo })?;
    emit(&Line::Registered(&registered))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_gitignore_covers_the_key_only_if_nothing_takes_it_back() {
        for covering in ["*\n", "board_ed25519*\n", "custom\n/board_ed25519\n", "  *  \n", "*\n!README\n"] {
            assert!(covers_the_key(covering), "{covering:?}");
        }
        for open in ["", "custom\n", "*.log\n", "*\n!board_ed25519\n", "*\n!*\n", "# *\n", "board_ed25519.old\n"] {
            assert!(!covers_the_key(open), "{open:?}");
        }
    }

    fn key(state: KeyState) -> Key {
        Key {
            fingerprint: "SHA256:x".to_owned(),
            name: "aspfuzz".to_owned(),
            state,
            item: Some(10_007),
            revoked: Some(10_020),
        }
    }

    #[test]
    fn only_a_trusted_or_owner_key_has_a_name() {
        assert_eq!(standing(Some(&key(KeyState::Trusted)), "fp").ok(), Some("aspfuzz"));
        assert_eq!(standing(Some(&key(KeyState::Owner)), "fp").ok(), Some("aspfuzz"));
        let refused = |key: Option<&Key>| standing(key, "SHA256:x").err().map(|err| (err.code(), err.to_string()));
        let pending = key(KeyState::Pending);
        let revoked = key(KeyState::Revoked);
        assert_eq!(refused(Some(&pending)).map(|(code, _)| code), Some("registration-pending"));
        assert_eq!(refused(Some(&key(KeyState::Declined))).map(|(code, _)| code), Some("registration-declined"));
        assert!(refused(Some(&revoked)).is_some_and(|(code, text)| code == "key-revoked" && text.contains("#10020")));
        assert!(refused(None).is_some_and(|(code, text)| code == "not-registered" && text.contains("board register")));
    }
}
