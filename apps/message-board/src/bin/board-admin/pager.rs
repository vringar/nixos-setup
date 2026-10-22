//! A text to read a screenful at a time: the state behind the screens that
//! show a note, a message or an item, and what the scrolling keys do.

use ratatui::crossterm::event::KeyCode;

use crate::picker::PAGE;

/// A text, scrolled to a line.
///
/// It scrolls by line of the text, not by row of the screen. A long line
/// wraps onto several rows, and how many rows a wrapped `Paragraph` takes
/// is something ratatui tells only behind an unstable feature
/// (`Paragraph::line_count`). Drawing from line `top` on, by contrast,
/// needs no such count, and every line of the text can be brought to the
/// top of the screen, the last one included.
pub struct Pager {
    pub title: String,
    pub lines: Vec<String>,
    pub footer: &'static str,
    /// The line at the top of the screen.
    pub top: usize,
}

/// What a key does to a text.
pub enum Read {
    /// Esc or q.
    Back,
    /// It scrolled, or nothing happened.
    Stay,
    /// A key texts do not use; the screen may have its own use for it.
    Other(KeyCode),
}

impl Pager {
    pub fn new(title: String, lines: Vec<String>, footer: &'static str) -> Self {
        Self { title, lines, footer, top: 0 }
    }

    /// Put `line` at the top, or the last line if there are fewer.
    fn scroll_to(&mut self, line: usize) {
        self.top = line.min(self.lines.len().saturating_sub(1));
    }

    pub fn key(&mut self, key: KeyCode) -> Read {
        match key {
            KeyCode::Up | KeyCode::Char('k') => self.scroll_to(self.top.saturating_sub(1)),
            KeyCode::Down | KeyCode::Char('j') => self.scroll_to(self.top.saturating_add(1)),
            KeyCode::PageUp => self.scroll_to(self.top.saturating_sub(PAGE)),
            KeyCode::PageDown | KeyCode::Char(' ') => self.scroll_to(self.top.saturating_add(PAGE)),
            KeyCode::Home | KeyCode::Char('g') => self.scroll_to(0),
            KeyCode::End | KeyCode::Char('G') => self.scroll_to(usize::MAX),
            KeyCode::Esc | KeyCode::Char('q') => return Read::Back,
            other => return Read::Other(other),
        }
        Read::Stay
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scrolling_stops_at_the_last_line() {
        let mut text = Pager::new(String::new(), (0..15).map(|n| n.to_string()).collect(), "");
        text.key(KeyCode::PageDown);
        assert_eq!(text.top, 10);
        text.key(KeyCode::PageDown);
        assert_eq!(text.top, 14);
        text.key(KeyCode::Char('j'));
        assert_eq!(text.top, 14);
        text.key(KeyCode::Char('g'));
        text.key(KeyCode::Up);
        assert_eq!(text.top, 0);
        assert!(matches!(text.key(KeyCode::Char('d')), Read::Other(KeyCode::Char('d'))));
        assert!(matches!(text.key(KeyCode::Esc), Read::Back));
    }
}
