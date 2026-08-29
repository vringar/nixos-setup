"""Unit tests for claude-sandbox's SSH agent scoping."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "claude_sandbox",
    Path(__file__).parent.parent / "apps" / "claude-sandbox" / "claude-sandbox.py",
)
sandbox = importlib.util.module_from_spec(_spec)
sys.modules["claude_sandbox"] = sandbox
_spec.loader.exec_module(sandbox)

SOCK = "/run/user/1000/ssh-agent"
DEDICATED = "/run/user/1000/ssh-agent-sandbox"


def everything_exists(_path):
    return True


def nothing_exists(_path):
    return False


# --- the default: no agent at all -------------------------------------------


def test_login_agent_is_not_passed_through_by_default():
    """The whole point: a login agent in the environment must not leak in."""
    args, warning = sandbox.ssh_agent_bind_args(
        {"SSH_AUTH_SOCK": SOCK}, exists=everything_exists
    )
    assert SOCK not in args
    assert "--ro-bind" not in args
    assert warning is None


def test_stale_ssh_auth_sock_is_unset_not_merely_unbound():
    """bwrap keeps the environment, so an inherited path would fail obscurely."""
    args, _ = sandbox.ssh_agent_bind_args(
        {"SSH_AUTH_SOCK": SOCK}, exists=everything_exists
    )
    assert args == ["--unsetenv", "SSH_AUTH_SOCK"]


def test_empty_environment_is_handled():
    args, warning = sandbox.ssh_agent_bind_args({}, exists=nothing_exists)
    assert args == ["--unsetenv", "SSH_AUTH_SOCK"]
    assert warning is None


# --- the dedicated agent ----------------------------------------------------


def test_dedicated_socket_is_bound_and_exported():
    args, warning = sandbox.ssh_agent_bind_args(
        {"SSH_AUTH_SOCK": SOCK, sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED},
        exists=everything_exists,
    )
    assert args == [
        "--ro-bind",
        DEDICATED,
        DEDICATED,
        "--setenv",
        "SSH_AUTH_SOCK",
        DEDICATED,
    ]
    assert warning is None


def test_dedicated_socket_wins_over_the_login_agent():
    """Both present: the login agent must still never be the one bound."""
    args, _ = sandbox.ssh_agent_bind_args(
        {"SSH_AUTH_SOCK": SOCK, sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED},
        exists=everything_exists,
    )
    assert SOCK not in args


def test_missing_dedicated_socket_warns_and_falls_back_to_nothing():
    """A typo in the path must not silently fall back to the login agent."""
    args, warning = sandbox.ssh_agent_bind_args(
        {"SSH_AUTH_SOCK": SOCK, sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED},
        exists=nothing_exists,
    )
    assert args == ["--unsetenv", "SSH_AUTH_SOCK"]
    assert SOCK not in args
    assert DEDICATED in warning


# --- the escape hatch -------------------------------------------------------


def test_full_agent_escape_hatch_binds_the_login_agent_and_warns():
    args, warning = sandbox.ssh_agent_bind_args(
        {"SSH_AUTH_SOCK": SOCK, sandbox.FULL_AGENT_ENV: "1"},
        exists=everything_exists,
    )
    assert args == ["--ro-bind", SOCK, SOCK, "--setenv", "SSH_AUTH_SOCK", SOCK]
    assert warning is not None
    assert "every key" in warning


def test_full_agent_hatch_does_nothing_without_an_agent():
    args, warning = sandbox.ssh_agent_bind_args(
        {sandbox.FULL_AGENT_ENV: "1"}, exists=everything_exists
    )
    assert args == ["--unsetenv", "SSH_AUTH_SOCK"]
    assert warning is None


def test_dedicated_socket_takes_precedence_over_the_hatch():
    args, _ = sandbox.ssh_agent_bind_args(
        {
            "SSH_AUTH_SOCK": SOCK,
            sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED,
            sandbox.FULL_AGENT_ENV: "1",
        },
        exists=everything_exists,
    )
    assert DEDICATED in args
    assert SOCK not in args
