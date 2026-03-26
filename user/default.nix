{pkgs, ...}: {
  isNormalUser = true;
  description = "Stefan Zabka";
  extraGroups = ["networkmanager" "wheel" "docker"];
  shell = pkgs.zsh;
  packages = with pkgs; [
    alejandra
    bat
    delta
    direnv
    file
    fzf
    htop
    jujutsu
    nil
    nmap
    starship
    tailscale
    typst
    tmux
    wget
  ];
}
