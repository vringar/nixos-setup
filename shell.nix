let
  sources = import ./npins;
  pkgs = import sources.nixpkgs {};
in
  pkgs.mkShell {
    packages = [
      pkgs.colmena
      pkgs.npins
      pkgs.pre-commit
      # tests/ imports the scripts under test directly, so the shell has to
      # carry their third-party dependencies: pytest to run them at all,
      # requests for apps/witcher-corpus/reconcile.py, without which
      # `pytest tests/` fails at collection rather than at any assertion, and
      # jinja2 for the message board's web view.
      (pkgs.python3.withPackages (ps: [
        ps.jinja2
        ps.pytest
        ps.requests
      ]))
    ];
  }
