//! The blob store's files: content-addressed, one per unique content, under
//! the state directory.
//!
//! ```text
//! <dir>/            0700
//!   .lock           held by board-server for as long as it runs
//!   tmp/<random>.part   an upload in flight, on the same file system
//!   ab/ab12...ef    0400; "ab" is the first two hex digits, the name the full hash
//! ```
//!
//! No path is ever built from anything but a validated hash. A file appears
//! with one rename of a finished, synced temp file and never changes, so
//! readers need no lock.

use std::fmt::Write as _;
use std::fs::{self, File, OpenOptions, TryLockError};
use std::io;
use std::os::unix::fs::{DirBuilderExt as _, OpenOptionsExt as _, PermissionsExt as _};
use std::path::{Path, PathBuf};

use crate::blob;

const TMP: &str = "tmp";
const LOCK: &str = ".lock";

/// The blob directory of a running server.
#[derive(Debug)]
pub struct BlobDir {
    root: PathBuf,
    /// Holding the lock for as long as this lives.
    _lock: File,
}

/// Why a blob directory cannot be taken.
#[derive(Debug, thiserror::Error)]
pub enum OpenError {
    #[error("cannot set up the blob directory {path}: {source}")]
    Io { path: String, source: io::Error },
    #[error("another board-server already holds the blob directory {0}: one server per state directory")]
    Locked(String),
}

fn private_dir(path: &Path) -> io::Result<bool> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => Ok(true),
        Err(err) if err.kind() == io::ErrorKind::AlreadyExists && path.is_dir() => Ok(false),
        Err(err) => Err(err),
    }
}

fn sync_dir(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

impl BlobDir {
    /// Make the directory if it is missing, take its lock, and delete the
    /// temp files an earlier run left (its uploads died with it). Nothing
    /// else is walked: the store may be tens of gigabytes.
    ///
    /// # Errors
    /// [`OpenError::Locked`] when another process holds it.
    pub fn open(root: &Path) -> Result<Self, OpenError> {
        let failed = |path: &Path| {
            let path = path.display().to_string();
            move |source| OpenError::Io { path, source }
        };
        if let Some(parent) = root.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent).map_err(failed(parent))?;
        }
        private_dir(root).map_err(failed(root))?;
        let tmp = root.join(TMP);
        private_dir(&tmp).map_err(failed(&tmp))?;
        let lock_path = root.join(LOCK);
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .mode(0o600)
            .open(&lock_path)
            .map_err(failed(&lock_path))?;
        match lock.try_lock() {
            Ok(()) => {}
            Err(TryLockError::WouldBlock) => return Err(OpenError::Locked(root.display().to_string())),
            Err(TryLockError::Error(err)) => return Err(failed(&lock_path)(err)),
        }
        let dir = Self { root: root.to_owned(), _lock: lock };
        dir.purge_tmp().map_err(failed(&tmp))?;
        Ok(dir)
    }

    #[must_use]
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Delete every temp file: none can belong to a live upload at start.
    ///
    /// # Errors
    /// If the directory cannot be read.
    pub fn purge_tmp(&self) -> io::Result<usize> {
        let mut purged = 0;
        for entry in fs::read_dir(self.root.join(TMP))? {
            let entry = entry?;
            if fs::remove_file(entry.path()).is_ok() {
                purged += 1;
            }
        }
        Ok(purged)
    }

    /// A new, empty temp file (never an existing one), 0600.
    ///
    /// # Errors
    /// If it cannot be made.
    pub fn create_temp(&self) -> io::Result<(File, PathBuf)> {
        let mut random = [0_u8; 16];
        getrandom::fill(&mut random).map_err(|err| io::Error::other(err.to_string()))?;
        let name = random.iter().fold(String::new(), |mut name, b| {
            // writing to a String cannot fail
            let _written = write!(name, "{b:02x}");
            name
        });
        let path = self.root.join(TMP).join(format!("{name}.part"));
        let file = OpenOptions::new().write(true).create_new(true).mode(0o600).open(&path)?;
        Ok((file, path))
    }

    /// Where blob `hash` lives, or `None` for text that is no hash (so that
    /// no path is built from anything else).
    #[must_use]
    pub fn path_of(&self, hash: &str) -> Option<PathBuf> {
        blob::is_hash(hash).then(|| self.root.join(hash.get(..2).unwrap_or_default()).join(hash))
    }

    /// Move the finished temp file `tmp` into place as blob `hash`: 0400,
    /// renamed, and the directory synced, so that the file is there after a
    /// crash. The data itself is synced by the caller first.
    ///
    /// # Errors
    /// If any step fails; the temp file is left for the caller to delete.
    pub fn place(&self, tmp: &Path, hash: &str) -> io::Result<()> {
        let target = self.path_of(hash).ok_or_else(|| io::Error::other("not a hash"))?;
        let dir = target.parent().ok_or_else(|| io::Error::other("no directory"))?;
        if private_dir(dir)? {
            sync_dir(&self.root)?;
        }
        fs::set_permissions(tmp, fs::Permissions::from_mode(0o400))?;
        fs::rename(tmp, &target)?;
        sync_dir(dir)
    }

    /// Open blob `hash` for reading.
    ///
    /// # Errors
    /// [`io::ErrorKind::NotFound`] if there is no such file.
    pub fn open_blob(&self, hash: &str) -> io::Result<File> {
        File::open(self.path_of(hash).ok_or_else(|| io::Error::other("not a hash"))?)
    }

    /// Delete blob `hash`'s file; a file that is not there is fine.
    ///
    /// # Errors
    /// If it is there and cannot be deleted.
    pub fn remove(&self, hash: &str) -> io::Result<()> {
        let path = self.path_of(hash).ok_or_else(|| io::Error::other("not a hash"))?;
        match fs::remove_file(path) {
            Err(err) if err.kind() != io::ErrorKind::NotFound => Err(err),
            _ => Ok(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write as _;

    use super::*;

    const H: &str = "ab12ef34ab12ef34ab12ef34ab12ef34ab12ef34ab12ef34ab12ef34ab12ef34";

    #[test]
    fn a_blob_lives_at_its_hash_under_its_first_two_digits() -> Result<(), Box<dyn std::error::Error>> {
        let tmp = tempfile::tempdir()?;
        let dir = BlobDir::open(&tmp.path().join("blobs"))?;
        assert_eq!(dir.path_of(H), Some(tmp.path().join("blobs/ab").join(H)));
        for bad in ["", "../..", "AB12", &H.to_uppercase(), &H[..63], "ab/../../etc/passwd"] {
            assert_eq!(dir.path_of(bad), None, "{bad}");
        }
        Ok(())
    }

    #[test]
    fn placing_a_file_makes_it_read_only_and_leaves_no_temp_file() -> Result<(), Box<dyn std::error::Error>> {
        let tmp = tempfile::tempdir()?;
        let dir = BlobDir::open(&tmp.path().join("blobs"))?;
        let (mut file, path) = dir.create_temp()?;
        file.write_all(b"bytes")?;
        file.sync_data()?;
        dir.place(&path, H)?;
        let placed = dir.path_of(H).ok_or("no path")?;
        assert_eq!(fs::read(&placed)?, b"bytes");
        assert_eq!(fs::metadata(&placed)?.permissions().mode() & 0o777, 0o400);
        assert_eq!(fs::metadata(placed.parent().ok_or("no parent")?)?.permissions().mode() & 0o777, 0o700);
        assert_eq!(fs::read_dir(tmp.path().join("blobs/tmp"))?.count(), 0);
        dir.remove(H)?;
        dir.remove(H)?;
        assert!(dir.open_blob(H).is_err());
        Ok(())
    }

    #[test]
    fn opening_purges_temp_files_and_a_second_holder_is_refused() -> Result<(), Box<dyn std::error::Error>> {
        let tmp = tempfile::tempdir()?;
        let root = tmp.path().join("blobs");
        {
            let dir = BlobDir::open(&root)?;
            let (_file, path) = dir.create_temp()?;
            assert!(path.exists());
            assert!(matches!(BlobDir::open(&root), Err(OpenError::Locked(_))));
        }
        let dir = BlobDir::open(&root)?;
        assert_eq!(fs::read_dir(dir.root().join("tmp"))?.count(), 0);
        Ok(())
    }
}
