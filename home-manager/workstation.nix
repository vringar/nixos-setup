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

  # Librarian vault server (AMD/NDA-derived pages): must stay on localhost.
  # The checkout lives on sz1 only, so the unit skips itself elsewhere. Started
  # at login and restarted on failure, which the lane used to do by hand after
  # every shutdown.
  systemd.user.services.librarian-vault = {
    Unit = {
      Description = "Librarian vault HTTP server (localhost only)";
      ConditionPathExists = "%h/projects/AMD-PSP/projects/Librarian";
    };
    Install.WantedBy = ["default.target"];
    Service = {
      Type = "simple";
      WorkingDirectory = "%h/projects/AMD-PSP/projects/Librarian";
      Environment = ["PATH=${lib.makeBinPath [pkgs.nix pkgs.bash pkgs.coreutils pkgs.git]}"];
      ExecStart = ''${pkgs.nix}/bin/nix-shell --run "python -m librarian.vaultserver --port 8861"'';
      Restart = "on-failure";
      RestartSec = "5s";
    };
  };

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
