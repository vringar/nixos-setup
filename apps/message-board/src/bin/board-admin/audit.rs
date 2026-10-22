//! The audit log: one line per deletion, kept outside the board (whose own
//! API this tool never touches), since unlike an archive none of it can be
//! undone.

use std::fs::{self, OpenOptions};
use std::io::Write as _;
use std::path::PathBuf;

use message_board::text::one_line;
use message_board::timefmt;

use crate::Failure;

pub struct Audit {
    pub path: PathBuf,
}

impl Audit {
    /// Append `<timestamp> <action> <detail>`, control characters in the
    /// detail escaped. A change that cannot be recorded is an error: the
    /// session ends and says so, naming the change, which is done by then.
    pub fn record(&self, action: &str, detail: &str) -> Result<(), Failure> {
        // One line per entry, whatever the names in it hold.
        let entry = format!("{action} {}", one_line(detail));
        let unrecorded = || format!("{entry:?} is done but not in the audit log {}", self.path.display());
        let now =
            timefmt::now().map_err(|err| Failure::Exit(format!("{}: no time to log it at: {err}", unrecorded())))?;
        let failed = |cause| Failure::Io { what: unrecorded(), cause };
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir).map_err(failed)?;
        }
        let line = format!("{now} {entry}\n");
        let mut file = OpenOptions::new().append(true).create(true).open(&self.path).map_err(failed)?;
        file.write_all(line.as_bytes()).map_err(failed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_entry_is_one_line_whatever_the_names_hold() -> Result<(), Box<dyn std::error::Error>> {
        let dir = tempfile::tempdir()?;
        let audit = Audit { path: dir.path().join("audit.log") };
        audit.record("delete_branch", "x\n2026-10-22T00:00:00Z delete_branch forged (1 nodes, 1 events)")?;
        let log = fs::read_to_string(&audit.path)?;
        assert_eq!(log.lines().count(), 1, "{log}");
        assert!(log.ends_with(" delete_branch x\\n2026-10-22T00:00:00Z delete_branch forged (1 nodes, 1 events)\n"));
        Ok(())
    }

    #[test]
    fn an_entry_that_cannot_be_written_is_named() -> Result<(), Box<dyn std::error::Error>> {
        let dir = tempfile::tempdir()?;
        let audit = Audit { path: dir.path().join("audit.log").join("not a directory") };
        fs::write(dir.path().join("audit.log"), "")?;
        let err = audit.record("delete_message", "#7 x/t from=a").err().ok_or("recorded into a file's child")?;
        assert!(
            err.to_string().starts_with("\"delete_message #7 x/t from=a\" is done but not in the audit log"),
            "{err}"
        );
        Ok(())
    }
}
