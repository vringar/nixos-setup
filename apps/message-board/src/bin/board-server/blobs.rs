//! The blob store over HTTP: `PUT /blob` (a signed, streamed upload),
//! `GET /sha256/<hash>` (the bytes, with ranges), the listings, and the
//! signed delete. The index and the rules (what pins, what is evicted) are
//! the store's; the files are [`BlobDir`]'s; this is the part that holds
//! them to the wire.
//!
//! An upload is checked in full before a body byte is read, and the answer to
//! a failed check is sent before the client is told to go on (`100 Continue`
//! only after the checks), so that a refused upload costs nothing to send:
//! the length, the address, the signature, the key, the nonce, whether the
//! bytes are stored already, whether they fit, and a free upload slot.
//!
//! Then the body is streamed into a temp file in the blob directory, hashed
//! as it goes, and synced; the hash must be the signed one. Landing is one
//! step at a time under the commit lock (not the database's: a fsync must not
//! hold up every other request): make room if need be, rename into place,
//! sync the directory, record the row. A row never exists without its durable
//! file; a crash in between leaves a temp file (purged at start) or an
//! orphan file (harmless, and found again by the next upload of the bytes).

use std::fmt::Write as _;
use std::io::{self, Write as _};
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Mutex, PoisonError};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};

use message_board::blob;
use message_board::blobdir::BlobDir;
use message_board::store::{
    Begun, BlobInfo, BlobQuery, Board, Error as StoreError, Landing, Limits, PutBlob, Signed, StoreFull, Uploader,
};

use crate::http::{Body, CLIENT_TIMEOUT, Request, Response};
use crate::log;
use crate::routes::{Answer, Context, Failure, Params};
use crate::writes::{self, Fields, Json};

/// How much of a body is read and written at once.
const CHUNK: usize = 1024 * 1024;
/// An upload takes at least this long to be given up on, and past it at
/// least a mebibyte per second: a trickling client must not hold a slot.
const MIN_DEADLINE: Duration = Duration::from_secs(600);
const MIB: u64 = 1024 * 1024;

/// What the blob store needs beside the board: the directory, the limits,
/// and the in-process state of the uploads in flight.
pub struct Blobs {
    dir: BlobDir,
    limits: Limits,
    /// Held while a blob lands: one at a time.
    commit: Mutex<()>,
    /// The bytes promised to the uploads in flight, so that two uploads that
    /// each fit do not both land in room for one.
    reserved: Mutex<u64>,
    uploads: AtomicUsize,
}

/// An upload's place: a slot and the bytes it promised, given back on drop,
/// whichever way it ends.
struct Place<'a> {
    blobs: &'a Blobs,
    size: u64,
}

impl Drop for Place<'_> {
    fn drop(&mut self) {
        let mut reserved = self.blobs.reserved.lock().unwrap_or_else(PoisonError::into_inner);
        *reserved = reserved.saturating_sub(self.size);
        self.blobs.uploads.fetch_sub(1, Ordering::SeqCst);
    }
}

/// A temp file, deleted when this goes away: after the rename that makes it
/// a blob there is nothing to delete.
struct Temp(PathBuf);

impl Drop for Temp {
    fn drop(&mut self) {
        // nothing there, after a rename; and nothing to do if it cannot be deleted:
        // the next start purges the directory
        if std::fs::remove_file(&self.0).is_ok() {
            log::line("board: removed an unfinished upload's temp file");
        }
    }
}

/// What deciding an upload came to.
enum Plan<'a> {
    Existed(Box<BlobInfo>),
    Upload(Place<'a>, Uploader),
}

impl Blobs {
    pub fn new(dir: BlobDir, limits: Limits) -> Self {
        Self { dir, limits, commit: Mutex::new(()), reserved: Mutex::new(0), uploads: AtomicUsize::new(0) }
    }

    pub fn limits(&self) -> &Limits {
        &self.limits
    }

    /// Decide an upload (see [`Board::blob_begin`]) and, if it goes on,
    /// promise its bytes and take a slot. The plan and the promise are made
    /// under one lock, so that concurrent plans see each other.
    fn begin(&self, board: &Board, signed: &Signed<'_, PutBlob<'_>>) -> Result<Plan<'_>, StoreError> {
        let mut reserved = self.reserved.lock().unwrap_or_else(PoisonError::into_inner);
        match board.blob_begin(signed, &self.limits, *reserved)? {
            Begun::Existed(info) => Ok(Plan::Existed(info)),
            Begun::Proceed(uploader) => {
                let size = signed.action.size;
                let taken = self.uploads.try_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                    (n < blob::UPLOADS_MAX).then(|| n.saturating_add(1))
                });
                if taken.is_err() {
                    return Err(StoreError::UploadsBusy(blob::UPLOADS_MAX));
                }
                *reserved = reserved.saturating_add(size);
                Ok(Plan::Upload(Place { blobs: self, size }, uploader))
            }
        }
    }

    /// What the other uploads in flight have promised, besides `size`.
    fn others(&self, size: u64) -> u64 {
        self.reserved.lock().unwrap_or_else(PoisonError::into_inner).saturating_sub(size)
    }
}

/// Whether a request is a blob upload, whose body is streamed.
pub fn streamed(method: &str, path: &str) -> bool {
    method == "PUT" && path == "/blob"
}

/// What an upload's answer says.
fn stored(info: &BlobInfo, existed: bool) -> Value {
    json!({
        "hash": info.hash, "url": info.url, "reference": info.reference, "size": info.size,
        "media_type": info.media_type, "filename": info.filename, "existed": existed,
    })
}

fn header_error(err: String) -> StoreError {
    StoreError::BadRequest(err)
}

fn json_answer(value: &Value) -> Answer {
    Ok(Response::json(200, serde_json::to_string(value)?))
}

fn internal(what: &str, err: &io::Error) -> Failure {
    log::line(&format!("board: {what}: {err}"));
    StoreError::Internal(format!("{what}: {err}")).into()
}

/// `PUT /blob`: the upload, as the module docs say.
pub fn put(app: &crate::routes::App, request: &Request, body: &mut Body<'_, '_>, ctx: &Context<'_>) -> Answer {
    let blobs = &app.blobs;
    let board = &app.board;
    let header = |name: &str| request.header(name).unwrap_or_default();
    let sha256 = header("board-sha256");
    if !blob::is_hash(sha256) {
        return Err(
            StoreError::BadRequest("Board-Sha256 is the blob's sha256: 64 lowercase hex digits".to_owned()).into()
        );
    }
    let length = request.length.ok_or(StoreError::LengthRequired)?;
    if length == 0 {
        return Err(StoreError::BadRequest("a blob is not empty".to_owned()).into());
    }
    if length > blobs.limits.max {
        return Err(StoreError::BlobTooLarge { size: length, max: blobs.limits.max }.into());
    }
    let media_type = match request.header("content-type") {
        Some(given) => blob::media_type(given).map_err(header_error)?,
        None => blob::DEFAULT_MEDIA_TYPE.to_owned(),
    };
    let filename = blob::pct_decode(header("board-filename")).map_err(header_error)?;
    blob::check_filename(&filename).map_err(header_error)?;
    let signature = header("board-signature");
    if signature.trim().is_empty() {
        return Err(Failure::Unsigned);
    }
    let put = PutBlob { sha256, size: length, media_type: &media_type, filename: &filename };
    let (key, nonce) = (header("board-key"), header("board-nonce"));
    let signed = Signed { action: put, key, nonce, signature };

    let (place, uploader) = match blobs.begin(board, &signed)? {
        Plan::Existed(info) => return json_answer(&stored(&info, true)),
        Plan::Upload(place, uploader) => (place, uploader),
    };
    if request.expect_continue && body.send_continue().is_err() {
        return Err(Failure::Gone);
    }
    let temp = receive(blobs, body, &put)?;
    let info = land(app, &place, &temp, &Landing { put, uploader: &uploader, nonce, signature })?;
    let (info, existed) = info;
    ctx.log(&format!(
        "{} blob {} ({} bytes) for {}",
        if existed { "had" } else { "stored" },
        info.hash,
        info.size,
        uploader.name
    ));
    json_answer(&stored(&info, existed))
}

/// The body into a temp file, hashed as it goes and synced; the hash must be
/// the signed one. A client that leaves, or stalls, gets no answer.
fn receive(blobs: &Blobs, body: &mut Body<'_, '_>, put: &PutBlob<'_>) -> Result<Temp, Failure> {
    let (mut file, path) = blobs.dir.create_temp().map_err(|err| internal("cannot make a temp file", &err))?;
    let temp = Temp(path);
    let deadline = Instant::now() + MIN_DEADLINE.max(Duration::from_secs(put.size / MIB));
    let mut hasher = Sha256::new();
    let mut buf = vec![0_u8; CHUNK];
    loop {
        let n = match body.read(&mut buf, CLIENT_TIMEOUT, deadline) {
            Ok(n) => n,
            Err(err) => {
                log::line(&format!("board: an upload of {} stopped: {err}", put.sha256));
                return Err(Failure::Gone);
            }
        };
        if n == 0 {
            break;
        }
        let chunk = buf.get(..n).unwrap_or_default();
        hasher.update(chunk);
        if let Err(err) = file.write_all(chunk) {
            if err.kind() == io::ErrorKind::StorageFull || err.raw_os_error() == Some(28) {
                return Err(StoreError::BlobStoreFull(Box::new(StoreFull::disk(put.size))).into());
            }
            return Err(internal("cannot write an upload", &err));
        }
    }
    if body.remaining() != 0 {
        log::line(&format!("board: the client left an upload of {} unfinished", put.sha256));
        return Err(Failure::Gone);
    }
    file.sync_data().map_err(|err| internal("cannot sync an upload", &err))?;
    let got = hasher.finalize().iter().fold(String::new(), |mut hex, b| {
        // writing to a String cannot fail
        let _written = write!(hex, "{b:02x}");
        hex
    });
    if got != put.sha256 {
        return Err(StoreError::HashMismatch { expected: put.sha256.to_owned(), got }.into());
    }
    Ok(temp)
}

/// Land a verified upload, under the commit lock: if the bytes arrived
/// meanwhile by another upload, only their time is refreshed; otherwise make
/// room, put the file in place and record it. The flag is whether the bytes
/// were stored already.
fn land(
    app: &crate::routes::App,
    place: &Place<'_>,
    temp: &Temp,
    landing: &Landing<'_>,
) -> Result<(BlobInfo, bool), Failure> {
    let blobs = &app.blobs;
    let board = &app.board;
    let put = &landing.put;
    let _commit = blobs.commit.lock().unwrap_or_else(PoisonError::into_inner);
    if let Some(info) = board.blob_touch(put.sha256)? {
        return Ok((info, true));
    }
    for victim in board.blob_make_room(put.size, blobs.others(place.size), &blobs.limits)? {
        log::line(&format!(
            "board: evicted {} {} uploaded {} by {}",
            victim.hash, victim.size, victim.ts, victim.uploader
        ));
        if let Err(err) = blobs.dir.remove(&victim.hash) {
            log::line(&format!("board: cannot delete the file of evicted {}: {err}", victim.hash));
        }
    }
    if let Err(err) = blobs.dir.place(&temp.0, put.sha256) {
        if err.raw_os_error() == Some(28) {
            return Err(StoreError::BlobStoreFull(Box::new(StoreFull::disk(put.size))).into());
        }
        return Err(internal("cannot place a blob", &err));
    }
    Ok((board.blob_land(landing)?, false))
}

/// What a request for a removed (or never stored) blob is told.
fn removed(hash: &str) -> StoreError {
    StoreError::NotFound(format!(
        "no such blob {hash}: it was removed (an unreferenced blob is evicted when the store is full, or it was \
         deleted); a page or message that linked it was loaded before that happened. To keep a file, reference it \
         from a note or a thread message."
    ))
}

/// Which bytes of a blob a `Range` header asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Span {
    Whole,
    /// First and last byte, both inclusive.
    Part(u64, u64),
    Unsatisfiable,
}

/// `Range` of a `total`-byte blob: one `bytes=a-b`, `a-` or `-n`; several
/// ranges and anything unreadable are ignored (the whole blob is sent).
pub fn span(header: Option<&str>, total: u64) -> Span {
    let Some(spec) = header.and_then(|h| h.trim().strip_prefix("bytes=")) else {
        return Span::Whole;
    };
    if spec.contains(',') {
        return Span::Whole;
    }
    let Some((first, last)) = spec.trim().split_once('-') else {
        return Span::Whole;
    };
    let number =
        |text: &str| (!text.is_empty() && text.bytes().all(|b| b.is_ascii_digit())).then(|| text.parse::<u64>().ok());
    let newest = total.saturating_sub(1);
    match (number(first), number(last)) {
        (Some(Some(start)), Some(Some(end))) if end >= start => {
            if start >= total {
                Span::Unsatisfiable
            } else {
                Span::Part(start, end.min(newest))
            }
        }
        (Some(Some(start)), None) if last.is_empty() => {
            if start >= total {
                Span::Unsatisfiable
            } else {
                Span::Part(start, newest)
            }
        }
        (None, Some(Some(n))) if first.is_empty() => {
            if n == 0 || total == 0 {
                Span::Unsatisfiable
            } else {
                Span::Part(total.saturating_sub(n), newest)
            }
        }
        _ => Span::Whole,
    }
}

/// Whether an `If-None-Match` names `etag` (or anything).
fn matches(header: &str, etag: &str) -> bool {
    header.split(',').map(str::trim).any(|candidate| candidate == "*" || candidate.trim_start_matches("W/") == etag)
}

/// `Content-Disposition` for an original file name (the hash if there is
/// none): attachment always, the name in ASCII and in UTF-8.
fn disposition(filename: &str, hash: &str) -> String {
    let name = if filename.is_empty() { hash } else { filename };
    let ascii: String = name
        .chars()
        .map(|c| if c.is_ascii_graphic() && !matches!(c, '"' | '\\') || c == ' ' { c } else { '_' })
        .collect();
    format!("attachment; filename=\"{ascii}\"; filename*=UTF-8''{}", blob::pct_encode(name))
}

/// `GET /sha256/<hash>`: the bytes. They are never opened, sniffed or
/// interpreted here: an opaque download, immutable for the hash it is
/// named by.
pub fn download(app: &crate::routes::App, hash: &str, request: &Request) -> Answer {
    if !blob::is_hash(hash) {
        return Err(StoreError::NotFound(format!("{hash:?} is no blob address: 64 lowercase hex digits")).into());
    }
    let Some(info) = app.board.blob(hash)? else {
        return Err(removed(hash).into());
    };
    let missing = |why: String| {
        log::line(&format!("board: blob {hash} has a row but {why}"));
        Failure::from(StoreError::BlobMissing { hash: hash.to_owned() })
    };
    let file = app.blobs.dir.open_blob(hash).map_err(|err| missing(format!("no readable file: {err}")))?;
    let on_disk = file.metadata().map_err(|err| missing(format!("no metadata: {err}")))?.len();
    if on_disk != info.size {
        return Err(missing(format!("a file of {on_disk} bytes, not {}", info.size)));
    }
    let etag = format!("\"{hash}\"");
    let fixed = |response: Response| {
        response
            .with_header("Cache-Control", "private, immutable, max-age=31536000")
            .with_header("ETag", &etag)
            .with_header("X-Content-Type-Options", "nosniff")
            .with_header("Content-Security-Policy", "default-src 'none'; sandbox")
            .with_header("Accept-Ranges", "bytes")
    };
    if request.header("if-none-match").is_some_and(|given| matches(given, &etag)) {
        return Ok(fixed(Response::bytes(304, "application/octet-stream", Vec::new())));
    }
    let total = info.size;
    let response = match span(request.header("range"), total) {
        Span::Whole => Response::file(200, file, 0, total),
        Span::Part(start, end) => {
            let len = end.saturating_sub(start).saturating_add(1);
            Response::file(206, file, start, len).with_header("Content-Range", &format!("bytes {start}-{end}/{total}"))
        }
        Span::Unsatisfiable => {
            return Ok(fixed(
                Response::bytes(416, "application/octet-stream", Vec::new())
                    .with_header("Content-Range", &format!("bytes */{total}")),
            ));
        }
    };
    Ok(fixed(response.with_header("Content-Disposition", &disposition(&info.filename, hash))))
}

/// `GET /blobs/<hash>`: what the board knows of one blob.
pub fn one(board: &Board, hash: &str) -> Answer {
    if !blob::is_hash(hash) {
        return Err(StoreError::NotFound(format!("{hash:?} is no blob address: 64 lowercase hex digits")).into());
    }
    match board.blob(hash)? {
        Some(info) => json_answer(&serde_json::to_value(info)?),
        None => Err(removed(hash).into()),
    }
}

/// `GET /blobs`: newest upload first, and the store's totals. Filters:
/// `unreferenced=1` (nothing pins it), `evictable=1` (and past the grace),
/// `mine=<fingerprint>`, `limit`, `after=<hash>`.
pub fn list(app: &crate::routes::App, q: &Params) -> Answer {
    let limits = app.blobs.limits();
    let query = BlobQuery {
        unreferenced: q.flag("unreferenced"),
        evictable: q.flag("evictable"),
        mine: q.get("mine").filter(|m| !m.is_empty()),
        limit: usize::try_from(q.number("limit")?.unwrap_or(100)).unwrap_or(100),
        after: q.get("after").filter(|a| !a.is_empty()),
        grace: limits.grace,
    };
    let page = app.board.blobs(&query)?;
    let stats = app.board.blob_stats()?;
    let mut out = serde_json::to_value(&page)?;
    if let Some(fields) = out.as_object_mut() {
        fields.insert("used".to_owned(), json!(stats.used));
        fields.insert("cap".to_owned(), json!(limits.cap));
        fields.insert("count".to_owned(), json!(stats.count));
        fields.insert("pinned_bytes".to_owned(), json!(stats.pinned_bytes));
        fields.insert("grace_secs".to_owned(), json!(limits.grace));
        fields.insert("max".to_owned(), json!(limits.max));
    }
    json_answer(&out)
}

/// `GET /blob-refs?event=ID...`: the references these events made, and what
/// each stands at now.
pub fn refs(board: &Board, q: &Params) -> Answer {
    let events = q
        .all("event")
        .map(|id| id.parse::<i64>().map_err(|err| StoreError::BadRequest(format!("{id:?} is not an event id: {err}"))))
        .collect::<Result<Vec<_>, _>>()?;
    if events.len() > 500 {
        return Err(StoreError::BadRequest("at most 500 events at a time".to_owned()).into());
    }
    json_answer(&json!({ "refs": board.blob_refs(&events)? }))
}

/// `POST /blob-delete`: the first uploader or the owner deletes an unpinned
/// blob; its file goes after its row.
pub fn delete(app: &crate::routes::App, fields: &Json) -> Answer {
    let hash = writes::blob_delete(&app.board, fields)?;
    if let Err(err) = app.blobs.dir.remove(&hash) {
        log::line(&format!("board: deleted blob {hash}, but not its file: {err}"));
    }
    json_answer(&json!({ "hash": hash }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_range_is_one_span_of_the_blob() {
        assert_eq!(span(Some("bytes=0-9"), 100), Span::Part(0, 9));
        assert_eq!(span(Some("bytes=90-"), 100), Span::Part(90, 99));
        assert_eq!(span(Some("bytes=-10"), 100), Span::Part(90, 99));
        assert_eq!(span(Some("bytes=-1000"), 100), Span::Part(0, 99));
        assert_eq!(span(Some("bytes=95-1000"), 100), Span::Part(95, 99));
        assert_eq!(span(Some("bytes=100-"), 100), Span::Unsatisfiable);
        assert_eq!(span(Some("bytes=100-120"), 100), Span::Unsatisfiable);
        assert_eq!(span(Some("bytes=-0"), 100), Span::Unsatisfiable);
        // several ranges, other units and nonsense are ignored
        for ignored in ["bytes=0-1,5-6", "items=0-1", "bytes=a-b", "bytes=5-2", "bytes=", "bytes=-", "0-9", "bytes=1"] {
            assert_eq!(span(Some(ignored), 100), Span::Whole, "{ignored}");
        }
        assert_eq!(span(None, 100), Span::Whole);
    }

    #[test]
    fn a_conditional_request_names_the_etag_or_anything() {
        assert!(matches("\"abc\"", "\"abc\""));
        assert!(matches("\"x\", \"abc\"", "\"abc\""));
        assert!(matches("W/\"abc\"", "\"abc\""));
        assert!(matches("*", "\"abc\""));
        assert!(!matches("\"abd\"", "\"abc\""));
    }

    #[test]
    fn a_download_is_an_attachment_named_by_the_original_file() {
        assert_eq!(
            disposition("trace v1.bin", "h"),
            "attachment; filename=\"trace v1.bin\"; filename*=UTF-8''trace%20v1.bin"
        );
        assert_eq!(
            disposition("ü\"x\\.png", "h"),
            "attachment; filename=\"__x_.png\"; filename*=UTF-8''%C3%BC%22x%5C.png"
        );
        assert_eq!(disposition("", "abc"), "attachment; filename=\"abc\"; filename*=UTF-8''abc");
    }

    #[test]
    fn only_a_put_to_blob_is_streamed() {
        assert!(streamed("PUT", "/blob"));
        assert!(!streamed("PUT", "/blob-delete") && !streamed("POST", "/blob") && !streamed("GET", "/blob"));
    }

    #[test]
    fn uploads_take_slots_and_promise_bytes_until_they_end() -> Result<(), Box<dyn std::error::Error>> {
        let dir = tempfile::tempdir()?;
        let blobs = Blobs::new(BlobDir::open(&dir.path().join("blobs"))?, Limits::default());
        blobs.uploads.store(1, Ordering::SeqCst);
        *blobs.reserved.lock().unwrap_or_else(PoisonError::into_inner) = 30;
        {
            let _place = Place { blobs: &blobs, size: 10 };
            assert_eq!(blobs.others(10), 20);
        }
        assert_eq!((blobs.uploads.load(Ordering::SeqCst), blobs.others(0)), (0, 20));
        Ok(())
    }
}
