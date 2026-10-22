let
  sources = import ./npins;
  pkgs = import sources.nixpkgs {};
in
  pkgs.mkShell {
    packages = [
      pkgs.colmena
      pkgs.npins
      pkgs.pre-commit
      # Runs the board's browser-side signing code against ssh-keygen in the tests.
      pkgs.nodejs
      # Runs real sandboxes in the claude-sandbox tests.
      pkgs.bubblewrap
      # tests/ imports the scripts under test directly, so the shell has to
      # carry their third-party dependencies: pytest to run them at all, and
      # requests for apps/witcher-corpus/reconcile.py, without which
      # `pytest tests/` fails at collection rather than at any assertion.
      # The message board's tests need nothing more: they drive its programs.
      (pkgs.python3.withPackages (ps: [
        ps.pytest
        ps.requests
      ]))
    ];
  }
