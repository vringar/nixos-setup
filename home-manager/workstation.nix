{
  pkgs,
  lib,
  ...
}: {
  imports = [./libreoffice.nix];

  home.packages = [
    pkgs.kdePackages.ksshaskpass
    pkgs.jetbrains.pycharm
    pkgs.lmstudio
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
    # Baloo's 4K-page LMDB index on 128K ZFS records rewrote ~50x its size,
    # saturating sz1's pool and stalling every fsync on it.
    configFile."baloofilerc"."Basic Settings"."Indexing-Enabled" = false;
  };
}
