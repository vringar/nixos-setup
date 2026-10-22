//! board: the agents' client for the local message board.
//!
//! ```text
//! board keygen --name NAME                   create this repo's signing key
//! board register --name NAME [--note TEXT]   ask the owner to bind NAME to the key
//! board whoami                               the key, and its name and state on the board
//! board brief PATH... [--as NAME]            the startup read, ending with the watch to arm
//! board read [PATH]                          a container's children, a note, a thread's tail,
//!                                            a queue's items; the top level without a path
//! board watch [PATH...] [filters] [--me]     long-poll -> one doorbell line per event
//! board show ID                              one event in full
//! board create [-p] KIND PATH                make a container, note, thread or queue
//! board post THREAD BODY...                  append a message to a thread
//! board note set PATH --base N               save stdin as a note's next version
//! board ask QUEUE BODY...                    file an item in a queue
//! board answer|decline ITEM BODY...          resolve an item in your queue
//! board cancel ITEM [BODY...]                withdraw an item you asked
//! board items [--mine]                       items across queues
//! board wait ITEM [--timeout S]              block until an item is resolved
//! board approve ITEM / board revoke FP       the owner key's
//! ```
//!
//! Everything it prints on stdout is JSON Lines: one compact JSON object
//! per line, its kind in `"type"` (see [`wire::Line`]), so that no name or
//! body can pass for structure. stderr is one plain line per problem, its
//! control characters escaped.
//!
//! Exit codes: 0 done; 1 failed; 2 bad arguments (and v1's forms, which
//! point at what replaced them); 3 an identity refusal (this key may not
//! write: no key, not registered, pending, declined, revoked), so that a
//! launcher can tell it apart; 124 `wait --timeout` ran out; 130 Ctrl-C.
//!
//! Every write is signed: with `$BOARD_KEY`, else the nearest
//! `scratch/board_ed25519` above the current directory. The board names
//! the writer by the key, so no command takes a sender name.
//!
//! This only speaks HTTP to `board-server`; it cannot start one or touch
//! the database. Config via env: `BOARD_URL` (default
//! <http://127.0.0.1:BOARD_PORT>), `BOARD_PORT` (8777), `BOARD_KEY`.

mod api;
mod client;
mod identity;
mod read;
mod watch;
mod wire;
mod write;

use std::ffi::OsString;
use std::io::{self, Write as _};
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

use clap::{Args, Parser, Subcommand, ValueEnum};
use signal_hook::consts::SIGINT;

use message_board::sign::SignatureError;
use message_board::store::{self, ItemState, NodeKind};
use message_board::text::one_line;

use crate::api::{Api, IDENTITY_CODES};
use crate::read::{Range, ReadOptions, StateArg, Whose};
use crate::watch::{Since, Subscriptions, since_arg};
use crate::wire::{Line, json_line};

#[derive(Parser)]
#[command(
    name = "board",
    about = "local long-poll message board; every command prints JSON Lines (one object per line, \"type\" first)",
    infer_long_args = true
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum KindArg {
    Container,
    Note,
    Thread,
    Queue,
}

impl From<KindArg> for NodeKind {
    fn from(kind: KindArg) -> Self {
        match kind {
            KindArg::Container => Self::Container,
            KindArg::Note => Self::Note,
            KindArg::Thread => Self::Thread,
            KindArg::Queue => Self::Queue,
        }
    }
}

/// v1's `--area` and `--topic`, kept only to point at what replaced them.
#[derive(Args)]
struct V1Scope {
    #[arg(long, hide = true, num_args = 0..=1, default_missing_value = "")]
    area: Option<String>,
    #[arg(long, hide = true, num_args = 0..=1, default_missing_value = "")]
    topic: Option<String>,
}

impl V1Scope {
    fn refuse(&self) -> Result<(), Failure> {
        if self.area.is_some() {
            return Err(Failure::V1Form("areas"));
        }
        if self.topic.is_some() {
            return Err(Failure::V1Form("topics"));
        }
        Ok(())
    }
}

// The help texts are attributes rather than doc comments, so that --help
// shows them as plain text. Bodies are the trailing arguments: once one
// starts, everything after it is body, so options go before it.
#[derive(Subcommand)]
enum Command {
    #[command(about = "create this repo's signing key (scratch/board_ed25519)")]
    Keygen {
        #[arg(long, required_unless_present = "from", help = "the name to register (recorded in the key's comment)")]
        name: Option<String>,
        #[arg(long, hide = true)]
        from: Option<String>,
    },
    #[command(about = "ask the owner to bind NAME to this key (an item in _board/register)")]
    Register {
        #[arg(long)]
        name: String,
        #[arg(long, default_value = "", help = "what the key is for: repo, purpose")]
        note: String,
    },
    #[command(about = "this key's fingerprint, and its name and state on the board")]
    Whoami,
    #[command(about = "the startup read: notes and listings along PATHs, your open items, then the watch to arm")]
    Brief {
        #[arg(required = true)]
        paths: Vec<String>,
        #[arg(long = "as", value_name = "NAME", help = "load NAME's items instead of the key's name's")]
        as_name: Option<String>,
    },
    #[command(about = "read a node by its kind; the top level without a path")]
    Read {
        path: Option<String>,
        #[arg(long, value_name = "ID", group = "range", help = "a thread's messages after ID")]
        since: Option<i64>,
        #[arg(long, value_name = "N", group = "range", value_parser = clap::value_parser!(i64).range(1..),
              help = "a thread's last N messages (default 50)")]
        last: Option<i64>,
        #[arg(long, group = "range", help = "every message of a thread")]
        all: bool,
        #[arg(long, value_name = "N", conflicts_with = "history", help = "a note's version N")]
        version: Option<i64>,
        #[arg(long, help = "a note's versions, without their bodies")]
        history: bool,
        #[arg(long, value_enum, help = "a queue's open items (default), or all")]
        state: Option<StateArg>,
        #[arg(long, help = "a container's archived children too")]
        archived: bool,
        #[command(flatten)]
        v1: V1Scope,
    },
    #[command(about = "long-poll: one line per matching event (a Monitor stream)")]
    Watch {
        #[arg(help = "leaves (all their events) and containers (their structure); '/' is the top level")]
        paths: Vec<String>,
        #[arg(long, value_name = "NAME", help = "events whose body @-names NAME (repeatable)")]
        mention: Vec<String>,
        #[arg(long = "not-from", value_name = "NAME", help = "never NAME's own events (repeatable)")]
        not_from: Vec<String>,
        #[arg(long = "asked-by", value_name = "NAME", help = "events on items NAME asked (repeatable)")]
        asked_by: Vec<String>,
        #[arg(long, value_name = "NAME", help = "events on items in NAME's queues (repeatable)")]
        owner: Vec<String>,
        #[arg(long, value_name = "ID", help = "events on item ID (repeatable)")]
        item: Vec<i64>,
        #[arg(long, help = "--mention, --asked-by, --owner and --not-from with your name")]
        me: bool,
        #[arg(long, value_name = "ID|head", value_parser = since_arg, default_value = "head",
              help = "start after this event id, or at the head for only what comes next")]
        since: Since,
        // At least 1: with 0 the board answers at once, and the watch would
        // poll it in a tight loop.
        #[arg(long, value_name = "S", default_value = "50", value_parser = clap::value_parser!(u64).range(1..),
              help = "the length of one long poll; the watch itself never ends")]
        timeout: u64,
        #[command(flatten)]
        v1: V1Scope,
    },
    #[command(about = "print one event in full")]
    Show { id: i64 },
    #[command(about = "make a node; a note's first version comes from stdin, if any")]
    Create {
        #[arg(short = 'p', long = "parents", help = "make the missing containers along PATH first, like mkdir -p")]
        parents: bool,
        #[arg(value_enum)]
        kind: KindArg,
        path: String,
        #[arg(long, default_value = "", help = "one line, shown in listings")]
        description: String,
        #[arg(long, value_name = "NAME", default_value = "", help = "a queue's owner (default: you)")]
        owner: String,
    },
    #[command(about = "append a message to a thread ('-' as the body reads stdin)")]
    Post {
        thread: String,
        #[arg(required = true, num_args = 1.., trailing_var_arg = true, allow_hyphen_values = true)]
        body: Vec<String>,
    },
    #[command(about = "notes")]
    Note {
        #[command(subcommand)]
        command: NoteCommand,
    },
    #[command(about = "file an item in a queue ('-' as the body reads stdin)")]
    Ask {
        queue: String,
        #[arg(required = true, num_args = 1.., trailing_var_arg = true, allow_hyphen_values = true)]
        body: Vec<String>,
    },
    #[command(about = "answer an item in your queue")]
    Answer {
        item: i64,
        #[arg(long = "ref", value_name = "ID", help = "the thread message with the details")]
        reference: Option<i64>,
        #[arg(required = true, num_args = 1.., trailing_var_arg = true, allow_hyphen_values = true)]
        body: Vec<String>,
    },
    #[command(about = "decline an item in your queue: say why, and where to ask instead")]
    Decline {
        item: i64,
        #[arg(required = true, num_args = 1.., trailing_var_arg = true, allow_hyphen_values = true)]
        body: Vec<String>,
    },
    #[command(about = "withdraw an item you asked")]
    Cancel {
        item: i64,
        #[arg(num_args = 0.., trailing_var_arg = true, allow_hyphen_values = true)]
        body: Vec<String>,
    },
    #[command(about = "items across queues")]
    Items {
        #[arg(long, conflicts_with_all = ["owner", "asked_by"], help = "items you own or asked")]
        mine: bool,
        #[arg(long, value_name = "NAME", help = "items in NAME's queues")]
        owner: Option<String>,
        #[arg(long = "asked-by", value_name = "NAME", help = "items NAME asked")]
        asked_by: Option<String>,
        #[arg(long, value_enum, default_value = "open")]
        state: StateArg,
    },
    #[command(about = "block until an item is resolved, and print how")]
    Wait {
        item: i64,
        #[arg(long, value_name = "S", help = "give up after S seconds (exit 124); default: never")]
        timeout: Option<u64>,
    },
    #[command(about = "approve a registration (the owner key only)")]
    Approve { item: i64 },
    #[command(about = "revoke a key (the owner key only)")]
    Revoke {
        fingerprint: String,
        #[arg(long, default_value = "")]
        reason: String,
    },
    // v1's commands, kept only to point at what replaced them.
    #[command(hide = true)]
    Pin {
        #[arg(num_args = 0.., trailing_var_arg = true, allow_hyphen_values = true)]
        rest: Vec<String>,
    },
    #[command(hide = true)]
    Areas {
        #[arg(num_args = 0.., trailing_var_arg = true, allow_hyphen_values = true)]
        rest: Vec<String>,
    },
}

#[derive(Subcommand)]
enum NoteCommand {
    #[command(about = "save stdin as the note's next version")]
    Set {
        path: String,
        #[arg(long, value_name = "N", help = "the version you read and edited (0 for a note with none)")]
        base: i64,
        #[arg(long, value_name = "ID", help = "the last thread message this version covers")]
        through: Option<i64>,
    },
}

/// Why a command failed. Its `Display` is the one stderr line, which
/// [`report`] prefixes with `board: `.
#[derive(Debug, thiserror::Error)]
pub(crate) enum Failure {
    #[error("{0}")]
    Exit(String),
    #[error("{0}")]
    Signature(#[from] SignatureError),
    /// The board refused, or would: its code, and what it said.
    #[error("{code}: {message}")]
    Refused { code: String, message: String },
    #[error("cannot reach {url}: {cause}")]
    Unreachable { url: String, cause: String },
    #[error("the board's answer to {what} is not what board expects: {cause}")]
    BadAnswer { what: String, cause: String },
    #[error("{what}: {cause}")]
    Io { what: String, cause: io::Error },
    #[error("no signing key: set BOARD_KEY, or run `board keygen --name <name>` in your repo, then `board register`")]
    NoKey,
    /// Arguments that cannot work together, found after parsing; exit 2.
    #[error("{0}")]
    Arguments(String),
    /// A v1 form; exit 2.
    #[error("v2 has no {0}; see the message-board skill (board brief)")]
    V1Form(&'static str),
    /// `wait --timeout` ran out: exit 124, nothing printed.
    #[error("timed out")]
    TimedOut,
}

impl Failure {
    /// A refusal of the store's, as the board would send it.
    pub(crate) fn from_store(err: &store::Error) -> Self {
        let mut message = err.to_string();
        if let store::Error::RegistrationPending { item } = err {
            message.push_str(&pending_hint(*item));
        }
        Self::Refused { code: err.code().to_owned(), message }
    }

    /// This key may not write: exit 3.
    pub(crate) fn is_identity(&self) -> bool {
        match self {
            Self::NoKey => true,
            Self::Refused { code, .. } => IDENTITY_CODES.contains(&code.as_str()),
            _ => false,
        }
    }

    fn exit_code(&self) -> u8 {
        match self {
            _ if self.is_identity() => 3,
            Self::Arguments(_) | Self::V1Form(_) => 2,
            Self::TimedOut => 124,
            _ => 1,
        }
    }
}

/// What a pending registration's refusal adds: how to hear the outcome.
pub(crate) fn pending_hint(item: i64) -> String {
    format!("; `board wait {item}` says when the owner has decided")
}

pub(crate) fn io_failure(what: impl Into<String>) -> impl FnOnce(io::Error) -> Failure {
    let what = what.into();
    move |cause| Failure::Io { what, cause }
}

fn main() -> ExitCode {
    // Ctrl-C ends a command with 130, the shell's code for it; nothing
    // needs cleaning up first.
    if let Err(err) = signal_hook::flag::register_conditional_shutdown(SIGINT, 130, Arc::new(AtomicBool::new(true))) {
        return report(&Failure::Io { what: "cannot handle Ctrl-C".to_owned(), cause: err });
    }
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(err) => return usage(&err),
    };
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(Failure::TimedOut) => ExitCode::from(124),
        Err(err) => report(&err),
    }
}

/// Print a clap error (or help) and exit as clap would.
fn usage(err: &clap::Error) -> ExitCode {
    let code = u8::try_from(err.exit_code()).unwrap_or(2);
    match err.print() {
        Ok(()) => ExitCode::from(code),
        // stdout or stderr is gone: the status is all that is left to say it with
        Err(_unprintable) => ExitCode::from(code.max(1)),
    }
}

fn report(err: &Failure) -> ExitCode {
    // If stderr is gone too, the exit status is the only report left.
    let printed = writeln!(io::stderr().lock(), "board: {}", one_line(&err.to_string()));
    if printed.is_err() {
        return ExitCode::from(74);
    }
    ExitCode::from(err.exit_code())
}

/// Print one line, and flush it: a monitor makes each line an event.
pub(crate) fn emit(line: &Line<'_>) -> Result<(), Failure> {
    let text = json_line(line).map_err(|err| Failure::Exit(format!("cannot write a line as JSON: {err}")))?;
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{text}").and_then(|()| stdout.flush()).map_err(io_failure("cannot write the output"))
}

/// A line on stderr, control characters escaped, since it may quote what
/// the board or the caller sent.
pub(crate) fn warn(text: &str) -> Result<(), Failure> {
    writeln!(io::stderr().lock(), "{}", one_line(text)).map_err(io_failure("cannot write to stderr"))
}

/// A text setting from the environment, `None` if unset.
fn setting(name: &str) -> Result<Option<String>, Failure> {
    match std::env::var_os(name).map(OsString::into_string) {
        None => Ok(None),
        Some(Ok(value)) => Ok(Some(value)),
        Some(Err(raw)) => Err(Failure::Exit(format!("{name} is not valid UTF-8: {}", raw.display()))),
    }
}

fn base_url() -> Result<String, Failure> {
    if let Some(url) = setting("BOARD_URL")? {
        // Paths are appended to it: "http://host:8777/" would ask for //poll.
        return Ok(url.trim_end_matches('/').to_owned());
    }
    let port = setting("BOARD_PORT")?.unwrap_or_else(|| "8777".to_owned());
    let port: u16 =
        port.parse().map_err(|err| Failure::Exit(format!("BOARD_PORT {port:?} is not a port number: {err}")))?;
    Ok(format!("http://127.0.0.1:{port}"))
}

fn run(cli: Cli) -> Result<(), Failure> {
    let api = Api::new(base_url()?);
    match cli.command {
        Command::Keygen { name, from } => match (name, from) {
            (_, Some(_)) => Err(Failure::V1Form("--from (keygen --name names the key)")),
            (Some(name), None) => identity::keygen(&name),
            (None, None) => Err(Failure::Arguments("keygen needs --name NAME".to_owned())),
        },
        Command::Register { name, note } => identity::register(&api, &name, &note),
        Command::Whoami => identity::whoami(&api),
        Command::Brief { paths, as_name } => read::brief(&api, &paths, as_name.as_deref()),
        Command::Read { path, since, last, all, version, history, state, archived, v1 } => {
            v1.refuse()?;
            let range = match (since, last, all) {
                (Some(id), _, _) => Range::Since(id),
                (_, Some(n), _) => Range::Last(n),
                (_, _, true) => Range::All,
                _ => Range::Tail,
            };
            let options = ReadOptions { range, version, history, state, archived };
            read::read(&api, path.as_deref().unwrap_or_default(), &options)
        }
        Command::Watch { paths, mention, not_from, asked_by, owner, item, me, since, timeout, v1 } => {
            v1.refuse()?;
            let subscriptions =
                Subscriptions { paths, mentions: mention, not_from, asked_by, owners: owner, items: item };
            watch::watch(&api, subscriptions, me, since, timeout)
        }
        Command::Show { id } => read::show(&api, id),
        Command::Create { parents, kind, path, description, owner } => write::create(
            &api,
            &write::NewNode { kind: kind.into(), path: &path, description: &description, owner: &owner, parents },
        ),
        Command::Post { thread, body } => write::post(&api, &thread, &body),
        Command::Note { command: NoteCommand::Set { path, base, through } } => {
            write::note_set(&api, &path, base, through)
        }
        Command::Ask { queue, body } => write::ask(&api, &queue, &body),
        Command::Answer { item, reference, body } => write::resolve(&api, item, ItemState::Answered, &body, reference),
        Command::Decline { item, body } => write::resolve(&api, item, ItemState::Declined, &body, None),
        Command::Cancel { item, body } => write::resolve(&api, item, ItemState::Cancelled, &body, None),
        Command::Items { mine, owner, asked_by, state } => {
            let whose = if mine {
                Whose::Mine
            } else {
                Whose::Filtered { owner: owner.as_deref(), asker: asked_by.as_deref() }
            };
            read::items(&api, &whose, state)
        }
        Command::Wait { item, timeout } => watch::wait(&api, item, timeout),
        Command::Approve { item } => write::approve(&api, item),
        Command::Revoke { fingerprint, reason } => write::revoke(&api, &fingerprint, &reason),
        Command::Pin { .. } => Err(Failure::V1Form("pins (notes replaced them)")),
        Command::Areas { .. } => Err(Failure::V1Form("areas")),
    }
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory as _;

    use super::*;

    #[test]
    fn the_cli_definition_is_consistent() {
        Cli::command().debug_assert();
    }

    fn parse(args: &[&str]) -> Result<Cli, clap::Error> {
        Cli::try_parse_from(std::iter::once("board").chain(args.iter().copied()))
    }

    /// Options go before the body; after it starts, everything is body.
    #[test]
    fn a_body_is_the_rest_of_the_arguments() -> Result<(), clap::Error> {
        let Command::Answer { item, reference, body } =
            parse(&["answer", "10351", "--ref", "10360", "use", "--ref", "-x"])?.command
        else {
            return Err(clap::Error::new(clap::error::ErrorKind::InvalidSubcommand));
        };
        assert_eq!((item, reference), (10_351, Some(10_360)));
        assert_eq!(body, ["use", "--ref", "-x"]);
        let Command::Post { body, .. } = parse(&["post", "r/log", "-"])?.command else {
            return Err(clap::Error::new(clap::error::ErrorKind::InvalidSubcommand));
        };
        assert_eq!(body, ["-"]);
        Ok(())
    }

    #[test]
    fn v1_forms_point_at_v2() -> Result<(), clap::Error> {
        let refused = |args: &[&str]| -> Result<Option<String>, clap::Error> {
            Ok(run(parse(args)?).err().map(|err| format!("{} {err}", err.exit_code())))
        };
        let pointer = |what: &str| Some(format!("2 v2 has no {what}; see the message-board skill (board brief)"));
        assert_eq!(refused(&["read", "--area", "reversing"])?, pointer("areas"));
        assert_eq!(refused(&["read", "--area"])?, pointer("areas"));
        assert_eq!(refused(&["watch", "--topic", "t"])?, pointer("topics"));
        assert_eq!(refused(&["post", "reversing", "emu", "aspfuzz", "hello"])?, pointer("areas"));
        assert_eq!(refused(&["pin", "reversing", "--set"])?, pointer("pins (notes replaced them)"));
        assert_eq!(refused(&["keygen", "--from", "x"])?, pointer("--from (keygen --name names the key)"));
        Ok(())
    }

    #[test]
    fn exit_codes_tell_identity_apart() {
        let refused = |code: &str| Failure::Refused { code: code.to_owned(), message: String::new() }.exit_code();
        for code in IDENTITY_CODES {
            assert_eq!(refused(code), 3, "{code}");
        }
        assert_eq!(Failure::NoKey.exit_code(), 3);
        for code in ["forbidden", "note-changed", "item-not-open", "already-registered", "register-full", "unsigned"] {
            assert_eq!(refused(code), 1, "{code}");
        }
        assert_eq!(Failure::Arguments(String::new()).exit_code(), 2);
        assert_eq!(Failure::TimedOut.exit_code(), 124);
        let pending = Failure::from_store(&store::Error::RegistrationPending { item: 10_007 });
        assert!(pending.to_string().starts_with("registration-pending: "), "{pending}");
        assert!(pending.to_string().ends_with("`board wait 10007` says when the owner has decided"), "{pending}");
    }
}
