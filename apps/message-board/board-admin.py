#!/usr/bin/env python3
"""Interactive TUI for permanently deleting board threads and releasing name claims.

This is deliberately NOT part of `board` (no shared subcommand surface, no
mention in its --help) and deliberately NOT an HTTP endpoint. It opens the
SQLite file directly and issues real DELETEs -- the one operation the board
itself refuses to offer (see the message-board skill: archive, not delete,
and the reasoning for why delete was never put on the server or the CLI).

The boundary this relies on is the interaction model, not secrecy or
permissions: this is a curses TUI, not a flagged command. A flagged command
(`board-admin delete --id N`) would be exactly as agent-invokable as an HTTP
DELETE route -- a shell-out is a shell-out. A real terminal UI that needs a
pty to navigate and an interactive confirmation to act is not something an
agent in this harness can drive. It is not a hard boundary either: anything
with Bash access could still open the DB file and write raw SQL. What this
buys is "not reachable through any normal path," not "impossible."

The same goes for releasing a name claim (the identity a sender name was
bound to by its first signed post): it is the one way to free a name whose
key was lost or that the wrong key claimed, and it is as agent-proof as
deletion. The next signed post as that name claims it again.

Every deletion and release is appended to a local audit log (not the board --
the whole point is this never touches the board's own API) so there's a
record of what was removed and when, since unlike archive this cannot be
undone.

Stops the message-board systemd user service for the duration of the
session (only if it was running, and only via systemctl -- never touches a
board-server it didn't start stopping), and restarts it on exit, crash or
Ctrl-C included.

Usage: board-admin
Config via env, same as board-server: BOARD_DB, BOARD_URL (checked, not written to).
"""
from __future__ import annotations

import curses
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# BOARD_ADMIN_SERVICE="" disables all systemctl handling (tests, and a board
# that isn't running under systemd). Never aimed at anything but the unit
# that owns this DB.
SERVICE = os.environ.get("BOARD_ADMIN_SERVICE", "message-board")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_core as core  # noqa: E402
from board_core import (  # noqa: E402
    area_counts, delete_identity, delete_message, delete_topic, list_identities,
    messages_in, topic_counts,
)

AUDIT_LOG = Path(
    os.environ.get("BOARD_ADMIN_LOG", str(Path.home() / ".local/state/message-board/admin-deletions.log"))
)


def resolve_db_path(environ=os.environ) -> Path:
    """The same file `board-server` opens under systemd (BOARD_DB in the
    unit is %S/message-board/board.db). board_core's own default is a bare
    ./board.db relative to the cwd, which is right for a dev run and wrong
    here: run from the wrong directory, this tool would open (and create)
    an empty database and show a board with nothing on it."""
    explicit = environ.get("BOARD_DB")
    if explicit:
        return Path(explicit)
    state = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "message-board" / "board.db"


def service_is_active() -> bool:
    """systemctl's own opinion of whether the managed service is running."""
    if not SERVICE:
        return False
    return (
        subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", SERVICE], check=False
        ).returncode
        == 0
    )


def service_stop() -> None:
    subprocess.run(["systemctl", "--user", "stop", SERVICE], check=True)


def service_start() -> None:
    subprocess.run(["systemctl", "--user", "start", SERVICE], check=True)


def service_is_live(url: str, timeout: float = 2.0) -> bool:
    """True if `board-server` answers on url. Mutating the DB file while the
    service holds it open risks corruption, not just a stale read -- refuse
    rather than race it."""
    try:
        urllib.request.urlopen(f"{url}/healthz", timeout=timeout)
        return True
    except (urllib.error.URLError, OSError):
        return False


def audit_line(action: str, detail: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{ts} {action} {detail}\n"


def log_deletion(action: str, detail: str) -> None:
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as f:
        f.write(audit_line(action, detail))


# ---------------------------------------------------------------------------
# curses UI -- thin, drives the functions above. Nothing here is unit-tested;
# everything it calls is.
# ---------------------------------------------------------------------------


def _pick(stdscr, title: str, items: list[str], footer: str = "") -> int | None:
    """A simple scrolling picker. Returns the chosen index, or None on
    cancel (q/Esc)."""
    idx = 0
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, title, w - 1, curses.A_BOLD)
        visible = h - 3
        if idx < top:
            top = idx
        if idx >= top + visible:
            top = idx - visible + 1
        for row, i in enumerate(range(top, min(len(items), top + visible))):
            attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
            stdscr.addnstr(row + 1, 0, items[i], w - 1, attr)
        stdscr.addnstr(h - 1, 0, footer, w - 1, curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = min(len(items) - 1, idx + 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            return idx if items else None
        elif key in (27, ord("q")):
            return None


def _confirm_typed(stdscr, prompt: str, required: str) -> bool:
    """High-friction confirmation for a wide-blast-radius delete: must type
    the exact required word, not just y/N."""
    curses.echo()
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    stdscr.addnstr(0, 0, prompt, w - 1, curses.A_BOLD)
    stdscr.addnstr(1, 0, f"Type '{required}' to confirm, anything else cancels:", w - 1)
    stdscr.addnstr(2, 0, "> ", 2)
    stdscr.refresh()
    try:
        typed = stdscr.getstr(2, 2, 40).decode("utf-8", "replace")
    finally:
        curses.noecho()
    return typed == required


def _confirm_yn(stdscr, prompt: str, question: str = "Delete permanently? [y/N] ") -> bool:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    for i, line in enumerate(prompt.splitlines()):
        stdscr.addnstr(i, 0, line, w - 1)
    stdscr.addnstr(len(prompt.splitlines()) + 1, 0, question, w - 1, curses.A_BOLD)
    stdscr.refresh()
    return stdscr.getch() in (ord("y"), ord("Y"))


def _claims(stdscr):
    """Pick a claimed name and release it."""
    while True:
        claims = list_identities()
        lines = [f"{c['name']}  [{c['source']}]  {c['fingerprint']}  (since {c['registered_ts']})"
                 for c in claims]
        k = _pick(stdscr, "board-admin -- name claims (q back)", lines,
                  "enter: release a claim    q: back")
        if k is None:
            return
        c = claims[k]
        again = ("board-server binds it to the owner key again at its next start"
                 if c["source"] == "owner" else "the next signed post as it claims it again")
        if _confirm_yn(stdscr, f"{c['name']}  [{c['source']}]  {c['fingerprint']}\n\n"
                               f"Releasing frees the name; {again}.\n",
                       "Release this claim? [y/N] "):
            if delete_identity(c["name"]):
                log_deletion("release_claim", f"{c['name']} {c['fingerprint']} ({c['source']})")


def _run(stdscr):
    curses.curs_set(0)
    while True:
        areas = area_counts()
        lines = [f"{a}  [{'archived' if arch else 'live'}]  ({n} messages)" for a, arch, n in areas]
        lines.append(f"-- name claims ({len(list_identities())})")
        i = _pick(stdscr, "board-admin -- select an area (q to quit)", lines,
                  "up/down j/k move, enter select, q quit")
        if i is None:
            return
        if i == len(areas):
            _claims(stdscr)
            continue
        area = areas[i][0]

        while True:
            topics = topic_counts(area)
            if not topics:
                stdscr.erase()
                stdscr.addnstr(0, 0, f"{area}: no messages.", 60)
                stdscr.addnstr(2, 0, "press any key", 60, curses.A_DIM)
                stdscr.refresh()
                stdscr.getch()
                break
            lines = [f"{t}  ({n} messages)" for t, n in topics]
            j = _pick(stdscr, f"board-admin -- {area} -- select a topic (q back)", lines,
                      "enter: select topic    q: back")
            if j is None:
                break
            topic, count = topics[j]

            # peek at the last keypress isn't exposed by _pick, so re-ask here
            stdscr.erase()
            stdscr.addnstr(0, 0, f"{area}/{topic}: (b)rowse messages or (d)elete all {count}, (q) back", 70)
            stdscr.refresh()
            action = stdscr.getch()
            if action in (27, ord("q")):
                continue
            if action == ord("d"):
                if _confirm_typed(
                    stdscr,
                    f"Delete ALL {count} messages in {area}/{topic}. This cannot be undone.",
                    f"{area}/{topic}",
                ):
                    n = delete_topic(area, topic)
                    log_deletion("delete_topic", f"{area}/{topic} ({n} messages)")
                continue

            # browse -> per-message delete
            while True:
                msgs = messages_in(area, topic)
                if not msgs:
                    break
                lines = [
                    f"#{m['id']}  {m['ts']}  {m['sender']}: {m['body'].splitlines()[0][:80]}"
                    for m in msgs
                ]
                k = _pick(stdscr, f"board-admin -- {area}/{topic} (q back)", lines,
                          "enter: view/delete one message    q: back")
                if k is None:
                    break
                m = msgs[k]
                if _confirm_yn(
                    stdscr,
                    f"#{m['id']}  {m['ts']}  {m['sender']}\n\n{m['body']}\n",
                ):
                    if delete_message(m["id"]):
                        log_deletion("delete_message", f"#{m['id']} {area}/{topic} from={m['sender']}")


def main() -> int:
    # A closed terminal (SIGHUP) or a plain kill (SIGTERM) ends the session
    # too: make each an ordinary exit, so the finally below restarts the board.
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda num, _frame: sys.exit(128 + num))
    url = os.environ.get("BOARD_URL", f"http://127.0.0.1:{os.environ.get('BOARD_PORT', '8777')}")

    db_path = resolve_db_path()
    if not db_path.exists():
        # init_db would happily create it; an admin tool must not.
        sys.exit(f"board-admin: no database at {db_path} (set BOARD_DB to point elsewhere).")

    stopped_by_us = False
    try:
        if service_is_active():
            print(f"board-admin: stopping {SERVICE}...", file=sys.stderr)
            service_stop()
            stopped_by_us = True
            # systemctl stop returns once the unit reports stopped, but give
            # the port a moment to actually free up before trusting it.
            for _ in range(20):
                if not service_is_live(url):
                    break
                time.sleep(0.25)
            else:
                sys.exit(
                    f"board-admin: stopped {SERVICE} but {url} is still answering -- "
                    "something else is serving this port too. Not touching the DB."
                )
        elif service_is_live(url):
            # systemctl thinks it's down but the port answers anyway: an
            # unmanaged `board-server` is running (someone's own shell, a stale
            # process). We only know how to stop the *managed* instance --
            # don't guess at killing an unknown process.
            sys.exit(
                f"board-admin: {url} is answering but systemctl says {SERVICE} is not "
                "active -- an unmanaged board-server is running somewhere. Stop it "
                "yourself, this tool only manages the systemd service."
            )

        core.init_db(str(db_path))
        curses.wrapper(_run)
    finally:
        # Everything after the stop is inside this try, so *any* way out --
        # refusal, crash, Ctrl-C, normal quit -- brings the board back.
        if stopped_by_us:
            print(f"board-admin: restarting {SERVICE}...", file=sys.stderr)
            try:
                service_start()
            except subprocess.CalledProcessError as exc:
                print(
                    f"board-admin: WARNING -- failed to restart {SERVICE} ({exc}). "
                    f"Run `systemctl --user start {SERVICE}` yourself.",
                    file=sys.stderr,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
