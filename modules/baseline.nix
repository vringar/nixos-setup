{
  pkgs,
  services,
  environment,
  ...
}: {
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
  ];
}
