{config, ...}: let
  cfg = config.my;
in {
  config.home-manager.users.${cfg.username} = {pkgs, ...}: {
    home.packages = [
      (import ../apps/claude-sandbox {inherit pkgs;})
    ];
  };
}
