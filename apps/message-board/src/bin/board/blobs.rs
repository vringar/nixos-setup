//! Files for other agents: `put`, `get`, `blobs` and `blob-rm`, and the
//! `blob_ref` lines that say what the references in a body stand at.
//!
//! A blob is addressed by the sha256 of its bytes and has no other name. An
//! upload is signed before its first byte (the signature covers the hash and
//! the size), so the file is hashed first; standard input is spooled to a
//! temp file for that. The upload sends `Expect: 100-continue`, so that a
//! refusal the board can make from the head alone (a stale nonce, a full
//! store, a blob too large, a key it does not trust) arrives before any of
//! the file is sent, and a blob the board has already costs no transfer.

use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, IsTerminal as _, Read, Write};
use std::os::unix::fs::OpenOptionsExt as _;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

use sha2::{Digest as _, Sha256};

use message_board::blob;
use message_board::store::{Action, DeleteBlob, PutBlob};

use crate::api::{Api, error_code, query};
use crate::client::HttpError;
use crate::identity::{Signer, signer};
use crate::wire::{Blob, BlobList, BlobRef, BlobRefs, BlobRemoved, Health, Line, json_line};
use crate::write::{NONCE_TRIES, backoff, client_key, submit};
use crate::{Failure, emit, io_failure, warn};

/// How long an upload waits for the board's answer once the bytes are sent:
/// it hashes, syncs and records them first.
const UPLOAD_TIMEOUT: Duration = Duration::from_secs(600);
/// How long a download may stand still.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(60);
/// Resends after no answer, waiting 1, 2, then 4 s before each.
const RETRIES: u32 = 3;
const CHUNK: usize = 1024 * 1024;

/// A temp file, deleted when this goes away.
struct Spool(PathBuf);

impl Drop for Spool {
    fn drop(&mut self) {
        // nothing to do if it cannot be deleted: it is in the temp directory
        drop(fs::remove_file(&self.0));
    }
}

/// Standard input in a file of its own, for hashing before sending.
fn spool_stdin() -> Result<Spool, Failure> {
    let mut random = [0_u8; 8];
    getrandom::fill(&mut random).map_err(|err| Failure::Exit(format!("no random bytes for a temp file: {err}")))?;
    let name: String =
        random.iter().flat_map(|b| [b >> 4, b & 0x0f]).filter_map(|n| char::from_digit(u32::from(n), 16)).collect();
    let path = std::env::temp_dir().join(format!("board-put-{name}"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)
        .map_err(io_failure(format!("cannot make a temp file {}", path.display())))?;
    let spool = Spool(path);
    io::copy(&mut io::stdin().lock(), &mut file).map_err(io_failure("cannot read standard input"))?;
    file.sync_all().map_err(io_failure("cannot write the temp file"))?;
    Ok(spool)
}

/// The sha256 (hex) and size of the file at `path`.
fn digest(path: &Path) -> Result<(String, u64), Failure> {
    let mut file = File::open(path).map_err(io_failure(format!("cannot read {}", path.display())))?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0_u8; CHUNK];
    let mut size = 0_u64;
    loop {
        let n = file.read(&mut buf).map_err(io_failure(format!("cannot read {}", path.display())))?;
        if n == 0 {
            break;
        }
        hasher.update(buf.get(..n).unwrap_or_default());
        size = size.saturating_add(u64::try_from(n).unwrap_or(0));
    }
    Ok((hex(&hasher.finalize()), size))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().flat_map(|b| [b >> 4, b & 0x0f]).filter_map(|n| char::from_digit(u32::from(n), 16)).collect()
}

/// The hash a command's argument names: `board-blob://<hash>` or the hash.
fn hash_of(argument: &str) -> Result<String, Failure> {
    let hash = argument.strip_prefix(blob::SCHEME).unwrap_or(argument);
    if blob::is_hash(hash) {
        Ok(hash.to_owned())
    } else {
        Err(Failure::Arguments(format!(
            "{argument:?} is no blob: give its 64-digit lowercase sha256, or board-blob://<hash> (board put prints it)"
        )))
    }
}

/// What `put` prints, with the url in full.
fn printed(api: &Api, mut blob: Blob) -> Blob {
    blob.url = api.absolute(&blob.url);
    blob
}

/// `board put FILE|-`: upload, and print the reference to paste into a body.
pub fn put(api: &Api, file: &str, name: Option<&str>, media: Option<&str>) -> Result<(), Failure> {
    let signer = signer()?;
    let (path, _spool, default_name) = if file == "-" {
        let spool = spool_stdin()?;
        (spool.0.clone(), Some(spool), String::new())
    } else {
        let name = Path::new(file).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        (PathBuf::from(file), None, name)
    };
    let filename = name.map_or(default_name, str::to_owned);
    blob::check_filename(&filename).map_err(Failure::Arguments)?;
    let media_type = match media {
        Some(given) => blob::media_type(given).map_err(Failure::Arguments)?,
        None => blob::DEFAULT_MEDIA_TYPE.to_owned(),
    };
    let (sha256, size) = digest(&path)?;
    if size == 0 {
        return Err(Failure::Exit(format!("{file} is empty: a blob has at least one byte")));
    }
    let put = PutBlob { sha256: &sha256, size, media_type: &media_type, filename: &filename };
    let answer = upload(api, &signer, &put, &path)?;
    emit(&Line::Blob(&printed(api, answer)))
}

/// Sign, send, and send again as the board's answers say: a signature gone
/// stale is made afresh; no answer is tried again (the bytes are the same
/// whoever sends them, so it is harmless).
fn upload(api: &Api, signer: &Signer, put: &PutBlob<'_>, path: &Path) -> Result<Blob, Failure> {
    let key = client_key()?;
    let (mut stale, mut dropped) = (0, 0);
    loop {
        let nonce = api.get::<Health>("/healthz")?.head.to_string();
        let signature = put.sign(&signer.key, &key, &nonce)?;
        let mut headers = vec![
            ("Content-Length", put.size.to_string()),
            ("Expect", "100-continue".to_owned()),
            ("Content-Type", put.media_type.to_owned()),
            ("Board-Sha256", put.sha256.to_owned()),
            ("Board-Key", key.clone()),
            ("Board-Nonce", nonce),
            ("Board-Signature", signature),
        ];
        if !put.filename.is_empty() {
            headers.push(("Board-Filename", blob::pct_encode(put.filename)));
        }
        let file = File::open(path).map_err(io_failure(format!("cannot read {}", path.display())))?;
        match api.put("/blob", &headers, file, UPLOAD_TIMEOUT) {
            Ok(text) => return crate::api::parse("/blob", &text),
            Err(err) if error_code(&err).as_deref() == Some("stale-nonce") => {
                stale += 1;
                if stale == NONCE_TRIES {
                    return Err(Failure::Exit(format!(
                        "gave up after {NONCE_TRIES} tries: each time, another write landed between reading the \
                         board's head and this one arriving (stale-nonce); run it again"
                    )));
                }
                thread::sleep(backoff(stale)?);
            }
            Err(HttpError::Transport(cause)) if dropped < RETRIES => {
                warn(&format!("board: no answer to the upload ({cause}); retrying"))?;
                thread::sleep(Duration::from_secs(1 << dropped));
                dropped += 1;
            }
            Err(err) => return Err(api.failure(err)),
        }
    }
}

/// Copy `from` to `to`, feeding what passes to `hasher`; the byte count.
fn copy_hashed(from: &mut dyn Read, to: &mut dyn Write, hasher: &mut Sha256) -> Result<u64, Failure> {
    let mut buf = vec![0_u8; CHUNK];
    let mut size = 0_u64;
    loop {
        let n = from.read(&mut buf).map_err(io_failure("the download broke off"))?;
        if n == 0 {
            break;
        }
        let chunk = buf.get(..n).unwrap_or_default();
        hasher.update(chunk);
        to.write_all(chunk).map_err(io_failure("cannot write the output"))?;
        size = size.saturating_add(u64::try_from(n).unwrap_or(0));
    }
    Ok(size)
}

/// What an earlier, broken-off download left in `part`: the hash state of
/// its bytes and their count. Nothing if there is no such file.
fn kept(part: &Path) -> Result<(Sha256, u64), Failure> {
    let mut hasher = Sha256::new();
    match File::open(part) {
        Ok(mut file) => {
            let size = copy_hashed(&mut file, &mut io::sink(), &mut hasher)?;
            Ok((hasher, size))
        }
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok((hasher, 0)),
        Err(err) => Err(io_failure(format!("cannot read {}", part.display()))(err)),
    }
}

/// `board get REF [-o FILE|-] [--resume]`: the bytes, checked against their
/// hash as they are written; a mismatch deletes the output. Without `-o`, the
/// bytes go to standard output unless that is a terminal. `-o -` is the one
/// place standard output is data and not JSON Lines; the blob's line goes to
/// standard error then.
///
/// A download into `FILE` goes to `FILE.part` first and stays there if the
/// transfer breaks off. With `resume`, what a `FILE.part` already holds is
/// kept (and hashed again, so a damaged start is caught at the end) and only
/// the rest is asked for with a `Range`; a part that is too long, or a board
/// that answers with the whole blob, starts over.
pub fn get(api: &Api, reference: &str, output: Option<&str>, resume: bool) -> Result<(), Failure> {
    let hash = hash_of(reference)?;
    let to_stdout = match output {
        Some("-") => true,
        Some(_) => false,
        None if io::stdout().is_terminal() => {
            return Err(Failure::Arguments(
                "standard output is a terminal: give -o FILE (or -o - to print the bytes)".to_owned(),
            ));
        }
        None => true,
    };
    if resume && to_stdout {
        return Err(Failure::Arguments(
            "--resume needs -o FILE: there is nothing to resume on standard output".to_owned(),
        ));
    }
    let path = format!("/sha256/{hash}");
    let mismatch = |got: &str| Failure::Refused {
        code: "hash-mismatch".to_owned(),
        message: format!("the bytes hash to {got}, not {hash}; nothing was kept"),
    };
    let size = match output.filter(|_| !to_stdout) {
        None => {
            let mut download = api.open(&path, DOWNLOAD_TIMEOUT, 0).map_err(|err| api.failure(err))?;
            let mut stdout = io::stdout().lock();
            let mut hasher = Sha256::new();
            let size = copy_hashed(&mut download.reader, &mut stdout, &mut hasher)?;
            stdout.flush().map_err(io_failure("cannot write the output"))?;
            let got = hex(&hasher.finalize());
            if got != hash {
                return Err(mismatch(&got));
            }
            size
        }
        Some(file) => {
            let mut part = std::ffi::OsString::from(file);
            part.push(".part");
            let part = PathBuf::from(part);
            let cannot = |part: &Path| io_failure(format!("cannot write {}", part.display()));
            let (mut hasher, mut have) = if resume { kept(&part)? } else { (Sha256::new(), 0) };
            let mut reader: Box<dyn Read> = Box::new(io::empty());
            // Never ask for bytes past the end: a part longer than the blob is no start of it.
            let total =
                if have > 0 { api.find::<Blob>(&format!("/blobs/{hash}"))?.map(|blob| blob.size) } else { None };
            if total.is_some_and(|total| have > total) {
                (hasher, have) = (Sha256::new(), 0);
            }
            let complete = have > 0 && total == Some(have);
            if !complete {
                let download = api.open(&path, DOWNLOAD_TIMEOUT, have).map_err(|err| api.failure(err))?;
                if have > 0 && !download.partial {
                    // the whole blob is coming, not the rest of it
                    (hasher, have) = (Sha256::new(), 0);
                }
                reader = download.reader;
            }
            let mut out = if have > 0 { OpenOptions::new().append(true).open(&part) } else { File::create(&part) }
                .map_err(cannot(&part))?;
            // a broken-off transfer leaves the part for --resume
            let copied = copy_hashed(&mut reader, &mut out, &mut hasher)?;
            out.sync_all().map_err(cannot(&part))?;
            let got = hex(&hasher.finalize());
            if got != hash {
                drop(fs::remove_file(&part));
                return Err(mismatch(&got));
            }
            fs::rename(&part, file).map_err(io_failure(format!("cannot move {} to {file}", part.display())))?;
            have.saturating_add(copied)
        }
    };
    let info = api.find::<Blob>(&format!("/blobs/{hash}"))?;
    let blob = info.unwrap_or_else(|| Blob {
        reference: blob::reference(&hash),
        url: format!("/sha256/{hash}"),
        hash,
        size,
        media_type: blob::DEFAULT_MEDIA_TYPE.to_owned(),
        filename: String::new(),
        existed: None,
        listed: None,
    });
    let blob = printed(api, blob);
    if to_stdout {
        let line = json_line(&Line::Blob(&blob))
            .map_err(|err| Failure::Exit(format!("cannot write a line as JSON: {err}")))?;
        warn(&line)
    } else {
        emit(&Line::Blob(&blob))
    }
}

/// `board blob-rm HASH`.
pub fn remove(api: &Api, reference: &str) -> Result<(), Failure> {
    let hash = hash_of(reference)?;
    let signer = signer()?;
    let removed: BlobRemoved = submit(api, &signer, "/blob-delete", &DeleteBlob { sha256: &hash })?;
    emit(&Line::BlobRemoved { hash: &removed.hash })
}

/// What `blobs` lists.
#[allow(clippy::struct_excessive_bools)]
pub struct Listing {
    pub mine: bool,
    pub unreferenced: bool,
    pub evictable: bool,
    pub limit: Option<u32>,
    pub summary: bool,
}

/// `board blobs`: newest upload first, or the store's totals.
pub fn list(api: &Api, what: &Listing) -> Result<(), Failure> {
    let mut pairs: Vec<(&str, String)> = Vec::new();
    if what.mine {
        pairs.push(("mine", signer()?.key.fingerprint()));
    }
    if what.unreferenced {
        pairs.push(("unreferenced", "1".to_owned()));
    }
    if what.evictable {
        pairs.push(("evictable", "1".to_owned()));
    }
    let limit = if what.summary { Some(1) } else { what.limit };
    pairs.extend(limit.map(|n| ("limit", n.to_string())));
    let list: BlobList = api.get(&format!("/blobs{}", query(&pairs)))?;
    if what.summary {
        return emit(&Line::BlobSummary(&list.summary));
    }
    for blob in list.blobs {
        emit(&Line::Blob(&printed(api, blob)))?;
    }
    if list.truncated == Some(true) {
        warn("board: blobs: more than --limit; raise it, or narrow the list")?;
    }
    Ok(())
}

/// The references events made, as the board resolves them: fetched once for
/// every event a read is about to print.
#[derive(Default)]
pub struct Refs(BTreeMap<i64, Vec<BlobRef>>);

impl Refs {
    /// What the events `ids` referenced; nothing is asked for none.
    pub fn of(api: &Api, ids: &[i64]) -> Result<Self, Failure> {
        let mut by_event: BTreeMap<i64, Vec<BlobRef>> = BTreeMap::new();
        for chunk in ids.chunks(200) {
            let pairs: Vec<(&str, String)> = chunk.iter().map(|id| ("event", id.to_string())).collect();
            let answer: BlobRefs = api.get(&format!("/blob-refs{}", query(&pairs)))?;
            for reference in answer.refs {
                by_event.entry(reference.event).or_default().push(reference);
            }
        }
        Ok(Self(by_event))
    }

    /// The `blob_ref` lines of event `id`, if it referenced any.
    pub fn emit(&self, id: i64) -> Result<(), Failure> {
        for reference in self.0.get(&id).into_iter().flatten() {
            emit(&Line::BlobRef(reference))?;
        }
        Ok(())
    }
}

/// Whether `body` may reference a blob: what is worth asking the board
/// about. A body that does not has none, whatever else it says.
pub fn may_reference(body: &str) -> bool {
    body.contains(blob::SCHEME)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_blob_is_named_by_its_hash_or_its_reference() {
        let hash = "ab".repeat(32);
        assert_eq!(hash_of(&hash).ok().as_deref(), Some(hash.as_str()));
        assert_eq!(hash_of(&format!("board-blob://{hash}")).ok().as_deref(), Some(hash.as_str()));
        for bad in ["", "abc", &hash.to_uppercase(), &format!("{hash}0"), "board-blob://", "../../x"] {
            assert!(matches!(hash_of(bad), Err(Failure::Arguments(_))), "{bad}");
        }
    }

    #[test]
    fn hex_is_lowercase_two_digits_a_byte() {
        assert_eq!(hex(&[0, 1, 0xab, 0xff]), "0001abff");
    }

    #[test]
    fn only_a_body_with_the_scheme_is_asked_about() {
        assert!(may_reference("see board-blob://x"));
        assert!(!may_reference("no files here"));
    }

    #[test]
    fn a_file_is_hashed_with_its_size() -> Result<(), Box<dyn std::error::Error>> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("f");
        fs::write(&path, b"abc")?;
        assert_eq!(digest(&path)?, ("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".to_owned(), 3));
        Ok(())
    }
}
