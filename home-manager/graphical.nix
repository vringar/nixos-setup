{
  pkgs,
  lib,
  config,
  ...
}: {
  home.packages = [
    pkgs.meld
    pkgs.vscode-langservers-extracted
    # Kate looks for this name; vscode-langservers-extracted provides the binary as vscode-json-language-server
    (pkgs.writeShellScriptBin "vscode-json-languageserver" ''
      exec ${lib.getExe' pkgs.vscode-langservers-extracted "vscode-json-language-server"} "$@"
    '')
    pkgs.ruff
  ];

  programs.zed-editor = {
    enable = true;
    # Auto-installed by Zed on first launch (fetched + compiled at runtime).
    extensions = [
      "nix"
      "toml"
      "dockerfile"
    ];
    userSettings = {
      telemetry = {
        diagnostics = false;
        metrics = false;
      };
      vim_mode = false;
      theme = "One Dark";
      buffer_font_family = "JetBrainsMono Nerd Font Mono";
      buffer_font_size = 14;
      ui_font_size = 15;
      format_on_save = "on";
      # Zed bundles Copilot; opt out unless explicitly enabled.
      edit_predictions.provider = "none";
      # Zed's auto-downloaded language servers are dynamically linked against a
      # standard loader and fail to start on NixOS. Point ruff at the Nix-built
      # binary and forbid the internet fetch.
      lsp.ruff.binary = {
        path = lib.getExe pkgs.ruff;
        arguments = ["server"];
        ignore_system_version = true;
      };
    };
  };

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

  programs.jujutsu.settings = {
    ui.merge-editor = "meld3";
    merge-tools.meld3.program = lib.getExe pkgs.meld;
    merge-tools.meld3.merge-args = ["$left" "$base" "$right" "-o" "$output"];
  };
}
