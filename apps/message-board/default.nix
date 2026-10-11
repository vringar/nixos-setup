{
  pkgs,
  sources,
}: let
  inherit (pkgs) lib;
  # Everything is stdlib except board-server's web view, which needs jinja2.
  pythonEnv = pkgs.python3.withPackages (ps: [ps.jinja2]);
in
  pkgs.stdenv.mkDerivation {
    pname = "message-board";
    version = "0.1.0";

    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [./board.py ./board_core.py ./board_server.py ./board-admin.py];
    };

    nativeBuildInputs = [pkgs.makeWrapper];

    dontConfigure = true;
    dontBuild = true;

    # Three commands over one storage module (board_core.py):
    #   board         agent-facing HTTP client (post / read / watch / pin ...)
    #   board-server  the HTTP server + web view; the only thing that needs
    #                 jinja2 and htmx (served from the pinned source)
    #   board-admin   the TUI that permanently deletes threads, straight on
    #                 the DB; deliberately not reachable from `board`
    # The scripts import board_core from their own directory, so all four
    # files install side by side.
    installPhase = ''
      install -Dm644 board.py board_core.py board_server.py board-admin.py \
        -t $out/share/message-board
      install -Dm644 ${sources.htmx}/dist/htmx.min.js \
        $out/share/message-board/static/htmx.min.js
      makeWrapper ${pythonEnv}/bin/python3 $out/bin/board \
        --add-flags $out/share/message-board/board.py
      makeWrapper ${pythonEnv}/bin/python3 $out/bin/board-server \
        --add-flags $out/share/message-board/board_server.py \
        --set BOARD_STATIC_DIR $out/share/message-board/static
      makeWrapper ${pythonEnv}/bin/python3 $out/bin/board-admin \
        --add-flags $out/share/message-board/board-admin.py
    '';

    doCheck = true;
    checkPhase = ''
      ${pythonEnv}/bin/python3 -m py_compile board.py board_core.py board_server.py board-admin.py
    '';

    meta = {
      description = "Local long-poll message board (area/topic) for agent coordination";
      mainProgram = "board";
    };
  }
