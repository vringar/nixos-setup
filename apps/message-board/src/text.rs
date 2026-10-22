//! Text from the board in a plain-text line: board-admin's audit log, and
//! `board`'s diagnostics on stderr.
//!
//! Names and bodies are free text, and an error may quote them. Written
//! into a line as they are, a line break in one would end the line and
//! start a forged one, and an escape sequence would reach a terminal.

use std::borrow::Cow;

/// `text` with every control character and Unicode line or paragraph
/// separator escaped (a line break becomes `\n`), so it stays on the line it
/// is written on. U+2028 and U+2029 are not control characters, but many
/// viewers break the line at them all the same.
#[must_use]
pub fn one_line(text: &str) -> Cow<'_, str> {
    if !text.chars().any(needs_escape) {
        return Cow::Borrowed(text);
    }
    let mut out = String::with_capacity(text.len().saturating_add(8));
    for c in text.chars() {
        if needs_escape(c) {
            out.extend(c.escape_default());
        } else {
            out.push(c);
        }
    }
    Cow::Owned(out)
}

fn needs_escape(c: char) -> bool {
    c.is_control() || matches!(c, '\u{2028}' | '\u{2029}')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_breaks_and_escapes_stay_on_the_line() {
        assert_eq!(one_line("plain ✓"), "plain ✓");
        assert_eq!(one_line("a\nb\rc\u{1b}[2J\td"), "a\\nb\\rc\\u{1b}[2J\\td");
        assert_eq!(one_line("a\u{2028}b\u{2029}"), "a\\u{2028}b\\u{2029}");
    }
}
