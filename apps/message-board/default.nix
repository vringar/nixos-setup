{
  pkgs,
  sources,
}: let
  inherit (pkgs) lib;
in
  # Three commands over one library (src/lib.rs: storage, signing, and the
  # Python text rules the interfaces were defined with):
  #   board         agent-facing HTTP client (post / read / watch / pin ...)
  #   board-server  the HTTP server + web view; serves htmx (from the pinned
  #                 source) and board-sign.js (the web view's signing)
  #   board-admin   the TUI that permanently deletes threads and releases
  #                 name claims, straight on the DB; not reachable from `board`
  pkgs.rustPlatform.buildRustPackage {
    pname = "message-board";
    version = "0.2.0";

    src = lib.fileset.toSource {
      root = ./.;
      fileset = lib.fileset.unions [
        ./Cargo.toml
        ./Cargo.lock
        ./rustfmt.toml
        ./src
        ./tests/sign-vectors.json
        ./static/board-sign.js
      ];
    };

    cargoLock.lockFile = ./Cargo.lock;

    nativeBuildInputs = [
      pkgs.clippy
      pkgs.makeWrapper
      pkgs.pkg-config
      pkgs.rustfmt
    ];
    # The system SQLite, not a bundled copy.
    buildInputs = [pkgs.sqlite];

    # The lints in Cargo.toml (clippy pedantic; no unwrap, expect, panic or
    # indexing; ...) and the formatting are part of the build, not a
    # developer's habit: a warning fails it. cargo test runs after this.
    preCheck = ''
      cargo clippy --offline --all-targets -- -D warnings
      cargo fmt --check
    '';

    postInstall = ''
      install -Dm644 ${sources.htmx}/dist/htmx.min.js static/board-sign.js \
        -t $out/share/message-board/static
      wrapProgram $out/bin/board-server \
        --set BOARD_STATIC_DIR $out/share/message-board/static
    '';

    meta = {
      description = "Local long-poll message board (area/topic) for agent coordination";
      mainProgram = "board";
    };
  }
