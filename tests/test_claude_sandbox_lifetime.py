"""Tests for how claude-sandbox runs the sandbox, and what it leaves behind."""

import importlib.util
import json
import shutil
import signal
import subprocess
import sys
import textwrap
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
