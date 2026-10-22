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
    """Stands in for podman: creates the socket file it is asked to serve on,
    then serves nothing until it is stopped."""
    path = tmp_path / "podman"
    path.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, time
        open(sys.argv[-1].removeprefix("unix://"), "w").close()
        time.sleep(3600)
    """))
    path.chmod(0o755)
    return path


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
