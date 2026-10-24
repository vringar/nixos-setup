//! Blobs: the index of the content-addressed files (see `blobdir.rs`), the
//! references bodies make to them, which references keep a blob alive, and
//! what gives way when the store is full.
//!
//! # The promise
//!
//! A reference is never rendered as a link to a file that does not exist at
//! read time. A blob that something live *pins* can be neither deleted nor
//! evicted, by anyone; every other reference is shown by whether its blob
//! exists when it is read (`pinned`, `released`, `gone`).
//!
//! # What pins
//!
//! Exactly three kinds of holder, and nothing else:
//!
//! - an **open** queue item (the filing's references; answering, declining or
//!   cancelling releases them, and the resolution's own pin nothing);
//! - a thread **message** that is not archived;
//! - the **latest version of each note** (saving a note replaces its pin set).
//!
//! To keep a file, reference it from a note or a thread message. Nothing is
//! deleted from `blob_refs` when a reference stops pinning: pinned-ness is
//! computed by [`live`], not stored.
//!
//! # No event
//!
//! An upload is not an event and does not move the head: the coordination
//! fact is the message that references the blob.
//!
//! # Eviction
//!
//! Only under upload pressure, never on a timer: an upload that does not fit
//! evicts unpinned blobs older than the grace, oldest upload first (an upload
//! of the same bytes again refreshes it), or is refused `blob-store-full`
//! naming what pins the most. The plan is made before the body is read and
//! made again, under the commit lock, before anything is deleted.

use std::collections::BTreeMap;

use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension as _, Row, params, params_from_iter};
use serde::Serialize;
use serde_json::{Value as Json, json};

use crate::blob;
use crate::timefmt;

use super::actions::{DeleteBlob, PutBlob, Signed, check_nonce, verify};
use super::events::event_archived;
use super::{Board, DbError, Error, head, keys, text};

/// The limits an upload is held to, set by the server's environment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    /// The most bytes the blobs may total.
    pub cap: u64,
    /// The most bytes one blob may have.
    pub max: u64,
    /// How long an unreferenced blob is safe from eviction, in seconds.
    pub grace: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Self { cap: blob::CAP_DEFAULT, max: blob::MAX_DEFAULT, grace: blob::GRACE_DEFAULT }
    }
}

/// The key that uploaded: the first uploader of a blob is recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Uploader {
    pub name: String,
    pub fingerprint: String,
}

/// One blob, as the API tells it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BlobInfo {
    pub hash: String,
    /// Relative to the board.
    pub url: String,
    pub reference: String,
    pub size: u64,
    pub media_type: String,
    pub filename: String,
    pub from: String,
    pub signed_by: String,
    /// If the uploading key has been revoked since: the event that did it.
    pub revoked_at: Option<i64>,
    /// The upload time: refreshed whenever anyone uploads the same bytes.
    pub ts: String,
    /// How many events reference it.
    pub refs: u64,
    /// Whether something live pins it.
    pub pinned: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
}

/// What a reference to a blob stands at, read now.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum RefState {
    /// Something live pins the blob: it cannot be removed.
    Pinned,
    /// It exists, nothing pins it: it may go when the store is full.
    Released,
    /// It no longer exists.
    Gone,
}

/// A reference some event made, with the blob's state now.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BlobRef {
    pub event: i64,
    pub hash: String,
    pub reference: String,
    pub state: RefState,
    /// `None` once the blob is gone.
    pub size: Option<u64>,
    pub filename: Option<String>,
    /// The event is archived.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub archived: bool,
}

/// A live event that references a blob.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BlobHolder {
    pub event: i64,
    pub path: String,
}

/// A node, with how much of the store its live references pin.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Pinner {
    pub path: String,
    pub bytes: u64,
    pub blobs: u64,
}

/// Why an upload does not fit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoreFull {
    pub used: u64,
    pub cap: u64,
    pub needed: u64,
    /// What eviction could free: unpinned, and past the grace.
    pub evictable: u64,
    pub pinned: u64,
    pub in_grace: u64,
    pub grace: u64,
    /// The five nodes that pin the most (a blob two nodes reference counts for both).
    pub pinned_by: Vec<Pinner>,
    /// The disk is full, whatever the cap says.
    pub disk: bool,
}

impl std::fmt::Display for StoreFull {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.disk {
            return write!(f, "the disk is full: this {} blob does not fit", blob::human(self.needed));
        }
        write!(
            f,
            "the blob store is full: {} of {} used; {} is pinned by live messages, open items and current notes, \
             {} is under the {} grace; ",
            blob::human(self.used),
            blob::human(self.cap),
            blob::human(self.pinned),
            blob::human(self.in_grace),
            grace_text(self.grace),
        )?;
        if self.evictable == 0 {
            write!(f, "nothing can be evicted for this {} upload.", blob::human(self.needed))?;
        } else {
            write!(
                f,
                "evicting everything that can go ({}) still leaves no room for this {} upload.",
                blob::human(self.evictable),
                blob::human(self.needed)
            )?;
        }
        if !self.pinned_by.is_empty() {
            let largest: Vec<String> = self
                .pinned_by
                .iter()
                .map(|p| format!("{} {} ({} blobs)", p.path, blob::human(p.bytes), p.blobs))
                .collect();
            write!(
                f,
                " Largest pinners: {}; archive or delete those messages to free space (board-admin).",
                largest.join(", ")
            )?;
        }
        Ok(())
    }
}

impl StoreFull {
    #[must_use]
    pub fn to_json(&self) -> Json {
        json!({
            "used": self.used, "cap": self.cap, "needed": self.needed, "evictable": self.evictable,
            "pinned": self.pinned, "in_grace": self.in_grace, "grace_secs": self.grace,
            "pinned_by": self.pinned_by,
        })
    }
}

/// A grace period as a message says it: `1 h`, `90 s`.
fn grace_text(secs: u64) -> String {
    if secs >= 3600 && secs.is_multiple_of(3600) {
        format!("{} h", secs / 3600)
    } else if secs >= 60 && secs.is_multiple_of(60) {
        format!("{} min", secs / 60)
    } else {
        format!("{secs} s")
    }
}

pub(super) fn in_use_message(hash: &str, refs: &[BlobHolder], total: usize) -> String {
    let shown: Vec<String> = refs.iter().take(3).map(|h| format!("{} {}", h.event, h.path)).collect();
    let more = total.saturating_sub(shown.len());
    let and_more = if more > 0 { format!(" (and {more} more)") } else { String::new() };
    format!(
        "board-blob://{hash} is referenced by {total} live {}: {}{and_more}. Archive or delete those messages first \
         (owner: board-admin)",
        if total == 1 { "holder" } else { "holders" },
        shown.join(", ")
    )
}

pub(super) fn gone_message(hashes: &[String]) -> String {
    let named: Vec<String> = hashes.iter().map(|h| blob::reference(h)).collect();
    format!(
        "{} {} evicted while this was archived; upload the same file again (board put) and retry",
        named.join(", "),
        if hashes.len() == 1 { "was" } else { "were" }
    )
}

/// The store's totals.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlobStats {
    pub used: u64,
    pub count: u64,
    pub pinned_bytes: u64,
}

/// A list of blobs, newest upload first.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BlobPage {
    pub blobs: Vec<BlobInfo>,
    /// There are more: ask again `after` the last hash.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub truncated: bool,
}

/// Which blobs a list asks for.
#[derive(Debug, Clone, Copy)]
pub struct BlobQuery<'a> {
    /// Only blobs nothing pins.
    pub unreferenced: bool,
    /// Only blobs eviction could take: unpinned and past the grace.
    pub evictable: bool,
    /// Only blobs this key (a fingerprint) uploaded first.
    pub mine: Option<&'a str>,
    pub limit: usize,
    /// Only blobs listed after this one.
    pub after: Option<&'a str>,
    pub grace: u64,
}

/// How an upload goes on once it is signed, admitted and has a place.
#[derive(Debug)]
pub enum Begun {
    /// The bytes are stored already (their upload time is refreshed).
    Existed(Box<BlobInfo>),
    /// Send the body.
    Proceed(Uploader),
}

/// A verified upload, to record.
#[derive(Debug, Clone, Copy)]
pub struct Landing<'a> {
    pub put: PutBlob<'a>,
    pub uploader: &'a Uploader,
    pub nonce: &'a str,
    pub signature: &'a str,
}

/// A blob given up to make room.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Victim {
    pub hash: String,
    pub size: u64,
    pub ts: String,
    pub uploader: String,
}

fn count(n: i64) -> u64 {
    u64::try_from(n).unwrap_or(0)
}

/// SQL: whether event `e` (an alias) is a holder that pins what it
/// references: not archived (itself, or by an archived node above it), and
/// the open item's filing, a message, or a note's latest version.
fn live(e: &str) -> String {
    format!(
        "({e}.archived IS NULL
          AND NOT EXISTS (SELECT 1 FROM nodes la JOIN nodes ln ON ln.id = {e}.node WHERE la.archived IS NOT NULL
                          AND (ln.path = la.path OR substr(ln.path, 1, length(la.path) + 1) = la.path || '/'))
          AND CASE {e}.type
                WHEN 'message' THEN 1
                WHEN 'note_version' THEN {e}.id = (SELECT max(id) FROM events WHERE node = {e}.node
                                                    AND type = 'note_version')
                WHEN 'item_filed' THEN EXISTS (SELECT 1 FROM items li WHERE li.id = {e}.id AND li.state = 'open')
                ELSE 0
              END)"
    )
}

/// SQL: whether blob `hash` (an expression) is pinned.
fn pinned(hash: &str) -> String {
    format!(
        "EXISTS (SELECT 1 FROM blob_refs pr JOIN events pe ON pe.id = pr.event WHERE pr.sha256 = {hash} AND {})",
        live("pe")
    )
}

const INFO_SELECT: &str = "SELECT b.sha256, b.size, b.media_type, b.filename, b.uploader, b.uploader_fp, k.revoked,
        b.ts, (SELECT count(*) FROM blob_refs cr WHERE cr.sha256 = b.sha256), PINNED, b.signature
   FROM blobs b LEFT JOIN keys k ON k.fingerprint = b.uploader_fp";

fn info_select() -> String {
    INFO_SELECT.replace("PINNED", &pinned("b.sha256"))
}

fn info_row(r: &Row<'_>) -> rusqlite::Result<BlobInfo> {
    let hash = text(r, 0)?;
    Ok(BlobInfo {
        url: format!("/sha256/{hash}"),
        reference: blob::reference(&hash),
        hash,
        size: count(r.get(1)?),
        media_type: text(r, 2)?,
        filename: text(r, 3)?,
        from: text(r, 4)?,
        signed_by: text(r, 5)?,
        revoked_at: r.get(6)?,
        ts: text(r, 7)?,
        refs: count(r.get(8)?),
        pinned: r.get(9)?,
        signature: Some(text(r, 10)?),
    })
}

fn info(conn: &Connection, hash: &str) -> rusqlite::Result<Option<BlobInfo>> {
    conn.query_row(&format!("{} WHERE b.sha256 = ?", info_select()), [hash], info_row).optional()
}

/// Refresh the upload time of `hash` if stored; its row.
fn touch(conn: &Connection, hash: &str, now: &str) -> rusqlite::Result<Option<BlobInfo>> {
    conn.execute("UPDATE blobs SET ts = ? WHERE sha256 = ?", params![now, hash])?;
    info(conn, hash)
}

/// The oldest upload time eviction may take.
fn cutoff(grace: u64) -> Result<String, Error> {
    Ok(timefmt::format(timefmt::unix_now()?.saturating_sub(grace)))
}

/// The store's bytes, by what may become of them.
struct Room {
    used: u64,
    evictable: u64,
    pinned: u64,
    in_grace: u64,
}

fn survey(conn: &Connection, cutoff: &str) -> rusqlite::Result<Room> {
    let sql = "SELECT COALESCE(sum(size), 0),
                      COALESCE(sum(CASE WHEN NOT pinned AND ts <= ?1 THEN size END), 0),
                      COALESCE(sum(CASE WHEN pinned THEN size END), 0),
                      COALESCE(sum(CASE WHEN NOT pinned AND ts > ?1 THEN size END), 0)
                 FROM (SELECT b.size AS size, b.ts AS ts, PINNED AS pinned FROM blobs b)"
        .replace("PINNED", &pinned("b.sha256"));
    conn.query_row(&sql, [cutoff], |r| {
        Ok(Room {
            used: count(r.get(0)?),
            evictable: count(r.get(1)?),
            pinned: count(r.get(2)?),
            in_grace: count(r.get(3)?),
        })
    })
}

/// Unpinned blobs past the grace, oldest upload first.
fn candidates(conn: &Connection, cutoff: &str) -> rusqlite::Result<Vec<Victim>> {
    let sql = "SELECT b.sha256, b.size, b.ts, b.uploader FROM blobs b WHERE b.ts <= ? AND NOT PINNED
                ORDER BY b.ts, b.sha256"
        .replace("PINNED", &pinned("b.sha256"));
    conn.prepare(&sql)?
        .query_map([cutoff], |r| {
            Ok(Victim { hash: text(r, 0)?, size: count(r.get(1)?), ts: text(r, 2)?, uploader: text(r, 3)? })
        })?
        .collect()
}

/// How many of `sizes` (oldest first), taken in order, make `needed` bytes
/// fit under `cap` beside `used` and `reserved`: `Some(0)` if they fit
/// already, `None` if even all of them do not.
fn victims_needed(used: u64, reserved: u64, cap: u64, needed: u64, sizes: &[u64]) -> Option<usize> {
    let over = used.saturating_add(reserved).saturating_add(needed).saturating_sub(cap);
    if over == 0 {
        return Some(0);
    }
    let mut freed = 0_u64;
    for (taken, size) in sizes.iter().enumerate() {
        freed = freed.saturating_add(*size);
        if freed >= over {
            return Some(taken.saturating_add(1));
        }
    }
    None
}

/// The five nodes whose live references pin the most bytes.
fn pinners(conn: &Connection) -> rusqlite::Result<Vec<Pinner>> {
    let sql = format!(
        "SELECT n.path, sum(b.size), count(*)
           FROM (SELECT DISTINCT e.node AS node, r.sha256 AS sha256
                   FROM blob_refs r JOIN events e ON e.id = r.event WHERE {}) d
           JOIN nodes n ON n.id = d.node JOIN blobs b ON b.sha256 = d.sha256
          GROUP BY n.id ORDER BY sum(b.size) DESC, n.path LIMIT 5",
        live("e")
    );
    conn.prepare(&sql)?
        .query_map([], |r| Ok(Pinner { path: text(r, 0)?, bytes: count(r.get(1)?), blobs: count(r.get(2)?) }))?
        .collect()
}

fn full(conn: &Connection, room: &Room, limits: &Limits, needed: u64) -> rusqlite::Result<StoreFull> {
    Ok(StoreFull {
        used: room.used,
        cap: limits.cap,
        needed,
        evictable: room.evictable,
        pinned: room.pinned,
        in_grace: room.in_grace,
        grace: limits.grace,
        pinned_by: pinners(conn)?,
        disk: false,
    })
}

impl StoreFull {
    /// The refusal for a disk with no room left.
    #[must_use]
    pub fn disk(needed: u64) -> Self {
        Self {
            used: 0,
            cap: 0,
            needed,
            evictable: 0,
            pinned: 0,
            in_grace: 0,
            grace: 0,
            pinned_by: Vec::new(),
            disk: true,
        }
    }
}

/// Check the references of event `id`'s `body` and record them, in the
/// transaction that writes the event: a reference that is not one is refused
/// `bad-reference`, one to a blob the board does not have `unknown-blob`, with
/// nothing written. The check and the write happen under the board's lock, so
/// no eviction or delete can come between them.
pub(super) fn record_refs(conn: &Connection, id: i64, body: &str) -> Result<(), Error> {
    let hashes = blob::references(body).map_err(|err| Error::BadReference(err.to_string()))?;
    for hash in hashes {
        let there: bool = conn.query_row("SELECT count(*) > 0 FROM blobs WHERE sha256 = ?", [&hash], |r| r.get(0))?;
        if !there {
            return Err(Error::UnknownBlob { hash });
        }
        conn.execute("INSERT OR IGNORE INTO blob_refs(event, sha256) VALUES(?, ?)", params![id, hash])?;
    }
    Ok(())
}

/// Refuse to show message or item `target` again if a file it references is
/// gone: it would link what does not exist.
pub(super) fn check_unarchive(conn: &Connection, target: i64) -> Result<(), Error> {
    let hashes: Vec<String> = conn
        .prepare(
            "SELECT DISTINCT r.sha256 FROM blob_refs r JOIN events e ON e.id = r.event
              WHERE (e.id = ?1 OR e.item = ?1) AND NOT EXISTS (SELECT 1 FROM blobs b WHERE b.sha256 = r.sha256)
              ORDER BY r.sha256",
        )?
        .query_map([target], |r| text(r, 0))?
        .collect::<rusqlite::Result<_>>()?;
    if hashes.is_empty() { Ok(()) } else { Err(Error::BlobGone { hashes }) }
}

/// The ids of `events` as an SQL list: they are the board's own integers.
fn id_list(events: &[i64]) -> String {
    events.iter().map(i64::to_string).collect::<Vec<_>>().join(",")
}

impl Board {
    /// Start an upload: verify its signature, admit the key, check the nonce,
    /// and decide. Bytes stored already only have their upload time
    /// refreshed ([`Begun::Existed`]); otherwise the store must have room for
    /// `size` beside what `reserved` uploads in flight promised, perhaps by
    /// evicting (which is done only at [`Board::blob_make_room`]).
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::StaleNonce`];
    /// [`Error::BadRequest`] for an address that is none or an empty blob;
    /// [`Error::BlobTooLarge`]; [`Error::BlobStoreFull`].
    pub fn blob_begin(&self, signed: &Signed<'_, PutBlob<'_>>, limits: &Limits, reserved: u64) -> Result<Begun, Error> {
        let put = &signed.action;
        if !blob::is_hash(put.sha256) {
            return Err(Error::BadRequest("sha256 is 64 lowercase hex digits".to_owned()));
        }
        if put.size == 0 {
            return Err(Error::BadRequest("a blob is not empty".to_owned()));
        }
        if put.size > limits.max {
            return Err(Error::BlobTooLarge { size: put.size, max: limits.max });
        }
        let verified = verify(signed)?;
        self.write(|conn, now| {
            let author = keys::admit(conn, verified)?;
            check_nonce(conn, signed.nonce)?;
            if let Some(info) = touch(conn, put.sha256, now)? {
                return Ok(Begun::Existed(Box::new(info)));
            }
            let cutoff = cutoff(limits.grace)?;
            let room = survey(conn, &cutoff)?;
            let sizes: Vec<u64> = candidates(conn, &cutoff)?.iter().map(|v| v.size).collect();
            if victims_needed(room.used, reserved, limits.cap, put.size, &sizes).is_none() {
                return Err(Error::BlobStoreFull(Box::new(full(conn, &room, limits, put.size)?)));
            }
            Ok(Begun::Proceed(Uploader { name: author.name, fingerprint: author.fingerprint }))
        })
    }

    /// Make room for `needed` bytes now, under the commit lock and with the
    /// bytes verified: the plan is made again (a reference may have landed
    /// since [`Board::blob_begin`]), and the blobs it picks are deleted from
    /// the index in one transaction; the caller then unlinks their files.
    /// `others` is what the other uploads in flight promised.
    ///
    /// # Errors
    /// [`Error::BlobStoreFull`] if it does not fit after all: nothing is
    /// evicted then.
    pub fn blob_make_room(&self, needed: u64, others: u64, limits: &Limits) -> Result<Vec<Victim>, Error> {
        self.write(|conn, _| {
            let cutoff = cutoff(limits.grace)?;
            let room = survey(conn, &cutoff)?;
            let all = candidates(conn, &cutoff)?;
            let sizes: Vec<u64> = all.iter().map(|v| v.size).collect();
            let Some(taken) = victims_needed(room.used, others, limits.cap, needed, &sizes) else {
                return Err(Error::BlobStoreFull(Box::new(full(conn, &room, limits, needed)?)));
            };
            let chosen: Vec<Victim> = all.into_iter().take(taken).collect();
            for victim in &chosen {
                conn.execute("DELETE FROM blobs WHERE sha256 = ?", [&victim.hash])?;
            }
            Ok(chosen)
        })
    }

    /// Refresh the upload time of blob `hash`, if the board has it.
    ///
    /// # Errors
    /// On a database fault.
    pub fn blob_touch(&self, hash: &str) -> Result<Option<BlobInfo>, Error> {
        self.write(|conn, now| Ok(touch(conn, hash, now)?))
    }

    /// Record a blob whose file is in place and durable.
    ///
    /// # Errors
    /// On a database fault.
    pub fn blob_land(&self, landing: &Landing<'_>) -> Result<BlobInfo, Error> {
        let put = &landing.put;
        self.write(|conn, now| {
            let head = head(conn)?;
            conn.execute(
                "INSERT INTO blobs(sha256, size, media_type, filename, uploader, uploader_fp, ts, head, nonce, signature)
                 VALUES(?,?,?,?,?,?,?,?,?,?)",
                params![
                    put.sha256,
                    i64::try_from(put.size).map_err(|_too_large| Error::BadRequest("a blob is too large".to_owned()))?,
                    put.media_type,
                    put.filename,
                    landing.uploader.name,
                    landing.uploader.fingerprint,
                    now,
                    head,
                    landing.nonce,
                    landing.signature
                ],
            )?;
            info(conn, put.sha256)?.ok_or_else(|| Error::BlobMissing { hash: put.sha256.to_owned() })
        })
    }

    /// Delete blob `hash` from the index (the caller unlinks the file): the
    /// first uploader or the owner may, and nobody while something pins it.
    ///
    /// # Errors
    /// A refusal of the signature or key; [`Error::NotFound`];
    /// [`Error::Forbidden`]; [`Error::BlobInUse`].
    pub fn blob_delete(&self, signed: &Signed<'_, DeleteBlob<'_>>) -> Result<(), Error> {
        let hash = signed.action.sha256;
        if !blob::is_hash(hash) {
            return Err(Error::BadRequest("sha256 is 64 lowercase hex digits".to_owned()));
        }
        let verified = verify(signed)?;
        self.write(|conn, _| {
            let author = keys::admit(conn, verified)?;
            check_nonce(conn, signed.nonce)?;
            let uploader: String = conn
                .query_row("SELECT uploader_fp FROM blobs WHERE sha256 = ?", [hash], |r| text(r, 0))
                .optional()?
                .ok_or_else(|| Error::NotFound(format!("no blob {hash}")))?;
            if uploader != author.fingerprint && !author.human {
                return Err(Error::Forbidden(
                    "only the key that uploaded a blob, or the owner key, deletes it".to_owned(),
                ));
            }
            let holders: Vec<BlobHolder> = conn
                .prepare(&format!(
                    "SELECT e.id, n.path FROM blob_refs r JOIN events e ON e.id = r.event JOIN nodes n ON n.id = e.node
                      WHERE r.sha256 = ? AND {} ORDER BY e.id",
                    live("e")
                ))?
                .query_map([hash], |r| Ok(BlobHolder { event: r.get(0)?, path: text(r, 1)? }))?
                .collect::<rusqlite::Result<_>>()?;
            if !holders.is_empty() {
                let total = holders.len();
                return Err(Error::BlobInUse {
                    hash: hash.to_owned(),
                    refs: holders.into_iter().take(20).collect(),
                    total,
                });
            }
            conn.execute("DELETE FROM blobs WHERE sha256 = ?", [hash])?;
            Ok(())
        })
    }

    /// Blob `hash`, if the board has it.
    ///
    /// # Errors
    /// On a database fault.
    pub fn blob(&self, hash: &str) -> Result<Option<BlobInfo>, DbError> {
        self.read_db(|conn| info(conn, hash))
    }

    /// The blobs a query asks for, newest upload first.
    ///
    /// # Errors
    /// [`Error::BadRequest`] for an `after` that is no address.
    pub fn blobs(&self, query: &BlobQuery<'_>) -> Result<BlobPage, Error> {
        if query.after.is_some_and(|after| !blob::is_hash(after)) {
            return Err(Error::BadRequest("after is a blob's 64-digit hash".to_owned()));
        }
        let cutoff = cutoff(query.grace)?;
        let mut sql = format!("{} WHERE 1 = 1", info_select());
        let mut args: Vec<Value> = Vec::new();
        if let Some(mine) = query.mine {
            sql.push_str(" AND b.uploader_fp = ?");
            args.push(Value::Text(mine.to_owned()));
        }
        if query.unreferenced || query.evictable {
            sql.push_str(" AND NOT ");
            sql.push_str(&pinned("b.sha256"));
        }
        if query.evictable {
            sql.push_str(" AND b.ts <= ?");
            args.push(Value::Text(cutoff));
        }
        if let Some(after) = query.after {
            sql.push_str(" AND (b.ts, b.sha256) < (SELECT ts, sha256 FROM blobs WHERE sha256 = ?)");
            args.push(Value::Text(after.to_owned()));
        }
        sql.push_str(" ORDER BY b.ts DESC, b.sha256 DESC LIMIT ?");
        let limit = query.limit.clamp(1, 1000);
        args.push(Value::Integer(i64::try_from(limit.saturating_add(1)).unwrap_or(i64::MAX)));
        let mut blobs: Vec<BlobInfo> = self.read(|conn| {
            Ok(conn.prepare(&sql)?.query_map(params_from_iter(args), info_row)?.collect::<rusqlite::Result<_>>()?)
        })?;
        let truncated = blobs.len() > limit;
        blobs.truncate(limit);
        Ok(BlobPage { blobs, truncated })
    }

    /// The store's totals.
    ///
    /// # Errors
    /// On a database fault.
    pub fn blob_stats(&self) -> Result<BlobStats, DbError> {
        self.read_db(|conn| {
            let room = survey(conn, "")?;
            let count: i64 = conn.query_row("SELECT count(*) FROM blobs", [], |r| r.get(0))?;
            Ok(BlobStats { used: room.used, count: self::count(count), pinned_bytes: room.pinned })
        })
    }

    /// The references events made and what each stands at now, in event
    /// order: what `blob_ref` lines say. No body is read.
    ///
    /// # Errors
    /// On a database fault.
    pub fn blob_refs(&self, events: &[i64]) -> Result<Vec<BlobRef>, DbError> {
        if events.is_empty() {
            return Ok(Vec::new());
        }
        let sql = format!(
            "SELECT r.event, r.sha256, b.size, b.filename, {hidden},
                    CASE WHEN b.sha256 IS NULL THEN 2 WHEN {pinned} THEN 0 ELSE 1 END
               FROM blob_refs r JOIN events e ON e.id = r.event LEFT JOIN blobs b ON b.sha256 = r.sha256
              WHERE r.event IN ({list}) ORDER BY r.event, r.sha256",
            hidden = event_archived!(),
            pinned = pinned("r.sha256"),
            list = id_list(events)
        );
        self.read_db(|conn| {
            conn.prepare(&sql)?
                .query_map([], |r| {
                    let hash = text(r, 1)?;
                    let state = match r.get::<_, i64>(5)? {
                        0 => RefState::Pinned,
                        1 => RefState::Released,
                        _ => RefState::Gone,
                    };
                    let gone = state == RefState::Gone;
                    Ok(BlobRef {
                        event: r.get(0)?,
                        reference: blob::reference(&hash),
                        hash,
                        state,
                        size: if gone { None } else { r.get::<_, Option<i64>>(2)?.map(count) },
                        filename: if gone { None } else { Some(text(r, 3)?) },
                        archived: r.get(4)?,
                    })
                })?
                .collect()
        })
    }

    /// Which of `hashes` exist: each one's size and original file name.
    /// One lookup for a page that links blobs.
    ///
    /// # Errors
    /// On a database fault.
    pub fn blobs_present(&self, hashes: &[String]) -> Result<BTreeMap<String, (u64, String)>, DbError> {
        let valid: Vec<&String> = hashes.iter().filter(|h| blob::is_hash(h)).collect();
        if valid.is_empty() {
            return Ok(BTreeMap::new());
        }
        let marks = vec!["?"; valid.len()].join(",");
        self.read_db(|conn| {
            conn.prepare(&format!("SELECT sha256, size, filename FROM blobs WHERE sha256 IN ({marks})"))?
                .query_map(params_from_iter(valid), |r| Ok((text(r, 0)?, (count(r.get(1)?), text(r, 2)?))))?
                .collect()
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sign;
    use crate::store::Action as _;

    #[test]
    fn eviction_takes_the_oldest_until_the_upload_fits() {
        // used 90 of 100, an upload of 30 needs 20 freed
        assert_eq!(victims_needed(90, 0, 100, 5, &[]), Some(0));
        assert_eq!(victims_needed(90, 0, 100, 30, &[10, 5, 50]), Some(3));
        assert_eq!(victims_needed(90, 0, 100, 30, &[10, 10, 50]), Some(2));
        assert_eq!(victims_needed(90, 0, 100, 30, &[25, 10]), Some(1));
        assert_eq!(victims_needed(90, 0, 100, 30, &[5, 5]), None);
        assert_eq!(victims_needed(90, 0, 100, 30, &[]), None);
        // what other uploads in flight promised counts as used
        assert_eq!(victims_needed(50, 40, 100, 20, &[10]), Some(1));
        assert_eq!(victims_needed(50, 40, 100, 10, &[]), Some(0));
        // arithmetic cannot wrap
        assert_eq!(victims_needed(u64::MAX, u64::MAX, 1, u64::MAX, &[u64::MAX]), Some(1));
    }

    #[test]
    fn a_full_store_says_what_pins_it() {
        let full = StoreFull {
            used: 49_800_000_000,
            cap: 50_000_000_000,
            needed: 3_100_000_000,
            evictable: 0,
            pinned: 31_000_000_000,
            in_grace: 3_000_000_000,
            grace: 3600,
            pinned_by: vec![
                Pinner { path: "reversing/emu/log".to_owned(), bytes: 21_000_000_000, blobs: 14 },
                Pinner { path: "reversing/emu/aspfuzz".to_owned(), bytes: 9_000_000_000, blobs: 3 },
            ],
            disk: false,
        };
        let message = full.to_string();
        assert!(message.starts_with("the blob store is full: 49.8 GB of 50 GB used; 31 GB is pinned"), "{message}");
        assert!(message.contains("3 GB is under the 1 h grace"), "{message}");
        assert!(message.contains("nothing can be evicted for this 3.1 GB upload"), "{message}");
        assert!(
            message.contains("reversing/emu/log 21 GB (14 blobs), reversing/emu/aspfuzz 9 GB (3 blobs)"),
            "{message}"
        );
        assert!(StoreFull::disk(5_000).to_string().contains("disk is full"));
    }

    #[test]
    fn a_blob_in_use_names_its_holders() {
        let holder = |event, path: &str| BlobHolder { event, path: path.to_owned() };
        let refs = [holder(10_360, "a/log"), holder(10_412, "a/log"), holder(10_500, "a/other"), holder(1, "x/y")];
        let message = in_use_message("ab", &refs, 7);
        assert_eq!(
            message,
            "board-blob://ab is referenced by 7 live holders: 10360 a/log, 10412 a/log, 10500 a/other (and 4 more). \
             Archive or delete those messages first (owner: board-admin)"
        );
        assert!(gone_message(&["ab".to_owned()]).starts_with("board-blob://ab was evicted while this was archived"));
        assert_eq!(grace_text(3600), "1 h");
        assert_eq!(grace_text(1), "1 s");
        assert_eq!(grace_text(120), "2 min");
    }

    #[test]
    fn the_signing_namespaces_are_the_specs() {
        assert_eq!(PutBlob::KIND.namespace, sign::BLOB.namespace);
        assert_eq!(DeleteBlob::KIND.fields, ["sha256", "key", "nonce"]);
    }
}
