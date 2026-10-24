//! The TUI's state and what keys do to it.
//!
//! The program follows the Elm-style structure the ratatui documentation
//! recommends, in three parts:
//!
//! - the *model*: [`App`] holds everything the screen shows, and the
//!   screens themselves are in `screens.rs`;
//! - the *update*: [`App::update`] turns one key press into the next
//!   state;
//! - the *view*: drawing (in `ui.rs`) only reads the state.
//!
//! Nothing here touches the terminal, so the whole flow is plain data plus
//! the database, and the tests below drive it without one.
//!
//! Deletions happen here, each written to the audit log as it happens. The
//! store refuses some (the board's own nodes; a message a note summarizes
//! up to, or an answer points to; a branch something outside it points
//! into): a refusal is shown and the session goes on, while a fault in the
//! database ends it.

use std::fs;
use std::io;
use std::path::PathBuf;

use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

use message_board::fsck;
use message_board::store::{self, BlobInfo, Board, Deleted, Message};

use crate::Failure;
use crate::audit::Audit;
use crate::pager::Read;
use crate::picker::Pick;
use crate::screens::{self, Screen, Sort, parent};

/// What the event loop does after a key.
#[derive(Debug, PartialEq, Eq)]
pub enum Flow {
    Continue,
    Quit,
    /// Ctrl-C: in raw mode it is a key, not a signal.
    Interrupted,
}

/// Where the blob files are and how long an unreferenced blob is safe from
/// eviction: what the blob screens need beside the database.
pub struct BlobSettings {
    pub root: PathBuf,
    pub grace: u64,
}

pub struct App<'a> {
    board: &'a Board,
    audit: Audit,
    blobs: BlobSettings,
    pub screen: Screen,
    /// What the last action did, or why it did not: shown under the screen
    /// until the next key.
    pub notice: Option<String>,
}

impl<'a> App<'a> {
    /// Start on the top level of the board.
    ///
    /// # Errors
    /// If the board cannot be read.
    pub fn new(board: &'a Board, audit: Audit, blobs: BlobSettings) -> Result<Self, Failure> {
        let screen = screens::tree(board, "", None)?;
        Ok(Self { board, audit, blobs, screen, notice: None })
    }

    /// Turn one key press into the next state.
    ///
    /// # Errors
    /// On a fault in the database or the audit log.
    pub fn update(&mut self, key: KeyEvent) -> Result<Flow, Failure> {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            return Ok(Flow::Interrupted);
        }
        self.notice = None;
        let board = self.board;
        let grace = self.blobs.grace;
        let next = match &mut self.screen {
            Screen::Tree { path, picker } => match picker.key(key.code) {
                Pick::Open(child) => screens::open(board, child)?,
                Pick::Back if path.is_empty() => return Ok(Flow::Quit),
                Pick::Back => screens::tree(board, parent(path), Some(path))?,
                Pick::Other(KeyCode::Char('d')) => match picker.selected() {
                    Some(child) => screens::confirm_branch(board, child)?,
                    None => return Ok(Flow::Continue),
                },
                Pick::Other(KeyCode::Char('b')) => screens::blobs(board, grace, Sort::Age, None)?,
                Pick::Stay | Pick::Other(_) => return Ok(Flow::Continue),
            },
            Screen::Note { path, pager } => match pager.key(key.code) {
                Read::Back => screens::tree(board, parent(path), Some(path))?,
                Read::Stay | Read::Other(_) => return Ok(Flow::Continue),
            },
            Screen::Thread { path, picker } => match picker.key(key.code) {
                Pick::Open(message) => screens::message(message.clone()),
                Pick::Back => screens::tree(board, parent(path), Some(path))?,
                Pick::Stay | Pick::Other(_) => return Ok(Flow::Continue),
            },
            Screen::Message { message, pager } => match pager.key(key.code) {
                Read::Other(KeyCode::Char('d')) => Screen::ConfirmMessage { message: message.clone() },
                Read::Back => screens::thread(board, &message.path, Some(message.id))?,
                Read::Stay | Read::Other(_) => return Ok(Flow::Continue),
            },
            Screen::Queue { path, picker } => match picker.key(key.code) {
                Pick::Open(item) => screens::item(board, path, item.id)?,
                Pick::Back => screens::tree(board, parent(path), Some(path))?,
                Pick::Stay | Pick::Other(_) => return Ok(Flow::Continue),
            },
            Screen::Item { path, id, pager } => match pager.key(key.code) {
                Read::Back => screens::queue(board, path, Some(*id))?,
                Read::Stay | Read::Other(_) => return Ok(Flow::Continue),
            },
            // Deleting a whole branch needs its path typed out, not just a y.
            Screen::ConfirmBranch { path, typed, .. } => match key.code {
                KeyCode::Enter if typed == path => {
                    let (screen, notice) = delete_branch(board, &self.audit, path)?;
                    self.notice = Some(notice);
                    screen
                }
                KeyCode::Enter | KeyCode::Esc => {
                    self.notice = Some("cancelled: nothing deleted".to_owned());
                    screens::tree(board, parent(path), Some(path))?
                }
                KeyCode::Backspace => {
                    typed.pop();
                    return Ok(Flow::Continue);
                }
                KeyCode::Char(c) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                    typed.push(c);
                    return Ok(Flow::Continue);
                }
                _ => return Ok(Flow::Continue),
            },
            Screen::Blobs { sort, picker } => match picker.key(key.code) {
                Pick::Open(info) => screens::blob(board, grace, *sort, &info.hash)?,
                Pick::Back => screens::tree(board, "", None)?,
                Pick::Other(KeyCode::Char('s')) => {
                    let came_from = picker.selected().map(|info| info.hash.clone());
                    screens::blobs(board, grace, sort.other(), came_from.as_deref().map(|hash| (hash, 0)))?
                }
                Pick::Stay | Pick::Other(_) => return Ok(Flow::Continue),
            },
            Screen::Blob { sort, info, pager } => match pager.key(key.code) {
                Read::Other(KeyCode::Char('d')) => Screen::ConfirmBlob { sort: *sort, info: info.clone() },
                Read::Back => screens::blobs(board, grace, *sort, Some((&info.hash, 0)))?,
                Read::Stay | Read::Other(_) => return Ok(Flow::Continue),
            },
            Screen::ConfirmBlob { sort, info } => {
                if matches!(key.code, KeyCode::Char('y' | 'Y')) {
                    let (screen, notice) = delete_blob(board, &self.audit, &self.blobs, *sort, info)?;
                    self.notice = Some(notice);
                    screen
                } else {
                    self.notice = Some("cancelled: nothing deleted".to_owned());
                    screens::blob(board, grace, *sort, &info.hash)?
                }
            }
            Screen::ConfirmMessage { message } => {
                if matches!(key.code, KeyCode::Char('y' | 'Y')) {
                    let (screen, notice) = delete_message(board, &self.audit, message)?;
                    self.notice = Some(notice);
                    screen
                } else {
                    self.notice = Some("cancelled: nothing deleted".to_owned());
                    screens::message(message.clone())
                }
            }
        };
        self.screen = next;
        Ok(Flow::Continue)
    }
}

/// Why the store refused, to show; or, for a fault in the database rather
/// than a refusal, the error that ends the session.
fn refusal(err: store::Error) -> Result<String, Failure> {
    match err {
        store::Error::Db(fault) => Err(fault.into()),
        refused => Ok(refused.to_string()),
    }
}

/// Delete the node at `path` with everything in it, record that, and go
/// back to the listing it was in, saying what happened.
fn delete_branch(board: &Board, audit: &Audit, path: &str) -> Result<(Screen, String), Failure> {
    let notice = match board.delete_branch(path) {
        Ok(Deleted { nodes, events }) => {
            audit.record("delete_branch", &format!("{path} ({nodes} nodes, {events} events)"))?;
            format!("deleted {path}: {nodes} nodes, {events} events")
        }
        Err(err) => format!("not deleted: {}", refusal(err)?),
    };
    Ok((screens::tree(board, parent(path), Some(path))?, notice))
}

/// Delete `message`, record that, and go back to its thread, saying what
/// happened.
fn delete_message(board: &Board, audit: &Audit, message: &Message) -> Result<(Screen, String), Failure> {
    let notice = match board.delete_message(message.id) {
        Ok(()) => {
            audit.record("delete_message", &format!("#{} {} from={}", message.id, message.path, message.from))?;
            format!("deleted #{} from {}", message.id, message.path)
        }
        Err(err) => format!("not deleted: {}", refusal(err)?),
    };
    Ok((screens::thread(board, &message.path, Some(message.id))?, notice))
}

/// Delete blob `info` (its row, unless something live pins it, and then its
/// file), record that, and go back to the blob list, saying what happened.
fn delete_blob(
    board: &Board,
    audit: &Audit,
    blobs: &BlobSettings,
    sort: Sort,
    info: &BlobInfo,
) -> Result<(Screen, String), Failure> {
    let notice = match board.blob_delete_unsigned(&info.hash) {
        Ok(()) => {
            audit.record("delete_blob", &format!("{} {} bytes {:?}", info.hash, info.size, info.filename))?;
            if let Some(path) = fsck::file_of(&blobs.root, &info.hash) {
                match fs::remove_file(&path) {
                    Err(err) if err.kind() != io::ErrorKind::NotFound => {
                        return Err(Failure::Io {
                            what: format!(
                                "deleted blob {} from the index but not its file {}",
                                info.hash,
                                path.display()
                            ),
                            cause: err,
                        });
                    }
                    _ => {}
                }
            }
            format!("deleted blob {}", info.hash)
        }
        Err(err) => format!("not deleted: {}", refusal(err)?),
    };
    // the row it was on, if it is the last one there (the list shows what is left)
    Ok((screens::blobs(board, blobs.grace, sort, Some((&info.hash, 0)))?, notice))
}

#[cfg(test)]
mod tests {
    use std::error::Error;

    use ratatui::Terminal;
    use ratatui::backend::TestBackend;
    use ratatui::crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    use message_board::store::Range;

    use super::*;
    use crate::fixture::Fixture;
    use crate::ui;

    type TestResult = Result<(), Box<dyn Error>>;

    fn press(app: &mut App<'_>, code: KeyCode) -> Result<Flow, Failure> {
        app.update(KeyEvent::new(code, KeyModifiers::NONE))
    }

    fn typing(app: &mut App<'_>, text: &str) -> Result<(), Failure> {
        text.chars().try_for_each(|c| press(app, KeyCode::Char(c)).map(drop))
    }

    /// Move down to the row whose text starts with `label`, from the first
    /// row, and press Enter on it (or `then`, if given).
    fn pick(app: &mut App<'_>, label: &str, then: KeyCode) -> TestResult {
        press(app, KeyCode::Home)?;
        let labels = match &app.screen {
            Screen::Tree { picker, .. } => picker.labels.clone(),
            Screen::Thread { picker, .. } => picker.labels.clone(),
            Screen::Queue { picker, .. } => picker.labels.clone(),
            Screen::Blobs { picker, .. } => picker.labels.clone(),
            _ => return Err("not a list".into()),
        };
        let row =
            labels.iter().position(|text| text.starts_with(label)).ok_or(format!("no {label:?} in {labels:?}"))?;
        for _ in 0..row {
            press(app, KeyCode::Down)?;
        }
        press(app, then)?;
        Ok(())
    }

    /// The selected row's text, on a list screen.
    fn selected(app: &App<'_>) -> Option<String> {
        let (labels, state) = match &app.screen {
            Screen::Tree { picker, .. } => (&picker.labels, &picker.state),
            Screen::Thread { picker, .. } => (&picker.labels, &picker.state),
            Screen::Queue { picker, .. } => (&picker.labels, &picker.state),
            _ => return None,
        };
        state.selected().and_then(|i| labels.get(i)).cloned()
    }

    /// Draw the current screen into the test backend's buffer.
    fn render(terminal: &mut Terminal<TestBackend>, app: &mut App<'_>) -> Result<(), Box<dyn Error>> {
        let mut drawn = Ok(());
        terminal.draw(|frame| drawn = ui::draw(frame, &mut app.screen, app.notice.as_deref()))?;
        Ok(drawn?)
    }

    /// The text of one row of a rendered screen.
    fn row(terminal: &Terminal<TestBackend>, y: u16) -> String {
        let buffer = terminal.backend().buffer();
        (0..buffer.area.width).filter_map(|x| buffer.cell((x, y)).map(|cell| cell.symbol().to_owned())).collect()
    }

    fn screen_text(terminal: &Terminal<TestBackend>) -> String {
        (0..terminal.backend().buffer().area.height).map(|y| row(terminal, y)).collect::<Vec<_>>().join("\n")
    }

    /// The state changes without a terminal, and drawing goes into ratatui's
    /// in-memory test backend: the Elm-style split is what makes both
    /// testable on their own.
    #[test]
    fn keys_move_through_the_tree_and_the_screens_draw() -> TestResult {
        let f = Fixture::populated()?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        let mut terminal = Terminal::new(TestBackend::new(100, 12))?;

        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 0).trim_end(), "board-admin -- the board");
        assert!(row(&terminal, 1).starts_with("_board  container  2 children  -- the board's own"));
        assert!(row(&terminal, 2).starts_with("rev     container  4 children"));
        assert!(row(&terminal, 11).starts_with("enter: open"));

        pick(&mut app, "rev ", KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 0).trim_end(), "board-admin -- rev");
        let listing = screen_text(&terminal);
        assert!(listing.contains("rev/asks   queue      stefan's, none open"), "{listing}");
        assert!(listing.contains("rev/empty  container  0 children"), "{listing}");
        assert!(listing.contains(&format!("rev/log    thread     3 messages, the last #{}", f.ids.third)), "{listing}");
        assert!(listing.contains("rev/state  note       v1 by stefan at"), "{listing}");
        assert!(listing.contains(&format!("summarizes rev/log through #{}", f.ids.second)), "{listing}");

        // a thread, a message in it, and back the way we came
        pick(&mut app, "rev/log ", KeyCode::Enter)?;
        pick(&mut app, &format!("#{}", f.ids.second), KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 0).trim_end(), format!("board-admin -- rev/log #{}", f.ids.second));
        assert_eq!(row(&terminal, 3).trim_end(), "second");
        press(&mut app, KeyCode::Char('q'))?;
        assert!(selected(&app).is_some_and(|text| text.starts_with(&format!("#{}", f.ids.second))));
        press(&mut app, KeyCode::Char('q'))?;
        assert!(selected(&app).is_some_and(|text| text.starts_with("rev/log ")));

        // a note
        pick(&mut app, "rev/state ", KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 0).trim_end(), "board-admin -- rev/state (note)");
        assert!(screen_text(&terminal).contains("the state of things"));
        press(&mut app, KeyCode::Esc)?;

        // a queue and its item
        pick(&mut app, "rev/asks ", KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        assert!(row(&terminal, 1).contains("answered    stefan: please look"), "{}", row(&terminal, 1));
        press(&mut app, KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        let item = screen_text(&terminal);
        assert!(item.contains(&format!("item_answered by stefan, ref #{}", f.ids.third)), "{item}");
        press(&mut app, KeyCode::Char('q'))?;
        assert!(matches!(app.screen, Screen::Queue { .. }));

        press(&mut app, KeyCode::Char('q'))?; // to rev
        assert_eq!(press(&mut app, KeyCode::Char('q'))?, Flow::Continue); // to the top
        assert!(selected(&app).is_some_and(|text| text.starts_with("rev ")));
        assert_eq!(press(&mut app, KeyCode::Char('q'))?, Flow::Quit);
        let ctrl_c = KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL);
        assert_eq!(app.update(ctrl_c)?, Flow::Interrupted);
        Ok(())
    }

    #[test]
    fn a_message_is_deleted_after_a_y_and_recorded() -> TestResult {
        let f = Fixture::populated()?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        pick(&mut app, "rev ", KeyCode::Enter)?;
        pick(&mut app, "rev/log ", KeyCode::Enter)?;

        // anything but y cancels
        pick(&mut app, &format!("#{}", f.ids.first), KeyCode::Enter)?;
        press(&mut app, KeyCode::Char('d'))?;
        assert!(matches!(app.screen, Screen::ConfirmMessage { .. }));
        press(&mut app, KeyCode::Char('n'))?;
        assert!(matches!(app.screen, Screen::Message { .. }));
        assert_eq!(app.notice.as_deref(), Some("cancelled: nothing deleted"));
        assert_eq!(f.board.thread("rev/log", Range::All)?.messages.len(), 3);

        press(&mut app, KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Char('y'))?;
        assert_eq!(app.notice, Some(format!("deleted #{} from rev/log", f.ids.first)));
        let left: Vec<i64> = f.board.thread("rev/log", Range::All)?.messages.iter().map(|m| m.id).collect();
        assert_eq!(left, [f.ids.second, f.ids.third]);
        // back on the thread, on the message after the one that went
        assert!(selected(&app).is_some_and(|text| text.starts_with(&format!("#{}", f.ids.second))));
        assert!(f.audit_log()?.ends_with(&format!(" delete_message #{} rev/log from=stefan\n", f.ids.first)));
        Ok(())
    }

    #[test]
    fn an_archived_message_is_listed_marked_and_can_be_deleted() -> TestResult {
        let f = Fixture::populated()?;
        f.archive(f.ids.first)?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        pick(&mut app, "rev ", KeyCode::Enter)?;
        pick(&mut app, "rev/log ", KeyCode::Enter)?;
        press(&mut app, KeyCode::Home)?;
        let Screen::Thread { picker, .. } = &app.screen else {
            return Err("not a thread".into());
        };
        assert_eq!(picker.labels.len(), 3, "the operator sees every row");
        assert!(picker.labels.first().is_some_and(|row| row.contains("[archived]")), "{:?}", picker.labels);
        assert!(picker.labels.iter().skip(1).all(|row| !row.contains("[archived]")));

        press(&mut app, KeyCode::Enter)?;
        press(&mut app, KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Char('y'))?;
        assert_eq!(app.notice, Some(format!("deleted #{} from rev/log", f.ids.first)));
        assert_eq!(f.board.thread_with("rev/log", Range::All, true)?.messages.len(), 2);
        Ok(())
    }

    #[test]
    fn a_message_something_points_to_stays() -> TestResult {
        let f = Fixture::populated()?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        pick(&mut app, "rev ", KeyCode::Enter)?;
        pick(&mut app, "rev/log ", KeyCode::Enter)?;
        // the note summarizes up to the second, the answer refers to the third
        for (id, by) in
            [(f.ids.second, "note_version on \"rev/state\""), (f.ids.third, "item_answered on \"rev/asks\"")]
        {
            pick(&mut app, &format!("#{id}"), KeyCode::Enter)?;
            press(&mut app, KeyCode::Char('d'))?;
            press(&mut app, KeyCode::Char('y'))?;
            let notice = app.notice.clone().unwrap_or_default();
            assert!(notice.starts_with("not deleted: #") && notice.contains(by), "{notice}");
        }
        assert_eq!(f.board.thread("rev/log", Range::All)?.messages.len(), 3);
        assert_eq!(f.audit_log()?, "");
        Ok(())
    }

    #[test]
    fn a_branch_goes_only_once_its_path_is_typed_out() -> TestResult {
        let f = Fixture::populated()?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        let mut terminal = Terminal::new(TestBackend::new(80, 14))?;

        // what will go is listed, every node and what it holds
        pick(&mut app, "rev ", KeyCode::Char('d'))?;
        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 0).trim_end(), "Delete rev and everything in it (5 nodes). This cannot be undone.");
        let question = screen_text(&terminal);
        for line in [
            "rev        container  4 children",
            "rev/asks   queue      1 item, 0 open",
            "rev/empty  container  0 children",
            "rev/log    thread     3 messages",
            "rev/state  note       1 version",
            "Type 'rev' to confirm, anything else cancels:",
            "> ",
        ] {
            assert!(question.contains(line), "no {line:?} in\n{question}");
        }

        // a typo cancels
        typing(&mut app, "re")?;
        press(&mut app, KeyCode::Enter)?;
        assert!(app.notice.as_deref().is_some_and(|notice| notice.starts_with("cancelled")));
        assert!(f.board.node("rev").is_ok());

        // the note in rev/state points into rev/log: deleting the thread alone is refused
        pick(&mut app, "rev ", KeyCode::Enter)?;
        pick(&mut app, "rev/log ", KeyCode::Char('d'))?;
        typing(&mut app, "rev/lgo")?;
        press(&mut app, KeyCode::Backspace)?;
        press(&mut app, KeyCode::Backspace)?;
        typing(&mut app, "og")?;
        press(&mut app, KeyCode::Enter)?;
        let notice = app.notice.clone().unwrap_or_default();
        assert!(notice.starts_with("not deleted: #") && notice.contains("points into \"rev/log\""), "{notice}");
        assert!(f.board.node("rev/log").is_ok());

        // the board's own nodes are refused too
        press(&mut app, KeyCode::Char('q'))?;
        pick(&mut app, "_board ", KeyCode::Char('d'))?;
        typing(&mut app, "_board")?;
        press(&mut app, KeyCode::Enter)?;
        assert!(app.notice.as_deref().is_some_and(|notice| notice.contains("the board's own")), "{:?}", app.notice);
        assert_eq!(f.audit_log()?, "");

        // the whole of rev, pointers and all, goes
        pick(&mut app, "rev ", KeyCode::Char('d'))?;
        typing(&mut app, "rev")?;
        press(&mut app, KeyCode::Enter)?;
        assert!(app.notice.as_deref().is_some_and(|notice| notice.starts_with("deleted rev: 5 nodes, ")));
        assert!(matches!(f.board.node("rev"), Err(store::Error::NotFound(_))));
        assert!(matches!(f.board.node("rev/log"), Err(store::Error::NotFound(_))));
        let log = f.audit_log()?;
        assert_eq!(log.lines().count(), 1);
        assert!(log.contains(" delete_branch rev (5 nodes, "), "{log}");
        // back on the top level, on the row after the one that went (none:
        // the last row)
        assert!(selected(&app).is_some_and(|text| text.starts_with("_board ")));
        Ok(())
    }

    #[test]
    fn the_notice_shows_until_the_next_key() -> TestResult {
        let f = Fixture::populated()?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        let mut terminal = Terminal::new(TestBackend::new(80, 8))?;
        pick(&mut app, "rev ", KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Esc)?;
        render(&mut terminal, &mut app)?;
        assert!(row(&terminal, 6).starts_with("cancelled: nothing deleted"));
        press(&mut app, KeyCode::Down)?;
        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 6).trim_end(), "");
        Ok(())
    }

    #[test]
    fn the_blobs_are_listed_opened_and_deleted_unless_something_pins_them() -> TestResult {
        let f = Fixture::populated()?;
        let (kept, loose) = (
            f.blob(b"pinned bytes", "trace.bin", "2026-01-02T00:00:00Z")?,
            f.blob(b"x".repeat(5000).as_slice(), "", "2026-01-01T00:00:00Z")?,
        );
        let message = f.post("rev/log", &format!("see board-blob://{kept}"))?;
        let mut app = App::new(&f.board, f.audit(), f.blobs())?;
        let mut terminal = Terminal::new(TestBackend::new(100, 14))?;

        // the top level's `b`: every blob, newest first, with what may become of it
        press(&mut app, KeyCode::Char('b'))?;
        render(&mut terminal, &mut app)?;
        assert_eq!(row(&terminal, 0).trim_end(), "board-admin -- blobs (2 blobs, 5 kB, newest first)");
        let list = screen_text(&terminal);
        assert!(row(&terminal, 1).starts_with(&kept[..12]), "newest first: {list}");
        assert!(list.contains("12 bytes  ") && list.contains("pinned      1 refs  trace.bin"), "{list}");
        assert!(list.contains("evictable   0 refs  (no name)"), "{list}");
        // biggest first
        press(&mut app, KeyCode::Char('s'))?;
        render(&mut terminal, &mut app)?;
        assert!(row(&terminal, 0).trim_end().ends_with("biggest first)"));
        assert!(row(&terminal, 1).starts_with(&loose[..12]), "{}", row(&terminal, 1));

        // a pinned blob names its holder and is refused
        pick(&mut app, &kept[..12], KeyCode::Enter)?;
        render(&mut terminal, &mut app)?;
        let detail = screen_text(&terminal);
        assert!(
            detail.contains("12 bytes (12 bytes), pinned") && detail.contains(&format!("#{message}  rev/log  pins it")),
            "{detail}"
        );
        press(&mut app, KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Char('y'))?;
        let notice = app.notice.clone().unwrap_or_default();
        assert!(
            notice.starts_with("not deleted: board-blob://") && notice.contains("referenced by 1 live holder"),
            "{notice}"
        );
        assert!(f.blobs().root.join(&kept[..2]).join(&kept).exists());
        assert_eq!(f.audit_log()?, "");

        // anything but y cancels; an unpinned one goes with its file, and it is recorded
        pick(&mut app, &loose[..12], KeyCode::Enter)?;
        press(&mut app, KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Char('n'))?;
        assert_eq!(app.notice.as_deref(), Some("cancelled: nothing deleted"));
        press(&mut app, KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Char('y'))?;
        assert_eq!(app.notice, Some(format!("deleted blob {loose}")));
        assert!(!f.blobs().root.join(&loose[..2]).join(&loose).exists());
        assert!(f.audit_log()?.ends_with(&format!(" delete_blob {loose} 5000 bytes \"\"\n")));

        // freeing the holder frees the blob
        f.archive(message)?;
        pick(&mut app, &kept[..12], KeyCode::Enter)?;
        press(&mut app, KeyCode::Char('d'))?;
        press(&mut app, KeyCode::Char('y'))?;
        assert_eq!(app.notice, Some(format!("deleted blob {kept}")));
        assert_eq!(f.board.blob_index()?.len(), 0);
        assert_eq!(press(&mut app, KeyCode::Char('q'))?, Flow::Continue);
        assert!(matches!(app.screen, Screen::Tree { .. }));
        Ok(())
    }
}
