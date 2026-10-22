//! A list to pick from: the state behind every list screen (a container's
//! children, a thread's messages, a queue's items), and what the keys that
//! move through a list do to it.

use ratatui::crossterm::event::KeyCode;
use ratatui::widgets::ListState;

/// How far `PgUp` and `PgDn` move, in rows.
pub const PAGE: usize = 10;

/// A list of `T`s, each shown as one row of text.
pub struct Picker<T> {
    pub title: String,
    /// What the rows stand for, in the order shown.
    rows: Vec<T>,
    /// Each row's text, made from its `T` once, when the list is read.
    pub labels: Vec<String>,
    pub footer: &'static str,
    /// The selection and scroll offset. A `ListState` is the state of a
    /// *stateful widget*: the program decides what is selected, and the
    /// `List` widget, when it renders, moves the offset so the selection
    /// stays in view. It lives here, with the rest of the state, because
    /// it has to outlast each frame.
    pub state: ListState,
}

/// What a key does to a list.
pub enum Pick<'a, T> {
    /// Enter, on this row.
    Open(&'a T),
    /// Esc or q.
    Back,
    /// The selection moved, or nothing happened (Enter on an empty list).
    Stay,
    /// A key lists do not use; the screen may have its own use for it.
    Other(KeyCode),
}

impl<T> Picker<T> {
    /// A list of `rows`, each shown as `label` makes it, with row
    /// `selected` selected (the last if there are fewer).
    pub fn new(
        title: String,
        rows: Vec<T>,
        label: impl Fn(&T) -> String,
        footer: &'static str,
        selected: usize,
    ) -> Self {
        let labels = rows.iter().map(label).collect();
        let last = rows.len().saturating_sub(1);
        Self { title, rows, labels, footer, state: ListState::default().with_selected(Some(selected.min(last))) }
    }

    /// Select `row`, or the last one if there are fewer.
    fn select(&mut self, row: usize) {
        self.state.select(Some(row.min(self.rows.len().saturating_sub(1))));
    }

    /// The selected row, or `None` if the list is empty.
    pub fn selected(&self) -> Option<&T> {
        self.state.selected().and_then(|i| self.rows.get(i))
    }

    pub fn key(&mut self, key: KeyCode) -> Pick<'_, T> {
        let current = self.state.selected().unwrap_or(0);
        match key {
            KeyCode::Up | KeyCode::Char('k') => self.select(current.saturating_sub(1)),
            KeyCode::Down | KeyCode::Char('j') => self.select(current.saturating_add(1)),
            KeyCode::PageUp => self.select(current.saturating_sub(PAGE)),
            KeyCode::PageDown => self.select(current.saturating_add(PAGE)),
            KeyCode::Home | KeyCode::Char('g') => self.select(0),
            KeyCode::End | KeyCode::Char('G') => self.select(usize::MAX),
            KeyCode::Enter => return self.selected().map_or(Pick::Stay, Pick::Open),
            KeyCode::Esc | KeyCode::Char('q') => return Pick::Back,
            other => return Pick::Other(other),
        }
        Pick::Stay
    }
}

/// The row to select in a list sorted by `keys`: the first row, unless the
/// list is shown again after leaving it from row `came_from`. Then it is
/// that row, or the one after it if it is gone (just deleted), or else the
/// last.
pub fn reselect<K: PartialOrd>(keys: impl IntoIterator<Item = K>, came_from: Option<K>) -> usize {
    let Some(wanted) = came_from else {
        return 0;
    };
    let mut last = 0;
    for (row, key) in keys.into_iter().enumerate() {
        if key >= wanted {
            return row;
        }
        last = row;
    }
    last
}

#[cfg(test)]
mod tests {
    use super::*;

    fn picker(rows: usize) -> Picker<usize> {
        Picker::new(String::new(), (0..rows).collect(), usize::to_string, "", 0)
    }

    #[test]
    fn the_selection_stays_on_the_list() {
        let mut list = picker(3);
        assert!(matches!(list.key(KeyCode::Up), Pick::Stay));
        assert_eq!(list.selected(), Some(&0));
        list.key(KeyCode::PageDown);
        assert_eq!(list.selected(), Some(&2));
        list.key(KeyCode::Char('j'));
        assert_eq!(list.selected(), Some(&2));
        list.key(KeyCode::Char('g'));
        assert!(matches!(list.key(KeyCode::Enter), Pick::Open(0)));
        assert!(matches!(list.key(KeyCode::Char('d')), Pick::Other(KeyCode::Char('d'))));

        let mut empty = picker(0);
        assert_eq!(empty.selected(), None);
        assert!(matches!(empty.key(KeyCode::Enter), Pick::Stay));
        assert!(matches!(empty.key(KeyCode::Char('q')), Pick::Back));
    }

    #[test]
    fn coming_back_selects_the_row_left_or_the_next() {
        assert_eq!(reselect([10, 20, 30], None), 0);
        assert_eq!(reselect([10, 20, 30], Some(20)), 1);
        assert_eq!(reselect([10, 30], Some(20)), 1); // 20 was deleted
        assert_eq!(reselect([10, 20], Some(30)), 1); // the last was deleted
        assert_eq!(reselect(Vec::<i64>::new(), Some(30)), 0);
        assert_eq!(reselect(["a", "a/b", "c"], Some("a/b")), 1);
    }
}
