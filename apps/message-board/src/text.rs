//! Text from the board in line-oriented output.
//!
//! Area and topic names, sender names from before the name rule, and
//! message bodies are free text. Written into a watch line, a `board read`
//! header or the audit log as they are, a line break in one would end the
//! line and start a forged one, and an escape sequence would reach a
//! terminal.

use std::borrow::Cow;

/// `text` with every control character escaped (a line break becomes
/// `\n`), so it stays on the line it is written on.
#[must_use]
pub fn one_line(text: &str) -> Cow<'_, str> {
    escaped(text, char::is_control)
}

/// A line of a message body: control characters escaped as for
/// [`one_line`], but tabs kept, since markdown uses them.
#[must_use]
pub fn body_line(line: &str) -> Cow<'_, str> {
    escaped(line, |c| c.is_control() && c != '\t')
}

fn escaped(text: &str, escape: impl Fn(char) -> bool) -> Cow<'_, str> {
    if !text.chars().any(&escape) {
        return Cow::Borrowed(text);
    }
    let mut out = String::with_capacity(text.len().saturating_add(8));
    for c in text.chars() {
        if escape(c) {
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
        assert_eq!(body_line("code:\tx\u{7}"), "code:\tx\\u{7}");
    }
}
