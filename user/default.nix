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
      filezilla
      fzf
      htop
      kdePackages.kate
      keepassxc
      nil
      starship
      signal-desktop
      tailscale
      typst
      tmux
      thunderbird
      vlc
      wget
    ];
}
