//! Event ids in text: where a body mentions one (a run of five or more
//! digits, as every id is since the counter starts at 10001), so that a
//! page can link it to its permalink if the board has such an event.
//!
//! This only finds candidates. Whether one is an id is the board's to say.

use std::ops::Range;

/// The fewest digits a mentioned id has.
const MIN_DIGITS: usize = 5;

/// A run of digits in a text that may be an event id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mention {
    /// Where it sits, in bytes of the text.
    pub at: Range<usize>,
    pub id: i64,
}

/// The spans of `text` that are code: fenced (three backticks, to the
/// closing fence or the end) or inline (a pair of backticks on one line).
fn code(text: &str) -> Vec<Range<usize>> {
    let mut spans = Vec::new();
    let mut from = 0;
    while let Some(open) = text.get(from..).and_then(|rest| rest.find('`')).map(|at| from.saturating_add(at)) {
        let after = |skip: usize| open.saturating_add(skip);
        let end = if text.get(open..).is_some_and(|rest| rest.starts_with("```")) {
            let body = text.get(after(3)..).unwrap_or_default();
            body.find("```").map_or(text.len(), |at| after(3).saturating_add(at).saturating_add(3))
        } else {
            let line = text.get(after(1)..).unwrap_or_default();
            let line = line.split('\n').next().unwrap_or_default();
            match line.find('`') {
                Some(at) => after(1).saturating_add(at).saturating_add(1),
                None => after(1), // a lone backtick is no code
            }
        };
        spans.push(open..end);
        from = end;
    }
    spans
}

/// Whether the character before or after a run says it is part of
/// something else: a word, a hash, a path, a URL's port or a decimal.
fn glued(c: Option<char>) -> bool {
    c.is_some_and(|c| c.is_alphanumeric() || matches!(c, '_' | '-' | '/' | '\\'))
}

/// Every candidate in `text`: a run of at least [`MIN_DIGITS`] ASCII
/// digits, without a leading zero, that is a whole word: not part of a
/// longer word, hash, path, decimal number or `host:port`, not in code,
/// and not in a URL (or a `board-blob://` reference).
#[must_use]
pub fn scan(text: &str) -> Vec<Mention> {
    let code = code(text);
    let bytes = text.as_bytes();
    let mut found = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        if !bytes.get(at).is_some_and(u8::is_ascii_digit) {
            at = at.saturating_add(1);
            continue;
        }
        let end = bytes
            .get(at..)
            .map_or(at, |rest| at.saturating_add(rest.iter().take_while(|b| b.is_ascii_digit()).count()));
        let run = at..end;
        at = end;
        let before = text.get(..run.start).and_then(|t| t.chars().next_back());
        let after = text.get(run.end..).and_then(|t| t.chars().next());
        let rest = text.get(run.end..).unwrap_or_default();
        // "1.5" and "10234.5": a decimal point has digits on both sides
        let decimal_before = before == Some('.')
            && text
                .get(..run.start.saturating_sub(1))
                .and_then(|t| t.chars().next_back())
                .is_some_and(|c| c.is_ascii_digit());
        let decimal_after = after == Some('.') && rest.chars().nth(1).is_some_and(|c| c.is_ascii_digit());
        let plain = run.len() >= MIN_DIGITS
            && bytes.get(run.start).is_none_or(|b| *b != b'0')
            && !glued(before)
            && before != Some(':')
            && !glued(after)
            && !decimal_before
            && !decimal_after
            && !code.iter().any(|span| span.contains(&run.start))
            && !in_url(text, &run);
        if let (true, Ok(id)) = (plain, text.get(run.clone()).unwrap_or_default().parse::<i64>()) {
            found.push(Mention { at: run, id });
        }
    }
    found
}

/// Whether the whitespace-delimited word around `run` is a URL.
fn in_url(text: &str, run: &Range<usize>) -> bool {
    let start = text.get(..run.start).and_then(|t| t.rfind(char::is_whitespace)).map_or(0, |at| at.saturating_add(1));
    let end = text
        .get(run.end..)
        .and_then(|t| t.find(char::is_whitespace))
        .map_or(text.len(), |at| run.end.saturating_add(at));
    text.get(start..end).is_some_and(|word| word.contains("://") || word.starts_with("www."))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ids(text: &str) -> Vec<i64> {
        scan(text).into_iter().map(|m| m.id).collect()
    }

    #[test]
    fn a_whole_run_of_five_digits_or_more_is_a_candidate() {
        assert_eq!(ids("see 10234 and 100000."), [10234, 100_000]);
        assert_eq!(ids("10234"), [10234]);
        assert_eq!(ids("1234 is too short"), Vec::<i64>::new());
        assert_eq!(ids("010234 has a leading zero"), Vec::<i64>::new());
        assert_eq!(ids("99999999999999999999 does not fit an id"), Vec::<i64>::new());
    }

    #[test]
    fn where_it_sits_is_in_bytes_of_the_text() {
        let text = "é 10234 é";
        let found = scan(text);
        assert_eq!(found.len(), 1);
        assert_eq!(found.first().and_then(|m| text.get(m.at.clone())), Some("10234"));
    }

    #[test]
    fn punctuation_around_it_does_not_hide_it() {
        for text in [
            "(10234)",
            "#10234",
            "#10234,",
            "10234: done",
            "[10234]",
            "\"10234\"",
            "see 10234.",
            "10234!",
            "a,10234",
            "id=10234",
            "10234\n",
        ] {
            assert_eq!(ids(text), [10234], "{text:?}");
        }
        assert_eq!(ids("#10234, #10235 and #10236"), [10234, 10235, 10236]);
    }

    #[test]
    fn part_of_a_longer_word_is_not_a_mention() {
        for text in [
            "abc10234",
            "10234abc",
            "x_10234",
            "10234_x",
            "deadbeef10234ff",
            "1a2b3-10234-4c5d",
            "10234-10235",
            "v1.10234",
            "3.14159",
            "10234.5",
            "a/10234",
            "10234/b",
            "/view/a/10234",
            "localhost:10234",
            "C:\\10234",
            "é10234",
        ] {
            assert_eq!(ids(text), Vec::<i64>::new(), "{text:?}");
        }
    }

    #[test]
    fn urls_and_blob_references_are_left_alone() {
        let blob = format!("board-blob://{}", "1".repeat(64));
        for text in [
            "https://example.org/10234",
            "go to http://h/?id=10234 now",
            "www.example.org/x?10234",
            &blob,
            "<https://x/y#10234>",
        ] {
            assert_eq!(ids(text), Vec::<i64>::new(), "{text:?}");
        }
        // the word after a URL is another word
        assert_eq!(ids("https://example.org/x 10234"), [10234]);
        assert_eq!(ids(&format!("{blob} then 10234")), [10234]);
    }

    #[test]
    fn code_is_left_alone() {
        assert_eq!(ids("`10234` and 10235"), [10235]);
        assert_eq!(ids("run `echo 10234` then 10235 and `x`"), [10235]);
        assert_eq!(ids("```\n10234\n```\n10235"), [10235]);
        assert_eq!(ids("```\n10234 never closed"), Vec::<i64>::new());
        // an inline span ends with its line; a lone backtick is no code
        assert_eq!(ids("it`s 10234\n10235"), [10234, 10235]);
        assert_eq!(ids("`open\n10234`"), [10234]);
    }
}
