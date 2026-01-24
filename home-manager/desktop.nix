{home-manager, ...}: {
  home-manager.users.vringar = {pkgs, ...}: {
    programs.obsidian.enable = true;
  };
}
