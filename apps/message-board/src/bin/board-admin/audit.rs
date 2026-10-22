//! The audit log: one line per deletion or released claim, kept outside
//! the board (whose own API this tool never touches), since unlike an
//! archive none of it can be undone.

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
    /// detail escaped. A deletion that cannot be recorded is an error: the
    /// session ends and says so.
    pub fn record(&self, action: &str, detail: &str) -> Result<(), Failure> {
        let failed = |cause| Failure::Io { what: format!("cannot write the audit log {}", self.path.display()), cause };
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir).map_err(failed)?;
        }
        // One line per entry, whatever the names in it hold.
        let line = format!("{} {action} {}\n", timefmt::now()?, one_line(detail));
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
        audit.record("delete_topic", "x\n2026-10-22T00:00:00Z delete_topic forged/one (0 messages)")?;
        let log = fs::read_to_string(&audit.path)?;
        assert_eq!(log.lines().count(), 1, "{log}");
        assert!(log.ends_with(" delete_topic x\\n2026-10-22T00:00:00Z delete_topic forged/one (0 messages)\n"));
        Ok(())
    }
}
