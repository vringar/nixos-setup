{
  config,
  pkgs,
  lib,
  ...
}: let
  sources = import ./npins;
  c8ctl = import ./apps/c8ctl {inherit pkgs;};
  username = builtins.getEnv "USER";
in {
  assertions = [
    {
      assertion = username != "";
      message = "USER environment variable must be set. This is expected when running via 'home-manager switch'.";
    }
  ];

  imports = [
    ./home-manager/baseline.nix
    ./home-manager/graphical.nix
    ./home-manager/ai.nix
  ];

  nixpkgs.config.allowUnfree = true;
  nixpkgs.overlays = [
    (final: prev: {
      nixgl = import sources.nixGL {pkgs = final;};
      pre-commit = prev.pre-commit.overrideAttrs (old: {
        patches = (old.patches or []) ++ [./apps/pre-commit/meta-hooks-pythonpath.patch];
      });
    })
  ];

  my.user.name = "Stefan Zabka";
  my.user.email = "stefan.zabka@camunda.com";
  my.user.sshKeyName = "id_ed25519";
  my.nixGL.enable = true;
  my.work.enable = true;
  my.crosslink.doCheck = false;

  home.sessionVariables.GIT_SSH = "/usr/bin/ssh";
  # Zed picks the compositor-hinted Intel iGPU on this hybrid Intel/NVIDIA laptop and fails
  # surface creation with no fallback (zed-industries/zed#52517, #54218). Forcing a device ID
  # makes it retry via the GL backend, which works. Device ID is this machine's NVIDIA GPU
  # (`vulkaninfo --summary`), so this stays out of the shared graphical.nix module.
  home.sessionVariables.ZED_DEVICE_ID = "0x28ba";

  programs.zsh.initContent = ''
    cm() { camunda-modeler "$@" &>/dev/null & disown; }
  '';

  home.packages = [
    c8ctl
    pkgs.auth0-cli
    (pkgs.writeShellScriptBin "camunda-modeler" ''
      exec ${lib.getExe' pkgs.nixgl.auto.nixGLDefault "nixGL"} ${lib.getExe pkgs.camunda-modeler} "$@"
    '')
  ];

  # Camunda Modeler only scans resources/plugins next to its Electron exe (a
  # shared nixpkgs derivation we can't drop files into) and under
  # $XDG_CONFIG_HOME/camunda-modeler/resources/plugins — never inside its own
  # store path. Personal plugins therefore have to land here, not in an
  # override on the package itself.
  home.file = {
    ".config/camunda-modeler/resources/plugins/camunda-ai-lint/index.js".source =
      "${sources.bpmnlint-aitools}/index.js";
    ".config/camunda-modeler/resources/plugins/camunda-ai-lint/dist".source =
      "${sources.bpmnlint-aitools}/dist";
    ".config/camunda-modeler/resources/plugins/spacing-guides/index.js".source =
      "${sources.bpmnlint-aitools}/spacing-guides/index.js";
    ".config/camunda-modeler/resources/plugins/spacing-guides/dist".source =
      "${sources.bpmnlint-aitools}/spacing-guides/dist";
  };

  programs.home-manager.enable = true;

  home.username = username;
  home.homeDirectory = "/home/${username}";
}
