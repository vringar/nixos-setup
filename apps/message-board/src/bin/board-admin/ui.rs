//! Drawing: the "view" of the Elm-style structure. Each function here
//! reads the state and puts widgets on the frame; none changes what the
//! program will do next.
//!
//! Ratatui redraws by *immediate mode*: every draw renders the whole
//! screen into a fresh buffer (the `Frame`), and the terminal backend then
//! writes out only the cells that differ from the previous frame. So a
//! draw function simply describes the screen as it should look now.
//!
//! What goes on the screen is *text*: a `Paragraph` or a `List` takes
//! `Line`s, each made of `Span`s, and every span has one `Style` (bold,
//! dim, reversed). A plain `String` converts into a line of one unstyled
//! span.

use std::array::TryFromSliceError;

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::Line;
use ratatui::widgets::{List, Paragraph, Wrap};

use message_board::blob;
use message_board::store::{BlobInfo, Message};

use crate::pager::Pager;
use crate::picker::Picker;
use crate::screens::Screen;

/// A body is untrusted text. Control characters in it would reach the
/// terminal as escape sequences, and ratatui drops them instead; show them
/// the way curses did, as `^X`, so the operator sees what is there. Tabs
/// are expanded to the next multiple of eight columns.
fn visible(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut column = 0_usize;
    for c in line.chars() {
        let code = u32::from(c);
        if c == '\t' {
            let to = (column / 8 + 1) * 8;
            out.extend(std::iter::repeat_n(' ', to - column));
            column = to;
        } else if code < 0x20 || code == 0x7f {
            out.push('^');
            out.push(char::from_u32(code ^ 0x40).unwrap_or('?'));
            column += 2;
        } else if c.is_control() {
            out.push('\u{fffd}');
            column += 1;
        } else {
            out.push(c);
            column += 1;
        }
    }
    out
}

fn bold() -> Style {
    Style::new().add_modifier(Modifier::BOLD)
}

fn dim() -> Style {
    Style::new().add_modifier(Modifier::DIM)
}

/// The parts of a list or text screen, top to bottom: a title row, the
/// list or text, a row for the notice, and a row of key hints.
///
/// `Layout` splits an area by *constraints*: `Length(1)` is exactly one
/// row, `Fill(1)` takes whatever the others leave. `try_areas` returns
/// them as an array to destructure (it fails only if the array's length
/// is not the number of constraints, which here it always is).
fn chrome(area: Rect) -> Result<[Rect; 4], TryFromSliceError> {
    Layout::vertical([Constraint::Length(1), Constraint::Fill(1), Constraint::Length(1), Constraint::Length(1)])
        .try_areas(area)
}

/// Everything the screen shows now, and `notice` under it.
///
/// It takes the screen mutably for one reason: a list's `ListState`
/// (selection and scroll offset) is updated by the widget while it renders.
pub fn draw(frame: &mut Frame<'_>, screen: &mut Screen, notice: Option<&str>) -> Result<(), TryFromSliceError> {
    match screen {
        Screen::Tree { picker, .. } => draw_picker(frame, picker, notice),
        Screen::Thread { picker, .. } => draw_picker(frame, picker, notice),
        Screen::Queue { picker, .. } => draw_picker(frame, picker, notice),
        Screen::Blobs { picker, .. } => draw_picker(frame, picker, notice),
        Screen::Note { pager, .. }
        | Screen::Message { pager, .. }
        | Screen::Item { pager, .. }
        | Screen::Blob { pager, .. } => draw_pager(frame, pager, notice),
        Screen::ConfirmBranch { path, nodes, typed } => confirm_branch(frame, path, nodes, typed),
        Screen::ConfirmMessage { message } => confirm_message(frame, message),
        Screen::ConfirmBlob { info, .. } => confirm_blob(frame, info),
    }
}

/// The title, the notice and the key hints around a list or a text.
fn draw_chrome(
    frame: &mut Frame<'_>,
    [title, _, notice_row, footer]: [Rect; 4],
    heading: &str,
    notice: Option<&str>,
    hints: &str,
) {
    frame.render_widget(Paragraph::new(visible(heading)).style(bold()), title);
    if let Some(notice) = notice {
        frame.render_widget(Paragraph::new(visible(notice)).style(bold()), notice_row);
    }
    frame.render_widget(Paragraph::new(hints.to_owned()).style(dim()), footer);
}

fn draw_picker<T>(
    frame: &mut Frame<'_>,
    picker: &mut Picker<T>,
    notice: Option<&str>,
) -> Result<(), TryFromSliceError> {
    let parts = chrome(frame.area())?;
    draw_chrome(frame, parts, &picker.title, notice, picker.footer);
    let [_, list, _, _] = parts;
    if picker.labels.is_empty() {
        frame.render_widget(Paragraph::new("(nothing here)").style(dim()), list);
        return Ok(());
    }
    let rows: Vec<String> = picker.labels.iter().map(|label| visible(label)).collect();
    // A stateful widget renders with its state: the `List` draws the rows
    // that fit, scrolled so the selected one shows, and reverses it.
    let widget = List::new(rows).highlight_style(Style::new().add_modifier(Modifier::REVERSED));
    frame.render_stateful_widget(widget, list, &mut picker.state);
    Ok(())
}

fn draw_pager(frame: &mut Frame<'_>, pager: &Pager, notice: Option<&str>) -> Result<(), TryFromSliceError> {
    let parts = chrome(frame.area())?;
    let total = pager.lines.len();
    let hints = format!("{}    line {} of {total}", pager.footer, pager.top.saturating_add(1).min(total));
    draw_chrome(frame, parts, &pager.title, notice, &hints);
    let [_, text, _, _] = parts;
    // From the top line on, as many lines as could show: each takes a row
    // at least. `Wrap` breaks a line too long for the width onto the next
    // rows (at word boundaries, `trim: false` keeping its indentation);
    // what does not fit at the bottom is cut off.
    let lines: Vec<Line> = pager
        .lines
        .iter()
        .skip(pager.top)
        .take(usize::from(text.height))
        .map(|line| Line::from(visible(line)))
        .collect();
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), text);
    Ok(())
}

/// Deleting a branch: what goes, and the path to type out. The question
/// sits at the bottom, so that it shows however long the list above it.
fn confirm_branch(frame: &mut Frame<'_>, path: &str, nodes: &[String], typed: &str) -> Result<(), TryFromSliceError> {
    let [warning, _, list, _, prompt, input] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .try_areas(frame.area())?;
    let count = if nodes.len() == 1 { "1 node".to_owned() } else { format!("{} nodes", nodes.len()) };
    let path = visible(path);
    let text = format!("Delete {path} and everything in it ({count}). This cannot be undone.");
    frame.render_widget(Paragraph::new(text).style(bold()), warning);
    let lines: Vec<Line> = nodes.iter().map(|node| Line::from(visible(node))).collect();
    frame.render_widget(Paragraph::new(lines), list);
    frame.render_widget(Paragraph::new(format!("Type '{path}' to confirm, anything else cancels:")), prompt);
    let shown = format!("> {}", visible(typed));
    // A frame hides the cursor unless told where it goes: here, after what
    // has been typed, like a shell prompt.
    let column = u16::try_from(shown.chars().count()).unwrap_or(u16::MAX).min(input.width.saturating_sub(1));
    frame.render_widget(Paragraph::new(shown), input);
    frame.set_cursor_position(Position { x: input.x.saturating_add(column), y: input.y });
    Ok(())
}

/// Deleting one message: the whole of it, and a y/N question under it.
fn confirm_message(frame: &mut Frame<'_>, message: &Message) -> Result<(), TryFromSliceError> {
    let [warning, _, body, _, question] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .try_areas(frame.area())?;
    let header = format!("#{}  {}  {}  in {}", message.id, message.ts, message.from, message.path);
    frame.render_widget(Paragraph::new(visible(&header)).style(bold()), warning);
    let lines: Vec<Line> = message.body.lines().map(|line| Line::from(visible(line))).collect();
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), body);
    frame.render_widget(Paragraph::new("Delete permanently? [y/N] ").style(bold()), question);
    Ok(())
}

/// Deleting one blob: what it is, and a y/N question under it.
fn confirm_blob(frame: &mut Frame<'_>, info: &BlobInfo) -> Result<(), TryFromSliceError> {
    let [warning, _, body, _, question] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Fill(1),
        Constraint::Length(1),
        Constraint::Length(1),
    ])
    .try_areas(frame.area())?;
    let header = format!("blob {}  {}  {}", info.hash, blob::human(info.size), info.filename);
    frame.render_widget(Paragraph::new(visible(&header)).style(bold()), warning);
    let text = format!(
        "uploaded {} by {}; {} {}.",
        info.ts,
        info.from,
        info.refs,
        if info.refs == 1 { "event references it" } else { "events reference it" }
    );
    frame.render_widget(Paragraph::new(visible(&text)).wrap(Wrap { trim: false }), body);
    frame.render_widget(Paragraph::new("Delete the file permanently? [y/N] ").style(bold()), question);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn control_characters_show_instead_of_reaching_the_terminal() {
        assert_eq!(visible("a\u{1b}[2Jb\u{7f}"), "a^[[2Jb^?");
        assert_eq!(visible("x\ty"), "x       y");
        assert_eq!(visible("\u{9b}"), "\u{fffd}");
    }
}
