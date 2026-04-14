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

  home.sessionVariables.GIT_SSH = "/usr/bin/ssh";

  home.packages = [
    c8ctl
    pkgs.auth0-cli
    (pkgs.writeShellScriptBin "camunda-modeler" ''
      exec ${lib.getExe' pkgs.nixgl.auto.nixGLDefault "nixGL"} ${lib.getExe pkgs.camunda-modeler} "$@"
    '')
  ];

  programs.home-manager.enable = true;

  home.username = username;
  home.homeDirectory = "/home/${username}";
}
