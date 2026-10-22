//! The audit log: one line per deletion or released claim, kept outside
//! the board (whose own API this tool never touches), since unlike an
//! archive none of it can be undone.

use std::fs::{self, OpenOptions};
use std::io::Write as _;
use std::path::PathBuf;

use message_board::timefmt;

use crate::Failure;

pub struct Audit {
    pub path: PathBuf,
}

impl Audit {
    /// Append `<timestamp> <action> <detail>`. A deletion that cannot be
    /// recorded is an error: the session ends and says so.
    pub fn record(&self, action: &str, detail: &str) -> Result<(), Failure> {
        let failed = |cause| Failure::Io { what: format!("cannot write the audit log {}", self.path.display()), cause };
        if let Some(dir) = self.path.parent() {
            fs::create_dir_all(dir).map_err(failed)?;
        }
        let line = format!("{} {action} {detail}\n", timefmt::now()?);
        let mut file = OpenOptions::new().append(true).create(true).open(&self.path).map_err(failed)?;
        file.write_all(line.as_bytes()).map_err(failed)
    }
}
