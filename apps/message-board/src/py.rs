//! Python's text rules, where the board's interface depends on them.
//!
//! The board was first written in Python, and part of what its clients see
//! is defined by that: an error quotes a name the way `ascii()` or `repr()`
//! does, a pin is blank when `str.strip()` empties it, a watch line is a
//! body's first `str.splitlines()` line, a query string decodes the way
//! `urllib.parse.parse_qs` decodes it, and JSON is laid out as `json.dumps`
//! lays it out. This module is those rules and nothing else.

use std::fmt::{self, Write as _};
use std::io;

use serde::Serialize;
use serde_json::ser::{CharEscape, Formatter, Serializer};

/// `str.isspace()`: Unicode `White_Space`, plus the four ASCII information
/// separators that Python also counts.
#[must_use]
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()`.
#[must_use]
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `str.rstrip()`.
#[must_use]
pub fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

/// `str.split()` without a separator: runs of whitespace separate, and no
/// part is empty.
pub fn split(s: &str) -> impl Iterator<Item = &str> {
    s.split(is_space).filter(|part| !part.is_empty())
}

fn is_line_break(c: char) -> bool {
    matches!(c, '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// `str.splitlines()`: each line without its line break, "\r\n" being one
/// break, and no empty line after a final break.
#[must_use]
pub fn splitlines(s: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut line = String::new();
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if is_line_break(c) {
            if c == '\r' && chars.peek() == Some(&'\n') {
                chars.next();
            }
            lines.push(std::mem::take(&mut line));
        } else {
            line.push(c);
        }
    }
    if !line.is_empty() {
        lines.push(line);
    }
    lines
}

/// `repr()` of a string, written out as it is displayed; `printable` says
/// which non-ASCII characters stay as they are rather than being escaped.
struct Repr<'a> {
    s: &'a str,
    printable: fn(char) -> bool,
}

impl fmt::Display for Repr<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let quote = if self.s.contains('\'') && !self.s.contains('"') { '"' } else { '\'' };
        f.write_char(quote)?;
        for c in self.s.chars() {
            let code = u32::from(c);
            match c {
                '\\' => f.write_str("\\\\")?,
                '\n' => f.write_str("\\n")?,
                '\r' => f.write_str("\\r")?,
                '\t' => f.write_str("\\t")?,
                _ if c == quote => {
                    f.write_char('\\')?;
                    f.write_char(c)?;
                }
                _ if code < 0x20 || code == 0x7f => write!(f, "\\x{code:02x}")?,
                _ if c.is_ascii() || (self.printable)(c) => f.write_char(c)?,
                _ if code < 0x100 => write!(f, "\\x{code:02x}")?,
                _ if code < 0x1_0000 => write!(f, "\\u{code:04x}")?,
                _ => write!(f, "\\U{code:08x}")?,
            }
        }
        f.write_char(quote)
    }
}

/// `ascii(s)`: `repr(s)` with every non-ASCII character escaped.
#[must_use]
pub fn ascii(s: &str) -> String {
    Repr { s, printable: |_| false }.to_string()
}

/// `repr(s)`: non-ASCII characters stay unless they are not printable.
///
/// Python decides "printable" from the Unicode database it ships; this
/// escapes the categories it never prints (controls, format characters,
/// separators other than the space, private use and noncharacters) and
/// treats every other character as printable, which differs from Python
/// only for code points unassigned in its Unicode version.
#[must_use]
pub fn repr(s: &str) -> String {
    Repr { s, printable: is_printable }.to_string()
}

fn is_printable(c: char) -> bool {
    let code = u32::from(c);
    let unprintable = c.is_control()
        || matches!(code,
            // Zs other than the space, Zl, Zp
            0xa0 | 0x1680 | 0x2000..=0x200a | 0x2028 | 0x2029 | 0x202f | 0x205f | 0x3000
            // Cf
            | 0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x890..=0x891 | 0x8e2 | 0x180e
            | 0x200b..=0x200f | 0x202a..=0x202e | 0x2060..=0x2064 | 0x2066..=0x206f | 0xfeff
            | 0xfff9..=0xfffb | 0x110bd | 0x110cd | 0x13430..=0x1343f | 0x1bca0..=0x1bca3
            | 0x1d173..=0x1d17a | 0xe0001 | 0xe0020..=0xe007f
            // Co
            | 0xe000..=0xf8ff | 0xf_0000..=0xf_fffd | 0x10_0000..=0x10_fffd
            // noncharacters
            | 0xfdd0..=0xfdef)
        || code & 0xfffe == 0xfffe;
    !unprintable
}

/// The digits of a Python integer or float literal part, with its
/// underscores (allowed only singly, between two digits) removed.
fn without_underscores(s: &str) -> Option<String> {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    let mut previous_digit = false;
    while let Some(c) = chars.next() {
        if c == '_' {
            let next_digit = chars.peek().is_some_and(char::is_ascii_digit);
            if !(previous_digit && next_digit) {
                return None;
            }
            previous_digit = false;
        } else {
            previous_digit = c.is_ascii_digit();
            out.push(c);
        }
    }
    Some(out)
}

/// `int(s)` for a decimal string: surrounding whitespace, a sign and
/// underscores between digits are allowed. `None` if it is not an integer
/// or does not fit an i64.
#[must_use]
pub fn int(s: &str) -> Option<i64> {
    let s = strip(s);
    let (sign, digits) = match s.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", s.strip_prefix('+').unwrap_or(s)),
    };
    let digits = without_underscores(digits)?;
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    format!("{sign}{digits}").parse().ok()
}

/// `float(s)`: decimal and exponent forms, `inf`, `infinity` and `nan` in
/// any case, an optional sign, and underscores between digits.
#[must_use]
pub fn float(s: &str) -> Option<f64> {
    without_underscores(strip(s))?.parse().ok()
}

/// The UTF-8 of a string percent-encoded, keeping letters, digits, `_.-~`
/// and the bytes in `safe`; with `plus`, a space becomes `+`.
struct Quoted<'a> {
    s: &'a str,
    safe: &'static [u8],
    plus: bool,
}

impl fmt::Display for Quoted<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        for b in self.s.bytes() {
            if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) || self.safe.contains(&b) {
                f.write_char(char::from(b))?;
            } else if self.plus && b == b' ' {
                f.write_char('+')?;
            } else {
                write!(f, "%{b:02X}")?;
            }
        }
        Ok(())
    }
}

/// Jinja's `urlencode` filter on a string: `quote(s, safe="/")`.
#[must_use]
pub fn quote(s: &str) -> String {
    Quoted { s, safe: b"/", plus: false }.to_string()
}

/// `urllib.parse.quote_plus(s)`.
#[must_use]
pub fn quote_plus(s: &str) -> String {
    Quoted { s, safe: b"", plus: true }.to_string()
}

/// `urllib.parse.urlencode(pairs)`.
#[must_use]
pub fn urlencode(pairs: &[(&str, &str)]) -> String {
    pairs
        .iter()
        .map(|(name, value)| format!("{}={}", quote_plus(name), quote_plus(value)))
        .collect::<Vec<_>>()
        .join("&")
}

fn hex_value(c: char) -> Option<u8> {
    c.to_digit(16).and_then(|d| u8::try_from(d).ok())
}

/// `urllib.parse.unquote(s)`: `%XX` escapes in runs of ASCII become bytes,
/// read as UTF-8 with each bad sequence a U+FFFD; anything else stays.
#[must_use]
pub fn unquote(s: &str) -> String {
    if !s.contains('%') {
        return s.to_owned();
    }
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    let mut run: Vec<u8> = Vec::new();
    let mut i = 0;
    while let Some(&c) = chars.get(i) {
        if !c.is_ascii() {
            out.push_str(&String::from_utf8_lossy(&run));
            run.clear();
            out.push(c);
            i = i.saturating_add(1);
            continue;
        }
        let escape = (c == '%')
            .then(|| {
                let hi = chars.get(i.saturating_add(1)).copied().and_then(hex_value)?;
                let lo = chars.get(i.saturating_add(2)).copied().and_then(hex_value)?;
                Some(hi << 4 | lo)
            })
            .flatten();
        if let Some(byte) = escape {
            run.push(byte);
            i = i.saturating_add(3);
        } else {
            let mut buf = [0; 4];
            run.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
            i = i.saturating_add(1);
        }
    }
    out.push_str(&String::from_utf8_lossy(&run));
    out
}

/// A parsed query string or form body: each name's first value, as
/// `parse_qs(...)[name][0]` gives it.
#[derive(Debug, Default)]
pub struct Query(Vec<(String, String)>);

impl Query {
    /// `urllib.parse.parse_qs(qs, keep_blank_values)`: `&`-separated
    /// `name=value` pairs, `+` meaning a space. Without `keep_blank`, a pair
    /// with an empty value (or no `=`) is dropped.
    #[must_use]
    pub fn parse(qs: &str, keep_blank: bool) -> Self {
        let mut pairs = Vec::new();
        for part in qs.split('&') {
            if part.is_empty() {
                continue;
            }
            let (name, value) = match part.split_once('=') {
                Some(pair) => pair,
                None if keep_blank => (part, ""),
                None => continue,
            };
            if !value.is_empty() || keep_blank {
                pairs.push((unquote(&name.replace('+', " ")), unquote(&value.replace('+', " "))));
            }
        }
        Self(pairs)
    }

    /// The first value given for `name`.
    #[must_use]
    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().find(|(n, _)| n == name).map(|(_, v)| v.as_str())
    }

    /// The first value for `name`, or `default` if there is none.
    #[must_use]
    pub fn get_or<'a>(&'a self, name: &str, default: &'a str) -> &'a str {
        self.get(name).unwrap_or(default)
    }

    /// `form.get(name) or default`: an empty value counts as missing.
    #[must_use]
    pub fn get_nonempty_or<'a>(&'a self, name: &str, default: &'a str) -> &'a str {
        self.get(name).filter(|v| !v.is_empty()).unwrap_or(default)
    }
}

/// Bytes decoded as ISO-8859-1, as Python's HTTP server decodes a request
/// line: every byte is the code point of the same value.
#[must_use]
pub fn latin1(bytes: &[u8]) -> String {
    bytes.iter().copied().map(char::from).collect()
}

/// `json.dumps` layout: `", "` between items, `": "` after keys (or, with
/// an indent, a line per item), and every non-ASCII character escaped.
struct PyFormatter {
    indent: Option<&'static [u8]>,
    level: usize,
    has_value: bool,
}

impl PyFormatter {
    fn newline<W: ?Sized + io::Write>(&self, writer: &mut W) -> io::Result<()> {
        if let Some(indent) = self.indent {
            writer.write_all(b"\n")?;
            for _ in 0..self.level {
                writer.write_all(indent)?;
            }
        }
        Ok(())
    }

    fn separator<W: ?Sized + io::Write>(&self, writer: &mut W, first: bool) -> io::Result<()> {
        match (first, self.indent) {
            (true, None) => Ok(()),
            (false, None) => writer.write_all(b", "),
            (true, Some(_)) => self.newline(writer),
            (false, Some(_)) => {
                writer.write_all(b",")?;
                self.newline(writer)
            }
        }
    }

    fn open<W: ?Sized + io::Write>(&mut self, writer: &mut W, bracket: &[u8]) -> io::Result<()> {
        self.level = self.level.saturating_add(1);
        self.has_value = false;
        writer.write_all(bracket)
    }

    fn close<W: ?Sized + io::Write>(&mut self, writer: &mut W, bracket: &[u8]) -> io::Result<()> {
        self.level = self.level.saturating_sub(1);
        if self.has_value {
            self.newline(writer)?;
        }
        writer.write_all(bracket)
    }
}

impl Formatter for PyFormatter {
    fn begin_array<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        self.open(writer, b"[")
    }

    fn end_array<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        self.close(writer, b"]")
    }

    fn begin_array_value<W: ?Sized + io::Write>(&mut self, writer: &mut W, first: bool) -> io::Result<()> {
        self.separator(writer, first)
    }

    fn end_array_value<W: ?Sized + io::Write>(&mut self, _writer: &mut W) -> io::Result<()> {
        self.has_value = true;
        Ok(())
    }

    fn begin_object<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        self.open(writer, b"{")
    }

    fn end_object<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        self.close(writer, b"}")
    }

    fn begin_object_key<W: ?Sized + io::Write>(&mut self, writer: &mut W, first: bool) -> io::Result<()> {
        self.separator(writer, first)
    }

    fn begin_object_value<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }

    fn end_object_value<W: ?Sized + io::Write>(&mut self, _writer: &mut W) -> io::Result<()> {
        self.has_value = true;
        Ok(())
    }

    fn write_string_fragment<W: ?Sized + io::Write>(&mut self, writer: &mut W, fragment: &str) -> io::Result<()> {
        for c in fragment.chars() {
            if c.is_ascii() && c != '\u{7f}' {
                writer.write_all(&[u8::try_from(c).map_err(io::Error::other)?])?;
            } else {
                let mut units = [0; 2];
                for unit in c.encode_utf16(&mut units) {
                    write!(writer, "\\u{unit:04x}")?;
                }
            }
        }
        Ok(())
    }

    fn write_char_escape<W: ?Sized + io::Write>(&mut self, writer: &mut W, char_escape: CharEscape) -> io::Result<()> {
        let escaped: &[u8] = match char_escape {
            CharEscape::Quote => b"\\\"",
            CharEscape::ReverseSolidus => b"\\\\",
            CharEscape::Solidus => b"\\/",
            CharEscape::Backspace => b"\\b",
            CharEscape::FormFeed => b"\\f",
            CharEscape::LineFeed => b"\\n",
            CharEscape::CarriageReturn => b"\\r",
            CharEscape::Tab => b"\\t",
            CharEscape::AsciiControl(byte) => return write!(writer, "\\u{byte:04x}"),
        };
        writer.write_all(escaped)
    }
}

fn dumps_with<T: Serialize + ?Sized>(value: &T, indent: Option<&'static [u8]>) -> Result<String, serde_json::Error> {
    let mut out = Vec::new();
    let formatter = PyFormatter { indent, level: 0, has_value: false };
    value.serialize(&mut Serializer::with_formatter(&mut out, formatter))?;
    String::from_utf8(out).map_err(<serde_json::Error as serde::ser::Error>::custom)
}

/// `json.dumps(value)`.
///
/// # Errors
/// If `value` cannot be represented as JSON.
pub fn dumps<T: Serialize + ?Sized>(value: &T) -> Result<String, serde_json::Error> {
    dumps_with(value, None)
}

/// `json.dumps(value, indent=2)`.
///
/// # Errors
/// If `value` cannot be represented as JSON.
pub fn dumps_indented<T: Serialize + ?Sized>(value: &T) -> Result<String, serde_json::Error> {
    dumps_with(value, Some(b"  "))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn whitespace_is_pythons() {
        assert_eq!(strip("\u{1c} pin \u{2028}\t"), "pin");
        assert_eq!(rstrip("body \n\u{1f}"), "body");
        assert_eq!(split(" a\u{1d}b  c ").collect::<Vec<_>>(), ["a", "b", "c"]);
        assert_eq!(strip("\u{1e}\u{a0}"), "");
    }

    #[test]
    fn splitlines_is_pythons() {
        assert_eq!(splitlines("a\r\nb\rc\u{2028}d\u{1c}e\n"), ["a", "b", "c", "d", "e"]);
        assert_eq!(splitlines("a\n\nb"), ["a", "", "b"]);
        assert_eq!(splitlines("\n"), [""]);
        assert_eq!(splitlines(""), Vec::<String>::new());
    }

    #[test]
    fn ascii_and_repr_quote_like_python() {
        assert_eq!(ascii("Stefan "), "'Stefan '");
        assert_eq!(ascii("Stef\u{430}n"), "'Stef\\u0430n'");
        assert_eq!(ascii("st\u{e9}fan\n"), "'st\\xe9fan\\n'");
        assert_eq!(ascii("\u{1f642}"), "'\\U0001f642'");
        assert_eq!(ascii("it's"), "\"it's\"");
        assert_eq!(ascii("'\""), "'\\'\"'");
        assert_eq!(ascii("\u{7f}\\"), "'\\x7f\\\\'");
        assert_eq!(repr("b\u{f5}ard\u{200b}"), "'b\u{f5}ard\\u200b'");
    }

    #[test]
    fn int_and_float_parse_like_python() {
        assert_eq!(int(" 42\n"), Some(42));
        assert_eq!(int("+4_2"), Some(42));
        assert_eq!(int("-7"), Some(-7));
        for bad in ["", "4__2", "_4", "4_", "1.0", "0x10", "banana", "--1"] {
            assert_eq!(int(bad), None, "{bad}");
        }
        assert_eq!(float("1_0.5"), Some(10.5));
        assert_eq!(float(" -INF "), Some(f64::NEG_INFINITY));
        assert!(float("nan").is_some_and(f64::is_nan));
        assert_eq!(float("1__0"), None);
    }

    #[test]
    fn quoting_matches_urllib_and_jinja() {
        assert_eq!(quote("a b/c&d=\u{e9}~_.-+"), "a%20b/c%26d%3D%C3%A9~_.-%2B");
        assert_eq!(quote_plus("a b/c"), "a+b%2Fc");
        assert_eq!(urlencode(&[("area", "a b"), ("x", "&")]), "area=a+b&x=%26");
    }

    #[test]
    fn parse_qs_and_unquote_like_urllib() {
        let q = Query::parse("a=1&b=&c&a=2&d=x+y%20z&e=%C3%A9%zz%4", false);
        assert_eq!(q.get("a"), Some("1"));
        assert_eq!(q.get("b"), None);
        assert_eq!(q.get("c"), None);
        assert_eq!(q.get("d"), Some("x y z"));
        assert_eq!(q.get("e"), Some("\u{e9}%zz%4"));
        let q = Query::parse("b=&c", true);
        assert_eq!((q.get("b"), q.get("c")), (Some(""), Some("")));
        assert_eq!(unquote("%C3"), "\u{fffd}");
        assert_eq!(unquote("%%41"), "%A");
        assert_eq!(unquote("\u{e9}%41"), "\u{e9}A");
    }

    #[derive(Serialize)]
    struct Sample<'a> {
        id: i64,
        body: &'a str,
        signed_by: Option<&'a str>,
        verified: bool,
        list: Vec<i64>,
    }

    #[test]
    fn dumps_lays_out_json_like_python() -> Result<(), serde_json::Error> {
        let sample =
            Sample { id: 2, body: "\u{fc}\u{1f642}\n\"\u{7f}\u{1}/", signed_by: None, verified: true, list: vec![] };
        assert_eq!(
            dumps(&sample)?,
            r#"{"id": 2, "body": "\u00fc\ud83d\ude42\n\"\u007f\u0001/", "signed_by": null, "verified": true, "list": []}"#
        );
        let nested = serde_json::json!({"pins": [], "messages": [{"id": 1}, {"id": 2}]});
        assert_eq!(
            dumps_indented(&nested)?,
            "{\n  \"pins\": [],\n  \"messages\": [\n    {\n      \"id\": 1\n    },\n    {\n      \"id\": 2\n    }\n  ]\n}"
        );
        Ok(())
    }
}
