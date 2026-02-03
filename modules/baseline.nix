{
  pkgs,
  services,
  environment,
  home-manager,
  ...
}: {
  imports = [
    ../home-manager/baseline.nix
  ];
  services.tailscale.enable = true;
  # This module will be imported by all hosts
  environment.systemPackages = with pkgs; [
    vim
    wget
    curl
    npins
    lixPackageSets.git.colmena
    git
    git-lfs
    cifs-utils
    jujutsu
    pkgs.home-manager
  ];

  home-manager.useGlobalPkgs = true;
  programs.zsh.ohMyZsh.enable = true;
  programs.zsh.enable = true;
}
