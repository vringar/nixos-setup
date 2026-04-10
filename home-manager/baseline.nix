{
  pkgs,
  config,
  lib,
  ...
}: let
  cfg = config.my.user;
  hasIdentity = cfg.name != null && cfg.email != null;
in {
  options.my.nixGL.enable = lib.mkEnableOption "nixGL wrappers for GPU-accelerated apps (needed on non-NixOS)";

  options.my.user = {
    name = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Full name used for version control identity (e.g. jj, git).";
    };
    email = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = "Email address used for version control identity (e.g. jj, git).";
    };
    sshKeyName = lib.mkOption {
      type = lib.types.str;
      default = "github_key";
      description = "Name of the default SSH key file in ~/.ssh/ (without extension).";
    };
  };

  config = {
    home.packages = [
      pkgs.alejandra
      pkgs.gh
      pkgs.git-cinnabar
      pkgs.mergiraf
      pkgs.pre-commit
      pkgs.ripgrep
      pkgs.shellcheck
    ];

    programs.bash = {
      enable = true;
      initExtra = ''
        [[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env"
      '';
    };

    programs.neovim = {
      enable = true;
      defaultEditor = true;
      viAlias = true;
      vimAlias = true;
      withPython3 = false;
      withRuby = false;
    };

    programs.zsh = {
      enable = true;
      initContent = ''
        [[ -f "$HOME/.cargo/env" ]] && source "$HOME/.cargo/env"

        if ! command -v code >/dev/null 2>&1 && command -v code-insiders >/dev/null 2>&1; then
          alias code='code-insiders'
        fi
      '';
      shellAliases = {
        tmux = "tmux -u";
        tf = "terraform";
        dck = "docker compose";
      };
      sessionVariables = {
        RUST_BACKTRACE = "1";
      };
      oh-my-zsh = {
        enable = true;
        plugins = [
          "tmux"
          "git"
          "python"
          "rust"
          "nix-shell"
          "nix-zsh-completions"
        ];
        extraConfig = ''
          COMPLETION_WAITING_DOTS="true"
          HIST_STAMPS="yyyy-mm-dd"
        '';
      };
    };
    home.shell.enableZshIntegration = true;

    programs.direnv = {
      enable = true;
      nix-direnv.enable = true;
    };
    programs.delta = {
      enable = true;
      enableGitIntegration = true;
      enableJujutsuIntegration = true;
      options = {
        navigate = true;
        side-by-side = true;
      };
    };

    # TODO: reintroduce nbdime
    programs.git = {
      package = pkgs.gitFull;
      enable = true;
      lfs.enable = true;
      settings.user.name = lib.mkIf hasIdentity cfg.name;
      settings.user.email = lib.mkIf hasIdentity cfg.email;
      signing = {
        signByDefault = true;
        format = "ssh";
        key = "/home/${config.home.username}/.ssh/${cfg.sshKeyName}.pub";
      };
      settings = {
        core = {
          editor = lib.getExe config.programs.neovim.finalPackage;
        };
        alias = {
          root = "rev-parse --show-toplevel";
        };
        rerere.enable = true;
        fetch.prune = true;
        checkout.defaultRemote = "origin";

        sendemail = {
          smtpserver = "smtp.migadu.com";
          smtpuser = "git@zabka.it";
          smtpencryption = "ssl";
          smptserverport = 465;
        };
        # TODO: Figure out how to refer to the git-cinnabar package
        cinnabar.helper = "git-cinnabar-helper";
        init.defaultBranch = "main";
        pull.ff = "only";
        merge.conflictstyle = "diff3";
        # TODO: Why explicitly set this to default?
        diff.colorMoved = "default";
        push = {
          default = "upstream";
          autoSetupRemote = true;
        };
      };
    };

    programs.starship = {
      enable = true;
      settings = {
        custom.jj = {
          description = "The current jj status";
          when = "jj --ignore-working-copy root";
          symbol = "🥋 ";
          command = ''
            jj log --revisions @ --no-graph --ignore-working-copy --color always --limit 1 --template '
            separate(" ",
                change_id.shortest(4),
                bookmarks,
                "|",
                concat(
                if(conflict, "💥"),
                if(divergent, "🚧"),
                if(hidden, "👻"),
                if(immutable, "🔒"),
                ),
                raw_escape_sequence("\x1b[1;32m") ++ if(empty, "(empty)"),
                raw_escape_sequence("\x1b[1;32m") ++ coalesce(
                truncate_end(29, description.first_line(), "…"),
                "(no description set)",
                ) ++ raw_escape_sequence("\x1b[0m"),
            )
            '
          '';
        };
        git_status.disabled = true;
        git_commit.disabled = true;
        git_metrics.disabled = true;
        git_branch.disabled = true;
      };
    };
    programs.zellij.enable = true;

    services.ssh-agent.enable = true;

    programs.ssh = {
      enable = true;
      enableDefaultConfig = false;
      extraOptionOverrides = {
        IgnoreUnknown = "GSSAPIKexAlgorithms,GSSAPIAuthentication";
        IdentityFile = "~/.ssh/${cfg.sshKeyName}";
        AddKeysToAgent = "yes";
      };
      matchBlocks = {
        "jannis-gogs" = {
          hostname = "omniskop.de";
          user = "gogs";
        };
        "hg.mozilla.org" = {
          user = "szabka@mozilla.com";
          identityFile = "~/.ssh/mozilla";
        };
        "b8" = {
          hostname = "100.127.109.161";
          user = "homeserver";
        };
        "tischtennis" = {
          hostname = "tischtennis.einetollewebsite.de";
          identityFile = "~/.ssh/${cfg.sshKeyName}";
          user = "ubuntu";
        };
        "sect" = {
          hostname = "192.168.140.52";
          identityFile = "~/.ssh/${cfg.sshKeyName}";
          user = "root";
        };
        "tompute" = {
          hostname = "tompute.sect.tu-berlin.de";
          user = "vringar";
        };
        "dmh" = {
          hostname = "dmh-neu.tailbaace.ts.net";
          user = "kleing";
        };
      };
    };

    # SSH config in Nix store is world-readable (444), but SSH requires 600.
    # This activation script replaces the symlink with a copy that has proper permissions.
    home.activation.fixSSHConfigPermissions = lib.hm.dag.entryAfter ["linkGeneration"] ''
      sshConfig="${config.home.homeDirectory}/.ssh/config"
      if [ -L "$sshConfig" ]; then
        realSource=$(readlink -f "$sshConfig")
        rm "$sshConfig"
        cp "$realSource" "$sshConfig"
        chmod 600 "$sshConfig"
      fi
    '';

    programs.jujutsu = {
      enable = true;
      settings =
        {
          aliases = {
            push = [
              "util"
              "exec"
              "--"
              "bash"
              "-c"
              ''
                set -euo pipefail
                if [[ ! -f .pre-commit-config.yaml ]]; then
                  exec jj git push "$@"
                fi
                commits=$(jj log -r 'trunk()..@' --no-graph -T 'change_id ++ "\n"' 2>/dev/null || true)
                if [[ -z "$commits" ]]; then
                  echo "No commits to push"
                  exit 0
                fi
                echo "Running pre-commit on commits since trunk..."
                pre-commit run --all-files
                echo "Pre-commit passed, pushing..."
                exec jj git push "$@"
              ''
              ""
            ];
          };
        }
        // lib.optionalAttrs hasIdentity {
          user = {
            name = cfg.name;
            email = cfg.email;
          };
        };
    };
    # The state version is required and should stay at the version you
    # originally installed.
    home.stateVersion = "25.11";
  };
}
