{
  lib,
  config,
  home-manager,
  ...
}:
let
  cfg = config.my;
in
{
  imports = [./config.nix];

  config.home-manager.users.${cfg.username} = {
    pkgs,
    config,
    ...
  }: {
    imports = [./ai.nix];
    home.packages = [pkgs.claude-code pkgs.gh pkgs.git-cinnabar];

    programs.bash.enable = true;

    programs.neovim = {
      enable = true;
      defaultEditor = true;
      viAlias = true;
      vimAlias = true;
    };

    programs.alacritty = {
      enable = true;
      settings = {
        font.normal = {
          family = "JetBrainsMono Nerd Font Mono";
          style = "Regular";
        };
        colors = {
          primary = {
            background = "#1f1f1f";
            foreground = "#e5e1d8";
          };
          normal = {
            black = "#000000";
            red = "#f7786d";
            green = "#bde97c";
            yellow = "#efdfac";
            blue = "#6ebaf8";
            magenta = "#ef88ff";
            cyan = "#90fdf8";
            white = "#e5e1d8";
          };
          bright = {
            black = "#b4b4b4";
            red = "#f99f92";
            green = "#e3f7a1";
            yellow = "#f2e9bf";
            blue = "#b3d2ff";
            magenta = "#e5bdff";
            cyan = "#c2fefa";
            white = "#ffffff";
          };
        };
        window.opacity = 0.9;
      };
    };

    programs.zsh = {
      enable = true;
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
      signing = {
        signByDefault = true;
        format = "ssh";
        key =
          if (builtins.pathExists "${builtins.toString "/home/${config.home.username}/.ssh/id_ed25519.pub"}")
          then "/home/${config.home.username}/.ssh/id_ed25519.pub"
          else "/home/${config.home.username}/.ssh/github_key.pub";
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
      settings =
        {
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
        }

      ;
    };
    programs.zellij.enable = true;
    # The state version is required and should stay at the version you
    # originally installed.
    home.stateVersion = "25.11";
  };
}
