"""Unit tests for the gh-issue-template-guard PreToolUse hook."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gh_issue_template_guard",
    Path(__file__).parent.parent
    / "home-manager"
    / "files"
    / "ai"
    / "hooks"
    / "gh-issue-template-guard.py",
)
guard = importlib.util.module_from_spec(_spec)
sys.modules["gh_issue_template_guard"] = guard
_spec.loader.exec_module(guard)


def never_called(repo):
    raise AssertionError(f"lookup should not have run for {repo}")


def templates(*names):
    return lambda repo: list(names)


# --- which commands the hook cares about ------------------------------------


def test_ignores_unrelated_commands():
    assert guard.decide("ls -la", never_called) is None
    assert guard.decide("git commit -m 'create an issue'", never_called) is None


def test_ignores_other_gh_subcommands():
    assert guard.decide("gh issue view 13844 -R owner/repo", never_called) is None
    assert guard.decide("gh pr create -R owner/repo", never_called) is None
    assert guard.decide("gh issue comment 1 -R owner/repo", never_called) is None


def test_matches_issue_create_with_flags_between():
    reason = guard.decide(
        "gh issue create -R owner/repo --title x --body-file b.md",
        templates("bug-report.yml"),
    )
    assert "bug-report.yml" in reason


def test_matches_across_line_continuations():
    """Real invocations are wrapped with backslashes; DOTALL must cover them."""
    command = "gh issue create \\\n  -R owner/repo \\\n  --title x"
    assert guard.decide(command, templates("bug-report.yml")) is not None


def test_does_not_match_a_word_ending_in_gh():
    assert guard.decide("highgh issue create -R o/r", never_called) is None


# --- the escape hatch --------------------------------------------------------


def test_marker_allows_the_command_through():
    command = "ISSUE_TEMPLATE_OK=1 gh issue create -R owner/repo --title x"
    assert guard.decide(command, never_called) is None


def test_marker_must_be_set_to_one():
    command = "ISSUE_TEMPLATE_OK=0 gh issue create -R owner/repo --title x"
    assert guard.decide(command, templates("bug-report.yml")) is not None


# --- resolving the target repo ----------------------------------------------


def test_missing_repo_flag_is_denied_without_a_lookup():
    reason = guard.decide("gh issue create --title x", never_called)
    assert "-R OWNER/REPO" in reason


def test_repo_parsed_from_long_and_quoted_forms():
    for flag in ("-R owner/repo", "--repo owner/repo", '--repo "owner/repo"'):
        command = f"gh issue create {flag} --title x"
        assert guard.decide(command, templates("bug-report.yml")) is not None
    assert guard.target_repo("gh issue create -R paperless-ngx/paperless-ngx") == (
        "paperless-ngx/paperless-ngx"
    )


# --- fail-open behaviour -----------------------------------------------------


def test_allows_when_the_repo_has_no_templates():
    assert guard.decide("gh issue create -R owner/repo", lambda repo: []) is None


def test_allows_when_the_lookup_fails():
    """A hook that blocks work because GitHub is unreachable is worse than the
    mistake it prevents."""
    assert guard.decide("gh issue create -R owner/repo", lambda repo: None) is None


# --- the denial message ------------------------------------------------------


def test_denial_names_every_template_and_how_to_proceed():
    reason = guard.decide(
        "gh issue create -R owner/repo --title x",
        templates("bug-report.yml", "feature-request.yml"),
    )
    assert "bug-report.yml" in reason
    assert "feature-request.yml" in reason
    assert "ISSUE_TEMPLATE_OK=1" in reason
    assert "invent" in reason
