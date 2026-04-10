let
  sources = import ./npins;
  pkgs = import sources.nixpkgs {};
in
  pkgs.mkShell {
    packages = [
      pkgs.colmena
      pkgs.npins
      pkgs.pre-commit
    ];
  }
