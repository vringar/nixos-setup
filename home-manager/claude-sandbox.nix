{config, ...}: let
  cfg = config.my;
in {
  config.home-manager.users.${cfg.username} = {pkgs, ...}: {
    home.packages = [
      (pkgs.writeTextFile {
        name = "claude-sandbox-zsh-completions";
        destination = "/share/zsh/site-functions/_claude-sandbox";
        text = ''
          #compdef claude-sandbox

          _claude-sandbox() {
            local -a opts
            opts=(
              '(--help -h)'{--help,-h}'[Show help message]'
              '--shell[Drop into interactive sandboxed shell instead of running Claude]'
              '--nix-file[Path to shell.nix relative to project dir]:nix file:_files -g "*.nix"'
            )
            _arguments -s $opts '1:project directory:_directories'
          }

          _claude-sandbox "$@"
        '';
      })
      (pkgs.writeShellScriptBin "claude-sandbox" ''
        set -euo pipefail

        SHELL_MODE=0
        PROJECT_DIR=""
        NIX_FILE=""

        while [[ $# -gt 0 ]]; do
          case "$1" in
            --help|-h)
              cat <<'USAGE'
        Usage: claude-sandbox [OPTIONS] [project-dir]

        Run Claude Code in a bubblewrap sandbox with filesystem isolation.

        The project directory is bind-mounted read-write while the rest of HOME
        is hidden behind a tmpfs. Nix store, system config, and network access
        are preserved.

        Options:
          --shell          Drop into an interactive shell instead of running Claude
          --nix-file PATH  Path to shell.nix relative to project dir
                           (auto-detects shell.nix and flake.nix at project root)
          -h, --help       Show this help message

        Examples:
          claude-sandbox                        # sandbox cwd, auto-detect shell.nix
          claude-sandbox ~/projects/foo         # sandbox a specific project
          claude-sandbox --nix-file nix/shell.nix ~/projects/bar
          claude-sandbox --shell ~/projects/foo # interactive shell in sandbox
        USAGE
              exit 0
              ;;
            --shell)
              SHELL_MODE=1
              shift
              ;;
            --nix-file)
              NIX_FILE="$2"
              shift 2
              ;;
            -*)
              echo "Unknown option: $1" >&2
              echo "Run 'claude-sandbox --help' for usage." >&2
              exit 1
              ;;
            *)
              PROJECT_DIR="$1"
              shift
              ;;
          esac
        done

        if [[ -z "$PROJECT_DIR" ]]; then
          PROJECT_DIR="$(pwd)"
        fi

        PROJECT_DIR="$(realpath "$PROJECT_DIR")"

        if [[ ! -d "$PROJECT_DIR" ]]; then
          echo "Error: $PROJECT_DIR is not a directory" >&2
          exit 1
        fi

        PROJECT_NAME="$(basename "$PROJECT_DIR")"
        SANDBOX_TMP="/tmp/claude-sandbox-$PROJECT_NAME"
        mkdir -p "$SANDBOX_TMP"

        HOME_DIR="$HOME"

        # Seed a history file with the launch command
        SANDBOX_HISTFILE="$SANDBOX_TMP/.zsh_history"
        if [[ ! -f "$SANDBOX_HISTFILE" ]]; then
          echo "claude --dangerously-skip-permissions" > "$SANDBOX_HISTFILE"
        fi

        BWRAP_ARGS=(
          # Empty HOME
          --tmpfs "$HOME_DIR"

          # Project directory (read-write)
          --bind "$PROJECT_DIR" "$PROJECT_DIR"
        )

        # If inside a .workspace/ folder, expose parent project's .jj and .git
        PARENT_DIR="$(dirname "$PROJECT_DIR")"
        if [[ "$(basename "$PARENT_DIR")" == ".workspace" ]]; then
          REPO_ROOT="$(dirname "$PARENT_DIR")"
          if [[ -d "$REPO_ROOT/.jj" ]]; then
            BWRAP_ARGS+=(--bind "$REPO_ROOT/.jj" "$REPO_ROOT/.jj")
          fi
          if [[ -d "$REPO_ROOT/.git" ]]; then
            BWRAP_ARGS+=(--bind "$REPO_ROOT/.git" "$REPO_ROOT/.git")
          fi
        fi

        BWRAP_ARGS+=(

          # Nix store and var (read-only)
          --ro-bind /nix /nix

          # System config (DNS, passwd, nix profiles)
          --ro-bind /etc /etc

          # Nix daemon socket, current-system binaries, wrappers
          --ro-bind /run /run
          # Block desktop session (D-Bus, Wayland, PipeWire, KWallet, etc.)
          --tmpfs /run/user

          # Kernel filesystems
          --proc /proc
          --dev /dev

          # Per-project tmp
          --bind "$SANDBOX_TMP" /tmp

          # Zsh config (read-only, symlink to nix store)
          --ro-bind "$HOME_DIR/.zshrc" "$HOME_DIR/.zshrc"

          # Seed history with launch command (ephemeral, on sandbox tmp)
          --bind "$SANDBOX_HISTFILE" "$HOME_DIR/.zsh_history"
          --setenv HISTFILE "$HOME_DIR/.zsh_history"

          # Claude config and auth (read-write)
          --bind "$HOME_DIR/.config/claude" "$HOME_DIR/.config/claude"
          --bind "$HOME_DIR/.claude" "$HOME_DIR/.claude"

          # Git config (read-only)
          --ro-bind "$HOME_DIR/.config/git" "$HOME_DIR/.config/git"

          # Jujutsu config (read-only, symlink resolves via /nix bind)
          --ro-bind "$HOME_DIR/.config/jj" "$HOME_DIR/.config/jj"

          # User nix profile (claude, etc. live here)
          --ro-bind "$HOME_DIR/.nix-profile" "$HOME_DIR/.nix-profile"

          # User nix profile symlinks
          --ro-bind "$HOME_DIR/.local/state/nix" "$HOME_DIR/.local/state/nix"

          # Network: unrestricted
          --share-net

          # PID namespace: isolated
          --unshare-pid

          # Work inside the project directory
          --chdir "$PROJECT_DIR"
        )

        # SSH agent passthrough
        if [[ -n "''${SSH_AUTH_SOCK:-}" ]]; then
          BWRAP_ARGS+=(--ro-bind "$SSH_AUTH_SOCK" "$SSH_AUTH_SOCK")
        fi

        # GitHub CLI config and auth (read-write, gh updates token expiry)
        if [[ -d "$HOME_DIR/.config/gh" ]]; then
          BWRAP_ARGS+=(--bind "$HOME_DIR/.config/gh" "$HOME_DIR/.config/gh")
        fi

        # SSH config (read-only, no private keys)
        if [[ -f "$HOME_DIR/.ssh/known_hosts" ]]; then
          BWRAP_ARGS+=(--ro-bind "$HOME_DIR/.ssh/known_hosts" "$HOME_DIR/.ssh/known_hosts")
        fi
        if [[ -f "$HOME_DIR/.ssh/config" ]]; then
          BWRAP_ARGS+=(--ro-bind "$HOME_DIR/.ssh/config" "$HOME_DIR/.ssh/config")
        fi

        if [[ "$SHELL_MODE" -eq 1 ]]; then
          echo "Entering sandboxed shell in $PROJECT_DIR"
          exec ${pkgs.bubblewrap}/bin/bwrap "''${BWRAP_ARGS[@]}" -- \
            "''${SHELL:-${pkgs.bashInteractive}/bin/bash}"
        else
          echo "Starting sandboxed Claude Code in $PROJECT_DIR"
          LAUNCH_CMD="claude --dangerously-skip-permissions"

          # Resolve nix file: explicit flag > shell.nix > flake.nix > none
          RESOLVED_NIX_FILE=""
          if [[ -n "$NIX_FILE" ]]; then
            RESOLVED_NIX_FILE="$PROJECT_DIR/$NIX_FILE"
            if [[ ! -f "$RESOLVED_NIX_FILE" ]]; then
              echo "Error: nix file not found: $RESOLVED_NIX_FILE" >&2
              exit 1
            fi
          elif [[ -f "$PROJECT_DIR/shell.nix" ]]; then
            RESOLVED_NIX_FILE="$PROJECT_DIR/shell.nix"
          elif [[ -f "$PROJECT_DIR/flake.nix" ]]; then
            RESOLVED_NIX_FILE="$PROJECT_DIR/flake.nix"
          fi

          if [[ -n "$RESOLVED_NIX_FILE" ]]; then
            exec ${pkgs.bubblewrap}/bin/bwrap "''${BWRAP_ARGS[@]}" -- \
              ${pkgs.nix}/bin/nix-shell "$RESOLVED_NIX_FILE" --run "$LAUNCH_CMD"
          else
            exec ${pkgs.bubblewrap}/bin/bwrap "''${BWRAP_ARGS[@]}" -- \
              ${pkgs.bashInteractive}/bin/bash -c "$LAUNCH_CMD"
          fi
        fi
      '')
    ];
  };
}
