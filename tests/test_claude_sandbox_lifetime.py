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
# Python's signal dispositions rather than pytest's: runs the command it is
# given in the sandbox and exits with what run_sandbox returns.
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


def wait_until_running(argv, launcher, timeout=10):
    deadline = time.monotonic() + timeout
    while not pids_running(argv):
        assert launcher.poll() is None, "the launcher exited before the sandbox was up"
        assert time.monotonic() < deadline, f"{argv} never started"
        time.sleep(0.02)


@needs_bwrap
def test_sandbox_dies_with_a_killed_launcher():
    sleep = sleeper()
    launcher = subprocess.Popen(launcher_cmd(sleep))
    try:
        wait_until_running(sleep, launcher)
        in_sandbox = pids_running(sleep)
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


def fake_podman(directory):
    """Stands in for podman as directory/podman, noting each call it can in
    podman.calls next to itself (inside the service's view that is read-only).

    `unshare CMD` runs CMD as root of a user namespace (unshare -r), with
    _CONTAINERS_USERNS_CONFIGURED=done, as podman runs it in its own.
    `system service` refuses to run anywhere else. There it writes what it
    sees to $FAKE_PODMAN_REPORT, if set, starts a "container", a daemonized
    `sleep $FAKE_CONTAINER`, if set, creates the socket file it is asked to
    serve on, and then serves nothing until it is stopped. Anything else
    does nothing.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "podman"
    path.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, subprocess, sys, time
        args = sys.argv[1:]
        while args[0].startswith("--"):  # --root, --runroot, --tmpdir, each with a value
            args = args[2:]
        try:
            with open(os.path.realpath(__file__) + ".calls", "a") as calls:
                print(json.dumps(sys.argv[1:]), file=calls)
        except OSError:
            pass  # read-only inside the service's view
        if args[0] == "unshare":
            env = dict(os.environ, _CONTAINERS_USERNS_CONFIGURED="done")
            sys.exit(subprocess.call([{shutil.which("unshare")!r}, "-r", *args[1:]], env=env))
        if args[:2] == ["system", "service"]:
            if os.environ.get("_CONTAINERS_USERNS_CONFIGURED") != "done" or os.getuid() != 0:
                sys.exit("Error: not in the user namespace")
            report = os.environ.get("FAKE_PODMAN_REPORT")
            if report:
                with open(report, "w") as out:
                    json.dump(dict(
                        uid=os.getuid(),
                        pid=os.getpid(),
                        tmp=sorted(os.listdir("/tmp")),
                        xdg_runtime_dir=os.environ.get("XDG_RUNTIME_DIR"),
                        dbus=os.environ.get("DBUS_SESSION_BUS_ADDRESS"),
                        sees={{p: os.path.exists(p) for p in json.loads(os.environ.get("FAKE_PODMAN_PATHS", "[]"))}},
                    ), out)
            container = os.environ.get("FAKE_CONTAINER")
            if container and os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    os.execv({shutil.which("sleep")!r}, ["sleep", container])
                os._exit(0)
            open(args[-1].removeprefix("unix://"), "w").close()
            time.sleep(3600)
    """))
    path.chmod(0o755)
    return path


def podman_calls(podman):
    calls = Path(f"{podman}.calls")
    return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def processes_with(*parts):
    """The pids of every process whose argv includes each of parts."""
    wanted = [os.fsencode(str(p)) for p in parts]
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue  # gone meanwhile
        if all(part in argv for part in wanted):
            pids.append(int(entry))
    return pids


def podman_services(podman):
    """The fake's `system service` processes, wherever they run; not the
    commands that start one."""
    return [
        pid for pid in processes_with(podman, "system", "service")
        if Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[1] == os.fsencode(str(podman))
        and b"unshare" not in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    ]


def eventually(probe, timeout=10):
    deadline = time.monotonic() + timeout
    while not (found := probe()):
        assert time.monotonic() < deadline, "never happened"
        time.sleep(0.02)
    return found


def store_args(sandbox_tmp):
    podman_dir = Path(sandbox_tmp) / "podman"
    return ["--root", str(podman_dir / "storage"), "--runroot", str(podman_dir / "run"),
            "--tmpdir", str(podman_dir / "tmp")]


# Starts Podman as the launcher does, then waits for the test to end it. It
# never stops Podman itself; only the lifetime mechanism can.
PODMAN_LAUNCHER = textwrap.dedent("""\
    import importlib.util, json, sys
    spec = importlib.util.spec_from_file_location("claude_sandbox", sys.argv[1])
    sandbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sandbox)
    sandbox_tmp, view_args, podman, bwrap = json.loads(sys.argv[2])
    proc, _ = sandbox.start_podman_service(sandbox_tmp, view_args, podman=podman, bwrap=bwrap)
    print(proc.pid, flush=True)
    sys.stdin.read()
""")


@needs_bwrap
def test_podman_and_its_containers_die_with_a_killed_launcher(tmp_path):
    sandbox_tmp = tmp_path / "sandbox"
    sandbox_tmp.mkdir()
    podman = fake_podman(tmp_path / "bin")
    container = sleeper()[1]
    report = sandbox_tmp / "report.json"
    # Everything read-only but the sandbox's temp dir, where the socket goes.
    view = BWRAP_ARGS + ["--bind", str(sandbox_tmp), str(sandbox_tmp)]
    launcher = subprocess.Popen(
        [sys.executable, "-c", PODMAN_LAUNCHER, str(SCRIPT),
         json.dumps([str(sandbox_tmp), view, str(podman), BWRAP])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, FAKE_CONTAINER=container, FAKE_PODMAN_REPORT=str(report)),
    )
    try:
        outer = int(launcher.stdout.readline())
        [service] = podman_services(podman)
        [in_container] = eventually(lambda: processes_with(container))
        seen = json.loads(report.read_text())
    finally:
        launcher.kill()  # no cleanup code of the launcher's gets to run
        launcher.wait()
        launcher.stdout.close()
        launcher.stdin.close()

    # The service ran as root of the user namespace, in a pid namespace of its
    # own, where bwrap's init is 1.
    assert seen["uid"] == 0
    assert seen["pid"] == 2
    assert_all_gone([outer, service, in_container])


# --- the launcher end to end --------------------------------------------------


class Launcher:
    """The launcher as default.nix builds it, from the tools on PATH, run
    against a throwaway home, project and temp dir."""

    def __init__(self, tmp_path, podman=False, bwrap=BWRAP, ssh_key_name="github_key"):
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
        path = [bin_dir]
        # In the profile, as on a real host, where the Podman service, which
        # sees only what the sandbox sees, can still run it.
        self.podman = fake_podman(self.home / ".nix-profile" / "bin") if podman else None
        if podman:
            path.append(self.podman.parent)

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
            "@ssh_key_name@": ssh_key_name,
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
        self.env.update(
            HOME=str(self.home), TMPDIR=str(self.tmpdir), PATH=os.pathsep.join(map(str, path))
        )

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
    launcher = Launcher(tmp_path, podman=True)
    container = sleeper()[1]
    launcher.env["FAKE_CONTAINER"] = container
    sleep = sleeper()
    proc = launcher.popen(*sleep, stderr=subprocess.PIPE, text=True)
    try:
        wait_until_running(sleep, proc)
        in_sandbox = pids_running(sleep)
        [service] = podman_services(launcher.podman)
        [in_container] = eventually(lambda: processes_with(container))
        os.kill(proc.pid, sig)
        _, stderr = proc.communicate(timeout=10)
    finally:
        proc.kill()
        proc.wait()

    assert proc.returncode == 128 + sig, stderr
    assert "Traceback" not in stderr
    assert_all_gone(in_sandbox + [service, in_container])
    assert launcher.leftovers() == []


@needs_bwrap
def test_a_bad_nix_file_starts_nothing(tmp_path):
    launcher = Launcher(tmp_path, podman=True)
    result = launcher.run(shutil.which("true"), options=["--nix-file", "missing.nix"])
    assert result.returncode == 1
    assert "nix file not found" in result.stderr
    assert podman_calls(launcher.podman) == []
    assert launcher.leftovers() == []


def test_a_failed_start_still_cleans_up(tmp_path):
    launcher = Launcher(tmp_path, podman=True, bwrap=str(tmp_path / "no-bwrap"))
    result = launcher.run(shutil.which("true"))
    assert result.returncode != 0
    assert "no-bwrap" in result.stderr
    assert podman_calls(launcher.podman)  # it was tried
    assert podman_services(launcher.podman) == []
    assert launcher.leftovers() == []


@needs_bwrap
def test_the_podman_service_sees_only_what_the_sandbox_sees(tmp_path):
    """Containers bind-mount from the service's view, so that view must not
    hold ~/.ssh or anything else the sandbox does not have, and a path a
    client in the sandbox names must be the same file for the service."""
    launcher = Launcher(tmp_path, podman=True)
    (launcher.home / ".ssh").mkdir()
    key = launcher.home / ".ssh" / "id_ed25519"
    key.write_text("private\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    report = launcher.project / "podman-view.json"
    launcher.env.update(
        FAKE_PODMAN_REPORT=str(report),
        FAKE_PODMAN_PATHS=json.dumps([str(key), str(outside), str(launcher.project)]),
    )
    result = launcher.run(shutil.which("sh"), "-c", 'test -e "${DOCKER_HOST#unix://}" && echo "$DOCKER_HOST"')
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unix:///tmp/podman/podman.sock\n"
    assert launcher_warnings(result.stderr) == []

    seen = json.loads(report.read_text())
    assert seen["sees"] == {str(key): False, str(outside): False, str(launcher.project): True}
    # Its /tmp is the sandbox's /tmp.
    assert {sandbox.OWNER_LOCK, "podman"} <= set(seen["tmp"])
    assert seen["xdg_runtime_dir"].endswith("/podman/runtime")
    assert seen["dbus"] is None
    assert seen["uid"] == 0


def test_a_podman_service_that_fails_says_why(tmp_path, capsys):
    podman = tmp_path / "podman"
    podman.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys
        print("Error: no such file or directory", file=sys.stderr)
        sys.exit(125)
    """))
    podman.chmod(0o755)
    sandbox_tmp = tmp_path / "sandbox"
    sandbox_tmp.mkdir()

    assert sandbox.start_podman_service(str(sandbox_tmp), [], podman=str(podman)) == (None, None)
    assert "exited with status 125 (Error: no such file or directory)" in capsys.readouterr().err


# --- the temp dir -------------------------------------------------------------


def test_a_killed_launchers_temp_dir_is_removed_by_the_next_launch(tmp_path):
    launcher = Launcher(tmp_path, podman=True)
    container = sleeper()[1]
    launcher.env["FAKE_CONTAINER"] = container
    sleep = sleeper()
    proc = launcher.popen(*sleep)
    try:
        wait_until_running(sleep, proc)
        [service] = podman_services(launcher.podman)
        [in_container] = eventually(lambda: processes_with(container))
    finally:
        proc.kill()  # no cleanup code of the launcher's gets to run
        proc.wait()
    # The kernel ends the service and its containers...
    assert_all_gone([service, in_container])
    # ...but the temp dir is left for the next launch.
    [leftover] = launcher.leftovers()

    del launcher.env["FAKE_CONTAINER"]
    result = launcher.run(shutil.which("true"))
    assert result.returncode == 0, result.stderr
    assert launcher_warnings(result.stderr) == []
    assert launcher.leftovers() == []
    # Its Podman storage went through podman's user namespace.
    assert [*store_args(leftover), "unshare", shutil.which("rm"), "-rf",
            str(leftover / "podman")] in podman_calls(launcher.podman)


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


@needs_bwrap
def test_the_sandbox_gets_the_forge_agent_but_no_private_key(tmp_path):
    launcher = Launcher(tmp_path)
    ssh_dir = launcher.home / ".ssh"
    ssh_dir.mkdir()
    keygen = shutil.which("ssh-keygen")
    for name in ("github_key", "mozilla"):
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(ssh_dir / name)],
            check=True,
        )
    result = launcher.run(shutil.which("sh"), "-c", textwrap.dedent(f"""\
        {shutil.which("ssh-add")} -l
        for key in {ssh_dir}/*; do
            {shutil.which("cat")} "$key" >/dev/null 2>&1 && echo "read $key"
        done
        exit 0
    """))
    assert result.returncode == 0, result.stderr
    [listed, *read] = result.stdout.splitlines()
    assert listed.endswith("github_key (ED25519)")
    assert sorted(read) == [f"read {ssh_dir}/github_key.pub", f"read {ssh_dir}/mozilla.pub"]
    assert launcher_warnings(result.stderr) == []
    assert launcher.leftovers() == []


# --- the GitHub token, and no D-Bus -------------------------------------------

TOKEN = "gho_sandboxtest0123456789"


def fake_gh(bin_dir, script):
    """A gh on PATH in bin_dir that runs script (sh) for any arguments."""
    gh = bin_dir / "gh"
    gh.write_text(f"#!{shutil.which('sh')}\n{script}\n")
    gh.chmod(0o755)
    return gh


def gh_env(tmp_path, **extra):
    """An environment whose PATH holds only what is in tmp_path."""
    return {"PATH": str(tmp_path), **extra}


def test_gh_token_asks_gh_outside(tmp_path):
    fake_gh(tmp_path, f'[ "$1 $2" = "auth token" ] && echo {TOKEN}')
    assert sandbox.gh_token(env=gh_env(tmp_path)) == (TOKEN, None)


def test_gh_token_leaves_a_set_token_alone(tmp_path):
    calls = tmp_path / "calls"
    fake_gh(tmp_path, f"echo called >> {calls}; echo other")
    assert sandbox.gh_token(env=gh_env(tmp_path, GH_TOKEN=TOKEN)) == (None, None)
    assert not calls.exists()


def test_gh_token_without_gh_passes_nothing(tmp_path):
    assert sandbox.gh_token(env=gh_env(tmp_path)) == (None, None)


def test_gh_token_says_why_gh_has_none(tmp_path):
    fake_gh(tmp_path, "echo 'no oauth token found for github.com' >&2; exit 1")
    token, warning = sandbox.gh_token(env=gh_env(tmp_path))
    assert token is None
    assert "no oauth token found for github.com" in warning


def test_gh_token_warns_on_a_silent_failure(tmp_path):
    fake_gh(tmp_path, "exit 0")
    token, warning = sandbox.gh_token(env=gh_env(tmp_path))
    assert token is None
    assert "no token" in warning


def test_gh_token_gives_up_on_a_hung_gh(tmp_path):
    fake_gh(tmp_path, f"exec {shutil.which('sleep')} 60")
    token, warning = sandbox.gh_token(env=gh_env(tmp_path), timeout=0.2)
    assert token is None
    assert "timed out" in warning


def test_the_session_bus_is_not_bound(tmp_path, monkeypatch):
    """Through the bus, the sandbox could have the user's service manager run
    anything outside it (systemd-run --user)."""
    bus = f"/run/user/{os.getuid()}/bus"
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: p == bus or real_exists(p))
    home = tmp_path / "home"
    home.mkdir()
    project = home / "project"
    project.mkdir()
    args = sandbox.build_bwrap_args(
        str(project), str(home), str(tmp_path / "tmp"), str(tmp_path / "hist"), "/bin/sh"
    )
    assert not [a for a in args if "/bus" in a]


@needs_bwrap
def test_the_sandbox_gets_the_token_through_its_environment(tmp_path):
    launcher = Launcher(tmp_path)
    launcher.env.pop("GH_TOKEN", None)
    fake_gh(tmp_path / "bin", f'[ "$1 $2" = "auth token" ] && echo {TOKEN}')
    result = launcher.run(shutil.which("sh"), "-c", 'printf %s "$GH_TOKEN"')
    assert result.returncode == 0, result.stderr
    assert result.stdout == TOKEN
    assert launcher_warnings(result.stderr) == []


@needs_bwrap
def test_the_token_stays_off_bwraps_command_line(tmp_path):
    """Any user on the host can read a process's command line."""
    launcher = Launcher(tmp_path)
    launcher.env.pop("GH_TOKEN", None)
    fake_gh(tmp_path / "bin", f"echo {TOKEN}")
    sleep = sleeper()
    proc = launcher.popen(*sleep)
    try:
        wait_until_running(sleep, proc)
        bwrap = bwrap_child(proc)
        cmdline = Path(f"/proc/{bwrap}/cmdline").read_bytes()
        environ = Path(f"/proc/{bwrap}/environ").read_bytes()
    finally:
        proc.kill()
        proc.wait()
    assert TOKEN.encode() not in cmdline
    assert f"GH_TOKEN={TOKEN}".encode() in environ.split(b"\0")



@needs_bwrap
def test_the_agent_holds_the_key_the_build_names(tmp_path):
    """default.nix substitutes my.user.sshKeyName; git inside signs with that
    key's .pub, as git outside does."""
    launcher = Launcher(tmp_path, ssh_key_name="id_ed25519")
    ssh_dir = launcher.home / ".ssh"
    ssh_dir.mkdir()
    keygen = shutil.which("ssh-keygen")
    for name in ("id_ed25519", "github_key"):
        subprocess.run(
            [keygen, "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(ssh_dir / name)],
            check=True,
        )
    result = launcher.run(shutil.which("sh"), "-c", textwrap.dedent(f"""\
        {shutil.which("ssh-add")} -l
        n=$((GIT_CONFIG_COUNT - 1))
        eval "echo \\"\\$GIT_CONFIG_KEY_$n \\$GIT_CONFIG_VALUE_$n\\""
    """))
    assert result.returncode == 0, result.stderr
    listed, signing = result.stdout.splitlines()
    assert listed.endswith(" id_ed25519 (ED25519)")
    assert signing == f"user.signingKey {ssh_dir}/id_ed25519.pub"

    help_text = subprocess.run(
        [str(launcher.script), "--help"], env=launcher.env, capture_output=True, text=True
    ).stdout
    assert "~/.ssh/id_ed25519" in help_text
    assert "@ssh_key_name@" not in help_text


# --- what the launcher does to the project ------------------------------------


@pytest.mark.skipif(shutil.which("jj") is None, reason="jj is not on PATH")
def test_finding_the_crossbridge_slug_records_nothing_in_the_project(tmp_path, monkeypatch):
    # No GIT_* from a hook runner: the launcher's git fallback would follow
    # GIT_DIR to the repository under test.
    for name in list(os.environ):
        if name.startswith(("GIT_", "JJ_", "CROSSBRIDGE_")):
            monkeypatch.delenv(name)
    config = tmp_path / "jj-config.toml"
    config.write_text('user.name = "t"\nuser.email = "t@example.com"\n')
    monkeypatch.setenv("JJ_CONFIG", str(config))
    project = tmp_path / "foo"
    subprocess.run(["jj", "git", "init", str(project)], check=True, capture_output=True)

    def jj(*args):
        return subprocess.run(
            ["jj", "--repository", str(project), *args],
            check=True, capture_output=True, text=True,
        ).stdout

    jj("git", "remote", "add", "origin", "git@example.com:someone/foo-repo.git")
    operations = jj("op", "log", "--no-graph", "-T", "id ++ \"\\n\"")
    (project / "new-file").write_text("not to be recorded by the launcher\n")

    assert sandbox.crossbridge_own_slug(str(project)) == "foo-repo"
    assert jj("op", "log", "--no-graph", "--ignore-working-copy", "-T", "id ++ \"\\n\"") == operations


# --- what the host later runs is read-only ------------------------------------

# Run in the sandbox: tries to change each path given, a file by appending to
# it or renaming another over it, a directory by creating a file in it or
# renaming it, and prints which paths it could change.
WRITE_PROBE = textwrap.dedent("""\
    import json, os, sys
    changed = {}
    for path in json.loads(sys.argv[1]):
        attempts = []
        if os.path.isdir(path):
            attempts.append(lambda: open(os.path.join(path, "planted"), "x").close())
            attempts.append(lambda: os.rename(path, path + ".moved"))
        else:
            attempts.append(lambda: open(path, "a").write("planted\\\\n"))
            def replace():
                other = path + ".planted"
                open(other, "w").close()
                os.replace(other, path)
            attempts.append(replace)
        changed[path] = False
        for attempt in attempts:
            try:
                attempt()
                changed[path] = True
            except OSError:
                pass
    print(json.dumps(changed))
""")


def changeable_inside(launcher, *paths):
    result = launcher.run(sys.executable, "-c", WRITE_PROBE, json.dumps([str(p) for p in paths]))
    assert result.returncode == 0, result.stderr
    return {Path(p): changed for p, changed in json.loads(result.stdout).items()}


def git_repo(path, hooks=True):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text("[core]\n\tbare = false\n")
    if hooks:
        (path / ".git" / "hooks").mkdir()
    return path / ".git"


@needs_bwrap
@pytest.mark.parametrize("hooks", [True, False])
def test_the_projects_git_config_and_hooks_are_read_only(tmp_path, hooks):
    launcher = Launcher(tmp_path)
    git_dir = git_repo(launcher.project, hooks=hooks)
    other = git_dir / "description"
    other.write_text("unnamed\n")

    changed = changeable_inside(launcher, git_dir / "config", git_dir / "hooks", other)
    assert changed == {git_dir / "config": False, git_dir / "hooks": False, other: True}
    assert (git_dir / "config").read_text() == "[core]\n\tbare = false\n"
    assert list((git_dir / "hooks").iterdir()) == []


@needs_bwrap
def test_a_linked_worktrees_git_config_and_hooks_are_read_only(tmp_path):
    """.git is a file there, naming a git dir whose commondir is the main
    repository's; config and hooks are the main repository's."""
    launcher = Launcher(tmp_path)
    main = tmp_path / "main"
    common = git_repo(main)
    own = common / "worktrees" / "wt"
    own.mkdir(parents=True)
    (own / "commondir").write_text("../..\n")
    (own / "config.worktree").write_text("")
    launcher.project = main / ".worktrees" / "wt"
    launcher.project.mkdir(parents=True)
    (launcher.project / ".git").write_text("gitdir: ../../.git/worktrees/wt\n")

    changed = changeable_inside(
        launcher, common / "config", common / "hooks", own / "config.worktree", own / "HEAD"
    )
    assert changed == {
        common / "config": False,
        common / "hooks": False,
        own / "config.worktree": False,
        own / "HEAD": True,
    }


@needs_bwrap
def test_the_parent_repositorys_git_config_and_hooks_are_read_only_from_a_workspace(tmp_path):
    launcher = Launcher(tmp_path)
    main = tmp_path / "main"
    git_dir = git_repo(main)
    (main / ".jj").mkdir()
    launcher.project = main / ".workspace" / "ws"
    launcher.project.mkdir(parents=True)

    changed = changeable_inside(launcher, git_dir / "config", git_dir / "hooks", git_dir / "index")
    assert changed == {git_dir / "config": False, git_dir / "hooks": False, git_dir / "index": True}


def jj_repo(path, legacy_config=False):
    repo = path / ".jj" / "repo"
    (repo / "op_heads").mkdir(parents=True)
    (repo / "config-id").write_text("0123456789abcdef0123")
    if legacy_config:
        (repo / "config.toml").write_text('[ui]\npager = "less"\n')
    else:
        (repo / "config.toml").symlink_to("/nonexistent/jj/repos/0123456789abcdef0123/config.toml")
    return repo


@needs_bwrap
def test_the_projects_jj_config_id_is_read_only(tmp_path):
    """Without config-id, jj on the host would take a config.toml planted in
    .jj/repo as the repository's config, and run the commands it names."""
    launcher = Launcher(tmp_path)
    repo = jj_repo(launcher.project)
    changed = changeable_inside(launcher, repo / "config-id", repo / "op_heads")
    assert changed == {repo / "config-id": False, repo / "op_heads": True}


@needs_bwrap
def test_a_jj_config_not_yet_moved_out_is_read_only(tmp_path):
    launcher = Launcher(tmp_path)
    repo = jj_repo(launcher.project, legacy_config=True)
    changed = changeable_inside(launcher, repo / "config-id", repo / "config.toml")
    assert changed == {repo / "config-id": False, repo / "config.toml": False}


@needs_bwrap
def test_the_parent_repositorys_jj_config_id_is_read_only_from_a_workspace(tmp_path):
    launcher = Launcher(tmp_path)
    main = tmp_path / "main"
    repo = jj_repo(main)
    launcher.project = main / ".workspace" / "ws"
    (launcher.project / ".jj").mkdir(parents=True)
    (launcher.project / ".jj" / "repo").write_text("../../../.jj/repo")
    changed = changeable_inside(launcher, repo / "config-id", repo / "op_heads")
    assert changed == {repo / "config-id": False, repo / "op_heads": True}


@needs_bwrap
def test_claude_settings_and_hooks_are_read_only(tmp_path):
    launcher = Launcher(tmp_path)
    config_dir = launcher.home / ".config" / "claude"
    dot_dir = launcher.home / ".claude"
    paths = []
    for claude_dir in (config_dir, dot_dir):
        (claude_dir / "hooks").mkdir(parents=True)
        (claude_dir / "hooks" / "hook.sh").write_text("#!/bin/sh\n")
        (claude_dir / "settings.json").write_text("{}\n")
        paths += [claude_dir / "settings.json", claude_dir / "hooks", claude_dir / "hooks" / "hook.sh"]
    # The rest stays writable: Claude Code keeps its state there.
    state = config_dir / ".claude.json"
    state.write_text("{}\n")

    changed = changeable_inside(launcher, *paths, state)
    assert changed == {**{path: False for path in paths}, state: True}


@needs_bwrap
def test_gh_config_is_read_only(tmp_path):
    launcher = Launcher(tmp_path)
    gh_config = launcher.home / ".config" / "gh"
    gh_config.mkdir(parents=True)
    (gh_config / "config.yml").write_text("aliases: {}\n")

    changed = changeable_inside(launcher, gh_config, gh_config / "config.yml")
    assert changed == {gh_config: False, gh_config / "config.yml": False}


@needs_bwrap
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_git_still_commits_with_its_config_read_only(tmp_path):
    launcher = Launcher(tmp_path)
    git = os.path.realpath(shutil.which("git"))  # a store path, which the sandbox sees
    # Launcher.env has no GIT_* of the caller's, which would aim git elsewhere.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run([git, "init", "-q", str(launcher.project)], check=True, env=env)
    launcher.env.update(
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
    )
    result = launcher.run(shutil.which("sh"), "-c", f"""
        {git} commit -q --allow-empty -m inside && echo committed
        {git} config --local x.y z 2>/dev/null || echo config refused
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "committed\nconfig refused\n"
    assert "x.y" not in (launcher.project / ".git" / "config").read_text()


@needs_bwrap
@pytest.mark.skipif(shutil.which("jj") is None, reason="jj is not on PATH")
def test_a_planted_jj_config_does_not_reach_the_host(tmp_path):
    """The attack the config-id bind stops, with the real jj: plant a
    config.toml naming a pager, which the next jj on the host would move into
    the repository's config and run, unless config-id says where that config
    is. jj writes config-id only once something asks for that place, so a
    fresh repository has none until the launcher asks."""
    launcher = Launcher(tmp_path)
    jj = os.path.realpath(shutil.which("jj"))
    (tmp_path / "bin" / "jj").symlink_to(jj)  # for the launcher
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "JJ_", "XDG_CONFIG"))}
    env["HOME"] = str(launcher.home)  # its repo config goes under the throwaway home

    def host_jj(*args):
        return subprocess.run(
            [jj, "--repository", str(launcher.project), "--ignore-working-copy", *args],
            env=env, check=True, capture_output=True, text=True,
        ).stdout

    subprocess.run([jj, "git", "init", str(launcher.project)], env=env, check=True,
                   capture_output=True)
    pager = host_jj("config", "get", "ui.pager")
    repo = launcher.project / ".jj" / "repo"
    assert not (repo / "config-id").exists()

    result = launcher.run(sys.executable, "-c", textwrap.dedent(f"""\
        import os
        try:
            os.unlink({str(repo / "config-id")!r})
        except OSError as e:
            print("config-id stays:", e.strerror)
        if os.path.lexists({str(repo / "config.toml")!r}):
            os.unlink({str(repo / "config.toml")!r})
        with open({str(repo / "config.toml")!r}, "w") as f:
            f.write('[ui]\\npager = "planted"\\n')
    """))
    assert result.returncode == 0, result.stderr
    assert launcher_warnings(result.stderr) == []
    assert result.stdout.startswith("config-id stays:")
    assert host_jj("config", "get", "ui.pager") == pager
