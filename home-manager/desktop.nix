{
  config,
  home-manager,
  ...
}: let
  cfg = config.my;
in {
  imports = [./config.nix];

  config.home-manager.users.${cfg.username} = {pkgs, ...}: {
    imports = [./libreoffice.nix];
    home.packages = [pkgs.kdePackages.ksshaskpass];
    programs.obsidian.enable = true;
  };
}
