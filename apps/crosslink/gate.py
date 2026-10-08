#!/usr/bin/env python3
"""Gate crosslink's CLI down to its issue-tracking surface.

crosslink grew its own autonomous-agent framework alongside its issue
tracker (kickoff, swarm, sentinel, agent, daemon, container, ...). Nothing
here stops a deliberate `crosslink-real <command>` -- this is not a
privilege boundary, it's a visibility one: an agent that runs `crosslink
--help` (or hallucinates a subcommand and gets curious) should not be
handed a menu of crosslink's own orchestration machinery when all this
deployment wants from crosslink is its issue tracker.

Three buckets, checked in order:
  - KEEP:  forwarded to the real binary.
  - DENY:  blocked, with a reason (also hidden from this gate's --help).
  - anything else: if it's a genuine top-level command of the pinned
    crosslink binary that isn't in either list, that means crosslink
    added something new since this file was last reviewed -- block it
    and say so explicitly, rather than silently allow or silently vanish
    it. If it isn't a real command at all (a typo, a hallucinated
    subcommand), forward it anyway and let the real binary's own clap
    error explain that -- inventing a "needs review" message for a
    command that was never real would be actively misleading.

Config via env: CROSSLINK_REAL_BIN (path to the unwrapped binary),
CROSSLINK_KNOWN_COMMANDS (path to a newline-separated list of every
top-level command name the pinned binary actually has, generated at
build time by extract-known-commands.sh against that same binary).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# name -> why it's excluded. Keep the reasons: they're both the error text
# and the only record of *why*, for whoever reconsiders this list later.
DENY: dict[str, str] = {
    "daemon": "standing background agent daemon",
    "agent": "crosslink's own agent registry/management",
    "kickoff": "launches autonomous agents in worktrees",
    "swarm": "AI-decomposes a design doc into multi-agent phased builds",
    "sentinel": "crosslink's own autonomous dispatch engine (notify/webhook/etc.)",
    "design": "AI design-doc + decompose pipeline, overlaps with the-architect",
    "container": "sandboxed agent execution environment",
    "tui": "interactive terminal UI, not for agent use",
    "mc": "mission-control monitoring dashboard",
    "dashboard": "the web dashboard and its local service",
    "serve": "backs the dashboard's API/websocket",
    "workflow": "workflow automation, part of the agent framework",
    "trust": "trust/permission model for crosslink's own agents",
    "heartbeat": "agent liveness signalling for sentinel/dashboard",
    "style": "pulls a remote 'house style' bundle of hooks/commands into this repo",
    "context": "measures an agent session's own context usage, not project context",
    "timer": "per-issue work-timer stopwatch, not issue tracking",
}

# Subcommands denied one level down, under an otherwise-kept top-level command.
DENY_SUBCOMMANDS: dict[str, dict[str, str]] = {
    "issue": {
        "intervene": "pauses/redirects a running agent, part of the agent framework",
    },
}

KEEP: frozenset[str] = frozenset(
    {
        "issue",
        # hidden top-level aliases for common `issue` subcommands
        "create",
        "list",
        "show",
        "close",
        "new",
        "quick",
        "issues",
        "subissue",
        "init",
        "doctor",
        "config",
        "knowledge",
        "cpitd",
        "sync",
        "compact",
        "prune",
        "locks",
        "migrate",
        "export",
        "import",
        "integrity",
        "archive",
        "session",
        # not real subcommands, but always safe to forward
        "help",
        "--help",
        "-h",
        "--version",
        "-V",
    }
)


def load_known_commands(path: str | None) -> frozenset[str]:
    """Every top-level command the pinned crosslink binary actually has,
    as of the last build (see extract-known-commands.sh). Missing/unreadable
    is treated as empty -- a missing file means we can't tell "new" from
    "fake", so we fail toward "fake" (forward to the real binary) rather
    than blocking commands we have no evidence are even real.
    """
    if not path:
        return frozenset()
    try:
        return frozenset(
            line.strip() for line in Path(path).read_text().splitlines() if line.strip()
        )
    except OSError:
        return frozenset()


def decide(
    argv: list[str], keep: frozenset[str], deny: dict[str, str], known: frozenset[str]
) -> tuple[str, str | None]:
    """Pure decision for one invocation. Returns (action, message):

    - ("forward", None): exec the real binary with argv unchanged.
    - ("forward_help", None): print this gate's curated help instead.
    - ("block", message): refuse, printing message.
    """
    if not argv or argv[0] in ("-h", "--help", "help"):
        return ("forward_help", None)

    word = argv[0]

    sub_deny = DENY_SUBCOMMANDS.get(word)
    if sub_deny and len(argv) > 1 and argv[1] in sub_deny:
        return (
            "block",
            f"crosslink {word} {argv[1]} is excluded in this deployment: "
            f"{sub_deny[argv[1]]}",
        )

    if word in deny:
        return ("block", f"crosslink {word} is excluded in this deployment: {deny[word]}")

    if word in keep:
        return ("forward", None)

    if word in known:
        return (
            "block",
            f"crosslink {word} is new since the last review of this gate "
            f"(apps/crosslink/gate.py) -- decide allow or deny there before use.",
        )

    # Not in our lists and not a real command the pinned binary has either:
    # most likely a typo or a hallucinated subcommand. Forward it and let
    # the real binary's own error say so -- a "needs review" message here
    # would wrongly imply the command exists.
    return ("forward", None)


def filter_help(raw_help: str, deny: dict[str, str]) -> str:
    """Strip denied commands out of the real binary's own `help` output.

    Deliberately reuses the real binary's descriptions instead of
    hand-maintaining our own -- they can't drift out of sync with the
    actual build, and the filter only changes *visibility*, never the text
    of anything it keeps.
    """
    out: list[str] = []
    in_commands = False
    for line in raw_help.splitlines():
        if line.startswith("Commands:"):
            in_commands = True
            out.append(line)
            continue
        if in_commands and line.startswith("Options:"):
            in_commands = False
            out.append(line)
            continue
        if in_commands and line.strip():
            name = line.split()[0]
            if name in deny:
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    real_bin = os.environ.get("CROSSLINK_REAL_BIN")
    if not real_bin:
        sys.exit("crosslink: CROSSLINK_REAL_BIN is not set (broken install)")

    known = load_known_commands(os.environ.get("CROSSLINK_KNOWN_COMMANDS"))
    action, message = decide(argv, KEEP, DENY, known)

    if action == "block":
        print(f"error: {message}", file=sys.stderr)
        return 1

    if action == "forward_help":
        try:
            # argv[0] = "crosslink", not the real_bin path: clap echoes argv[0]
            # into its own usage/error text, and the rename to crosslink-real
            # is an install detail no caller should see.
            raw = subprocess.run(
                ["crosslink", "help"],
                executable=real_bin,
                capture_output=True,
                text=True,
                check=False,
            ).stdout
        except OSError as exc:
            sys.exit(f"crosslink: cannot reach {real_bin}: {exc}")
        print(filter_help(raw, DENY), end="")
        return 0

    # Same argv[0] override here, for the same reason: a forwarded command
    # that errors (e.g. missing arguments) should say "crosslink ...", not
    # "crosslink-real ...".
    os.execv(real_bin, ["crosslink", *argv])  # noqa: S606 -- exact passthrough by design


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
