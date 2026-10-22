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
  The sandbox never gets the login SSH agent by default. That agent usually
  holds a key that authorized_keys accepts on every host, so passing it through
  would grant anything inside silent SSH to the whole fleet.

  Instead, each sandbox gets an agent of its own, started with it and stopped
  with it, holding only the forge key ~/.ssh/forge_ed25519 (override the path
  with {FORGE_KEY_ENV}). The key file stays outside; the sandbox
  sees only the agent's socket, and git inside signs commits with that key.
  Without the key the sandbox has no agent.

  One-time setup per machine:

    ssh-keygen -t ed25519 -N "" -C forge:$(hostname) -f ~/.ssh/forge_ed25519

  then add ~/.ssh/forge_ed25519.pub on GitHub as both an authentication and a
  signing key, and authorize it for SSO in work orgs. Authorize it on no host.

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


# Written into the sandbox's Podman dir once the service is up. From then on
# the storage can hold files only Podman's user namespace can delete, and
# containers that keep running after the service stops.
PODMAN_SERVED = "served"


def podman_paths(sandbox_tmp):
    """The Podman dir under sandbox_tmp, and its storage and run roots."""
    podman_dir = os.path.join(sandbox_tmp, "podman")
    return podman_dir, os.path.join(podman_dir, "storage"), os.path.join(podman_dir, "run")


def start_podman_service(sandbox_tmp, podman=None):
    """Start a per-sandbox rootless Podman service.

    Its storage is kept under sandbox_tmp, so it is isolated from every other
    sandbox and goes with the sandbox's temp dir (see remove_sandbox_tmp).

    Returns (proc, socket_path) on success, (None, None) if Podman is absent.
    The socket is placed inside sandbox_tmp, which is already bind-mounted as
    /tmp inside the sandbox, so it appears at /tmp/podman/podman.sock there —
    no extra --bind is needed.

    The launcher stops the service once the sandbox exits; should the launcher
    die without getting to, the kernel sends the service SIGTERM, which podman
    passes on to the copy of itself it re-runs inside its user namespace.
    """
    podman = podman or shutil.which("podman")
    if not podman:
        return None, None

    podman_dir, storage_root, run_root = podman_paths(sandbox_tmp)
    socket_path = os.path.join(podman_dir, "podman.sock")
    os.makedirs(run_root)
    os.makedirs(storage_root)

    proc = subprocess.Popen(
        [podman, "--root", storage_root, "--runroot", run_root,
         "system", "service", "--time=0", f"unix://{socket_path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=die_with_launcher(signal.SIGTERM),
    )

    deadline = time.monotonic() + 10
    while not os.path.exists(socket_path):
        if time.monotonic() > deadline:
            proc.terminate()
            proc.wait()
            print("Warning: Podman service did not start in time; containers unavailable",
                  file=sys.stderr)
            return None, None
        if proc.poll() is not None:
            print("Warning: Podman service exited early; containers unavailable",
                  file=sys.stderr)
            return None, None
        time.sleep(0.05)

    open(os.path.join(podman_dir, PODMAN_SERVED), "x").close()
    return proc, socket_path


def remove_podman_storage(sandbox_tmp, podman):
    """Remove the containers and storage of a sandbox's Podman, once stopped.

    Containers started through the service outlive it, so they are removed
    first, which also unmounts their file systems. Image layers hold files
    owned by the subordinate uids of Podman's user namespace, which the
    launcher cannot delete, so rm runs in that namespace, through `podman
    unshare`. Returns warnings.
    """
    _, storage_root, run_root = podman_paths(sandbox_tmp)
    podman_cmd = [podman, "--root", storage_root, "--runroot", run_root]
    warnings = []
    for args in (
        ["rm", "--all", "--force", "--time", "0"],
        ["unshare", RM, "-rf", storage_root, run_root],
    ):
        done = subprocess.run(
            podman_cmd + args, stdin=subprocess.DEVNULL, capture_output=True, text=True
        )
        if done.returncode != 0:
            warnings.append(f"podman {args[0]} failed ({one_line(done.stderr)})")
    return warnings


def remove_tree(path):
    """shutil.rmtree, returning what it could not delete as warnings."""
    failed = []

    def note(_function, failed_path, error):
        if not isinstance(error, FileNotFoundError):  # gone is what we want
            failed.append(f"{failed_path}: {error.strerror or error}")

    shutil.rmtree(path, onexc=note)
    if not failed:
        return []
    return [f"could not delete {len(failed)} path(s) under {path}, first {failed[0]}"]


def remove_sandbox_tmp(sandbox_tmp, podman=None):
    """Delete a sandbox's temp dir, Podman's storage included. Returns warnings."""
    warnings = []
    podman_dir, _, _ = podman_paths(sandbox_tmp)
    podman = podman or shutil.which("podman")
    if podman and os.path.exists(os.path.join(podman_dir, PODMAN_SERVED)):
        warnings += remove_podman_storage(sandbox_tmp, podman)
    return warnings + remove_tree(sandbox_tmp)


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


def crossbridge_socket_root():
    """Mirror crossbridge's socket-root precedence:
    $CROSSBRIDGE_SOCKET_ROOT > $XDG_RUNTIME_DIR/crossbridge > /run/crossbridge.
    """
    root = os.environ.get("CROSSBRIDGE_SOCKET_ROOT")
    if root:
        return root
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "crossbridge")
    return "/run/crossbridge"


def crossbridge_own_slug(project_dir):
    """Derive the repo's crossbridge slug the way crossbridge-client does:
    $CROSSBRIDGE_OWN_SLUG, else the last path segment of the origin remote URL
    with an optional .git suffix stripped. Returns None if it can't be found.

    Must stay in lockstep with crossbridge-client's slug.rs — if the launcher
    and the in-sandbox client disagree, the bound directory won't match the
    path the client looks up.
    """
    slug = os.environ.get("CROSSBRIDGE_OWN_SLUG", "").strip()
    if slug:
        return slug

    url = None
    if os.path.isdir(os.path.join(project_dir, ".jj")):
        try:
            out = subprocess.run(
                ["jj", "--repository", project_dir, "git", "remote", "list"],
                capture_output=True, text=True, check=True,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "origin":
                    url = parts[1]
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    if not url:
        try:
            url = subprocess.run(
                ["git", "-C", project_dir, "remote", "get-url", "origin"],
                capture_output=True, text=True, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None

    # Last segment after the final '/' or ':', '.git' suffix stripped.
    last = url.strip().replace(":", "/").rsplit("/", 1)[-1]
    last = last[:-4] if last.endswith(".git") else last
    return last or None


# Environment variable naming a dedicated ssh-agent socket for the sandbox —
# one holding only keys the sandbox is meant to have.
SANDBOX_SSH_SOCK_ENV = "CLAUDE_SANDBOX_SSH_AUTH_SOCK"

# Escape hatch restoring the old behaviour of passing the login agent straight
# through. Deliberately awkward to reach for, and only "1" counts: a "0" meant
# as "off" must not hand over every key.
FULL_AGENT_ENV = "CLAUDE_SANDBOX_SSH_FULL_AGENT"

# The key each sandbox's own agent holds: one per machine, authorised on the
# forge and on no host, and without a passphrase so the agent can be loaded
# unattended at every start.
FORGE_KEY_ENV = "CLAUDE_SANDBOX_FORGE_KEY"

# The forge agent's socket, by its name inside the sandbox's temp dir. The
# sandbox sees that dir as /tmp, so it reaches the agent at /tmp/<name>.
FORGE_AGENT_SOCK = "ssh-agent.sock"


def forge_key_path(env, home_dir):
    path = env.get(FORGE_KEY_ENV)
    if path:
        # Absolute, since ssh-add runs from the sandbox's temp dir.
        return os.path.abspath(os.path.expanduser(path))
    return os.path.join(home_dir, ".ssh", "forge_ed25519")


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
        return None, None, None

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

    uid = os.getuid()

    # D-Bus user socket (needed for KWallet/keyring access, e.g. gh credentials)
    dbus_socket = f"/run/user/{uid}/bus"
    if os.path.exists(dbus_socket):
        args += ["--bind", dbus_socket, dbus_socket]

    # Crossbridge: expose only this repo's own per-peer socket directory
    # (<root>/<own-slug>/), read-only. The crossbridge server and orchestrator
    # run outside the sandbox; the sandboxed agent is a client only, so a
    # read-only bind is enough — connect() does not write the filesystem.
    # Binding just this repo's own-slug dir keeps every other repo's sockets
    # hidden, and the isdir() guard doubles as a readiness gate: the directory
    # exists only once the orchestrator and server for this repo are up.
    crossbridge_slug = crossbridge_own_slug(project_dir)
    if crossbridge_slug:
        crossbridge_dir = os.path.join(crossbridge_socket_root(), crossbridge_slug)
        if os.path.isdir(crossbridge_dir):
            args += ["--ro-bind", crossbridge_dir, crossbridge_dir]

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

    # GitHub CLI config and auth (read-write, gh updates token expiry)
    gh_config = os.path.join(home_dir, ".config", "gh")
    if os.path.isdir(gh_config):
        args += ["--bind", gh_config, gh_config]

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


def run_sandbox(bwrap_args, inner_cmd, bwrap=BWRAP):
    """Run inner_cmd under bwrap and wait for it; returns bwrap's returncode.

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
    stop_on_signals()
    try:
        # Seed a history file with the launch command
        histfile = os.path.join(sandbox_tmp, ".zsh_history")
        with open(histfile, "w") as f:
            f.write((args.launch_cmd or "") + "\n")

        bwrap_args = build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile, shell_path)

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
        podman_proc, podman_socket = start_podman_service(sandbox_tmp)
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

        print(banner)
        status = exit_status(run_sandbox(bwrap_args, inner_cmd))
    except Stopped as stopped:
        status = 128 + stopped.signum
    finally:
        ignore_stop_signals()
        for proc in helpers:
            proc.terminate()
            proc.wait()
        for warning in remove_sandbox_tmp(sandbox_tmp):
            print(f"claude-sandbox: {warning}", file=sys.stderr)
        lock.close()

    sys.exit(status)


if __name__ == "__main__":
    main()
