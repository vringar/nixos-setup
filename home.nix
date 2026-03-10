{
  config,
  pkgs,
  lib,
  ...
}: let
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

  my.user.name = "Stefan Zabka";
  my.user.email = "stefan.zabka@camunda.com";
  my.user.sshKeyName = "id_ed25519";

  home.sessionVariables.GIT_SSH = "/usr/bin/ssh";

  programs.home-manager.enable = true;

  home.username = username;
  home.homeDirectory = "/home/${username}";
}
