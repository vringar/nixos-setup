"""Tests for claude-sandbox's SSH agent scoping and its per-sandbox agent."""

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "apps" / "claude-sandbox" / "claude-sandbox.py"
_spec = importlib.util.spec_from_file_location("claude_sandbox", SCRIPT)
sandbox = importlib.util.module_from_spec(_spec)
sys.modules["claude_sandbox"] = sandbox
_spec.loader.exec_module(sandbox)

HOME = "/home/user"
SOCK = "/run/user/1000/ssh-agent"
DEDICATED = "/run/user/1000/ssh-agent-sandbox"
FORGE_KEY = HOME + "/.ssh/forge_ed25519"
SANDBOX_TMP = "/tmp/claude-sandbox-project-abcdefgh"
INNER_SOCK = "/tmp/" + sandbox.FORGE_AGENT_SOCK
NO_AGENT = ["--unsetenv", "SSH_AUTH_SOCK"]


def only(*paths):
    present = set(paths)
    return lambda path: path in present


def everything_exists(_path):
    return True


class FakeAgent:
    """Stands in for start_forge_agent and records what it was asked for."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, key_path, sandbox_tmp):
        self.calls.append((key_path, sandbox_tmp))
        if self.fail:
            return None, self.fail
        return "agent-proc", None


def setup(env, exists, agent=None):
    return sandbox.ssh_agent_setup(
        HOME, SANDBOX_TMP, env=env, exists=exists, start_agent=agent or FakeAgent()
    )


def setenvs(args):
    """The --setenv pairs in bwrap args, as a dict."""
    return {
        args[i + 1]: args[i + 2]
        for i in range(len(args))
        if args[i] == "--setenv"
    }


# --- the default: never the login agent ------------------------------------


def test_login_agent_is_not_passed_through_by_default():
    """The whole point: a login agent in the environment must not leak in."""
    args, proc, _ = setup({"SSH_AUTH_SOCK": SOCK}, only(SOCK))
    assert SOCK not in args
    assert args == NO_AGENT
    assert proc is None


def test_login_agent_is_not_passed_through_alongside_the_forge_agent():
    args, proc, warning = setup({"SSH_AUTH_SOCK": SOCK}, only(SOCK, FORGE_KEY))
    assert SOCK not in args
    assert setenvs(args)["SSH_AUTH_SOCK"] == INNER_SOCK
    assert proc == "agent-proc"
    assert warning is None


def test_empty_environment_is_handled():
    args, proc, _ = setup({}, only())
    assert args == NO_AGENT
    assert proc is None


# --- precedence: explicit socket > escape hatch > forge agent > nothing -----


def test_dedicated_socket_is_bound_and_exported():
    agent = FakeAgent()
    args, proc, warning = setup(
        {"SSH_AUTH_SOCK": SOCK, sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED},
        everything_exists,
        agent,
    )
    assert args == ["--ro-bind", DEDICATED, DEDICATED, "--setenv", "SSH_AUTH_SOCK", DEDICATED]
    assert proc is None
    assert warning is None
    assert agent.calls == []


def test_dedicated_socket_wins_over_the_forge_agent_and_the_login_agent():
    agent = FakeAgent()
    args, _, _ = setup(
        {"SSH_AUTH_SOCK": SOCK, sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED},
        everything_exists,
        agent,
    )
    assert SOCK not in args
    assert agent.calls == []


def test_missing_dedicated_socket_gets_no_agent_at_all():
    """A typo in the path must fall back to neither the login nor the forge agent."""
    agent = FakeAgent()
    args, proc, warning = setup(
        {"SSH_AUTH_SOCK": SOCK, sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED},
        only(SOCK, FORGE_KEY),
        agent,
    )
    assert args == NO_AGENT
    assert proc is None
    assert DEDICATED in warning
    assert agent.calls == []


def test_full_agent_escape_hatch_binds_the_login_agent_and_warns():
    agent = FakeAgent()
    args, proc, warning = setup(
        {"SSH_AUTH_SOCK": SOCK, sandbox.FULL_AGENT_ENV: "1"},
        only(SOCK, FORGE_KEY),
        agent,
    )
    assert args == ["--ro-bind", SOCK, SOCK, "--setenv", "SSH_AUTH_SOCK", SOCK]
    assert proc is None
    assert "every key" in warning
    assert agent.calls == []


def test_full_agent_hatch_only_opens_on_1():
    """A "0" written to mean "off" must not hand over every key."""
    for value in ("0", ""):
        args, _, _ = setup(
            {"SSH_AUTH_SOCK": SOCK, sandbox.FULL_AGENT_ENV: value},
            only(SOCK, FORGE_KEY),
        )
        assert SOCK not in args, value
        assert setenvs(args)["SSH_AUTH_SOCK"] == INNER_SOCK, value


def test_full_agent_hatch_without_a_login_agent_gets_no_agent_and_says_so():
    for env, exists in [
        ({sandbox.FULL_AGENT_ENV: "1"}, everything_exists),
        ({"SSH_AUTH_SOCK": SOCK, sandbox.FULL_AGENT_ENV: "1"}, only(FORGE_KEY)),
    ]:
        agent = FakeAgent()
        args, proc, warning = setup(env, exists, agent)
        assert args == NO_AGENT
        assert proc is None
        assert sandbox.FULL_AGENT_ENV in warning
        assert "no SSH agent" in warning
        assert agent.calls == []


def test_dedicated_socket_takes_precedence_over_the_hatch():
    args, _, _ = setup(
        {
            "SSH_AUTH_SOCK": SOCK,
            sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED,
            sandbox.FULL_AGENT_ENV: "1",
        },
        everything_exists,
    )
    assert DEDICATED in args
    assert SOCK not in args


# --- the forge agent ----------------------------------------------------------


def test_forge_agent_is_started_with_the_default_key():
    agent = FakeAgent()
    setup({}, only(FORGE_KEY), agent)
    assert agent.calls == [(FORGE_KEY, SANDBOX_TMP)]


def test_forge_key_path_can_be_overridden():
    agent = FakeAgent()
    setup({sandbox.FORGE_KEY_ENV: "/keys/forge"}, only("/keys/forge", FORGE_KEY), agent)
    assert agent.calls == [("/keys/forge", SANDBOX_TMP)]


def test_forge_agent_failure_falls_back_to_nothing_not_the_login_agent():
    args, proc, warning = setup(
        {"SSH_AUTH_SOCK": SOCK}, only(SOCK, FORGE_KEY), FakeAgent(fail="ssh-add failed")
    )
    assert args == NO_AGENT
    assert proc is None
    assert warning == "ssh-add failed"


def test_private_key_is_never_bound():
    args, _, _ = setup({}, everything_exists)
    assert FORGE_KEY not in args


def test_sandbox_sees_the_forge_pub_but_not_the_private_key(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "forge_ed25519"
    key.write_text("private\n")
    Path(f"{key}.pub").write_text("ssh-ed25519 AAAA forge:test\n")
    project = tmp_path / "project"
    project.mkdir()

    args = sandbox.build_bwrap_args(
        str(project), str(home), str(tmp_path / "tmp"), str(tmp_path / "hist"), "/bin/sh"
    )
    pub = f"{key}.pub"
    assert any(args[i:i + 3] == ["--ro-bind", pub, pub] for i in range(len(args)))
    assert str(key) not in args


# --- the missing-key message --------------------------------------------------


def test_missing_key_message_gives_the_exact_command():
    _, _, warning = setup({}, only())
    assert 'ssh-keygen -t ed25519 -N "" -C forge:$(hostname) -f ~/.ssh/forge_ed25519' in warning
    assert "authentication and a signing key" in warning
    assert "SSO" in warning
    assert "\n" not in warning


def test_missing_key_message_names_an_overridden_path():
    _, _, warning = setup({sandbox.FORGE_KEY_ENV: "/keys/my forge"}, only())
    assert "-f '/keys/my forge'" in warning


# --- git signing --------------------------------------------------------------


def test_forge_agent_points_git_signing_at_its_key():
    args, _, _ = setup({}, everything_exists)
    env = setenvs(args)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "user.signingKey"
    assert env["GIT_CONFIG_VALUE_0"] == FORGE_KEY + ".pub"
    # ~/.ssh/*.pub is bound already; no second bind for it.
    assert "--ro-bind" not in args


def test_git_config_env_is_appended_to_not_overwritten():
    args, _, _ = setup({"GIT_CONFIG_COUNT": "2"}, everything_exists)
    env = setenvs(args)
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_2"] == "user.signingKey"
    assert "GIT_CONFIG_KEY_0" not in env


def test_forge_pub_outside_dot_ssh_is_bound():
    args, _, _ = setup(
        {sandbox.FORGE_KEY_ENV: "/keys/forge"}, only("/keys/forge", "/keys/forge.pub")
    )
    assert args[-3:] == ["--ro-bind", "/keys/forge.pub", "/keys/forge.pub"]
    assert "/keys/forge" not in args


def test_git_signing_is_left_alone_without_the_forge_agent():
    """No agent: commits fail to sign loudly rather than going out unsigned."""
    for env, exists in [
        ({}, only()),
        ({sandbox.SANDBOX_SSH_SOCK_ENV: DEDICATED}, everything_exists),
        ({"SSH_AUTH_SOCK": SOCK, sandbox.FULL_AGENT_ENV: "1"}, everything_exists),
    ]:
        args, _, _ = setup(env, exists)
        assert not any(name.startswith("GIT_CONFIG") for name in setenvs(args)), env


# --- a real agent -------------------------------------------------------------


def ssh_tool(name):
    path = shutil.which(name)
    assert path, f"{name} is not on PATH"
    return path


def clean_env(**extra):
    """The environment minus any agent, so nothing here can reach the login one."""
    env = {k: v for k, v in os.environ.items() if k not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}
    env.update(extra)
    return env


def make_key(path, passphrase=""):
    subprocess.run(
        [ssh_tool("ssh-keygen"), "-q", "-t", "ed25519", "-N", passphrase, "-C", "throwaway",
         "-f", str(path)],
        check=True, env=clean_env(),
    )
    return path


def fingerprint(pub):
    out = subprocess.run(
        [ssh_tool("ssh-keygen"), "-lf", str(pub)], check=True, capture_output=True, text=True
    ).stdout
    return out.split()[1]


def is_gone(pid, timeout=5):
    """True once pid has exited (a zombie awaiting its reaper counts)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.02)
    return False


def deep_sandbox_tmp(tmp_path):
    """A temp dir deep enough that the agent socket's absolute path would not
    fit in a Unix socket address, as happens when TMPDIR is itself deep."""
    path = tmp_path / ("t" * 100)
    path.mkdir()
    assert len(os.fsencode(path / sandbox.FORGE_AGENT_SOCK)) > 108
    return path


# Plays the launcher: starts the agent, reports it, then waits for the test to
# end it. It never stops the agent itself; only the lifetime mechanism can.
PARENT = textwrap.dedent("""\
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("claude_sandbox", sys.argv[1])
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    proc, warning = sandbox.start_forge_agent(
        sys.argv[2], sys.argv[3], ssh_agent=sys.argv[4], ssh_add=sys.argv[5]
    )
    print(proc.pid if proc else "", warning or "", sep="\\t", flush=True)
    sys.stdin.read()
""")


@pytest.mark.parametrize("end", ["exit", "sigkill"])
def test_real_agent_holds_only_the_forge_key_and_dies_with_its_parent(tmp_path, end):
    key = make_key(tmp_path / "forge_ed25519")
    sandbox_tmp = deep_sandbox_tmp(tmp_path)
    sock = sandbox_tmp / sandbox.FORGE_AGENT_SOCK

    parent = subprocess.Popen(
        [sys.executable, "-c", PARENT, str(SCRIPT), str(key), str(sandbox_tmp),
         ssh_tool("ssh-agent"), ssh_tool("ssh-add")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=clean_env(),
    )
    try:
        pid, warning = parent.stdout.readline().rstrip("\n").split("\t")
        assert warning == ""
        agent_pid = int(pid)
        assert sock.exists()

        listed = subprocess.run(
            [ssh_tool("ssh-add"), "-l"],
            cwd=sandbox_tmp, env=clean_env(SSH_AUTH_SOCK=sock.name),
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        assert len(listed) == 1
        assert listed[0].split()[1] == fingerprint(f"{key}.pub")
    finally:
        if end == "sigkill":
            parent.kill()  # no cleanup code of the parent's gets to run
        else:
            parent.stdin.close()  # the parent returns without stopping the agent
        parent.wait()
        parent.stdout.close()

    gone = is_gone(agent_pid)
    if not gone:
        os.kill(agent_pid, signal.SIGTERM)  # do not leave it behind for the next run
    assert gone
    assert not sock.exists()


def test_real_agent_signs_git_commits_with_the_forge_key(tmp_path):
    key = make_key(tmp_path / "forge_ed25519")
    repo = deep_sandbox_tmp(tmp_path)  # the repo doubles as the agent's dir
    proc, warning = sandbox.start_forge_agent(
        str(key), str(repo), ssh_agent=ssh_tool("ssh-agent"), ssh_add=ssh_tool("ssh-add")
    )
    assert warning is None
    try:
        # The sandbox's environment, with the socket at its host-side name.
        # Inherited GIT_* goes first: a hook runner's GIT_DIR would otherwise
        # aim these commands at the repository under test.
        env = {k: v for k, v in clean_env().items() if not k.startswith("GIT_")}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        )
        env.update(setenvs(sandbox.forge_agent_args(str(key), HOME, {})))
        env["SSH_AUTH_SOCK"] = sandbox.FORGE_AGENT_SOCK

        def git(*args):
            return subprocess.run(
                ["git", "-c", "gpg.format=ssh", "-c", "commit.gpgSign=true", *args],
                cwd=repo, env=env, check=True, capture_output=True, text=True,
            ).stdout

        git("init", "-q")
        git("commit", "-q", "--allow-empty", "-m", "signed")
        assert "-----BEGIN SSH SIGNATURE-----" in git("cat-file", "commit", "HEAD")
    finally:
        proc.terminate()
        proc.wait()


def test_real_agent_refuses_a_passphrase_key_without_prompting(tmp_path):
    key = make_key(tmp_path / "forge_ed25519", passphrase="not-empty")
    sandbox_tmp = deep_sandbox_tmp(tmp_path)
    proc, warning = sandbox.start_forge_agent(
        str(key), str(sandbox_tmp), ssh_agent=ssh_tool("ssh-agent"), ssh_add=ssh_tool("ssh-add")
    )
    assert proc is None
    assert "continuing with no SSH agent" in warning
    assert not (sandbox_tmp / sandbox.FORGE_AGENT_SOCK).exists()
