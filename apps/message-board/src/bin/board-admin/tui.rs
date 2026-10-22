//! The terminal session: setting the terminal up, the event loop, and
//! giving the terminal back.
//!
//! The terminal is given back on every way out of the loop, an error
//! included: `session` never returns before it has called `leave`, and
//! nothing in this program panics. (ratatui's `try_init` also installs a panic hook that
//! restores the terminal, for a panic inside a dependency.)

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use ratatui::DefaultTerminal;
use ratatui::crossterm::event::{self, Event, KeyEventKind};
use signal_hook::consts::{SIGHUP, SIGINT, SIGTERM};

use crate::Failure;
use crate::app::{App, Flow};
use crate::ui;

/// How often the loop looks for a signal while no key comes.
const TICK: Duration = Duration::from_millis(250);

/// The signals that end a session the way a quit does: the service must
/// come back and the terminal be restored whatever stops this program, a
/// plain kill or the terminal closing (SIGHUP) included.
pub struct Signals(Arc<AtomicUsize>);

impl Signals {
    pub fn install() -> Result<Self, Failure> {
        let received = Arc::new(AtomicUsize::new(0));
        for signal in [SIGTERM, SIGHUP, SIGINT] {
            let cannot = |cause| Failure::Io { what: format!("cannot handle signal {signal}"), cause };
            let number = usize::try_from(signal).map_err(|err| cannot(std::io::Error::other(err)))?;
            signal_hook::flag::register_usize(signal, Arc::clone(&received), number).map_err(cannot)?;
        }
        Ok(Self(received))
    }

    /// The signal that has arrived, if one has.
    pub fn received(&self) -> Option<i32> {
        match self.0.load(Ordering::SeqCst) {
            0 => None,
            n => i32::try_from(n).ok(),
        }
    }
}

/// How a session ended.
#[derive(Debug, PartialEq, Eq)]
pub enum Ending {
    Quit,
    /// Ctrl-C, which raw mode delivers as a key.
    Interrupted,
    Signalled(i32),
}

/// Raw mode (keys arrive one at a time, unechoed) and the alternate
/// screen (the shell's screen comes back untouched afterwards).
fn enter() -> Result<DefaultTerminal, Failure> {
    let entered = ratatui::try_init();
    entered.map_err(|cause| {
        // try_init may have switched raw mode on before failing
        let undone = ratatui::try_restore();
        Failure::Terminal { cause, restoring: undone.err() }
    })
}

/// Give the terminal back: cursor shown, raw mode off, the alternate
/// screen left. Both steps are tried even if the first fails.
fn leave(terminal: &mut DefaultTerminal) -> Result<(), Failure> {
    let shown = terminal.show_cursor();
    let restored = ratatui::try_restore();
    match (shown, restored) {
        (Ok(()), Ok(())) => Ok(()),
        (Err(cause), restored) => Err(Failure::Terminal { cause, restoring: restored.err() }),
        (Ok(()), Err(cause)) => Err(Failure::Terminal { cause, restoring: None }),
    }
}

/// Run the session to its end, and give the terminal back however it ends.
pub fn session(app: &mut App, signals: &Signals) -> Result<Ending, Failure> {
    let mut terminal = enter()?;
    let ended = event_loop(&mut terminal, app, signals);
    let left = leave(&mut terminal);
    match (ended, left) {
        (Ok(ending), Ok(())) => Ok(ending),
        (Err(err), Ok(())) | (Ok(_), Err(err)) => Err(err),
        (Err(err), Err(restoring)) => Err(Failure::Both(Box::new(err), Box::new(restoring))),
    }
}

fn event_loop(terminal: &mut DefaultTerminal, app: &mut App, signals: &Signals) -> Result<Ending, Failure> {
    let mut dirty = true;
    loop {
        if dirty {
            terminal
                .try_draw(|frame| ui::draw(frame, &mut app.screen).map_err(std::io::Error::other))
                .map_err(|cause| Failure::Io { what: "cannot draw".to_owned(), cause })?;
            dirty = false;
        }
        if let Some(signal) = signals.received() {
            return Ok(Ending::Signalled(signal));
        }
        // Wait for input a tick at a time rather than block in `read`, so a
        // signal is noticed even while no key comes.
        let ready =
            event::poll(TICK).map_err(|cause| Failure::Io { what: "cannot read the keyboard".to_owned(), cause })?;
        if !ready {
            continue;
        }
        match event::read().map_err(|cause| Failure::Io { what: "cannot read the keyboard".to_owned(), cause })? {
            // Only presses: terminals that report key releases would act twice.
            Event::Key(key) if key.kind == KeyEventKind::Press => {
                match app.update(key)? {
                    Flow::Continue => {}
                    Flow::Quit => return Ok(Ending::Quit),
                    Flow::Interrupted => return Ok(Ending::Interrupted),
                }
                dirty = true;
            }
            // The next draw lays the screen out for the new size.
            Event::Resize(..) => dirty = true,
            _ => {}
        }
    }
}
