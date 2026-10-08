"""Unit tests for apps/crosslink/gate.py's pure decision logic."""

import importlib.util
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "crosslink"
_spec = importlib.util.spec_from_file_location("gate", _APP / "gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["gate"] = gate
_spec.loader.exec_module(gate)


KEEP = frozenset({"issue", "config", "help", "--help", "-h", "--version", "-V"})
DENY = {"kickoff": "launches autonomous agents in worktrees"}
# A superset of KEEP|DENY, standing in for the real binary's full command
# surface -- includes "milestone", a real crosslink command neither kept
# nor denied, to exercise the "new since review" branch.
KNOWN = KEEP | frozenset(DENY) | frozenset({"milestone"})


def test_bare_invocation_shows_curated_help():
    assert gate.decide([], KEEP, DENY, KNOWN) == ("forward_help", None)


@pytest.mark.parametrize("flag", ["-h", "--help", "help"])
def test_help_flags_show_curated_help_regardless_of_position(flag):
    assert gate.decide([flag], KEEP, DENY, KNOWN) == ("forward_help", None)


def test_kept_command_forwards_unchanged():
    assert gate.decide(["issue", "list"], KEEP, DENY, KNOWN) == ("forward", None)


def test_denied_command_is_blocked_with_its_reason():
    action, message = gate.decide(["kickoff", "run", "x"], KEEP, DENY, KNOWN)
    assert action == "block"
    assert "kickoff" in message
    assert "launches autonomous agents" in message


def test_denied_issue_subcommand_is_blocked_but_issue_itself_is_kept():
    action, message = gate.decide(["issue", "intervene", "42"], KEEP, DENY, KNOWN)
    assert action == "block"
    assert "intervene" in message
    # the rest of `issue` is unaffected
    assert gate.decide(["issue", "show", "42"], KEEP, DENY, KNOWN) == ("forward", None)


def test_real_but_unreviewed_command_is_blocked_and_says_so():
    action, message = gate.decide(["milestone", "list"], KEEP, DENY, KNOWN)
    assert action == "block"
    assert "new since the last review" in message
    assert "gate.py" in message


def test_hallucinated_command_is_forwarded_not_blocked():
    # Not in KEEP, not in DENY, and -- critically -- not in KNOWN either:
    # the real binary has never heard of it, so inventing a "needs review"
    # message would claim a nonexistent command is real. Forward it and let
    # the real binary's own error say so.
    action, message = gate.decide(["autopilot", "engage"], KEEP, DENY, KNOWN)
    assert action == "forward"
    assert message is None


def test_empty_known_commands_fails_toward_forwarding_not_blocking():
    # If known-commands.txt is missing/unreadable, we can't distinguish
    # "new" from "fake" -- gate.load_known_commands already returns empty
    # in that case, so decide() should forward (let the real binary judge)
    # rather than block everything unreviewed as if it were all genuine.
    action, _ = gate.decide(["milestone"], KEEP, DENY, frozenset())
    assert action == "forward"


def test_load_known_commands_missing_file_is_empty(tmp_path):
    assert gate.load_known_commands(str(tmp_path / "nope.txt")) == frozenset()


def test_load_known_commands_reads_and_strips_lines(tmp_path):
    f = tmp_path / "known.txt"
    f.write_text("issue\nkickoff\n\nmilestone\n")
    assert gate.load_known_commands(str(f)) == {"issue", "kickoff", "milestone"}


def test_filter_help_strips_only_denied_commands_from_the_listing():
    raw = (
        "crosslink -- issue tracker\n\n"
        "Commands:\n"
        "  issue      Issue tracking\n"
        "  kickoff    Launch autonomous agents\n"
        "  config     Settings\n"
        "  help       Print this message\n\n"
        "Options:\n"
        "  -h, --help  Print help\n"
    )
    out = gate.filter_help(raw, DENY)
    assert "issue" in out
    assert "config" in out
    assert "help" in out
    assert "kickoff" not in out
    # untouched outside the Commands: block
    assert "crosslink -- issue tracker" in out
    assert "-h, --help  Print help" in out


def test_filter_help_is_a_pure_visibility_change_never_rewrites_kept_text():
    raw = "Commands:\n  issue      Issue tracking, verbatim\nOptions:\n"
    out = gate.filter_help(raw, DENY)
    assert "Issue tracking, verbatim" in out
