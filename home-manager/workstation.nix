{
  pkgs,
  lib,
  ...
}: {
  imports = [./libreoffice.nix];

  home.packages = [
    pkgs.kdePackages.ksshaskpass
    pkgs.jetbrains.pycharm
  ];

  home.sessionPath = [
    "$HOME/.local/share/JetBrains/Toolbox/scripts"
  ];

  programs.obsidian.enable = true;
}
