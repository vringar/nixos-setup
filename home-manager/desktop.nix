{
  pkgs,
  lib,
  config,
  ...
}: {
  imports = [./libreoffice.nix];

  home.packages = [
    pkgs.kdePackages.ksshaskpass
    pkgs.jetbrains.pycharm
    pkgs.meld
    pkgs.vscode-langservers-extracted
    # Kate looks for this name; vscode-langservers-extracted provides the binary as vscode-json-language-server
    (pkgs.writeShellScriptBin "vscode-json-languageserver" ''
      exec ${lib.getExe' pkgs.vscode-langservers-extracted "vscode-json-language-server"} "$@"
    '')
  ];

  home.sessionPath = [
    "$HOME/.local/share/JetBrains/Toolbox/scripts"
  ];

  programs.alacritty = {
    enable = true;
    package = lib.mkIf config.my.nixGL.enable (
      pkgs.writeShellScriptBin "alacritty" ''
        exec ${lib.getExe' pkgs.nixgl.auto.nixGLDefault "nixGL"} ${lib.getExe pkgs.alacritty} "$@"
      ''
    );
    settings = {
      font.normal = {
        family = "JetBrainsMono Nerd Font Mono";
        style = "Regular";
      };
      colors = {
        primary = {
          background = "#1f1f1f";
          foreground = "#e5e1d8";
        };
        normal = {
          black = "#000000";
          red = "#f7786d";
          green = "#bde97c";
          yellow = "#efdfac";
          blue = "#6ebaf8";
          magenta = "#ef88ff";
          cyan = "#90fdf8";
          white = "#e5e1d8";
        };
        bright = {
          black = "#b4b4b4";
          red = "#f99f92";
          green = "#e3f7a1";
          yellow = "#f2e9bf";
          blue = "#b3d2ff";
          magenta = "#e5bdff";
          cyan = "#c2fefa";
          white = "#ffffff";
        };
      };
    };
  };

  programs.obsidian.enable = true;
}
