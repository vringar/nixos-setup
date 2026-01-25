{
  config,
  home-manager,
  ...
}:
let
  cfg = config.my;
in
{
  imports = [./config.nix];

  config.home-manager.users.${cfg.username} = {pkgs, ...}: {
    programs.obsidian.enable = true;
  };
}
