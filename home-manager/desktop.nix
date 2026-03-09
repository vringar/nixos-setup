{pkgs, ...}: {
  imports = [./libreoffice.nix];
  home.packages = [pkgs.kdePackages.ksshaskpass];
  programs.obsidian.enable = true;
}
