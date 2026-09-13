{
  pkgs,
  sources,
}: let
  inherit (pkgs) lib;
  # The core CLI is stdlib; only `board serve`'s web view needs jinja2.
  pythonEnv = pkgs.python3.withPackages (ps: [ps.jinja2]);
in
  pkgs.stdenv.mkDerivation {
    pname = "message-board";
    version = "0.1.0";

    src = lib.fileset.toSource {
      root = ./.;
      fileset = ./board.py;
    };

    nativeBuildInputs = [pkgs.makeWrapper];

    dontConfigure = true;
    dontBuild = true;

    # One `board` command (subcommands serve / post / watch / areas). The web
    # view serves htmx from the pinned source; the wrapper points the server at
    # it via BOARD_STATIC_DIR.
    installPhase = ''
      install -Dm644 board.py $out/share/message-board/board.py
      install -Dm644 ${sources.htmx}/dist/htmx.min.js \
        $out/share/message-board/static/htmx.min.js
      makeWrapper ${pythonEnv}/bin/python3 $out/bin/board \
        --add-flags $out/share/message-board/board.py \
        --set BOARD_STATIC_DIR $out/share/message-board/static
    '';

    doCheck = true;
    checkPhase = ''
      ${pythonEnv}/bin/python3 -m py_compile board.py
    '';

    meta = {
      description = "Local long-poll message board (area/topic) for agent coordination";
      mainProgram = "board";
    };
  }
