{
  lib,
  home-manager,
  ...
}: {
  home-manager.users.vringar = {
    pkgs,
    config,
    ...
  }: {
    home.packages = [pkgs.atool pkgs.httpie pkgs.git-cinnabar];
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

    programs.starship.enable = true;
    programs.zellij.enable = true;
    # The state version is required and should stay at the version you
    # originally installed.
    home.stateVersion = "25.11";
  };
}
