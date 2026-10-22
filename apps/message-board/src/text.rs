//! Text from the board in a plain-text line: board-admin's audit log, and
//! `board`'s diagnostics on stderr.
//!
//! Names and bodies are free text, and an error may quote them. Written
//! into a line as they are, a line break in one would end the line and
//! start a forged one, and an escape sequence would reach a terminal.

use std::borrow::Cow;

/// `text` with every control character escaped (a line break becomes
/// `\n`), so it stays on the line it is written on.
#[must_use]
pub fn one_line(text: &str) -> Cow<'_, str> {
    if !text.chars().any(char::is_control) {
        return Cow::Borrowed(text);
    }
    let mut out = String::with_capacity(text.len().saturating_add(8));
    for c in text.chars() {
        if c.is_control() {
            out.extend(c.escape_default());
        } else {
            out.push(c);
        }
    }
    Cow::Owned(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_breaks_and_escapes_stay_on_the_line() {
        assert_eq!(one_line("plain ✓"), "plain ✓");
        assert_eq!(one_line("a\nb\rc\u{1b}[2J\td"), "a\\nb\\rc\\u{1b}[2J\\td");
    }
}
