{
  config,
  pkgs,
  lib,
  ...
}: let
  sources = import ./npins;
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
  ];

  nixpkgs.config.allowUnfree = true;
  nixpkgs.overlays = [
    (final: _: {
      nixgl = import sources.nixGL {pkgs = final;};
    })
  ];

  my.user.name = "Stefan Zabka";
  my.user.email = "stefan.zabka@camunda.com";
  my.user.sshKeyName = "id_ed25519";
  my.nixGL.enable = true;

  home.sessionVariables.GIT_SSH = "/usr/bin/ssh";

  programs.home-manager.enable = true;

  home.username = username;
  home.homeDirectory = "/home/${username}";
}
