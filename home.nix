{
  config,
  pkgs,
  lib,
  ...
}: let
  sources = import ./npins;
  c8ctl = import ./apps/c8ctl {inherit pkgs;};
  camunda-modeler = import ./apps/camunda-modeler {inherit pkgs sources;};
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

  home.packages = [
    c8ctl
    pkgs.auth0-cli
    (pkgs.writeShellScriptBin "camunda-modeler" ''
      exec ${lib.getExe' pkgs.nixgl.auto.nixGLDefault "nixGL"} ${lib.getExe camunda-modeler} "$@"
    '')
  ];

  programs.home-manager.enable = true;

  home.username = username;
  home.homeDirectory = "/home/${username}";
}
