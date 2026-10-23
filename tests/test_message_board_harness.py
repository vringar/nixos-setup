"""The harness leaves no process behind: not when the process running it is
killed outright, where no finalizer runs. See board_harness."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import board_harness
from board_harness import HANG_TIMEOUT, bin_dir, drive_tui

TESTS = Path(__file__).resolve().parent

# What a test session does: a server, a watch on it and a board-admin under a
# pty, all started through the harness; then it hangs, as a pytest stuck in a
# test does.
OWNER_OF_PROCESSES = """
import sys, time
sys.path.insert(0, {tests!r})
from pathlib import Path
from board_harness import Server, Watch, spawn, program
import pty

tmp = Path({tmp!r})
server = Server(tmp / "board-v2.db", tmp / "logs").start()
watch = Watch("/", "--since", "head", env={{"BOARD_URL": server.url}})
master, slave = pty.openpty()
admin = spawn([program("board-admin")], stdin=slave, stdout=slave, stderr=slave,
              env={{"TERM": "xterm", "BOARD_DB": str(server.db), "PATH": "/nonexistent"}})
print(server.proc.pid, watch.proc.pid, admin.pid, flush=True)
time.sleep(600)
"""


def alive(pid):
    try:  # a zombie is dead; so is a pid that goes between two lines
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def test_killing_the_process_that_owns_them_kills_server_watch_and_admin(tmp_path):
    env = {**os.environ, "BOARD_BIN_DIR": str(bin_dir())}
    owner = subprocess.Popen(
        [sys.executable, "-c",
         textwrap.dedent(OWNER_OF_PROCESSES).format(tests=str(TESTS), tmp=str(tmp_path))],
        stdout=subprocess.PIPE, env=env, start_new_session=True)
    try:
        pids = [int(p) for p in owner.stdout.readline().split()]
        assert len(pids) == 3 and all(alive(p) for p in pids)
        owner.kill()  # SIGKILL: no atexit, no finally
        owner.wait()
        deadline = time.monotonic() + HANG_TIMEOUT
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert [p for p in pids if alive(p)] == []
    finally:
        owner.kill()
        owner.wait()
        for pid in locals().get("pids", []):
            if alive(pid):
                os.kill(pid, 9)  # only when the test failed


def test_a_terminal_that_closed_but_whose_program_lives_is_waited_on_not_spun_on(tmp_path, monkeypatch):
    """board-admin shut its tty and went on running: reading the pty failed
    at once, and the wait for its exit used to loop on that at 100% CPU."""
    fake = tmp_path / "board-admin"
    fake.write_text("#!/bin/sh\nexec 0<&- 1>&- 2>&-\nexec sleep 3\n")
    fake.chmod(0o755)
    monkeypatch.setattr(board_harness, "program", lambda name: str(fake))
    before, started = os.times(), time.monotonic()
    drive_tui(dict(os.environ), [])
    after = os.times()
    cpu = (after.user + after.system) - (before.user + before.system)
    assert time.monotonic() - started > 2  # it did wait for the sleep
    assert cpu < 0.5, f"{cpu:.1f} s of CPU in 3 s of waiting"
