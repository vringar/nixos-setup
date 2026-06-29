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

  # Make alacritty KDE's default terminal (Open Terminal Here, Ctrl+Alt+T, etc.).
  programs.plasma = {
    enable = true;
    configFile."kdeglobals"."General" = {
      "TerminalApplication" = "alacritty";
      "TerminalService" = "Alacritty.desktop";
    };
  };
}
