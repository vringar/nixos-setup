//! The server's log: its state on stdout, what went wrong on stderr.
//! Under systemd both reach the journal.
//!
//! A line that cannot be written cannot be reported anywhere else either,
//! and a server that cannot report its faults would be swallowing them, so
//! it exits instead, with [`EXIT_NO_LOG`], and the unit restarts it.

use std::io::{self, Write};
use std::process;

/// The exit status when the log itself cannot be written (`EX_IOERR`).
pub const EXIT_NO_LOG: i32 = 74;

fn write_line(mut out: impl Write, text: &str) {
    let written = out.write_all(text.as_bytes()).and_then(|()| out.write_all(b"\n")).and_then(|()| out.flush());
    if written.is_err() {
        process::exit(EXIT_NO_LOG);
    }
}

/// Something that went wrong, with what it concerned.
pub fn line(text: &str) {
    write_line(io::stderr().lock(), text);
}

/// The server's own state: where it serves, whose the owner key is.
pub fn info(text: &str) {
    write_line(io::stdout().lock(), text);
}
