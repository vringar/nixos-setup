//! What the blob store's clients and the server agree on, without a
//! database or a file system: the address of a blob, the reference to one
//! in a body, the limits, and the rules for the two header values that
//! describe an upload (its media type and its original file name).
//!
//! A blob is addressed by the SHA-256 of its bytes (64 lowercase hex digits)
//! and has no other name. A body names one as `board-blob://<64 hex>`; the
//! board checks every such reference when the body is written and keeps the
//! result in its `blob_refs` table, so a body is never searched again except
//! to find where a reference sits in it ([`scan`]).

use std::collections::BTreeSet;
use std::fmt::Write as _;

/// How a reference starts.
pub const SCHEME: &str = "board-blob://";
/// The most distinct blobs one body may reference.
pub const REFS_MAX: usize = 32;
/// The default total size of the store, in bytes.
pub const CAP_DEFAULT: u64 = 50_000_000_000;
/// The default size limit of one blob: 8 GiB.
pub const MAX_DEFAULT: u64 = 8 * 1024 * 1024 * 1024;
/// The default free space the disk keeps beyond the blobs: 5 GB.
pub const RESERVE_DEFAULT: u64 = 5_000_000_000;
/// How long an unreferenced blob is safe from eviction, in seconds.
pub const GRACE_DEFAULT: u64 = 3600;
/// The most uploads in flight at once.
pub const UPLOADS_MAX: usize = 4;
/// The longest media type, in bytes.
pub const MEDIA_TYPE_MAX: usize = 127;
/// The longest original file name, in bytes once decoded.
pub const FILENAME_MAX: usize = 255;
/// A media type a blob has unless its uploader says.
pub const DEFAULT_MEDIA_TYPE: &str = "application/octet-stream";

/// Whether `text` is a blob's address: 64 lowercase hex digits.
#[must_use]
pub fn is_hash(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// The reference to blob `hash`, as it is pasted into a body.
#[must_use]
pub fn reference(hash: &str) -> String {
    format!("{SCHEME}{hash}")
}

/// A reference found in a body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Found {
    /// Where it starts (at the scheme) and ends, in bytes of the body.
    pub start: usize,
    pub end: usize,
    pub hash: String,
}

/// What looks like a reference in a body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Spot {
    Ref(Found),
    /// Starts like a reference but is not one: the text of it (cut short).
    Bad(String),
}

/// Why a body's references are refused.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RefError {
    #[error(
        "{0:?} is no reference: a reference is board-blob:// and the full 64-digit lowercase sha256, followed by \
         the end of the text, a space or a closing punctuation mark (`board put` prints it); to write the syntax \
         itself, put a backslash before it"
    )]
    Bad(String),
    #[error("a body references at most {REFS_MAX} different blobs")]
    TooMany,
}

/// Whether `c` before the scheme keeps it from starting a reference: it
/// would be part of a longer word, a path or a name, or an escape.
fn glues(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '+' | '-' | '\\')
}

/// A character that may end a reference.
fn closing(c: char) -> bool {
    matches!(c, '.' | ',' | ';' | ':' | '!' | '?' | ')' | ']' | '}' | '>' | '"' | '\'' | '*' | '`')
}

/// Whether what follows a reference ends it: the end of the text, white
/// space, or closing punctuation that is itself followed by the end, white
/// space or more such punctuation.
fn terminated(after: &str) -> bool {
    let mut chars = after.chars();
    match chars.next() {
        None => true,
        Some(c) if c.is_whitespace() => true,
        Some(c) if closing(c) => loop {
            match chars.next() {
                None => break true,
                Some(next) if next.is_whitespace() => break true,
                Some(next) if closing(next) => {}
                Some(_) => break false,
            }
        },
        Some(_) => false,
    }
}

/// Every place in `body` that starts like a reference, in order, and whether
/// it is one. `\board-blob://...` and the scheme inside a longer word are
/// text, not spots.
#[must_use]
pub fn scan(body: &str) -> Vec<Spot> {
    let mut spots = Vec::new();
    for (start, _) in body.match_indices(SCHEME) {
        if body.get(..start).and_then(|before| before.chars().next_back()).is_some_and(glues) {
            continue;
        }
        let from = start.saturating_add(SCHEME.len());
        let rest = body.get(from..).unwrap_or_default();
        let run = rest.bytes().take_while(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-')).count();
        let (token, after) = rest.split_at(run);
        if is_hash(token) && terminated(after) {
            spots.push(Spot::Ref(Found { start, end: from.saturating_add(run), hash: token.to_owned() }));
        } else {
            let shown: String = format!("{SCHEME}{token}").chars().take(100).collect();
            spots.push(Spot::Bad(shown));
        }
    }
    spots
}

/// The distinct blobs `body` references, sorted.
///
/// # Errors
/// [`RefError::Bad`] for text that starts like a reference and is not one,
/// [`RefError::TooMany`] for more than [`REFS_MAX`] blobs.
pub fn references(body: &str) -> Result<Vec<String>, RefError> {
    let mut hashes = BTreeSet::new();
    for spot in scan(body) {
        match spot {
            Spot::Ref(found) => {
                hashes.insert(found.hash);
            }
            Spot::Bad(text) => return Err(RefError::Bad(text)),
        }
    }
    if hashes.len() > REFS_MAX {
        return Err(RefError::TooMany);
    }
    Ok(hashes.into_iter().collect())
}

/// The references in `body` that are valid, with where they sit: what a
/// page links. Anything else stays text.
#[must_use]
pub fn found(body: &str) -> Vec<Found> {
    scan(body)
        .into_iter()
        .filter_map(|spot| match spot {
            Spot::Ref(found) => Some(found),
            Spot::Bad(_) => None,
        })
        .collect()
}

/// A media type as an upload states it: parameters dropped, lowercase, and
/// `type/subtype` of token characters.
///
/// # Errors
/// What is wrong with it.
pub fn media_type(value: &str) -> Result<String, String> {
    let main = value.split(';').next().unwrap_or_default().trim().to_ascii_lowercase();
    let token =
        |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_alphanumeric() || b"!#$&^_.+-".contains(&b));
    match main.split_once('/') {
        Some((kind, sub)) if main.len() <= MEDIA_TYPE_MAX && token(kind) && token(sub) => Ok(main),
        _ => Err(format!("{value:?} is not a media type (type/subtype of at most {MEDIA_TYPE_MAX} bytes)")),
    }
}

/// The biggest image a page shows inline; a bigger one stays a link.
pub const PREVIEW_MAX: u64 = 10_000_000;

/// Whether a web page may show a blob of this stored media type and size
/// inline as an image. Raster formats only, by the uploader's say (the
/// server never sniffs): an `<img>` never runs what it shows, but a type
/// that is not an image the browsers draw would only be a broken picture.
#[must_use]
pub fn previewable(media_type: &str, size: u64) -> bool {
    size <= PREVIEW_MAX && matches!(media_type, "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "image/avif")
}

/// An original file name, once decoded: refused rather than rewritten.
///
/// # Errors
/// What is wrong with it.
pub fn check_filename(name: &str) -> Result<(), String> {
    if name.len() > FILENAME_MAX {
        return Err(format!("a file name is at most {FILENAME_MAX} bytes"));
    }
    if name.chars().any(|c| c.is_control() || matches!(c, '/' | '\\' | '\0')) {
        return Err("a file name has no control characters, '/' or '\\'".to_owned());
    }
    Ok(())
}

/// `text` percent-encoded: every byte but letters, digits and `-._~`.
#[must_use]
pub fn pct_encode(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for b in text.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'.' | b'_' | b'~') {
            out.push(char::from(b));
        } else {
            // writing to a String cannot fail
            let _written = write!(out, "%{b:02X}");
        }
    }
    out
}

/// `text` with its `%XX` escapes decoded; a `%` that starts no escape is
/// refused, as is a result that is not UTF-8.
///
/// # Errors
/// What is wrong with it.
pub fn pct_decode(text: &str) -> Result<String, String> {
    let mut bytes = Vec::with_capacity(text.len());
    let mut rest = text.as_bytes();
    while let Some((&first, tail)) = rest.split_first() {
        if first == b'%' {
            let digits = tail.get(..2).and_then(|pair| std::str::from_utf8(pair).ok());
            let byte = digits
                .filter(|d| d.bytes().all(|b| b.is_ascii_hexdigit()))
                .and_then(|d| u8::from_str_radix(d, 16).ok());
            let byte = byte.ok_or_else(|| format!("{text:?}: a % that is not followed by two hex digits"))?;
            bytes.push(byte);
            rest = tail.get(2..).unwrap_or_default();
        } else {
            bytes.push(first);
            rest = tail;
        }
    }
    String::from_utf8(bytes).map_err(|err| format!("{text:?} is not UTF-8 once decoded: {err}"))
}

/// A byte count for a message: decimal units, one decimal.
#[must_use]
pub fn human(bytes: u64) -> String {
    const UNITS: [(&str, u64); 3] = [("GB", 1_000_000_000), ("MB", 1_000_000), ("kB", 1000)];
    for (unit, size) in UNITS {
        if bytes >= size {
            let tenths = bytes.saturating_mul(10) / size;
            let (whole, tenth) = (tenths / 10, tenths % 10);
            return if tenth == 0 { format!("{whole} {unit}") } else { format!("{whole}.{tenth} {unit}") };
        }
    }
    format!("{bytes} bytes")
}

/// How long ago `ts` (a board timestamp) was at `now`, as a message says it:
/// `42 s`, `5 min`, `3 h`, `2 d`; `?` for a timestamp that is none.
#[must_use]
pub fn age(ts: &str, now: u64) -> String {
    let Some(then) = crate::timefmt::parse(ts) else {
        return "?".to_owned();
    };
    let secs = now.saturating_sub(then);
    match secs {
        0..=59 => format!("{secs} s"),
        60..=3599 => format!("{} min", secs / 60),
        3600..=86_399 => format!("{} h", secs / 3600),
        _ => format!("{} d", secs / 86_400),
    }
}

/// What may become of a blob: `pinned` (nothing removes it), `in grace` (not
/// pinned, but uploaded less than `grace` seconds before `now`), or
/// `evictable` (an upload that does not fit may take it).
#[must_use]
pub fn standing(pinned: bool, ts: &str, now: u64, grace: u64) -> &'static str {
    if pinned {
        "pinned"
    } else if crate::timefmt::parse(ts).is_some_and(|then| then.saturating_add(grace) > now) {
        "in grace"
    } else {
        "evictable"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const H: &str = "3f786850e387550fdab836ed7e6dc881de23001b9e14a9f8b5a8d1c2e4f60a1b";

    fn refs(body: &str) -> Result<Vec<String>, RefError> {
        references(body)
    }

    #[test]
    fn a_reference_starts_at_the_beginning_or_after_what_is_not_part_of_a_word() {
        for body in [
            format!("board-blob://{H}"),
            format!("see board-blob://{H}"),
            format!("({SCHEME}{H})"),
            format!("[trace]({SCHEME}{H})"),
            format!("\"{SCHEME}{H}\""),
            format!("file={SCHEME}{H}"),
            format!("a\n{SCHEME}{H}\nb"),
            format!("<{SCHEME}{H}>"),
        ] {
            assert_eq!(refs(&body), Ok(vec![H.to_owned()]), "{body}");
        }
        for body in [
            format!("x{SCHEME}{H}"),
            format!("my-{SCHEME}{H}"),
            format!("a.{SCHEME}{H}"),
            format!("a+{SCHEME}{H}"),
            format!("a_{SCHEME}{H}"),
            format!("\\{SCHEME}{H}"),
            format!("my-{SCHEME}is the scheme"),
        ] {
            assert_eq!(refs(&body), Ok(vec![]), "{body}");
        }
    }

    #[test]
    fn a_reference_ends_at_the_end_space_or_closing_punctuation() {
        for end in [".", ",", ";", ":", "!", "?", ")", "]", "}", ">", "\"", "'", "*", "`", ").", ")*.", " more", "\n"] {
            assert_eq!(refs(&format!("{SCHEME}{H}{end}")), Ok(vec![H.to_owned()]), "{end:?}");
        }
        for end in [".bin", "/x", "#x", "%20", "é", "-x", "_", ".x", ")x", "=", "~"] {
            assert!(matches!(refs(&format!("{SCHEME}{H}{end}")), Err(RefError::Bad(_))), "{end:?}");
        }
    }

    #[test]
    fn only_the_full_lowercase_hash_is_a_reference() {
        let bad = [
            format!("{SCHEME}{}", &H[..63]),
            format!("{SCHEME}{H}0"),
            format!("{SCHEME}{}", H.to_uppercase()),
            format!("{SCHEME}{}", &H[..12]),
            SCHEME.to_owned(),
            format!("{SCHEME}{}g", &H[..63]),
        ];
        for body in bad {
            assert!(matches!(refs(&body), Err(RefError::Bad(_))), "{body}");
        }
        let message = refs(&format!("{SCHEME}abc")).err().map(|e| e.to_string()).unwrap_or_default();
        assert!(message.contains("full 64-digit lowercase sha256") && message.contains("board put"), "{message}");
    }

    #[test]
    fn references_are_distinct_sorted_and_capped() {
        let hash = |n: usize| format!("{n:064x}");
        let body = (0..32).map(|n| reference(&hash(n))).collect::<Vec<_>>().join(" ");
        assert_eq!(refs(&format!("{body} {body}")).map(|r| r.len()), Ok(32));
        assert_eq!(refs(&format!("{body} {}", reference(&hash(32)))), Err(RefError::TooMany));
        assert_eq!(refs(&format!("{} {}", reference(&hash(2)), reference(&hash(1)))), Ok(vec![hash(1), hash(2)]));
    }

    #[test]
    fn found_gives_the_place_of_each_valid_reference_only() {
        let body = format!("a {SCHEME}{H}. b \\{SCHEME}{H} c {SCHEME}short");
        let found = found(&body);
        assert_eq!(found.len(), 1);
        let first = found.first().cloned().unwrap_or(Found { start: 0, end: 0, hash: String::new() });
        assert_eq!(body.get(first.start..first.end), Some(reference(H).as_str()));
    }

    #[test]
    fn a_media_type_is_a_token_pair_without_parameters() {
        assert_eq!(media_type("Text/Plain; charset=utf-8").as_deref(), Ok("text/plain"));
        assert_eq!(media_type("application/vnd.x+json").as_deref(), Ok("application/vnd.x+json"));
        for bad in ["", "text", "text/", "/x", "a b/c", "a/b/c", &format!("a/{}", "b".repeat(MEDIA_TYPE_MAX))] {
            assert!(media_type(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn only_raster_images_of_a_sane_size_are_previewed() {
        assert!(previewable("image/png", 1) && previewable("image/jpeg", PREVIEW_MAX));
        assert!(!previewable("image/png", PREVIEW_MAX + 1));
        for other in ["image/svg+xml", "text/html", "application/octet-stream", "image/png; x", ""] {
            assert!(!previewable(other, 1), "{other:?}");
        }
    }

    #[test]
    fn a_file_name_has_no_path_and_no_control_characters() {
        for good in ["trace.bin", "..hidden", "a b.tar.gz", "ü🙂", ""] {
            assert_eq!(check_filename(good), Ok(()), "{good:?}");
        }
        for bad in ["a/b", "a\\b", "a\nb", "a\0b", &"x".repeat(FILENAME_MAX + 1)] {
            assert!(check_filename(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn percent_coding_round_trips() {
        assert_eq!(pct_encode("trace v1.bin"), "trace%20v1.bin");
        assert_eq!(pct_decode(&pct_encode("ü🙂 a/b%")).as_deref(), Ok("ü🙂 a/b%"));
        assert!(pct_decode("%zz").is_err() && pct_decode("%4").is_err() && pct_decode("%ff").is_err());
    }

    #[test]
    fn sizes_read_in_decimal_units() {
        assert_eq!(human(0), "0 bytes");
        assert_eq!(human(999), "999 bytes");
        assert_eq!(human(1500), "1.5 kB");
        assert_eq!(human(49_800_000_000), "49.8 GB");
        assert_eq!(human(50_000_000_000), "50 GB");
        assert_eq!(human(3_100_000), "3.1 MB");
    }

    #[test]
    fn an_age_and_a_standing_read_from_the_upload_time() {
        let at = |secs| crate::timefmt::format(secs);
        assert_eq!(age(&at(1000), 1042), "42 s");
        assert_eq!(age(&at(1000), 1300), "5 min");
        assert_eq!(age(&at(1000), 1000 + 3 * 3600 + 5), "3 h");
        assert_eq!(age(&at(1000), 1000 + 2 * 86_400), "2 d");
        assert_eq!(age("nonsense", 5), "?");
        assert_eq!(standing(true, &at(0), 10_000, 3600), "pinned");
        assert_eq!(standing(false, &at(9000), 10_000, 3600), "in grace");
        assert_eq!(standing(false, &at(6400), 10_000, 3600), "evictable");
    }
}
