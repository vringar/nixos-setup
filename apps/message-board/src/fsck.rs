//! `board-admin fsck`: the blob index against the blob directory, read-only.
//!
//! It finds what the store's own rules should make impossible, or leave
//! behind only by a crash:
//!
//! - `missing`: a row whose file is not there (a row never exists without its
//!   durable file; a read of it is a 500 `blob-missing`);
//! - `size`: a row and its file disagree about the size;
//! - `orphan`: a file with no row (a crash between the rename and the row, or
//!   between the row's deletion and the unlink; harmless, and found again by
//!   the next upload of the bytes);
//! - `misplaced`: anything else in the directory: a file not named by its
//!   hash or in the directory of another hash, a directory or file of no
//!   kind the store makes;
//! - `tmp`: an upload's temp file that nothing has written to for a long
//!   while, so it is no upload in flight (the server purges them at start);
//! - `reference`: a reference a live holder makes (one that pins) to a blob
//!   the board does not have: the promise that a pinned reference always
//!   exists, broken;
//! - `corrupt` (only with `deep`): the file's bytes do not hash to its name;
//! - `over-cap`: the rows total more than the cap.
//!
//! It runs beside the server (SQLite lets two programs share a file, and
//! nothing is written), so a finding that a running server could explain
//! (a blob landing or being evicted between two looks) is looked at again
//! before it is reported.

use std::collections::BTreeSet;
use std::fmt;
use std::fs::{self, File};
use std::io::{self, Read as _};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use sha2::{Digest as _, Sha256};

use crate::blob;
use crate::blobdir;
use crate::store::{Board, DbError};

/// How long an upload's temp file may have stood still to be called left over.
/// An upload gives up on a client that sends nothing for far less.
pub const TMP_IDLE: Duration = Duration::from_secs(600);

/// What kind of thing is wrong.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Missing,
    Size,
    Orphan,
    Misplaced,
    Tmp,
    Reference,
    Corrupt,
    OverCap,
}

impl Kind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Missing => "missing",
            Self::Size => "size",
            Self::Orphan => "orphan",
            Self::Misplaced => "misplaced",
            Self::Tmp => "tmp",
            Self::Reference => "reference",
            Self::Corrupt => "corrupt",
            Self::OverCap => "over-cap",
        }
    }
}

/// One thing wrong.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finding {
    pub kind: Kind,
    pub detail: String,
}

impl fmt::Display for Finding {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}", self.kind.as_str(), self.detail)
    }
}

/// What a check found, and what it looked at.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Report {
    pub findings: Vec<Finding>,
    /// The rows of the index, and their total size.
    pub blobs: u64,
    pub indexed_bytes: u64,
    /// The blob files, and their total size.
    pub files: u64,
    pub file_bytes: u64,
    /// Files whose bytes were hashed (only with `deep`).
    pub hashed: u64,
}

impl Report {
    fn find(&mut self, kind: Kind, detail: String) {
        self.findings.push(Finding { kind, detail });
    }
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("database: {0}")]
    Db(#[from] DbError),
    #[error("{what}: {source}")]
    Io { what: String, source: io::Error },
}

fn io_error(what: impl Into<String>) -> impl FnOnce(io::Error) -> Error {
    let what = what.into();
    move |source| Error::Io { what, source }
}

/// What the check is told.
#[derive(Debug, Clone, Copy)]
pub struct Options {
    /// Hash every file's bytes.
    pub deep: bool,
    /// The cap on the store's total size.
    pub cap: u64,
    /// How long a temp file must have stood still to be a finding.
    pub tmp_idle: Duration,
}

/// The sha256 (hex) of the file at `path`.
fn hash_file(path: &Path) -> io::Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0_u8; 1024 * 1024];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(buf.get(..n).unwrap_or_default());
    }
    Ok(hasher
        .finalize()
        .iter()
        .flat_map(|b| [b >> 4, b & 0x0f])
        .filter_map(|n| char::from_digit(u32::from(n), 16))
        .collect())
}

fn is_prefix(name: &str) -> bool {
    name.len() == 2 && name.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn display(path: &Path) -> String {
    path.display().to_string()
}

/// Look at the files in `root` (a blob directory), setting aside what is
/// fine: the hash and size of every blob file, and findings for the rest.
fn walk(root: &Path, options: &Options, report: &mut Report) -> Result<Vec<(String, u64, PathBuf)>, Error> {
    let mut files = Vec::new();
    let entries = fs::read_dir(root).map_err(io_error(format!("cannot read the blob directory {}", root.display())))?;
    for entry in entries {
        let entry = entry.map_err(io_error(format!("cannot read the blob directory {}", root.display())))?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let kind = entry.file_type().map_err(io_error(format!("cannot look at {}", path.display())))?;
        if name == ".lock" && kind.is_file() {
            continue;
        }
        if name == "tmp" && kind.is_dir() {
            tmp(&path, options, report)?;
        } else if is_prefix(&name) && kind.is_dir() {
            shard(&path, &name, &mut files, report)?;
        } else {
            report.find(Kind::Misplaced, format!("{} is not something the store makes", display(&path)));
        }
    }
    Ok(files)
}

fn tmp(dir: &Path, options: &Options, report: &mut Report) -> Result<(), Error> {
    let now = SystemTime::now();
    for entry in fs::read_dir(dir).map_err(io_error(format!("cannot read {}", dir.display())))? {
        let entry = entry.map_err(io_error(format!("cannot read {}", dir.display())))?;
        let path = entry.path();
        let modified = entry
            .metadata()
            .and_then(|m| m.modified())
            .map_err(io_error(format!("cannot look at {}", path.display())))?;
        let idle = now.duration_since(modified).unwrap_or_default();
        if idle >= options.tmp_idle {
            report.find(Kind::Tmp, format!("{} has not been written to for {} s", display(&path), idle.as_secs()));
        }
    }
    Ok(())
}

fn shard(dir: &Path, prefix: &str, files: &mut Vec<(String, u64, PathBuf)>, report: &mut Report) -> Result<(), Error> {
    for entry in fs::read_dir(dir).map_err(io_error(format!("cannot read {}", dir.display())))? {
        let entry = entry.map_err(io_error(format!("cannot read {}", dir.display())))?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        let metadata = entry.metadata().map_err(io_error(format!("cannot look at {}", path.display())))?;
        if !metadata.is_file() || !blob::is_hash(&name) || !name.starts_with(prefix) {
            report.find(Kind::Misplaced, format!("{} is not a blob file under its own hash", display(&path)));
            continue;
        }
        report.files += 1;
        report.file_bytes = report.file_bytes.saturating_add(metadata.len());
        files.push((name, metadata.len(), path));
    }
    Ok(())
}

/// Check the blob directory `root` against the board's index.
///
/// # Errors
/// If the database or the directory cannot be read: that is a failure to
/// check, not a finding.
pub fn check(board: &Board, root: &Path, options: &Options) -> Result<Report, Error> {
    let mut report = Report::default();
    let index = board.blob_index()?;
    report.blobs = index.len() as u64;
    report.indexed_bytes = index.iter().map(|(_, size)| *size).fold(0, u64::saturating_add);
    let files = walk(root, options, &mut report)?;

    let present: BTreeSet<&str> = files.iter().map(|(hash, _, _)| hash.as_str()).collect();
    for (hash, size) in &index {
        if !present.contains(hash.as_str()) && board.blob(hash)?.is_some() {
            report.find(Kind::Missing, format!("{hash} has a row ({size} bytes) but no file"));
        }
    }
    let sizes: std::collections::BTreeMap<&str, u64> = index.iter().map(|(h, s)| (h.as_str(), *s)).collect();
    for (hash, on_disk, path) in &files {
        match sizes.get(hash.as_str()) {
            None if board.blob(hash)?.is_none() => {
                report.find(Kind::Orphan, format!("{hash} is a file ({on_disk} bytes) with no row"));
            }
            Some(size) if size != on_disk => {
                report.find(Kind::Size, format!("{hash} has a row of {size} bytes but a file of {on_disk}"));
            }
            Some(_) if options.deep => {
                let got = hash_file(path).map_err(io_error(format!("cannot read {}", path.display())))?;
                report.hashed += 1;
                if got != *hash {
                    report.find(Kind::Corrupt, format!("{hash} has bytes that hash to {got}"));
                }
            }
            _ => {}
        }
    }
    for (event, hash) in board.pinned_without_blob()? {
        report.find(Kind::Reference, format!("#{event} pins board-blob://{hash}, which the board does not have"));
    }
    if report.indexed_bytes > options.cap {
        report.find(
            Kind::OverCap,
            format!("the blobs total {} bytes, over the cap of {}", report.indexed_bytes, options.cap),
        );
    }
    report.findings.sort_by(|a, b| a.kind.as_str().cmp(b.kind.as_str()).then_with(|| a.detail.cmp(&b.detail)));
    Ok(report)
}

/// Where the blobs are, as the server decides it: `BOARD_BLOBS`, else
/// `blobs/` beside the database.
#[must_use]
pub fn blob_root(blobs: Option<std::ffi::OsString>, db: &Path) -> PathBuf {
    match blobs {
        Some(dir) => PathBuf::from(dir),
        None => db.parent().unwrap_or(Path::new("")).join("blobs"),
    }
}

/// The path of blob `hash` under `root`.
#[must_use]
pub fn file_of(root: &Path, hash: &str) -> Option<PathBuf> {
    blobdir::path_in(root, hash)
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt as _;

    use rusqlite::Connection;

    use super::*;

    type TestResult = Result<(), Box<dyn std::error::Error>>;

    const BYTES: &[u8] = b"some blob";

    fn hash_of(bytes: &[u8]) -> String {
        Sha256::digest(bytes)
            .iter()
            .flat_map(|b| [b >> 4, b & 0x0f])
            .filter_map(|n| char::from_digit(u32::from(n), 16))
            .collect()
    }

    struct Store {
        _dir: tempfile::TempDir,
        db: PathBuf,
        root: PathBuf,
        board: Board,
    }

    fn store() -> Result<Store, Box<dyn std::error::Error>> {
        let dir = tempfile::tempdir()?;
        let db = dir.path().join("board-v2.db");
        let root = dir.path().join("blobs");
        fs::create_dir_all(root.join("tmp"))?;
        let board = Board::open(&db)?.board;
        Ok(Store { _dir: dir, db, root, board })
    }

    impl Store {
        /// A row, and (if `file`) its file with `bytes`.
        fn add(&self, hash: &str, size: u64, bytes: Option<&[u8]>) -> TestResult {
            let conn = Connection::open(&self.db)?;
            conn.execute(
                "INSERT INTO blobs(sha256, size, uploader, uploader_fp, ts, head, nonce, signature)
                 VALUES(?, ?, 'u', 'fp', '2026-01-01T00:00:00Z', 0, 'n', 's')",
                rusqlite::params![hash, i64::try_from(size)?],
            )?;
            if let Some(bytes) = bytes {
                self.file(hash, bytes)?;
            }
            Ok(())
        }

        fn file(&self, hash: &str, bytes: &[u8]) -> TestResult {
            let path = self.root.join(hash.get(..2).unwrap_or_default());
            fs::create_dir_all(&path)?;
            fs::write(path.join(hash), bytes)?;
            fs::set_permissions(path.join(hash), fs::Permissions::from_mode(0o400))?;
            Ok(())
        }

        fn check(&self, deep: bool, tmp_idle: Duration) -> Result<Report, Error> {
            check(&self.board, &self.root, &Options { deep, cap: 1 << 30, tmp_idle })
        }
    }

    fn kinds(report: &Report) -> Vec<&'static str> {
        report.findings.iter().map(|f| f.kind.as_str()).collect()
    }

    #[test]
    fn a_store_whose_files_match_its_rows_is_clean() -> TestResult {
        let s = store()?;
        let h = hash_of(BYTES);
        s.add(&h, BYTES.len() as u64, Some(BYTES))?;
        for deep in [false, true] {
            let report = s.check(deep, TMP_IDLE)?;
            assert_eq!(report.findings, []);
            assert_eq!((report.blobs, report.files, report.indexed_bytes, report.file_bytes), (1, 1, 9, 9));
            assert_eq!(report.hashed, u64::from(deep));
        }
        Ok(())
    }

    #[test]
    fn rows_without_files_files_without_rows_and_wrong_sizes_are_found() -> TestResult {
        let s = store()?;
        let (missing, wrong, orphan) = (hash_of(b"1"), hash_of(b"2"), hash_of(b"3"));
        s.add(&missing, 5, None)?;
        s.add(&wrong, 7, Some(b"two"))?;
        s.file(&orphan, b"three")?;
        let report = s.check(false, TMP_IDLE)?;
        assert_eq!(kinds(&report), ["missing", "orphan", "size"]);
        let text: Vec<String> = report.findings.iter().map(ToString::to_string).collect();
        assert_eq!(text.first(), Some(&format!("missing {missing} has a row (5 bytes) but no file")));
        assert_eq!(text.get(2), Some(&format!("size {wrong} has a row of 7 bytes but a file of 3")));
        Ok(())
    }

    #[test]
    fn only_a_deep_check_reads_the_bytes() -> TestResult {
        let s = store()?;
        let h = hash_of(BYTES);
        s.add(&h, BYTES.len() as u64, Some(b"other ones"[..9].as_ref()))?;
        assert_eq!(s.check(false, TMP_IDLE)?.findings, []);
        let report = s.check(true, TMP_IDLE)?;
        assert_eq!(kinds(&report), ["corrupt"]);
        assert!(report.findings.iter().all(|f| f.detail.starts_with(&h)));
        Ok(())
    }

    #[test]
    fn strays_in_the_directory_are_misplaced_and_old_temp_files_are_found() -> TestResult {
        let s = store()?;
        let h = hash_of(BYTES);
        // a file in another hash's directory, a non-hash name, and an unknown entry
        fs::create_dir_all(s.root.join("zz"))?;
        fs::create_dir_all(s.root.join("00"))?;
        fs::write(s.root.join("00").join(&h), BYTES)?;
        fs::write(s.root.join("00").join("notes.txt"), "x")?;
        fs::write(s.root.join("stray"), "x")?;
        fs::write(s.root.join(".lock"), "")?;
        fs::write(s.root.join("tmp").join("abc.part"), "half")?;
        let report = s.check(false, TMP_IDLE)?;
        assert_eq!(kinds(&report), ["misplaced"; 4]);
        // a temp file nothing wrote to for a while is found; a fresh one is an upload in flight
        let report = s.check(false, Duration::ZERO)?;
        assert!(kinds(&report).contains(&"tmp"));
        assert!(!kinds(&s.check(false, TMP_IDLE)?).contains(&"tmp"));
        Ok(())
    }

    #[test]
    fn the_roots_follow_the_server() {
        let db = Path::new("/state/board-v2.db");
        assert_eq!(blob_root(None, db), Path::new("/state/blobs"));
        assert_eq!(blob_root(Some("/elsewhere".into()), db), Path::new("/elsewhere"));
        assert_eq!(file_of(Path::new("/b"), &hash_of(b"x")).map(|p| p.starts_with("/b")), Some(true));
        assert_eq!(file_of(Path::new("/b"), "nope"), None);
    }
}
