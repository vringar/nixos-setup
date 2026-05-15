{
  pkgs,
  sources,
}: let
  inherit (pkgs) lib;
in
  pkgs.rustPlatform.buildRustPackage {
    pname = "crossbridge";
    version = "0-unstable";

    src = sources.crossbridge;

    # Compile against the device's crosslink source via a path patch, so
    # crossbridge always links the exact crosslink the rest of the system
    # uses (its on-disk SQLite schema is internal and unstable). The patch
    # overrides whatever rev crossbridge's Cargo.lock points at.
    #
    # crosslink is copied into a writable tree first: its rust-embed derive
    # needs a dashboard/dist/ directory to exist at compile time, and the
    # npins store path is read-only. The React dashboard frontend is not
    # built here — a placeholder index.html is stubbed in instead.
    postPatch = ''
      cp -r ${sources.crosslink} ./crosslink-src
      chmod -R u+w ./crosslink-src
      mkdir -p ./crosslink-src/dashboard/dist
      cat > ./crosslink-src/dashboard/dist/index.html <<'EOF'
      <!doctype html>
      <title>crosslink dashboard — not built</title>
      EOF
      cat >> Cargo.toml <<EOF

      [patch."https://github.com/forecast-bio/crosslink.git"]
      crosslink = { path = "./crosslink-src/crosslink" }
      EOF
    '';

    cargoHash = "sha256-w3WqkEybEyMpawr4somXSp8dC9YXWJvd7/eBrbFAe6s=";

    nativeBuildInputs = [pkgs.pkg-config];
    buildInputs = [pkgs.sqlite];

    nativeCheckInputs = [
      pkgs.git
      pkgs.which
    ];

    meta = with lib; {
      description = "Cross-project coordination bridge for crosslink repositories";
      license = licenses.mit;
      mainProgram = "crossbridge-supervisor";
    };
  }
