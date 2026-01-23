{home-manager,...}: {
    home-manager.users.vringar = { pkgs, ... }: {
        home.packages = [ pkgs.atool pkgs.httpie ];
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
            enable = true;
        };

        programs.starship.enable = true;
        programs.zellij.enable = true;
        # The state version is required and should stay at the version you
        # originally installed.
        home.stateVersion = "25.11";
  };
}
