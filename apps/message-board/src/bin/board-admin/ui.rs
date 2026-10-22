//! Drawing: the "view" of the Elm-style structure. Each function here
//! reads the state and puts widgets on the frame; none changes what the
//! program will do next.
//!
//! Ratatui redraws by *immediate mode*: every draw renders the whole
//! screen into a fresh buffer (the `Frame`), and the terminal backend then
//! writes out only the cells that differ from the previous frame. So a
//! draw function simply describes the screen as it should look now.

use std::array::TryFromSliceError;

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, Paragraph};

use crate::app::{Picker, Screen};

/// A message body is untrusted text. Control characters in it would reach
/// the terminal as escape sequences, and ratatui drops them instead; show
/// them the way curses did, as `^X`, so the operator sees what is there.
/// Tabs are expanded to the next multiple of eight columns.
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

/// The first three rows of a screen. `Layout` splits an area by
/// *constraints*: `Length(1)` is exactly one row, `Fill(1)` takes whatever
/// is left.
fn three_rows(area: Rect) -> Result<[Rect; 3], TryFromSliceError> {
    let [first, second, third, _rest] =
        Layout::vertical([Constraint::Length(1), Constraint::Length(1), Constraint::Length(1), Constraint::Fill(1)])
            .try_areas(area)?;
    Ok([first, second, third])
}

/// Everything the screen shows now.
///
/// It takes the screen mutably for one reason: a list's `ListState`
/// (selection and scroll offset) is updated by the widget while it renders.
pub fn draw(frame: &mut Frame<'_>, screen: &mut Screen) -> Result<(), TryFromSliceError> {
    match screen {
        Screen::Areas { picker, .. }
        | Screen::Topics { picker, .. }
        | Screen::Messages { picker, .. }
        | Screen::Claims { picker, .. } => draw_picker(frame, picker),
        Screen::NoMessages { area } => {
            let [message, _, hint] = three_rows(frame.area())?;
            frame.render_widget(Paragraph::new(format!("{}: no messages.", visible(area))), message);
            frame.render_widget(Paragraph::new("press any key").style(Style::new().add_modifier(Modifier::DIM)), hint);
            Ok(())
        }
        Screen::TopicAction { area, topic, count } => {
            let text =
                format!("{}/{}: (b)rowse messages or (d)elete all {count}, (q) back", visible(area), visible(topic));
            frame.render_widget(Paragraph::new(text), frame.area());
            Ok(())
        }
        Screen::ConfirmTopic { area, topic, count, typed } => {
            let required = format!("{area}/{topic}");
            let [prompt, explain, input] = three_rows(frame.area())?;
            let warning = format!("Delete ALL {count} messages in {}. This cannot be undone.", visible(&required));
            frame.render_widget(Paragraph::new(warning).style(Style::new().add_modifier(Modifier::BOLD)), prompt);
            frame.render_widget(
                Paragraph::new(format!("Type '{}' to confirm, anything else cancels:", visible(&required))),
                explain,
            );
            let shown = format!("> {}", visible(typed));
            // A frame hides the cursor unless told where it goes: here, after
            // what has been typed, like a shell prompt.
            let column = u16::try_from(shown.chars().count()).unwrap_or(u16::MAX).min(input.width.saturating_sub(1));
            frame.render_widget(Paragraph::new(shown), input);
            frame.set_cursor_position(Position { x: input.x.saturating_add(column), y: input.y });
            Ok(())
        }
        Screen::ConfirmMessage { message, .. } => {
            let header = format!("#{}  {}  {}", message.id, message.ts, visible(&message.sender));
            confirm(frame, &header, &message.body, "Delete permanently? [y/N] ");
            Ok(())
        }
        Screen::ConfirmRelease { identity } => {
            let again = if identity.source == "owner" {
                "board-server binds it to the owner key again at its next start"
            } else {
                "the next signed post as it claims it again"
            };
            let header = format!("{}  [{}]  {}", identity.name, identity.source, identity.fingerprint);
            confirm(frame, &header, &format!("Releasing frees the name; {again}."), "Release this claim? [y/N] ");
            Ok(())
        }
    }
}

/// A list screen: its title on the first row, the list, and a hint on the
/// last row.
fn draw_picker(frame: &mut Frame<'_>, picker: &mut Picker) -> Result<(), TryFromSliceError> {
    let [title, list, _, footer] =
        Layout::vertical([Constraint::Length(1), Constraint::Fill(1), Constraint::Length(1), Constraint::Length(1)])
            .try_areas(frame.area())?;
    frame.render_widget(Paragraph::new(picker.title.as_str()).style(Style::new().add_modifier(Modifier::BOLD)), title);
    let items: Vec<String> = picker.items.iter().map(|item| visible(item)).collect();
    // A stateful widget renders with its state: the `List` draws the rows
    // that fit, scrolled so the selected one shows, and reverses it.
    let widget = List::new(items).highlight_style(Style::new().add_modifier(Modifier::REVERSED));
    frame.render_stateful_widget(widget, list, &mut picker.state);
    frame.render_widget(Paragraph::new(picker.footer).style(Style::new().add_modifier(Modifier::DIM)), footer);
    Ok(())
}

/// A y/N question under what it is about. Lines past the bottom of the
/// screen are cut off rather than drawn outside it.
fn confirm(frame: &mut Frame<'_>, header: &str, details: &str, question: &str) {
    let mut lines = vec![Line::from(visible(header)), Line::default()];
    lines.extend(message_board::py::splitlines(details).iter().map(|line| Line::from(visible(line))));
    lines.push(Line::default());
    lines.push(Line::from(Span::styled(question, Style::new().add_modifier(Modifier::BOLD))));
    frame.render_widget(Paragraph::new(lines), frame.area());
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
