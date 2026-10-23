"""No process a test starts outlives the tests.

Every process started through `spawn` (or `run`) is tied to the process that
imported this module in three ways, so that a killed or timed-out pytest, a
test that errors in setup, or a fixture torn down half way leaves nothing
running:

- the kernel kills it when that process dies, however it dies (SIGKILL
  included, where no finalizer runs): PR_SET_PDEATHSIG;
- it leads a process group of its own (unless `new_session=False`), which
  `kill_tree` kills whole, so its children go with it, and which a watchdog
  process kills whole once this process is gone, so that its children
  (that did not leave the group) die with it too, SIGKILL or not;
- whatever is still alive at a normal exit is killed then.

The tie is on the child relative to the test process only. A test that kills
an intermediate parent of its own and then asserts that the grandchildren
died still measures exactly that.
"""

import atexit
import ctypes
import os
import signal
import subprocess
import sys

_PR_SET_PDEATHSIG = 1  # <linux/prctl.h>
_prctl = ctypes.CDLL(None, use_errno=True).prctl
_OWNER = os.getpid()
_LIVE = {}  # Popen -> whether it leads a group of its own


# The watchdog is told the group of every child by line ("+pgid", and
# "-pgid" once it is reaped), and kills the groups left when its stdin, whose
# other end only the owner holds, reaches EOF.
_WATCHDOG = """
import os, signal, sys
groups = set()
for line in sys.stdin:
    (groups.add if line[0] == "+" else groups.discard)(int(line[1:]))
for pgid in groups:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""
_watchdog = None


def _tell_watchdog(sign, pgid):
    global _watchdog
    if _watchdog is None:
        if sign == "-":
            return
        _watchdog = subprocess.Popen([sys.executable, "-c", _WATCHDOG], stdin=subprocess.PIPE,
                                     start_new_session=True, text=True)
    try:
        _watchdog.stdin.write(f"{sign}{pgid}\n")
        _watchdog.stdin.flush()
    except OSError:  # the watchdog is gone; the other ties remain
        pass


def _die_with_owner():
    """A preexec_fn: SIGKILL this child when the owner process dies."""
    if _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")
    if os.getppid() != _OWNER:  # it died before prctl took effect
        os._exit(1)


# For subprocess.run / Popen: the first two ties above.
TIED = {"preexec_fn": _die_with_owner, "start_new_session": True}


def spawn(argv, *, new_session=True, **kwargs):
    """subprocess.Popen, tied to this process (see above)."""
    proc = subprocess.Popen(argv, **{**TIED, "start_new_session": new_session}, **kwargs)
    _LIVE[proc] = new_session
    if new_session:
        _tell_watchdog("+", proc.pid)
    return proc


def kill_tree(proc):
    """SIGKILL `proc` and, if it leads a group, everything in it; reap it."""
    leads = _LIVE.pop(proc, True)
    if proc.returncode is None:
        try:
            if leads:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    proc.wait()
    if leads:
        _tell_watchdog("-", proc.pid)


@atexit.register
def _kill_leftovers():
    for proc in list(_LIVE):
        kill_tree(proc)


def run(argv, *, new_session=True, **kwargs):
    """subprocess.run, tied to this process (see above)."""
    return subprocess.run(argv, **{**TIED, "start_new_session": new_session}, **kwargs)
