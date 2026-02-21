#!@python3@
"""Run Claude Code in a bubblewrap sandbox with filesystem isolation."""

import argparse
import os
import sys

BWRAP = "@bwrap@"
NIX_SHELL = "@nix_shell@"
BASH = "@bash@"

HELP_EPILOG = """\
examples:
  claude-sandbox                        # sandbox cwd, auto-detect shell.nix
  claude-sandbox ~/projects/foo         # sandbox a specific project
  claude-sandbox --nix-file nix/shell.nix ~/projects/bar
  claude-sandbox --shell ~/projects/foo # interactive shell in sandbox
"""


def create_parser():
    parser = argparse.ArgumentParser(
        prog="claude-sandbox",
        description="Run Claude Code in a bubblewrap sandbox with filesystem isolation.",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Drop into an interactive shell instead of running Claude",
    )
    nix_file_arg = parser.add_argument(
        "--nix-file",
        metavar="PATH",
        help="Path to shell.nix relative to project dir "
        "(auto-detects shell.nix and flake.nix at project root)",
    )
    nix_file_arg.complete = {"zsh": "_files -g '*.nix'"}
    project_dir_arg = parser.add_argument(
        "project_dir",
        nargs="?",
        default=os.getcwd(),
        help="Project directory to sandbox (default: cwd)",
    )
    project_dir_arg.complete = {"zsh": "_directories"}
    return parser


def parse_args():
    return create_parser().parse_args()


def build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile):
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
        # Per-project tmp
        "--bind", sandbox_tmp, "/tmp",
        # Zsh config (read-only, symlink to nix store)
        "--ro-bind", os.path.join(home_dir, ".zshrc"),
                     os.path.join(home_dir, ".zshrc"),
        # Seed history with launch command (ephemeral, on sandbox tmp)
        "--bind", histfile, os.path.join(home_dir, ".zsh_history"),
        "--setenv", "HISTFILE", os.path.join(home_dir, ".zsh_history"),
        # Claude config and auth (read-write)
        "--bind", os.path.join(home_dir, ".config", "claude"),
                  os.path.join(home_dir, ".config", "claude"),
        "--bind", os.path.join(home_dir, ".claude"),
                  os.path.join(home_dir, ".claude"),
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
    """Resolve nix file: explicit flag > shell.nix > flake.nix > None."""
    if nix_file_arg:
        resolved = os.path.join(project_dir, nix_file_arg)
        if not os.path.isfile(resolved):
            print(f"Error: nix file not found: {resolved}", file=sys.stderr)
            sys.exit(1)
        return resolved

    shell_nix = os.path.join(project_dir, "shell.nix")
    if os.path.isfile(shell_nix):
        return shell_nix

    flake_nix = os.path.join(project_dir, "flake.nix")
    if os.path.isfile(flake_nix):
        return flake_nix

    return None


def main():
    args = parse_args()

    project_dir = os.path.realpath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    project_name = os.path.basename(project_dir)
    sandbox_tmp = f"/tmp/claude-sandbox-{project_name}"
    os.makedirs(sandbox_tmp, exist_ok=True)

    home_dir = os.environ["HOME"]

    # Seed a history file with the launch command
    histfile = os.path.join(sandbox_tmp, ".zsh_history")
    if not os.path.isfile(histfile):
        with open(histfile, "w") as f:
            f.write("claude --dangerously-skip-permissions\n")

    bwrap_args = build_bwrap_args(project_dir, home_dir, sandbox_tmp, histfile)

    if args.shell:
        print(f"Entering sandboxed shell in {project_dir}")
        shell = os.environ.get("SHELL", BASH)
        os.execvp(BWRAP, [BWRAP] + bwrap_args + ["--", shell])
    else:
        print(f"Starting sandboxed Claude Code in {project_dir}")
        launch_cmd = "claude --dangerously-skip-permissions"

        nix_file = resolve_nix_file(project_dir, args.nix_file)
        if nix_file:
            os.execvp(BWRAP, [BWRAP] + bwrap_args + [
                "--", NIX_SHELL, nix_file, "--run", launch_cmd,
            ])
        else:
            os.execvp(BWRAP, [BWRAP] + bwrap_args + [
                "--", BASH, "-c", launch_cmd,
            ])


if __name__ == "__main__":
    main()
