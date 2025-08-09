{ pkgs, ... }: {
    isNormalUser = true;
    description = "Stefan Zabka";
    extraGroups = [ "networkmanager" "wheel" "docker" ];
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
      jujutsu
      kdePackages.kate
      keepassxc
      nextcloud-client
      nil
      nmap
      pdfpc
      polylux2pdfpc
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
