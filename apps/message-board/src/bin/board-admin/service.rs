//! The board's user service: taken down for the session so that nothing
//! else has the database open while rows are deleted, and brought back
//! after. Only ever the unit that owns this database, and only through
//! systemctl: an unmanaged board-server is never touched.

use std::process::Command;
use std::time::Duration;

use crate::Failure;

/// The managed unit, or none (`BOARD_ADMIN_SERVICE=""`: tests, and a board
/// not run by systemd).
pub struct Service {
    pub unit: Option<String>,
}

fn systemctl(args: &[&str]) -> Result<std::process::ExitStatus, Failure> {
    Command::new("systemctl")
        .arg("--user")
        .args(args)
        .status()
        .map_err(|cause| Failure::Io { what: "cannot run systemctl".to_owned(), cause })
}

impl Service {
    /// systemctl's own opinion of whether the unit is running.
    pub fn is_active(&self) -> Result<bool, Failure> {
        match &self.unit {
            None => Ok(false),
            Some(unit) => Ok(systemctl(&["is-active", "--quiet", unit])?.success()),
        }
    }

    fn run(&self, verb: &str) -> Result<(), Failure> {
        let Some(unit) = &self.unit else {
            return Ok(());
        };
        let status = systemctl(&[verb, unit])?;
        if status.success() {
            Ok(())
        } else {
            Err(Failure::Exit(format!("systemctl --user {verb} {unit} failed ({status})")))
        }
    }

    pub fn stop(&self) -> Result<(), Failure> {
        self.run("stop")
    }

    pub fn start(&self) -> Result<(), Failure> {
        self.run("start")
    }
}

/// Whether something answers on the board's URL. Changing the database
/// file while a server holds it open risks corruption, not just a stale
/// read, so only a refused connection counts as "nothing there": any other
/// failure (a timeout, an error status, garbage) is something on the port,
/// and is said so.
pub fn is_live(url: &str) -> Result<bool, Failure> {
    let agent = ureq::Agent::config_builder()
        .http_status_as_error(false)
        .max_idle_connections(0)
        .timeout_global(Some(Duration::from_secs(2)))
        .build()
        .new_agent();
    match agent.get(format!("{url}/healthz")).call() {
        Ok(response) if response.status().is_success() => Ok(true),
        Ok(response) => {
            note(&format!(
                "board-admin: {url}/healthz answered {}; something is serving that port",
                response.status()
            ))?;
            Ok(true)
        }
        Err(ureq::Error::Io(err)) if err.kind() == std::io::ErrorKind::ConnectionRefused => Ok(false),
        Err(err) => {
            note(&format!("board-admin: probing {url}/healthz failed ({err}); taking the port as in use"))?;
            Ok(true)
        }
    }
}

/// A line on stderr, outside the TUI.
pub fn note(text: &str) -> Result<(), Failure> {
    use std::io::Write as _;
    writeln!(std::io::stderr().lock(), "{text}")
        .map_err(|cause| Failure::Io { what: "cannot write to stderr".to_owned(), cause })
}
