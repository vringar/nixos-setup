#!@python3@
"""Run commands in a bubblewrap sandbox with filesystem isolation."""

import argparse
import os
import shlex
import sys

BWRAP = "@bwrap@"
NIX_SHELL = "@nix_shell@"
BASH = "@bash@"
PYTHON3_BIN_DIR = "@python3_bin_dir@"

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


def build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile, shell_path):
    claude_config_dir = os.environ.get(
        "CLAUDE_CONFIG_DIR",
        os.path.join(home_dir, ".config", "claude"),
    )
    claude_dot_dir = os.path.join(home_dir, ".claude")

    args = [
        # Empty HOME
        "--tmpfs", home_dir,
        # Project directory (read-write)
        "--bind", project_dir, project_dir,
    ]

    # If inside a .workspace/ folder, expose parent project's .jj and .git
    parent_dir = os.path.dirname(project_dir)
    if os.path.basename(parent_dir) == ".workspace":
        repo_root = os.path.dirname(parent_dir)
        jj_dir = os.path.join(repo_root, ".jj")
        git_dir = os.path.join(repo_root, ".git")
        if os.path.isdir(jj_dir):
            args += ["--bind", jj_dir, jj_dir]
        if os.path.isdir(git_dir):
            args += ["--bind", git_dir, git_dir]

    args += [
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
        # Nix daemon socket, current-system binaries, wrappers
        "--ro-bind", "/run", "/run",
        # Block most of desktop session (Wayland, PipeWire, etc.)
        "--tmpfs", "/run/user",
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

    # Claude config and auth (read-write)
    if os.path.isdir(claude_config_dir):
        args += ["--bind", claude_config_dir, claude_config_dir]
    if os.path.isdir(claude_dot_dir):
        args += ["--bind", claude_dot_dir, claude_dot_dir]

    legacy_credentials = os.path.join(claude_dot_dir, ".credentials.json")
    config_credentials = os.path.join(claude_config_dir, ".credentials.json")
    if os.path.isfile(legacy_credentials) and not os.path.exists(config_credentials):
        args += ["--bind", legacy_credentials, config_credentials]

    # D-Bus user socket (needed for KWallet/keyring access, e.g. gh credentials)
    uid = os.getuid()
    dbus_socket = f"/run/user/{uid}/bus"
    if os.path.exists(dbus_socket):
        args += ["--bind", dbus_socket, dbus_socket]

    # SSH agent passthrough
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if ssh_auth_sock:
        args += ["--ro-bind", ssh_auth_sock, ssh_auth_sock]

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

    return args


def resolve_nix_file(project_dir, nix_file_arg):
    """Resolve nix file: explicit flag > shell.nix > nix/shell.nix > flake.nix > default.nix > None.

    Returns (path, use_attr) where use_attr is True when the file is a
    default.nix that should be invoked with ``nix-shell -A shell``.
    """
    if nix_file_arg:
        resolved = os.path.join(project_dir, nix_file_arg)
        if not os.path.isfile(resolved):
            print(f"Error: nix file not found: {resolved}", file=sys.stderr)
            sys.exit(1)
        return resolved, False

    shell_nix = os.path.join(project_dir, "shell.nix")
    if os.path.isfile(shell_nix):
        return shell_nix, False

    nix_shell_nix = os.path.join(project_dir, "nix", "shell.nix")
    if os.path.isfile(nix_shell_nix):
        return nix_shell_nix, False

    flake_nix = os.path.join(project_dir, "flake.nix")
    if os.path.isfile(flake_nix):
        return flake_nix, False

    default_nix = os.path.join(project_dir, "default.nix")
    if os.path.isfile(default_nix):
        return default_nix, True

    return None, False


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

    project_name = os.path.basename(project_dir)
    script_name = get_script_name()
    sandbox_tmp = f"/tmp/{script_name}-{project_name}"
    os.makedirs(sandbox_tmp, exist_ok=True)

    home_dir = os.environ["HOME"]

    # Seed a history file with the launch command
    histfile = os.path.join(sandbox_tmp, ".zsh_history")
    if not os.path.isfile(histfile):
        seed = args.launch_cmd or ""
        with open(histfile, "w") as f:
            f.write(seed + "\n")

    shell_path = resolve_shell(home_dir)
    bwrap_args = build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile, shell_path)

    if args.shell:
        print(f"Entering sandboxed shell in {project_dir}")
        os.execvp(BWRAP, [BWRAP] + bwrap_args + ["--", shell_path])
    else:
        launch_cmd = args.launch_cmd
        print(f"Starting sandboxed {launch_cmd.split()[0]} in {project_dir}")

        nix_file, use_attr = resolve_nix_file(project_dir, args.nix_file)
        if nix_file:
            nix_args = [NIX_SHELL, nix_file]
            if use_attr:
                nix_args += ["-A", "shell"]
            nix_args += ["--run", launch_cmd]
            os.execvp(BWRAP, [BWRAP] + bwrap_args + ["--"] + nix_args)
        else:
            os.execvp(BWRAP, [BWRAP] + bwrap_args + [
                "--", BASH, "-c", launch_cmd,
            ])


if __name__ == "__main__":
    main()
