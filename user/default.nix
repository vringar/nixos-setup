{ pkgs, ... }: {
    isNormalUser = true;
    description = "Stefan Zabka";
    extraGroups = [ "networkmanager" "wheel" ];
    shell = pkgs.zsh;
    packages = with pkgs; [
      alacritty
      bat
      delta
      direnv
      element-desktop
      file
      fzf
      kdePackages.kate
      keepassxc
      starship
      signal-desktop
      tailscale
      typst
      tmux
      thunderbird
      nil
    ];
}
