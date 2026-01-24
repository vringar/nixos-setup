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
    home.packages = [pkgs.atool pkgs.httpie];
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
    programs.git = {
      package = pkgs.gitFull;
      enable = true;
      settings = {
      };
      lfs.enable = true;
      signing = {
        signByDefault = true;
        format = "ssh";
        key =
          if (builtins.pathExists "${builtins.toString "/home/${config.home.username}/.ssh/id_ed25519.pub"}")
          then "/home/${config.home.username}/.ssh/id_ed25519.pub"
          else "/home/${config.home.username}/.ssh/github_key.pub";
      };
    };

    programs.starship.enable = true;
    programs.zellij.enable = true;
    # The state version is required and should stay at the version you
    # originally installed.
    home.stateVersion = "25.11";
  };
}
