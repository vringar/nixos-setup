"""proc_lifetime: what a test spawns does not outlive the process that
spawned it, killed outright or not."""

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent

# A process that owns a child (a shell) and a grandchild (its sleep), then
# either hangs or returns.
OWNER = """
import sys, time
sys.path.insert(0, {tests!r})
from proc_lifetime import spawn
child = spawn(["sh", "-c", "sleep 600 & echo $!; wait"], stdout=-1, text=True)
grandchild = int(child.stdout.readline())
print(child.pid, grandchild, flush=True)
if {hang}:
    time.sleep(600)
"""


def alive(pid):
    try:  # a zombie is dead; so is a pid that goes between two lines
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def owned(hang):
    code = textwrap.dedent(OWNER).format(tests=str(TESTS), hang=hang)
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)


def gone_soon(pids):
    deadline = time.monotonic() + 20
    while any(alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    return [p for p in pids if alive(p)] == []


def test_killing_the_owner_kills_its_child_and_grandchild():
    owner = owned(hang=True)
    pids = []
    try:
        pids = [int(p) for p in owner.stdout.readline().split()]
        assert all(alive(p) for p in pids)
        owner.kill()  # SIGKILL: no atexit, no finally
        owner.wait()
        assert gone_soon(pids)
    finally:
        owner.kill()
        owner.wait()
        for pid in pids:
            if alive(pid):
                os.kill(pid, 9)  # only when the test failed


def test_a_normal_exit_leaves_nothing():
    owner = owned(hang=False)
    pids = [int(p) for p in owner.stdout.readline().split()]
    try:
        owner.wait(timeout=20)
        assert gone_soon(pids)
    finally:
        owner.kill()
        owner.wait()
        for pid in pids:
            if alive(pid):
                os.kill(pid, 9)  # only when the test failed
