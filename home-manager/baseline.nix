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
    home.packages = [pkgs.claude-code pkgs.git-cinnabar];
    programs.bash.enable = true;

    programs.neovim = {
      enable = true;
      defaultEditor = true;
      viAlias = true;
      vimAlias = true;
    };
    programs.zsh.enable = true;
    home.shell.enableZshIntegration = true;
    programs.zsh.oh-my-zsh.enable = true;
    programs.zsh.oh-my-zsh.plugins = [
      "tmux"
      "git"
      "python"
      "rust"
      "nix-shell"
      "nix-zsh-completions"
    ];
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
