{pkgs, ...}: {
  home.packages = [
    (import ../apps/claude-sandbox {inherit pkgs;})
  ];
}
