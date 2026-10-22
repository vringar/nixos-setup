//! The TUI's state and what keys do to it.
//!
//! This is the "model" and "update" of the Elm-style structure the ratatui
//! documentation recommends: [`App`] holds everything the screen shows,
//! [`App::update`] turns one key press into the next state, and drawing
//! (in `ui.rs`) only reads the state. Nothing here touches the terminal,
//! so the whole flow is plain data plus the database.
//!
//! One screen is up at a time. Each time a list is shown it is read fresh
//! from the database and its selection starts on the first row, which is
//! what an operator (and the tests driving this through a terminal) rely
//! on.

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::widgets::ListState;

use message_board::py;
use message_board::store::{Board, Identity, Listed};

use crate::Failure;
use crate::audit::Audit;

/// A list to pick from: what the picker screens have in common.
pub struct Picker {
    pub title: String,
    pub items: Vec<String>,
    pub footer: &'static str,
    /// The selection and scroll offset. A `ListState` is the state of a
    /// *stateful widget*: the program decides what is selected, and the
    /// `List` widget, when it renders, moves the offset so the selection
    /// stays in view. It lives here, with the rest of the state, because
    /// it has to outlast each frame.
    pub state: ListState,
}

impl Picker {
    fn new(title: String, items: Vec<String>, footer: &'static str) -> Self {
        Self { title, items, footer, state: ListState::default().with_selected(Some(0)) }
    }

    fn up(&mut self) {
        let current = self.state.selected().unwrap_or(0);
        self.state.select(Some(current.saturating_sub(1)));
    }

    fn down(&mut self) {
        let last = self.items.len().saturating_sub(1);
        let current = self.state.selected().unwrap_or(0);
        self.state.select(Some(current.saturating_add(1).min(last)));
    }

    /// The selected row, or `None` if the list is empty.
    fn chosen(&self) -> Option<usize> {
        self.state.selected().filter(|&i| i < self.items.len())
    }
}

/// What a picker does with a key.
enum Pick {
    Chosen(usize),
    Back,
    Stay,
}

fn pick(picker: &mut Picker, key: KeyCode) -> Pick {
    match key {
        KeyCode::Up | KeyCode::Char('k') => picker.up(),
        KeyCode::Down | KeyCode::Char('j') => picker.down(),
        // Enter on an empty list goes back, as there is nothing to choose.
        KeyCode::Enter => return picker.chosen().map_or(Pick::Back, Pick::Chosen),
        KeyCode::Esc | KeyCode::Char('q') => return Pick::Back,
        _ => {}
    }
    Pick::Stay
}

/// The screens, one at a time.
pub enum Screen {
    /// Every area, then the way into the name claims.
    Areas {
        picker: Picker,
        areas: Vec<String>,
    },
    /// An area with nothing left in it: any key goes back.
    NoMessages {
        area: String,
    },
    Topics {
        area: String,
        picker: Picker,
        topics: Vec<(String, i64)>,
    },
    /// Browse a topic, or delete all of it.
    TopicAction {
        area: String,
        topic: String,
        count: i64,
    },
    /// Deleting a whole topic needs its name typed out, not just a y.
    ConfirmTopic {
        area: String,
        topic: String,
        count: i64,
        typed: String,
    },
    Messages {
        area: String,
        topic: String,
        picker: Picker,
        messages: Vec<Listed>,
    },
    ConfirmMessage {
        area: String,
        topic: String,
        message: Listed,
    },
    Claims {
        picker: Picker,
        claims: Vec<Identity>,
    },
    ConfirmRelease {
        identity: Identity,
    },
}

/// What the event loop does after a key.
#[derive(Debug, PartialEq, Eq)]
pub enum Flow {
    Continue,
    Quit,
    /// Ctrl-C: in raw mode it is a key, not a signal.
    Interrupted,
}

pub struct App {
    board: Board,
    audit: Audit,
    pub screen: Screen,
}

/// The first line of a message, cut to 80 characters, for a list row.
fn first_line(body: &str) -> String {
    py::splitlines(body).into_iter().next().unwrap_or_default().chars().take(80).collect()
}

impl App {
    /// Start on the list of areas.
    pub fn new(board: Board, audit: Audit) -> Result<Self, Failure> {
        let screen = areas_screen(&board)?;
        Ok(Self { board, audit, screen })
    }

    /// Turn one key press into the next state. Deletions happen here, and
    /// each is written to the audit log as it happens.
    pub fn update(&mut self, key: KeyEvent) -> Result<Flow, Failure> {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            return Ok(Flow::Interrupted);
        }
        let next = match &mut self.screen {
            Screen::Areas { picker, areas } => match pick(picker, key.code) {
                Pick::Chosen(i) => match areas.get(i) {
                    Some(area) => topics_screen(&self.board, area)?,
                    None => claims_screen(&self.board)?, // the entry after the areas
                },
                Pick::Back => return Ok(Flow::Quit),
                Pick::Stay => return Ok(Flow::Continue),
            },
            Screen::NoMessages { .. } => areas_screen(&self.board)?,
            Screen::Topics { area, picker, topics } => match pick(picker, key.code) {
                Pick::Chosen(i) => match topics.get(i) {
                    Some((topic, count)) => {
                        Screen::TopicAction { area: area.clone(), topic: topic.clone(), count: *count }
                    }
                    None => return Ok(Flow::Continue),
                },
                Pick::Back => areas_screen(&self.board)?,
                Pick::Stay => return Ok(Flow::Continue),
            },
            Screen::TopicAction { area, topic, count } => match key.code {
                KeyCode::Esc | KeyCode::Char('q') => topics_screen(&self.board, area)?,
                KeyCode::Char('d') => Screen::ConfirmTopic {
                    area: area.clone(),
                    topic: topic.clone(),
                    count: *count,
                    typed: String::new(),
                },
                // any other key browses
                _ => messages_screen(&self.board, area, topic)?,
            },
            Screen::ConfirmTopic { area, topic, typed, .. } => match key.code {
                KeyCode::Enter => {
                    if *typed == format!("{area}/{topic}") {
                        let deleted = self.board.delete_topic(area, topic)?;
                        self.audit.record("delete_topic", &format!("{area}/{topic} ({deleted} messages)"))?;
                    }
                    topics_screen(&self.board, area)?
                }
                KeyCode::Backspace => {
                    typed.pop();
                    return Ok(Flow::Continue);
                }
                KeyCode::Esc => topics_screen(&self.board, area)?,
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    typed.push(c);
                    return Ok(Flow::Continue);
                }
                _ => return Ok(Flow::Continue),
            },
            Screen::Messages { area, topic, picker, messages } => match pick(picker, key.code) {
                Pick::Chosen(i) => match messages.get(i) {
                    Some(message) => {
                        Screen::ConfirmMessage { area: area.clone(), topic: topic.clone(), message: message.clone() }
                    }
                    None => return Ok(Flow::Continue),
                },
                Pick::Back => topics_screen(&self.board, area)?,
                Pick::Stay => return Ok(Flow::Continue),
            },
            Screen::ConfirmMessage { area, topic, message } => {
                // Gone already (someone else deleted it) is not an error: there is
                // just nothing to record.
                if matches!(key.code, KeyCode::Char('y' | 'Y')) && self.board.delete_message(message.id)? {
                    self.audit
                        .record("delete_message", &format!("#{} {area}/{topic} from={}", message.id, message.sender))?;
                }
                messages_screen(&self.board, area, topic)?
            }
            Screen::Claims { picker, claims } => match pick(picker, key.code) {
                Pick::Chosen(i) => match claims.get(i) {
                    Some(identity) => Screen::ConfirmRelease { identity: identity.clone() },
                    None => return Ok(Flow::Continue),
                },
                Pick::Back => areas_screen(&self.board)?,
                Pick::Stay => return Ok(Flow::Continue),
            },
            Screen::ConfirmRelease { identity } => {
                if matches!(key.code, KeyCode::Char('y' | 'Y')) && self.board.delete_identity(&identity.name)? {
                    self.audit.record(
                        "release_claim",
                        &format!("{} {} ({})", identity.name, identity.fingerprint, identity.source),
                    )?;
                }
                claims_screen(&self.board)?
            }
        };
        self.screen = next;
        Ok(Flow::Continue)
    }
}

fn areas_screen(board: &Board) -> Result<Screen, Failure> {
    let counts = board.area_counts()?;
    let claims = board.list_identities()?.len();
    let mut items: Vec<String> = counts
        .iter()
        .map(|(area, archived, n)| format!("{area}  [{}]  ({n} messages)", if *archived { "archived" } else { "live" }))
        .collect();
    items.push(format!("-- name claims ({claims})"));
    let picker = Picker::new(
        "board-admin -- select an area (q to quit)".to_owned(),
        items,
        "up/down j/k move, enter select, q quit",
    );
    Ok(Screen::Areas { picker, areas: counts.into_iter().map(|(area, _, _)| area).collect() })
}

fn topics_screen(board: &Board, area: &str) -> Result<Screen, Failure> {
    let topics = board.topic_counts(area)?;
    if topics.is_empty() {
        return Ok(Screen::NoMessages { area: area.to_owned() });
    }
    let items = topics.iter().map(|(topic, n)| format!("{topic}  ({n} messages)")).collect();
    let picker = Picker::new(
        format!("board-admin -- {area} -- select a topic (q back)"),
        items,
        "enter: select topic    q: back",
    );
    Ok(Screen::Topics { area: area.to_owned(), picker, topics })
}

fn messages_screen(board: &Board, area: &str, topic: &str) -> Result<Screen, Failure> {
    let messages = board.messages_in(area, topic)?;
    if messages.is_empty() {
        return topics_screen(board, area);
    }
    let items =
        messages.iter().map(|m| format!("#{}  {}  {}: {}", m.id, m.ts, m.sender, first_line(&m.body))).collect();
    let picker = Picker::new(
        format!("board-admin -- {area}/{topic} (q back)"),
        items,
        "enter: view/delete one message    q: back",
    );
    Ok(Screen::Messages { area: area.to_owned(), topic: topic.to_owned(), picker, messages })
}

fn claims_screen(board: &Board) -> Result<Screen, Failure> {
    let claims = board.list_identities()?;
    let items = claims
        .iter()
        .map(|c| format!("{}  [{}]  {}  (since {})", c.name, c.source, c.fingerprint, c.registered_ts))
        .collect();
    let picker =
        Picker::new("board-admin -- name claims (q back)".to_owned(), items, "enter: release a claim    q: back");
    Ok(Screen::Claims { picker, claims })
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    use message_board::store::NewPost;

    use super::*;
    use crate::ui;

    fn press(app: &mut App, code: KeyCode) -> Result<Flow, Failure> {
        app.update(KeyEvent::new(code, KeyModifiers::NONE))
    }

    /// Draw the current screen into the test backend's buffer.
    fn render(terminal: &mut Terminal<TestBackend>, app: &mut App) -> Result<(), Box<dyn Error>> {
        let mut drawn = Ok(());
        terminal.draw(|frame| drawn = ui::draw(frame, &mut app.screen))?;
        Ok(drawn?)
    }

    /// The text of one row of a rendered screen.
    fn row(terminal: &Terminal<TestBackend>, y: u16) -> String {
        let buffer = terminal.backend().buffer();
        (0..buffer.area.width).filter_map(|x| buffer.cell((x, y)).map(|cell| cell.symbol().to_owned())).collect()
    }

    /// The state changes without a terminal, and drawing goes into ratatui's
    /// in-memory test backend: the Elm-style split is what makes both
    /// testable on their own.
    #[test]
    fn keys_move_through_the_screens_and_the_screens_draw() -> Result<(), Box<dyn Error>> {
        let dir = tempfile::tempdir()?;
        let board = Board::open(&dir.path().join("board.db"))?.board;
        board.post(&NewPost { area: "proj", topic: "design", sender: "alice", body: "one", key: None }, None)?;
        let mut app = App::new(board, Audit { path: dir.path().join("audit.log") })?;
        let mut terminal = Terminal::new(TestBackend::new(60, 8))?;

        render(&mut terminal, &mut app)?;
        assert!(row(&terminal, 0).starts_with("board-admin -- select an area"));
        let areas: Vec<String> = (1..4).map(|y| row(&terminal, y)).collect();
        let proj = areas.iter().position(|line| line.starts_with("proj  [live]  (1 messages)")).ok_or("no proj")?;
        assert!(row(&terminal, 7).starts_with("up/down j/k move"));

        for _ in 0..proj {
            assert_eq!(press(&mut app, KeyCode::Char('j'))?, Flow::Continue);
        }
        press(&mut app, KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        assert!(row(&terminal, 0).starts_with("board-admin -- proj -- select a topic"));
        assert!(row(&terminal, 1).starts_with("design  (1 messages)"));

        assert_eq!(press(&mut app, KeyCode::Char('q'))?, Flow::Continue); // back to the areas
        assert_eq!(press(&mut app, KeyCode::Char('q'))?, Flow::Quit);
        let ctrl_c = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        assert_eq!(app.update(ctrl_c)?, Flow::Interrupted);
        Ok(())
    }
}
