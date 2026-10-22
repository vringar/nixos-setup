"""Tests for how claude-sandbox runs the sandbox, and what it leaves behind."""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "apps" / "claude-sandbox" / "claude-sandbox.py"
_spec = importlib.util.spec_from_file_location("claude_sandbox_lifetime", SCRIPT)
sandbox = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sandbox)

BWRAP = shutil.which("bwrap")
needs_bwrap = pytest.mark.skipif(BWRAP is None, reason="bwrap is not on PATH")

# Just enough of a sandbox to run a host binary in a pid namespace of its own,
# as the launcher's sandbox does.
BWRAP_ARGS = ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid"]

# Plays the launcher in a Python process of its own, so that it starts with
# Python's signal dispositions rather than pytest's: runs argv[2:] in the
# sandbox and exits with what run_sandbox returns.
LAUNCHER = textwrap.dedent("""\
    import importlib.util, json, sys
    spec = importlib.util.spec_from_file_location("claude_sandbox", sys.argv[1])
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    bwrap, bwrap_args, inner_cmd = json.loads(sys.argv[2])
    sys.exit(sandbox.run_sandbox(bwrap_args, inner_cmd, bwrap=bwrap))
""")


def launcher_cmd(inner_cmd, bwrap_args=BWRAP_ARGS):
    return [sys.executable, "-c", LAUNCHER, str(SCRIPT), json.dumps([BWRAP, bwrap_args, inner_cmd])]


@needs_bwrap
def test_sandbox_does_not_inherit_ignored_sigpipe():
    """Python ignores SIGPIPE and SIGXFSZ; the sandbox must not, or `yes | head`
    inside fails with "Broken pipe"."""
    out = subprocess.run(
        launcher_cmd([shutil.which("grep"), "SigIgn", "/proc/self/status"]),
        check=True, capture_output=True, text=True,
    ).stdout
    ignored = int(out.split()[1], 16)
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        assert not ignored & (1 << (sig - 1)), sig.name


# --- the sandbox never outlives the launcher ----------------------------------


def sleeper():
    """A sleep command with an argument no other process has, to find it by."""
    return [shutil.which("sleep"), f"3600.{uuid.uuid4().int % 10**12}"]


def pids_running(argv):
    """The pids of every process whose argv is exactly argv."""
    wanted = b"\0".join(os.fsencode(a) for a in argv) + b"\0"
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if Path(f"/proc/{entry}/cmdline").read_bytes() == wanted:
                pids.append(int(entry))
        except OSError:
            continue  # gone meanwhile
    return pids


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


def assert_all_gone(pids):
    left = [pid for pid in pids if not is_gone(pid)]
    for pid in left:
        os.kill(pid, signal.SIGKILL)  # do not leave it behind for the next run
    assert left == []


def wait_for(path, proc, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, "the launcher exited before the sandbox was up"
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.02)


@needs_bwrap
def test_sandbox_dies_with_a_killed_launcher(tmp_path):
    sleep = sleeper()
    ready = tmp_path / "ready"
    launcher = subprocess.Popen(launcher_cmd(
        [shutil.which("sh"), "-c", f'touch {ready} && exec "$@"', "sh", *sleep],
        BWRAP_ARGS + ["--bind", str(tmp_path), str(tmp_path)],
    ))
    try:
        wait_for(ready, launcher)
        in_sandbox = pids_running(sleep)
        assert len(in_sandbox) == 1
    finally:
        launcher.kill()  # no cleanup code of the launcher's gets to run
        launcher.wait()

    assert_all_gone(in_sandbox)


@needs_bwrap
def test_background_processes_end_with_the_sandbox():
    sleep = sleeper()
    # The command returns as soon as its sleep is running in the background.
    subprocess.run(
        launcher_cmd([
            shutil.which("sh"), "-c",
            '"$@" & until grep -qa "$2" /proc/$!/cmdline; do :; done', "sh", *sleep,
        ]),
        check=True,
    )
    deadline = time.monotonic() + 5
    while (left := pids_running(sleep)) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert_all_gone(left)


# --- Podman -------------------------------------------------------------------


def fake_podman(tmp_path):
    """Stands in for podman, noting each call in podman.calls next to itself.

    `system service` notes its pid in podman.pids, creates the socket file it
    is asked to serve on, then serves nothing until it is stopped. `unshare`
    runs its command, and anything else does nothing.
    """
    path = tmp_path / "podman"
    path.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys, time
        me = os.path.realpath(__file__)
        args = sys.argv[1:]
        while args[0].startswith("--"):  # --root and --runroot, with a value each
            args = args[2:]
        with open(me + ".calls", "a") as calls:
            print(json.dumps(sys.argv[1:]), file=calls)
        if args[:2] == ["system", "service"]:
            with open(me + ".pids", "a") as pids:
                print(os.getpid(), file=pids)
            open(args[-1].removeprefix("unix://"), "w").close()
            time.sleep(3600)
        elif args[0] == "unshare":
            os.execv(args[1], args[1:])
    """))
    path.chmod(0o755)
    return path


def podman_pids(podman):
    pids = Path(f"{podman}.pids")
    return [int(pid) for pid in pids.read_text().split()] if pids.exists() else []


def podman_calls(podman):
    calls = Path(f"{podman}.calls")
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


# Starts Podman as the launcher does, reports its pid, then waits for the test
# to end it. It never stops Podman itself; only the lifetime mechanism can.
PODMAN_LAUNCHER = textwrap.dedent("""\
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("claude_sandbox", sys.argv[1])
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    proc, _ = sandbox.start_podman_service(sys.argv[2], podman=sys.argv[3])
    print(proc.pid, flush=True)
    sys.stdin.read()
""")


def test_podman_dies_with_a_killed_launcher(tmp_path):
    sandbox_tmp = tmp_path / "sandbox"
    sandbox_tmp.mkdir()
    launcher = subprocess.Popen(
        [sys.executable, "-c", PODMAN_LAUNCHER, str(SCRIPT), str(sandbox_tmp),
         str(fake_podman(tmp_path))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        podman_pid = int(launcher.stdout.readline())
    finally:
        launcher.kill()  # no cleanup code of the launcher's gets to run
        launcher.wait()
        launcher.stdout.close()
        launcher.stdin.close()

    assert_all_gone([podman_pid])


# --- exit status --------------------------------------------------------------


def test_exit_status_follows_the_shell_convention():
    assert sandbox.exit_status(0) == 0
    assert sandbox.exit_status(3) == 3
    assert sandbox.exit_status(-signal.SIGTERM) == 128 + signal.SIGTERM
    assert sandbox.exit_status(-signal.SIGKILL) == 128 + signal.SIGKILL


# --- the launcher end to end --------------------------------------------------


class Launcher:
    """The launcher as default.nix builds it, from the tools on PATH, run
    against a throwaway home, project and temp dir."""

    def __init__(self, tmp_path, podman=None, bwrap=BWRAP):
        self.home = tmp_path / "home"
        for path in (".config/git", ".config/jj", ".nix-profile", ".local/state/nix"):
            (self.home / path).mkdir(parents=True)
        (self.home / ".zshrc").touch()
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.tmpdir = tmp_path / "tmp"
        self.tmpdir.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        if podman:
            (bin_dir / "podman").symlink_to(podman)

        text = SCRIPT.read_text()
        for placeholder, value in {
            "@python3@": sys.executable,
            "@bwrap@": bwrap,
            "@nix_shell@": "/nonexistent/nix-shell",
            "@nix@": "/nonexistent/nix",
            "@bash@": shutil.which("bash"),
            "@python3_bin_dir@": os.path.dirname(sys.executable),
            "@ssh_agent@": shutil.which("ssh-agent"),
            "@ssh_add@": shutil.which("ssh-add"),
            "@rm@": shutil.which("rm"),
        }.items():
            text = text.replace(placeholder, value)
        self.script = tmp_path / "claude-sandbox"
        self.script.write_text(text)
        self.script.chmod(0o755)

        # Nothing of the caller's: no agent, no Claude config, and no GIT_*,
        # which would aim the launcher's git at the repository under test.
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("GIT_", "CLAUDE_", "CROSSBRIDGE_", "SSH_"))
        }
        # PATH holds podman, if anything: commands are given by absolute path.
        self.env.update(HOME=str(self.home), TMPDIR=str(self.tmpdir), PATH=str(bin_dir))

    def argv(self, command, options):
        return [str(self.script), "--project-dir", str(self.project), *options, "--", *command]

    def popen(self, *command, options=(), **kwargs):
        return subprocess.Popen(self.argv(command, options), env=self.env, **kwargs)

    def run(self, *command, options=()):
        return subprocess.run(
            self.argv(command, options), env=self.env, capture_output=True, text=True
        )

    def leftovers(self):
        return sorted(self.tmpdir.iterdir())


def launcher_warnings(stderr):
    """The launcher's warnings, bar the one about the forge key, which the
    throwaway home never has."""
    return [
        line for line in stderr.splitlines()
        if line.startswith("claude-sandbox:") and "no forge key" not in line
    ]


def children(pid):
    return [int(c) for c in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]


def bwrap_child(launcher):
    [pid] = [
        c for c in children(launcher.pid)
        if Path(f"/proc/{c}/cmdline").read_bytes().split(b"\0")[0] == os.fsencode(BWRAP)
    ]
    return pid


def wait_until_running(argv, proc, timeout=10):
    deadline = time.monotonic() + timeout
    while not pids_running(argv):
        assert proc.poll() is None, "the launcher exited before the sandbox was up"
        assert time.monotonic() < deadline, f"{argv} never started"
        time.sleep(0.02)


@needs_bwrap
def test_project_under_tmp_is_not_covered_by_the_sandbox_tmp(tmp_path):
    """The sandbox's own /tmp is bound over the host's; a project kept in the
    host's /tmp has to be bound after it to stay visible."""
    launcher = Launcher(tmp_path)
    assert str(launcher.project).startswith("/tmp/")
    (launcher.project / "marker").write_text("here\n")
    result = launcher.run(shutil.which("cat"), "marker")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "here\n"


@needs_bwrap
def test_stdout_is_the_commands_alone(tmp_path):
    """The launcher's own messages go to stderr: with stdout a pipe, a line
    of its own there would also land after the command's output."""
    result = Launcher(tmp_path).run(shutil.which("echo"), "out")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "out\n"
    assert "Starting sandboxed" in result.stderr


@needs_bwrap
def test_launcher_passes_on_the_exit_code(tmp_path):
    result = Launcher(tmp_path).run(shutil.which("sh"), "-c", "exit 3")
    assert result.returncode == 3, result.stderr


@needs_bwrap
def test_sandbox_killed_by_a_signal_exits_128_plus_the_signal(tmp_path):
    launcher = Launcher(tmp_path)
    sleep = sleeper()
    proc = launcher.popen(*sleep)
    try:
        wait_until_running(sleep, proc)
        os.kill(bwrap_child(proc), signal.SIGTERM)
        assert proc.wait(timeout=10) == 128 + signal.SIGTERM
    finally:
        proc.kill()
        proc.wait()


# --- stopping the launcher, and failing to start ------------------------------


@needs_bwrap
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT])
def test_a_stopped_launcher_stops_the_sandbox_and_cleans_up(tmp_path, sig):
    podman = fake_podman(tmp_path)
    launcher = Launcher(tmp_path, podman=podman)
    sleep = sleeper()
    proc = launcher.popen(*sleep, stderr=subprocess.PIPE, text=True)
    try:
        wait_until_running(sleep, proc)
        in_sandbox = pids_running(sleep)
        os.kill(proc.pid, sig)
        _, stderr = proc.communicate(timeout=10)
    finally:
        proc.kill()
        proc.wait()

    assert proc.returncode == 128 + sig, stderr
    assert "Traceback" not in stderr
    [podman_pid] = podman_pids(podman)
    assert_all_gone(in_sandbox + [podman_pid])
    assert launcher.leftovers() == []


@needs_bwrap
def test_a_bad_nix_file_starts_nothing(tmp_path):
    podman = fake_podman(tmp_path)
    launcher = Launcher(tmp_path, podman=podman)
    result = launcher.run(shutil.which("true"), options=["--nix-file", "missing.nix"])
    assert result.returncode == 1
    assert "nix file not found" in result.stderr
    assert podman_pids(podman) == []
    assert launcher.leftovers() == []


def test_a_failed_start_still_cleans_up(tmp_path):
    podman = fake_podman(tmp_path)
    launcher = Launcher(tmp_path, podman=podman, bwrap=str(tmp_path / "no-bwrap"))
    result = launcher.run(shutil.which("true"))
    assert result.returncode != 0
    assert "no-bwrap" in result.stderr
    [podman_pid] = podman_pids(podman)
    assert_all_gone([podman_pid])
    assert launcher.leftovers() == []


# --- the temp dir -------------------------------------------------------------


def test_a_killed_launchers_temp_dir_is_removed_by_the_next_launch(tmp_path):
    podman = fake_podman(tmp_path)
    launcher = Launcher(tmp_path, podman=podman)
    sleep = sleeper()
    proc = launcher.popen(*sleep)
    try:
        wait_until_running(sleep, proc)
    finally:
        proc.kill()  # no cleanup code of the launcher's gets to run
        proc.wait()
    [leftover] = launcher.leftovers()
    _, storage_root, run_root = sandbox.podman_paths(str(leftover))

    result = launcher.run(shutil.which("true"))
    assert result.returncode == 0, result.stderr
    assert launcher_warnings(result.stderr) == []
    assert launcher.leftovers() == []
    # Its Podman's containers and storage went through podman.
    calls = podman_calls(podman)
    assert ["--root", storage_root, "--runroot", run_root,
            "rm", "--all", "--force", "--time", "0"] in calls
    assert ["--root", storage_root, "--runroot", run_root,
            "unshare", shutil.which("rm"), "-rf", storage_root, run_root] in calls


def test_a_live_sandboxs_temp_dir_is_left_alone(tmp_path):
    launcher = Launcher(tmp_path)
    sleep = sleeper()
    proc = launcher.popen(*sleep)
    try:
        wait_until_running(sleep, proc)
        [live] = launcher.leftovers()
        assert launcher.run(shutil.which("true")).returncode == 0
        assert launcher.leftovers() == [live]
        assert proc.poll() is None
        assert pids_running(sleep)
    finally:
        proc.terminate()
        proc.wait()
    assert launcher.leftovers() == []


def test_only_unlocked_dirs_of_this_launcher_are_removed(tmp_path):
    def sandbox_dir(name, lock=True):
        path = tmp_path / name
        path.mkdir()
        if lock:
            (path / sandbox.OWNER_LOCK).touch()
        return path

    dead = sandbox_dir("claude-sandbox-dead-1234")
    unlocked_other = sandbox_dir("crosslink-sandbox-dead-1234")
    being_set_up = sandbox_dir("claude-sandbox-new-1234", lock=False)
    (tmp_path / "claude-sandbox-link").symlink_to(being_set_up)
    live, holder = sandbox.make_sandbox_tmp(str(tmp_path / "claude-sandbox-live-"))
    try:
        # The lock is per open file, so this process's own lock blocks too.
        assert sandbox.remove_leftovers("claude-sandbox-", tmp_root=tmp_path) == []
    finally:
        holder.close()

    assert not dead.exists()
    assert unlocked_other.exists()
    assert being_set_up.exists()
    assert (tmp_path / "claude-sandbox-link").is_symlink()
    assert Path(live).exists()


def test_what_cannot_be_deleted_is_reported(tmp_path):
    launcher = Launcher(tmp_path)
    try:
        # A directory the launcher cannot empty: no write permission.
        result = launcher.run(sys.executable, "-c", textwrap.dedent("""\
            import os
            os.mkdir("/tmp/stuck")
            open("/tmp/stuck/file", "w").close()
            os.chmod("/tmp/stuck", 0o500)
        """))
        assert result.returncode == 0, result.stderr
        assert "could not delete" in result.stderr
        assert "Permission denied" in result.stderr
        [leftover] = launcher.leftovers()
        assert sorted(p.name for p in leftover.iterdir()) == ["stuck"]
    finally:
        for leftover in launcher.leftovers():
            (leftover / "stuck").chmod(0o700)


# --- what the sandbox gets ----------------------------------------------------


@needs_bwrap
@pytest.mark.parametrize("project", ["home", "above home"])
def test_a_project_holding_the_home_is_refused(tmp_path, project):
    launcher = Launcher(tmp_path)
    launcher.project = launcher.home if project == "home" else tmp_path
    (launcher.home / ".ssh").mkdir()
    (launcher.home / ".ssh" / "id_ed25519").write_text("private\n")
    result = launcher.run(shutil.which("cat"), str(launcher.home / ".ssh" / "id_ed25519"))
    assert result.returncode == 1
    assert "private" not in result.stdout
    assert "holds your home directory" in result.stderr
    assert launcher.leftovers() == []


@needs_bwrap
def test_a_project_inside_the_home_is_fine(tmp_path):
    launcher = Launcher(tmp_path)
    launcher.project = launcher.home / "project"
    launcher.project.mkdir()
    result = launcher.run(shutil.which("true"))
    assert result.returncode == 0, result.stderr
