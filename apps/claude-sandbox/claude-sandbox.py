#!@python3@
"""Run commands in a bubblewrap sandbox with filesystem isolation."""

import argparse
import ctypes
import fcntl
import glob
import os
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

BWRAP = "@bwrap@"
NIX_SHELL = "@nix_shell@"
NIX = "@nix@"
BASH = "@bash@"
PYTHON3_BIN_DIR = "@python3_bin_dir@"
SSH_AGENT = "@ssh_agent@"
SSH_ADD = "@ssh_add@"
RM = "@rm@"
# The key each sandbox's own SSH agent holds, ~/.ssh/<name>: home-manager's
# my.user.sshKeyName, the key git and ssh use outside the sandbox as well.
SSH_KEY_NAME = "@ssh_key_name@"

# Default launch commands per script name (when no command is given after --)
DEFAULT_COMMANDS = {
    "claude-sandbox": "claude --dangerously-skip-permissions",
}


def get_script_name():
    return os.path.basename(sys.argv[0])


def create_parser(prog=None):
    prog = prog or get_script_name()
    epilog = f"""\
examples:
  {prog}                                   # sandbox cwd, auto-detect shell.nix
  {prog} ~/projects/foo                    # sandbox a specific project
  {prog} --nix-file nix/shell.nix .        # explicit nix file
  {prog} --shell ~/projects/foo            # interactive shell in sandbox
  {prog} --project-dir ~/p/foo -- cmd arg  # run a custom command

ssh agent:
  The sandbox never gets the login SSH agent by default. That agent can hold
  every key there is, so passing it through would hand all of them to
  anything inside.

  Instead, each sandbox gets an agent of its own, started with it and stopped
  with it, holding only the key ~/.ssh/{SSH_KEY_NAME} (home-manager's
  my.user.sshKeyName; override the path with {FORGE_KEY_ENV}). The
  key file stays outside; the sandbox sees only the agent's socket, and git
  inside signs commits with that key, as it does outside. Without the key the
  sandbox has no agent. Anything inside can use the key wherever it is
  authorized, so authorize it on the forge and nowhere else.

  The key is loaded without a prompt, so it has no passphrase. To create it:

    ssh-keygen -t ed25519 -N "" -C forge:$(hostname) -f ~/.ssh/{SSH_KEY_NAME}

  then add its .pub on GitHub as both an authentication and a signing key,
  and authorize it for SSO in work orgs.

  {SANDBOX_SSH_SOCK_ENV}=<socket> exposes that agent instead, and
  {FULL_AGENT_ENV}=1 the login agent, handing over every key it
  holds. If the agent asked for is missing, the sandbox gets none rather than
  the next choice down.
"""
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run commands in a bubblewrap sandbox with filesystem isolation.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Drop into an interactive shell instead of running a command",
    )
    nix_file_arg = parser.add_argument(
        "--nix-file",
        metavar="PATH",
        help="Path to shell.nix relative to project dir "
        "(auto-detects shell.nix, nix/shell.nix, flake.nix, and default.nix)",
    )
    nix_file_arg.complete = {"zsh": "_files -g '*.nix'"}
    parser.add_argument(
        "--project-dir",
        metavar="DIR",
        dest="project_dir_flag",
        default=None,
        help="Project directory to sandbox (alternative to positional arg)",
    )
    project_dir_arg = parser.add_argument(
        "project_dir",
        nargs="?",
        default=None,
        help="Project directory to sandbox (default: cwd)",
    )
    project_dir_arg.complete = {"zsh": "_directories"}
    return parser


def parse_args():
    # Split at '--' to separate sandbox flags from the command to run
    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        sandbox_argv = argv[:sep]
        run_command = argv[sep + 1:]
    else:
        sandbox_argv = argv
        run_command = None

    args = create_parser().parse_args(sandbox_argv)

    # Resolve project directory: --project-dir flag > positional > cwd
    args.project_dir = args.project_dir_flag or args.project_dir or os.getcwd()

    # Resolve the command to run inside the sandbox
    if run_command is not None:
        args.launch_cmd = shlex.join(run_command) if run_command else None
    else:
        args.launch_cmd = DEFAULT_COMMANDS.get(get_script_name())

    return args


PR_SET_PDEATHSIG = 1  # <linux/prctl.h>


def die_with_launcher(sig):
    """A preexec_fn having the kernel send sig to the child when the launcher
    dies, however it dies: SIGKILL included, where no cleanup of its own runs.
    """
    prctl = ctypes.CDLL(None, use_errno=True).prctl
    launcher = os.getpid()

    def preexec():
        if prctl(PR_SET_PDEATHSIG, sig) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")
        if os.getppid() != launcher:  # it died before prctl took effect
            os._exit(1)

    return preexec


# The dirs under the sandbox's Podman dir that only Podman writes to.
PODMAN_DIRS = ("storage", "run", "tmp", "runtime")


def podman_dir(sandbox_tmp):
    return os.path.join(sandbox_tmp, "podman")


def podman_wrote(sandbox_tmp):
    """Whether Podman left anything in the sandbox's Podman dir: then it can
    hold files only Podman's user namespace can delete, whether or not the
    service ever came up. A podman killed during its first start leaves
    half-made storage behind."""
    base = podman_dir(sandbox_tmp)
    for sub in PODMAN_DIRS:
        try:
            with os.scandir(os.path.join(base, sub)) as entries:
                if any(entries):
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            return True  # what cannot even be looked into needs Podman most
    return False


def podman_store_args(sandbox_tmp):
    """podman's global options for the sandbox's own store.

    Every podman run against the store needs the same ones: libpod refuses a
    database made with different paths. The tmp dir is given explicitly
    because the service runs with an XDG_RUNTIME_DIR of its own, from which
    it would otherwise be derived.
    """
    base = podman_dir(sandbox_tmp)
    return [
        "--root", os.path.join(base, "storage"),
        "--runroot", os.path.join(base, "run"),
        "--tmpdir", os.path.join(base, "tmp"),
    ]


def podman_view_args(sandbox_tmp):
    """bwrap arguments the Podman service needs on top of the sandbox's view.

    Run in that view, the service has no systemd (no /run/systemd/system),
    so podman behaves as on a host without it: cgroupfs as cgroup manager,
    k8s-file container logs and a file of events, none of which need
    anything from outside the view.
    """
    return [
        # /run/user is not in the view; podman needs a runtime dir that exists.
        "--setenv", "XDG_RUNTIME_DIR", os.path.join(podman_dir(sandbox_tmp), "runtime"),
        "--unsetenv", "DBUS_SESSION_BUS_ADDRESS",
        # cgroup2 detection, and the sysfs crun bind-mounts into containers.
        "--ro-bind-try", "/sys", "/sys",
        # fuse-overlayfs, where the kernel's overlayfs cannot be used.
        "--dev-bind-try", "/dev/fuse", "/dev/fuse",
        # pasta, which gives containers their network.
        "--dev-bind-try", "/dev/net/tun", "/dev/net/tun",
    ]


# How long the Podman service gets to come up, in seconds.
PODMAN_START_TIMEOUT = 10

# How many of its last log lines a service that did not come up is quoted with.
PODMAN_LOG_LINES = 8


# How many saved Podman service logs are kept.
PODMAN_LOGS_KEPT = 10


def state_dir():
    """The launcher's per-user state dir: $XDG_STATE_HOME/claude-sandbox."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "claude-sandbox")


def keep_podman_log(log_path, label, directory=None, keep=PODMAN_LOGS_KEPT):
    """Copy a Podman service log out of the sandbox's temp dir, which goes
    when the sandbox does, to the state dir, mode 0600, and prune all but
    the newest `keep` such logs. Returns (path or None, warnings): nothing
    here raises, and a failure is a warning, since the launch goes on."""
    directory = directory or state_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    warnings = []
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        saved = os.path.join(directory, f"podman-{label}-{stamp}.log")
        fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, open(log_path, "rb") as src:
            shutil.copyfileobj(src, out)
    except OSError as e:
        return None, [f"could not keep the Podman log {log_path} in {directory}: {e}"]
    try:
        names = [n for n in os.listdir(directory) if n.startswith("podman-") and n.endswith(".log")]
        # Newest last: the timestamp is in the name, but modification time
        # breaks ties between logs of the same second.
        names.sort(key=lambda n: (os.stat(os.path.join(directory, n)).st_mtime, n))
    except OSError as e:
        return saved, [f"could not list {directory} to prune old Podman logs: {e}"]
    for name in names[:-keep] if keep else names:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError as e:
            warnings.append(f"could not prune the old Podman log {name}: {e}")
    return saved, warnings


def start_podman_service(sandbox_tmp, view_args, podman=None, bwrap=BWRAP, log_dir=None):
    """Start a per-sandbox rootless Podman service that sees what the sandbox sees.

    The service runs in a mount namespace set up with view_args, the bwrap
    arguments of the sandbox's own view: containers can bind-mount only what
    the sandbox could reach anyway, never ~/.ssh or the login agent, and a
    path a client in the sandbox names, /tmp included, is the same file for
    the service. It cannot run inside the sandbox itself: bwrap sets
    no_new_privs, under which newuidmap cannot map the subordinate uid range,
    and images that switch users would fail. So `podman unshare` sets up the
    user namespace with that range, and bwrap runs in it as its root, which
    keeps bwrap from creating a user namespace of its own and keeps the
    capabilities podman needs. podman unshare also sets
    _CONTAINERS_USERNS_CONFIGURED, by which the service knows it is in its
    namespace already and runs there rather than re-executing itself.

    Its storage is kept under sandbox_tmp, so it is isolated from every other
    sandbox and goes with the sandbox's temp dir (see remove_sandbox_tmp).

    Returns (proc, socket_path) on success, (None, None) if Podman is absent.
    The socket is placed inside sandbox_tmp, which is already bind-mounted as
    /tmp inside the sandbox, so it appears at /tmp/podman/podman.sock there —
    no extra --bind is needed.

    Lifetime: the launcher stops the service once the sandbox exits; should
    the launcher die without getting to, the kernel sends podman SIGTERM,
    which it passes on to the copy of itself it re-runs inside its user
    namespace. Either way that copy exits, and the service's bwrap dies with
    it (--die-with-parent). The service is the first process in a pid
    namespace of its own, which the view's --unshare-pid makes, so every
    container, conmon and pasta it started dies with it too.
    """
    podman = podman or shutil.which("podman")
    if not podman:
        return None, None

    base = podman_dir(sandbox_tmp)
    socket_path = os.path.join(base, "podman.sock")
    for sub in PODMAN_DIRS:
        os.makedirs(os.path.join(base, sub), mode=0o700)

    store = podman_store_args(sandbox_tmp)
    # Its log goes to a file rather than the terminal, and says why when the
    # service does not come up. At level info both podmans log the steps of
    # their start, which shows where a start that never ends got to.
    log_path = os.path.join(base, "service.log")
    log_level = "--log-level=info"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [podman, log_level, *store, "unshare",
             bwrap, "--die-with-parent", *view_args, *podman_view_args(sandbox_tmp), "--",
             podman, log_level, *store, "system", "service", "--time=0", f"unix://{socket_path}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            preexec_fn=die_with_launcher(signal.SIGTERM),
        )

    deadline = time.monotonic() + PODMAN_START_TIMEOUT
    while not os.path.exists(socket_path):
        if time.monotonic() > deadline:
            proc.terminate()
            proc.wait()
            failure = f"did not start within {PODMAN_START_TIMEOUT} s"
        elif proc.poll() is not None:
            failure = f"exited with status {proc.returncode}"
        else:
            time.sleep(0.05)
            continue
        with open(log_path, errors="replace") as log:
            tail = [line for line in log.read().splitlines() if line.strip()][-PODMAN_LOG_LINES:]
        saved, warnings = keep_podman_log(
            log_path, os.path.basename(sandbox_tmp), directory=log_dir
        )
        for warning in warnings:
            print(f"claude-sandbox: {warning}", file=sys.stderr)
        where = f"Its whole log is kept in {saved}" if saved else "Its log could not be kept"
        print(f"claude-sandbox: the Podman service {failure}; containers are unavailable. "
              f"{where}; it ends:", file=sys.stderr)
        for line in tail or ["(nothing)"]:
            print(f"  {line}", file=sys.stderr)
        return None, None

    return proc, socket_path


def remove_podman_storage(sandbox_tmp, podman):
    """Delete the storage of a sandbox's Podman, once the service is gone.

    Image layers hold files owned by the subordinate uids of Podman's user
    namespace, which the launcher cannot delete, so rm runs in that
    namespace, through `podman unshare`. No container is left to remove
    first: they all died with the service's pid namespace. Returns warnings.
    """
    done = subprocess.run(
        [podman, *podman_store_args(sandbox_tmp), "unshare", RM, "-rf", podman_dir(sandbox_tmp)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
    )
    if done.returncode != 0:
        return [f"podman unshare rm failed ({one_line(done.stderr)})"]
    return []


def remove_tree(path):
    """shutil.rmtree, returning what it could not delete as warnings.

    A directory of the user's own that denies the user, as overlayfs leaves
    its work dir (mode 000), is given back its permissions and deleted; so
    is a file in a directory the user may not write to. What belongs to
    someone else stays, and is reported.
    """
    failed = []
    retried = set()

    def retry(function, failed_path, error):
        if isinstance(error, FileNotFoundError):  # gone is what we want
            return
        if isinstance(error, PermissionError) and failed_path not in retried:
            retried.add(failed_path)
            try:
                parent = os.path.dirname(failed_path)
                if os.path.commonpath([parent, path]) == path:  # nothing above path
                    os.chmod(parent, os.stat(parent).st_mode | stat.S_IRWXU)
                if stat.S_ISDIR(os.lstat(failed_path).st_mode):
                    os.chmod(failed_path, stat.S_IRWXU)
                    shutil.rmtree(failed_path, onexc=retry)
                else:
                    os.unlink(failed_path)
                return
            except FileNotFoundError:
                return
            except OSError as e:
                error = e
        failed.append(f"{failed_path}: {error.strerror or error}")

    shutil.rmtree(path, onexc=retry)
    if not failed:
        return []
    return [f"could not delete {len(failed)} path(s) under {path}, first {failed[0]}"]


def remove_sandbox_tmp(sandbox_tmp, podman=None):
    """Delete a sandbox's temp dir, Podman's storage included. Returns warnings.

    Whatever Podman left goes through `podman unshare rm`, which can delete
    files of the namespace's other uids. That failing matters only if the
    plain delete after it cannot finish the job either, so it is reported
    along with what is left, not on its own: where Podman cannot run at all,
    as in a nested sandbox, its leftovers are the user's own.
    """
    podman_warnings = []
    podman = podman or shutil.which("podman")
    if podman and podman_wrote(sandbox_tmp):
        podman_warnings = remove_podman_storage(sandbox_tmp, podman)
    left = remove_tree(sandbox_tmp)
    return podman_warnings + left if left else []


# A file in each sandbox's temp dir, locked by its launcher for as long as
# the sandbox lives. A dir whose lock can be taken has lost its launcher.
OWNER_LOCK = ".claude-sandbox.lock"


def make_sandbox_tmp(prefix):
    """A new temp dir for one sandbox, and the lock file holding it.

    The lock is taken before the file gets its name, so a launcher clearing
    leftovers can never take it from a dir that is still being set up.
    """
    sandbox_tmp = tempfile.mkdtemp(prefix=prefix)
    staging = os.path.join(sandbox_tmp, OWNER_LOCK + ".new")
    lock = open(staging, "x")
    fcntl.flock(lock, fcntl.LOCK_EX)
    os.rename(staging, os.path.join(sandbox_tmp, OWNER_LOCK))
    return sandbox_tmp, lock


def remove_leftovers(prefix, tmp_root=None, podman=None):
    """Delete the temp dirs that launchers killed outright left behind.

    A launcher that is SIGKILLed cannot clean up; the kernel takes care of
    its sandbox and helpers, but its temp dir stays. Returns warnings.
    """
    tmp_root = tmp_root or tempfile.gettempdir()
    warnings = []
    for entry in os.scandir(tmp_root):
        if not entry.name.startswith(prefix) or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_uid != os.getuid():
                continue
            lock = open(os.path.join(entry.path, OWNER_LOCK))
        except FileNotFoundError:
            continue  # being set up, or another launcher just cleared it
        except OSError as e:
            warnings.append(f"cannot tell whether {entry.path} is in use: {e}")
            continue
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue  # its launcher is alive
            warnings += remove_sandbox_tmp(entry.path, podman)
    return warnings


# Environment variable naming a dedicated ssh-agent socket for the sandbox —
# one holding only keys the sandbox is meant to have.
SANDBOX_SSH_SOCK_ENV = "CLAUDE_SANDBOX_SSH_AUTH_SOCK"

# Escape hatch restoring the old behaviour of passing the login agent straight
# through. Deliberately awkward to reach for, and only "1" counts: a "0" meant
# as "off" must not hand over every key.
FULL_AGENT_ENV = "CLAUDE_SANDBOX_SSH_FULL_AGENT"

# Overrides the path of the key each sandbox's own agent holds, ~/.ssh/
# <SSH_KEY_NAME> otherwise. The key has no passphrase, so that the agent can
# be loaded unattended at every start.
FORGE_KEY_ENV = "CLAUDE_SANDBOX_FORGE_KEY"

# The forge agent's socket, by its name inside the sandbox's temp dir. The
# sandbox sees that dir as /tmp, so it reaches the agent at /tmp/<name>.
FORGE_AGENT_SOCK = "ssh-agent.sock"


def forge_key_path(env, home_dir):
    path = env.get(FORGE_KEY_ENV)
    if path:
        # Absolute, since ssh-add runs from the sandbox's temp dir.
        return os.path.abspath(os.path.expanduser(path))
    return os.path.join(home_dir, ".ssh", SSH_KEY_NAME)


def tilde_path(path, home_dir):
    """path as one would type it: ~ for the home dir, quoted if need be."""
    if path.startswith(home_dir + os.sep):
        return "~/" + shlex.quote(path[len(home_dir) + 1:])
    return shlex.quote(path)


def missing_forge_key_message(key_path, home_dir):
    key = tilde_path(key_path, home_dir)
    return (
        f"no forge key at {key}, so the sandbox gets no SSH agent. Add its .pub "
        "on GitHub as both an authentication and a signing key, and authorize "
        "it for SSO in work orgs, after creating it with: "
        f'ssh-keygen -t ed25519 -N "" -C forge:$(hostname) -f {key}'
    )


def choose_ssh_agent(env, home_dir, exists=os.path.exists):
    """Which SSH agent the sandbox gets, as (kind, path, warning).

    kind is "socket" to bind the existing agent socket at path, "forge" to
    start an agent for this sandbox holding only the key at path, or None for
    no agent at all.

    The login agent typically holds a key listed in authorized_keys on every
    host, so passing it through grants anything inside the sandbox silent SSH
    to the whole fleet — including hosts with passwordless sudo. A socket is a
    capability, not a secret: --ro-bind buys nothing, because *using* an agent
    is a write to the socket. So the login agent is never the default.

    Precedence: an explicit socket, then the escape hatch, then the forge key.
    An explicit choice that cannot be honoured gets no agent rather than the
    next one down, so a typo cannot quietly hand over a different agent, least
    of all the login one.
    """
    dedicated = env.get(SANDBOX_SSH_SOCK_ENV)
    if dedicated:
        if exists(dedicated):
            return "socket", dedicated, None
        return (
            None,
            None,
            f"{SANDBOX_SSH_SOCK_ENV} is set to {dedicated}, which does not exist; "
            "continuing with no SSH agent.",
        )

    if env.get(FULL_AGENT_ENV) == "1":
        login_sock = env.get("SSH_AUTH_SOCK")
        if login_sock and exists(login_sock):
            return (
                "socket",
                login_sock,
                f"{FULL_AGENT_ENV} is set: the login SSH agent is exposed to the "
                "sandbox, which can use every key it holds.",
            )
        return (
            None,
            None,
            f"{FULL_AGENT_ENV} is set, but there is no login SSH agent to expose; "
            "continuing with no SSH agent.",
        )

    key = forge_key_path(env, home_dir)
    if exists(key):
        return "forge", key, None
    return None, None, missing_forge_key_message(key, home_dir)


def one_line(text):
    return " ".join(text.split())


def start_forge_agent(key_path, sandbox_tmp, ssh_agent=SSH_AGENT, ssh_add=SSH_ADD, timeout=10):
    """Start an ssh-agent for one sandbox, holding only the key at key_path.

    Returns (proc, warning). proc is None when the agent could not be set up,
    and then nothing is left running.

    The agent runs on the host with its socket in sandbox_tmp, which the
    sandbox already sees as /tmp. ssh-add reads the private key out here; the
    key file never enters the sandbox. The socket is named relative to
    sandbox_tmp because its absolute path can outgrow the 108 bytes a Unix
    socket address holds once TMPDIR is itself deep.

    Lifetime: the launcher stops the agent when the sandbox exits, and the
    kernel stops it if the launcher dies without getting to — SIGKILL, a
    SIGHUP or SIGTERM taking the default action, any exit that skips cleanup.
    PR_SET_PDEATHSIG makes that SIGTERM rather than SIGKILL because ssh-agent
    removes its socket on SIGTERM. The agent gets its own session so that
    terminal keys (Ctrl-C, Ctrl-Z) reach the sandbox and the launcher as
    before, but never the agent directly.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}
    env["SSH_AUTH_SOCK"] = FORGE_AGENT_SOCK  # relative to cwd=sandbox_tmp
    # Never prompt: the key has no passphrase by design, and a prompt or an
    # askpass window popping up as the sandbox starts is worse than a clear
    # failure.
    env["SSH_ASKPASS_REQUIRE"] = "never"

    try:
        proc = subprocess.Popen(
            [ssh_agent, "-D", "-a", FORGE_AGENT_SOCK],
            cwd=sandbox_tmp,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=die_with_launcher(signal.SIGTERM),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"could not start ssh-agent ({e}); continuing with no SSH agent."

    # With -D, ssh-agent prints its environment lines once it is listening.
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready or not proc.stdout.readline():
        proc.terminate()
        proc.wait()
        err = one_line(proc.stderr.read().decode(errors="replace"))
        return None, f"ssh-agent did not start ({err}); continuing with no SSH agent."
    proc.stdout.close()
    proc.stderr.close()  # later log lines are dropped, not sent to the terminal

    added = subprocess.run(
        [ssh_add, "-q", key_path],
        cwd=sandbox_tmp,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        start_new_session=True,  # no controlling terminal to prompt on
    )
    if added.returncode != 0:
        proc.terminate()
        proc.wait()
        return None, (
            f"ssh-add {key_path} failed ({one_line(added.stderr)}); "
            "continuing with no SSH agent."
        )
    return proc, None


def forge_agent_args(key_path, home_dir, env, exists=os.path.exists):
    """bwrap arguments pointing the sandbox at its forge agent."""
    pub = key_path + ".pub"
    args = ["--setenv", "SSH_AUTH_SOCK", "/tmp/" + FORGE_AGENT_SOCK]

    # git signs through the agent with user.signingKey, which names the login
    # key; point it at the one key this agent holds. GIT_CONFIG_* is appended
    # to rather than overwritten, in case the caller already uses it.
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    args += [
        "--setenv", "GIT_CONFIG_COUNT", str(n + 1),
        "--setenv", f"GIT_CONFIG_KEY_{n}", "user.signingKey",
        "--setenv", f"GIT_CONFIG_VALUE_{n}", pub,
    ]

    # ~/.ssh/*.pub is bound anyway; a key kept elsewhere needs its .pub bound.
    if os.path.dirname(pub) != os.path.join(home_dir, ".ssh") and exists(pub):
        args += ["--ro-bind", pub, pub]
    return args


def ssh_agent_setup(home_dir, sandbox_tmp, env=None, exists=os.path.exists, start_agent=None):
    """bwrap arguments for the sandbox's SSH agent, the agent process to stop
    once the sandbox exits (or None), and a warning to print (or None).

    Without an agent SSH_AUTH_SOCK is actively unset rather than left
    inherited, because bwrap keeps the environment otherwise and a variable
    pointing at an unbound path fails as a baffling "Permission denied
    (publickey)" instead of an honest "no agent".
    """
    env = os.environ if env is None else env
    kind, path, warning = choose_ssh_agent(env, home_dir, exists)
    if kind == "socket":
        return ["--ro-bind", path, path, "--setenv", "SSH_AUTH_SOCK", path], None, warning
    if kind == "forge":
        proc, warning = (start_agent or start_forge_agent)(path, sandbox_tmp)
        if proc is not None:
            return forge_agent_args(path, home_dir, env, exists), proc, None
    return ["--unsetenv", "SSH_AUTH_SOCK"], None, warning


def git_dirs(worktree):
    """The git dirs a work tree uses: its own, which .git is or (a file, as in
    a linked worktree or a submodule) names, and the common dir it shares
    with the main worktree, if it names one."""
    dot_git = os.path.join(worktree, ".git")
    if os.path.isdir(dot_git):
        git_dir = dot_git
    elif os.path.isfile(dot_git):
        with open(dot_git) as f:
            line = f.readline().strip()
        if not line.startswith("gitdir:"):
            return []
        git_dir = os.path.normpath(os.path.join(worktree, line[len("gitdir:"):].strip()))
    else:
        return []
    dirs = [git_dir]
    commondir = os.path.join(git_dir, "commondir")
    if os.path.isfile(commondir):
        with open(commondir) as f:
            dirs.append(os.path.normpath(os.path.join(git_dir, f.read().strip())))
    return dirs


def jj_repo_dir(worktree):
    """The jj repo dir a work tree uses: its .jj/repo, or for a workspace, the
    dir that its .jj/repo file names. None without one."""
    repo = os.path.join(worktree, ".jj", "repo")
    if os.path.isdir(repo):
        return repo
    if os.path.isfile(repo):
        with open(repo) as f:
            return os.path.normpath(os.path.join(worktree, ".jj", f.read().strip()))
    return None


def pin_jj_repo_config(worktree, jj_repo):
    """Make jj record where the repository's config lives, if it has not yet.

    jj keeps that config outside the repository, under ~/.config/jj, and
    finds it through .jj/repo/config-id. Without that file it takes a
    .jj/repo/config.toml for config written before the move and moves it
    there: a file the sandbox planted would be the repository's config on
    the next jj run on the host. jj writes config-id only once it needs the
    location, so it is asked for it here. Returns a warning, or None.
    """
    if os.path.exists(os.path.join(jj_repo, "config-id")):
        return None
    try:
        subprocess.run(
            ["jj", "--repository", worktree, "--ignore-working-copy", "config", "path", "--repo"],
            # From inside the work tree: jj reads the config of the repository
            # it is started in, too, and that may be another one.
            capture_output=True, text=True, check=True, cwd=worktree,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        reason = one_line(getattr(e, "stderr", None) or str(e))
        return (
            f"could not have jj record where {worktree}'s config lives ({reason}); "
            "the sandbox can plant a config there that jj on the host would run."
        )
    return None


def build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile, shell_path):
    claude_config_dir = os.environ.get(
        "CLAUDE_CONFIG_DIR",
        os.path.join(home_dir, ".config", "claude"),
    )
    claude_dot_dir = os.path.join(home_dir, ".claude")

    # Project directory (read-write). Bound after the system paths and /tmp
    # below, which would otherwise cover a project kept under one of them.
    project_args = ["--bind", project_dir, project_dir]

    # If inside a .workspace/ or .worktrees/ folder, expose parent project's
    # .jj and .git so that worktree gitlinks (and jj workspace pointers) can
    # be followed inside the sandbox.
    parent_dir = os.path.dirname(project_dir)
    repo_root = None
    if os.path.basename(parent_dir) in (".workspace", ".worktrees"):
        repo_root = os.path.dirname(parent_dir)
        jj_dir = os.path.join(repo_root, ".jj")
        git_dir = os.path.join(repo_root, ".git")
        if os.path.isdir(jj_dir):
            project_args += ["--bind", jj_dir, jj_dir]
        if os.path.isdir(git_dir):
            project_args += ["--bind", git_dir, git_dir]

    args = [
        # Empty HOME
        "--tmpfs", home_dir,
        # /bin/sh (needed by programs like Claude Code that spawn /bin/sh)
        "--symlink", BASH, "/bin/sh",
        # Ensure tools inside the sandbox see a usable shell path
        "--setenv", "SHELL", shell_path,
        # Python (needed by hooks and tools invoked inside the sandbox)
        "--setenv", "PATH", PYTHON3_BIN_DIR + ":" + os.environ.get("PATH", ""),
        # Nix store and var (read-only)
        "--ro-bind", "/nix", "/nix",
        # System config (DNS, passwd, nix profiles)
        "--ro-bind", "/etc", "/etc",
        # Shadow SSH system drop-ins — they appear owned by 'nobody' inside the
        # sandbox (bwrap user namespace maps root→nobody) and SSH refuses them.
        # The base /etc/ssh/ssh_config is kept; only the .d/ snippet dir is emptied.
        # Only applied when the directory exists (absent on NixOS).
        *(["--tmpfs", "/etc/ssh/ssh_config.d"] if os.path.isdir("/etc/ssh/ssh_config.d") else []),
        # Empty /run — avoids exposing docker socket, mounted media, etc.
        "--tmpfs", "/run",
    ]

    # Selective /run binds — only what's actually needed:
    #   systemd/resolve: resolv.conf on systemd distros (Fedora etc.) is a symlink here
    #   current-system, booted-system, wrappers: NixOS runtime paths (no-op on non-NixOS)
    for run_path in ["/run/systemd/resolve", "/run/current-system", "/run/booted-system", "/run/wrappers"]:
        if os.path.islink(run_path):
            args += ["--symlink", os.readlink(run_path), run_path]
        elif os.path.isdir(run_path):
            args += ["--ro-bind", run_path, run_path]

    # Non-NixOS system paths (absent or /nix-backed on NixOS, real on distros).
    # Symlinked compat paths (e.g. /lib64 -> usr/lib64 on Fedora usrmerge) are
    # recreated as --symlink so ELF interpreter paths resolve correctly.
    for sys_path in ["/usr", "/opt", "/lib", "/lib64", "/lib32", "/sbin"]:
        if os.path.islink(sys_path):
            args += ["--symlink", os.readlink(sys_path), sys_path]
        elif os.path.isdir(sys_path):
            args += ["--ro-bind", sys_path, sys_path]

    args += [
        # Kernel filesystems
        "--proc", "/proc",
        "--dev", "/dev",
        # Per-project tmp (isolated at /tmp for general use)
        "--bind", sandbox_tmp, "/tmp",
        # Also at its real host path so the nix daemon (outside the sandbox)
        # can access temp files created by nix-shell inside the sandbox
        "--bind", sandbox_tmp, sandbox_tmp,
        "--setenv", "TMPDIR", sandbox_tmp,
        # Zsh config (read-only, symlink to nix store)
        "--ro-bind", os.path.join(home_dir, ".zshrc"),
                     os.path.join(home_dir, ".zshrc"),
        # Seed history with launch command (ephemeral, on sandbox tmp)
        "--bind", histfile, os.path.join(home_dir, ".zsh_history"),
        "--setenv", "HISTFILE", os.path.join(home_dir, ".zsh_history"),
        # Git config (read-only)
        "--ro-bind", os.path.join(home_dir, ".config", "git"),
                     os.path.join(home_dir, ".config", "git"),
        # Jujutsu config (read-only, symlink resolves via /nix bind)
        "--ro-bind", os.path.join(home_dir, ".config", "jj"),
                     os.path.join(home_dir, ".config", "jj"),
        # User nix profile (claude, etc. live here)
        "--ro-bind", os.path.join(home_dir, ".nix-profile"),
                     os.path.join(home_dir, ".nix-profile"),
        # User nix profile symlinks
        "--ro-bind", os.path.join(home_dir, ".local", "state", "nix"),
                     os.path.join(home_dir, ".local", "state", "nix"),
        # Network: unrestricted
        "--share-net",
        # PID namespace: isolated
        "--unshare-pid",
        # Work inside the project directory
        "--chdir", project_dir,
    ]
    args += project_args

    # Claude config and auth (read-write)
    if os.path.isdir(claude_config_dir):
        args += ["--bind", claude_config_dir, claude_config_dir]
    if os.path.isdir(claude_dot_dir):
        args += ["--bind", claude_dot_dir, claude_dot_dir]

    legacy_credentials = os.path.join(claude_dot_dir, ".credentials.json")
    config_credentials = os.path.join(claude_config_dir, ".credentials.json")
    if os.path.isfile(legacy_credentials) and not os.path.exists(config_credentials):
        args += ["--bind", legacy_credentials, config_credentials]

    # No D-Bus session bus: through it the sandbox could have the user's
    # service manager run anything outside it (systemd-run --user), and read
    # every secret in the keyring. gh's token comes in as GH_TOKEN instead
    # (see gh_token).

    # c8ctl config (read-only): plugins and profiles for preprod debugging
    c8ctl_plugins_dir = os.path.join(home_dir, ".config", "c8ctl", "plugins")
    c8ctl_plugins_json = os.path.join(home_dir, ".config", "c8ctl", "plugins.json")
    c8ctl_profiles_json = os.path.join(home_dir, ".config", "c8ctl", "profiles.json")
    if os.path.isdir(c8ctl_plugins_dir):
        args += ["--ro-bind", c8ctl_plugins_dir, c8ctl_plugins_dir]
    if os.path.isfile(c8ctl_plugins_json):
        args += ["--ro-bind", c8ctl_plugins_json, c8ctl_plugins_json]
    if os.path.isfile(c8ctl_profiles_json):
        args += ["--ro-bind", c8ctl_profiles_json, c8ctl_profiles_json]

    # GitHub CLI config (read-only: its token comes in as GH_TOKEN, so gh has
    # nothing to write, and the config names commands gh runs, pager,
    # editor and browser among them)
    gh_config = os.path.join(home_dir, ".config", "gh")
    if os.path.isdir(gh_config):
        args += ["--ro-bind", gh_config, gh_config]

    # SSH config (read-only, no private keys)
    ssh_known_hosts = os.path.join(home_dir, ".ssh", "known_hosts")
    if os.path.isfile(ssh_known_hosts):
        args += ["--ro-bind", ssh_known_hosts, ssh_known_hosts]

    ssh_config = os.path.join(home_dir, ".ssh", "config")
    if os.path.isfile(ssh_config):
        args += ["--ro-bind", ssh_config, ssh_config]

    # SSH public keys (read-only) — required for git/jj SSH commit signing,
    # which references the pub key path via user.signingkey. Private keys are
    # never bound; signing goes through the SSH agent socket.
    for pub_key in glob.glob(os.path.join(home_dir, ".ssh", "*.pub")):
        args += ["--ro-bind", pub_key, pub_key]

    # Allowed signers file (read-only) — used for `git log --show-signature`.
    ssh_allowed_signers = os.path.join(home_dir, ".ssh", "allowed_signers")
    if os.path.isfile(ssh_allowed_signers):
        args += ["--ro-bind", ssh_allowed_signers, ssh_allowed_signers]

    # Maven settings (read-only — repository credentials and mirrors)
    m2_settings = os.path.join(home_dir, ".m2", "settings.xml")
    if os.path.isfile(m2_settings):
        args += ["--ro-bind", m2_settings, m2_settings]

    # Read-only over the read-write binds above: what git and Claude Code run
    # on the host later, with no review step, so the sandbox cannot plant
    # code there. A git config can name commands (core.fsmonitor,
    # core.hooksPath, core.pager, aliases, filters) and hooks are commands;
    # Claude Code's settings.json defines its hooks, which run the scripts
    # in hooks/. A hooks dir that does not exist is created first, since the
    # sandbox could otherwise create it with hooks of its own in it.
    #
    # jj's repository config is under ~/.config/jj, read-only already, but
    # what points jj at it is .jj/repo/config-id (see pin_jj_repo_config). A
    # config.toml that is still a file of its own, from before jj moved repo
    # config out, is the config itself.
    #
    # jj fetches and pushes by running git on its backing git repository:
    # the colocated .git, or one inside .jj/repo/store, whose config and
    # hooks git runs the same way.
    protected = []

    def protect_git_dir(git_dir):
        if os.path.isdir(git_dir):
            os.makedirs(os.path.join(git_dir, "hooks"), exist_ok=True)
        protected.extend([os.path.join(git_dir, "config"), os.path.join(git_dir, "hooks")])

    for worktree in [project_dir] + ([repo_root] if repo_root else []):
        jj_repo = jj_repo_dir(worktree)
        if jj_repo:
            warning = pin_jj_repo_config(worktree, jj_repo)
            if warning:
                print(f"claude-sandbox: {warning}", file=sys.stderr)
            protected.append(os.path.join(jj_repo, "config-id"))
            if not os.path.islink(os.path.join(jj_repo, "config.toml")):
                protected.append(os.path.join(jj_repo, "config.toml"))
            git_target = os.path.join(jj_repo, "store", "git_target")
            if os.path.isfile(git_target):
                with open(git_target) as f:
                    protect_git_dir(os.path.normpath(
                        os.path.join(jj_repo, "store", f.read().strip())
                    ))
        dirs = git_dirs(worktree)
        if not dirs:
            continue
        # A linked worktree keeps only config.worktree in its own git dir;
        # config and hooks are the common dir's.
        protected.append(os.path.join(dirs[0], "config.worktree"))
        protect_git_dir(dirs[-1])
    for claude_dir in (claude_config_dir, claude_dot_dir):
        protected += [os.path.join(claude_dir, "settings.json"), os.path.join(claude_dir, "hooks")]
    for path in dict.fromkeys(protected):
        if os.path.exists(path):
            args += ["--ro-bind", path, path]

    return args


# How a resolved nix file has to be invoked.
MODE_SHELL = "shell"  # nix-shell <file>
MODE_ATTR = "attr"  # nix-shell <file> -A shell
MODE_FLAKE = "flake"  # nix develop <dir>


def resolve_nix_file(project_dir, nix_file_arg):
    """Resolve nix file: explicit flag > shell.nix > nix/shell.nix > flake.nix > default.nix > None.

    Returns (path, mode) where mode is one of MODE_SHELL, MODE_ATTR or
    MODE_FLAKE. A flake.nix evaluates to a plain attrset rather than a
    derivation, so its dev shell is only reachable via ``nix develop``.
    """
    if nix_file_arg:
        resolved = os.path.join(project_dir, nix_file_arg)
        if not os.path.isfile(resolved):
            print(f"Error: nix file not found: {resolved}", file=sys.stderr)
            sys.exit(1)
        if os.path.basename(resolved) == "flake.nix":
            return resolved, MODE_FLAKE
        return resolved, MODE_SHELL

    shell_nix = os.path.join(project_dir, "shell.nix")
    if os.path.isfile(shell_nix):
        return shell_nix, MODE_SHELL

    nix_shell_nix = os.path.join(project_dir, "nix", "shell.nix")
    if os.path.isfile(nix_shell_nix):
        return nix_shell_nix, MODE_SHELL

    flake_nix = os.path.join(project_dir, "flake.nix")
    if os.path.isfile(flake_nix):
        return flake_nix, MODE_FLAKE

    default_nix = os.path.join(project_dir, "default.nix")
    if os.path.isfile(default_nix):
        return default_nix, MODE_ATTR

    return None, MODE_SHELL


def path_is_visible_in_sandbox(path, home_dir):
    real = os.path.realpath(path)
    profile_dir = os.path.join(home_dir, ".nix-profile")
    return (
        path == "/bin/sh"
        or path.startswith(profile_dir + os.sep)
        or real.startswith("/nix/store/")
    )


def resolve_shell(home_dir):
    shell = os.environ.get("SHELL")
    candidates = []
    if shell:
        candidates.append(shell)
        shell_name = os.path.basename(shell)
        candidates.append(os.path.join(home_dir, ".nix-profile", "bin", shell_name))

    candidates += [
        os.path.join(home_dir, ".nix-profile", "bin", "zsh"),
        os.path.join(home_dir, ".nix-profile", "bin", "bash"),
        BASH,
    ]

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.exists(candidate) and path_is_visible_in_sandbox(candidate, home_dir):
            return candidate

    return BASH


def launch_command(launch_cmd, project_dir, nix_file_arg):
    """What bwrap runs for launch_cmd: inside the project's dev shell, if any."""
    nix_file, mode = resolve_nix_file(project_dir, nix_file_arg)
    if nix_file and mode == MODE_FLAKE:
        return [
            NIX, "develop",
            "--extra-experimental-features", "nix-command flakes",
            os.path.dirname(nix_file),
            "--command", BASH, "-c", launch_cmd,
        ]
    if nix_file:
        nix_args = [NIX_SHELL, nix_file]
        if mode == MODE_ATTR:
            nix_args += ["-A", "shell"]
        return nix_args + ["--run", launch_cmd]
    return [BASH, "-c", launch_cmd]


# Signals that stop the launcher. It does not die of them on the spot, which
# would leave its cleanup undone, but takes the sandbox down, cleans up after
# it and then exits 128+N, as the shell reports a command killed by signal N.
STOP_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)


class Stopped(Exception):
    """The launcher received one of STOP_SIGNALS."""

    def __init__(self, signum):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def stop_on_signals():
    def stop(signum, _frame):
        ignore_stop_signals()  # once is enough
        raise Stopped(signum)

    for sig in STOP_SIGNALS:
        signal.signal(sig, stop)


def ignore_stop_signals():
    """Let no signal cut the cleanup short. A handler that does nothing rather
    than SIG_IGN, which the cleanup's own child processes would inherit."""
    for sig in STOP_SIGNALS:
        signal.signal(sig, lambda *_: None)


def gh_token(env=None, gh=None, timeout=60):
    """gh's GitHub token for the sandbox, as (token, warning).

    gh keeps its token in the desktop keyring, which it reaches over the
    D-Bus session bus, and the sandbox does not get that bus. So gh is asked
    for its token out here, and only the token goes in, as GH_TOKEN. The
    timeout leaves time to unlock a locked keyring.

    token is None when there is nothing to pass: GH_TOKEN is set already, and
    the sandbox inherits it; gh is not installed; or gh has no token, which
    comes with a warning.
    """
    env = os.environ if env is None else env
    if env.get("GH_TOKEN"):
        return None, None
    gh = gh or shutil.which("gh", path=env.get("PATH"))
    if not gh:
        return None, None
    try:
        result = subprocess.run(
            [gh, "auth", "token"], env=env, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"gh in the sandbox is not logged in: `gh auth token` failed: {e}"
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        reason = one_line(result.stderr) or f"exit status {result.returncode}, no token"
        return None, f"gh in the sandbox is not logged in: `gh auth token` failed: {reason}"
    return token, None


def run_sandbox(bwrap_args, inner_cmd, bwrap=BWRAP, env=None):
    """Run inner_cmd under bwrap and wait for it; returns bwrap's returncode.

    env is bwrap's environment, which the sandbox inherits; None is the
    launcher's own. Secrets go there rather than into a --setenv: every user
    on the host can read bwrap's command line, but only its own user its
    environment.

    Through subprocess rather than a bare fork and exec, because Python
    ignores SIGPIPE and SIGXFSZ and an ignored signal survives exec: the
    sandbox would inherit that, and `yes | head -1` inside would fail with
    "Broken pipe" instead of ending quietly. subprocess restores them.

    The sandbox never outlives the launcher, which has to clean up after it.
    bwrap is killed the moment the launcher dies, and --die-with-parent passes
    that on: the sandbox's pid 1 dies with bwrap, and the kernel kills
    everything left in a pid namespace whose pid 1 is gone. That includes
    processes the command left running in the background, which would
    otherwise carry on after bwrap returns, against a temp dir and helpers
    that are about to go. When the wait is cut short (Stopped), bwrap is
    killed and reaped before the launcher moves on to clean up.
    """
    sandbox = subprocess.Popen(
        [bwrap, "--die-with-parent"] + bwrap_args + ["--"] + inner_cmd,
        env=env,
        # bwrap sets this itself only once the sandbox is already set up.
        preexec_fn=die_with_launcher(signal.SIGKILL),
    )
    try:
        return sandbox.wait()
    except BaseException:
        sandbox.kill()
        sandbox.wait()
        raise


def exit_status(returncode):
    """The launcher's exit status for a sandbox that ended with returncode.

    The shell's convention: the exit code, or 128+N for death by signal N.
    subprocess reports that death as -N, which sys.exit would turn into 256-N.
    """
    return 128 - returncode if returncode < 0 else returncode


def main():
    args = parse_args()

    project_dir = os.path.realpath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if not args.launch_cmd and not args.shell:
        script = get_script_name()
        print(
            f"Error: no command specified. Provide a command after '--':\n"
            f"  {script} --project-dir /path -- command arg1 arg2",
            file=sys.stderr,
        )
        sys.exit(1)

    home_dir = os.environ["HOME"]
    # The project is bound read-write over the empty home. Were it the home
    # itself or a directory above it, the sandbox would get the whole home,
    # the private keys in ~/.ssh included.
    if os.path.commonpath([project_dir, os.path.realpath(home_dir)]) == project_dir:
        print(
            f"Error: {project_dir} holds your home directory, which the sandbox "
            "must not get; sandbox a project inside it instead",
            file=sys.stderr,
        )
        sys.exit(1)

    shell_path = resolve_shell(home_dir)

    # Settled before anything is started, so that a mistake ends the launch
    # with nothing to clean up.
    if args.shell:
        inner_cmd = [shell_path]
        banner = f"Entering sandboxed shell in {project_dir}"
    else:
        inner_cmd = launch_command(args.launch_cmd, project_dir, args.nix_file)
        banner = f"Starting sandboxed {args.launch_cmd.split()[0]} in {project_dir}"

    script_name = get_script_name()
    for warning in remove_leftovers(f"{script_name}-"):
        print(f"claude-sandbox: {warning}", file=sys.stderr)
    project_name = os.path.basename(project_dir)
    sandbox_tmp, lock = make_sandbox_tmp(f"{script_name}-{project_name}-")

    # The host-side processes serving this sandbox (its SSH agent, Podman),
    # stopped once it exits.
    helpers = []
    podman_proc = None
    stop_on_signals()
    try:
        # Seed a history file with the launch command
        histfile = os.path.join(sandbox_tmp, ".zsh_history")
        with open(histfile, "w") as f:
            f.write((args.launch_cmd or "") + "\n")

        # The sandbox's view of the file system, which its Podman service gets too.
        view_args = build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile, shell_path)
        bwrap_args = list(view_args)

        # SSH agent: never the login one by default. See choose_ssh_agent.
        ssh_args, agent_proc, ssh_warning = ssh_agent_setup(home_dir, sandbox_tmp)
        if ssh_warning:
            print(f"claude-sandbox: {ssh_warning}", file=sys.stderr)
        bwrap_args += ssh_args
        if agent_proc is not None:
            helpers.append(agent_proc)

        # Per-sandbox Podman: isolated storage root and socket under sandbox_tmp.
        # The socket ends up at sandbox_tmp/podman/podman.sock, which is visible
        # inside the sandbox as /tmp/podman/podman.sock via the existing /tmp bind.
        # Testcontainers: TESTCONTAINERS_RYUK_DISABLED is always set (Ryuk requests a
        # privileged container which fails in rootless Podman; JVM hooks still clean up).
        podman_proc, podman_socket = start_podman_service(sandbox_tmp, view_args)
        if podman_proc is not None:
            helpers.append(podman_proc)
        if podman_socket:
            # Inside the sandbox, sandbox_tmp == /tmp, so rewrite the path.
            inner_socket = "/tmp" + podman_socket[len(sandbox_tmp):]
            bwrap_args += [
                "--setenv", "DOCKER_HOST", f"unix://{inner_socket}",
                "--setenv", "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", inner_socket,
                "--setenv", "TESTCONTAINERS_RYUK_DISABLED", "true",
            ]

        sandbox_env = None
        token, gh_warning = gh_token()
        if gh_warning:
            print(f"claude-sandbox: {gh_warning}", file=sys.stderr)
        if token:
            sandbox_env = dict(os.environ, GH_TOKEN=token)

        # On stderr: stdout is the sandboxed command's alone, and anything
        # printed to it here would be buffered past the command's output.
        print(banner, file=sys.stderr)
        status = exit_status(run_sandbox(bwrap_args, inner_cmd, env=sandbox_env))
    except Stopped as stopped:
        status = 128 + stopped.signum
    finally:
        ignore_stop_signals()
        # A service that died on its own while the sandbox ran: keep its log.
        if podman_proc is not None and podman_proc.poll() not in (None, 0):
            saved, warnings = keep_podman_log(
                os.path.join(podman_dir(sandbox_tmp), "service.log"),
                os.path.basename(sandbox_tmp),
            )
            for warning in warnings:
                print(f"claude-sandbox: {warning}", file=sys.stderr)
            print(f"claude-sandbox: the Podman service exited with status "
                  f"{podman_proc.returncode} during the session; "
                  + (f"its log is kept in {saved}" if saved else "its log could not be kept"),
                  file=sys.stderr)
        for proc in helpers:
            proc.terminate()
            proc.wait()
        for warning in remove_sandbox_tmp(sandbox_tmp):
            print(f"claude-sandbox: {warning}", file=sys.stderr)
        lock.close()

    sys.exit(status)


if __name__ == "__main__":
    main()
