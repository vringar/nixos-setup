{
  pkgs,
  sources,
}: let
  inherit (pkgs) lib;
  expectedCrosslinkRev = sources.crosslink.revision;
  cargoLockText = builtins.readFile "${sources.crossbridge}/Cargo.lock";
  cargoLockMatchesCrosslink = lib.strings.hasInfix expectedCrosslinkRev cargoLockText;
in
  assert lib.assertMsg cargoLockMatchesCrosslink ''
    crossbridge/Cargo.lock pins a crosslink revision that does not match
    the npins crosslink pin (${expectedCrosslinkRev}).

    crosslink's on-disk SQLite schema is internal and unstable; crossbridge
    must compile against the exact crosslink source the device uses.

    To resolve, do one of:
      - bump npins crosslink to match the rev pinned in crossbridge upstream:
          npins update crosslink
      - bump crossbridge upstream's Cargo.toml/Cargo.lock to a rev that
        matches sources.crosslink, then `npins update crossbridge`.
  '';
    pkgs.rustPlatform.buildRustPackage {
      pname = "crossbridge";
      version = "0-unstable";

      src = sources.crossbridge;

      # Force the build to link against the device's crosslink source, not
      # whatever git rev crossbridge's Cargo.lock happens to point at. The
      # assertion above guarantees the rev matches; this guarantees the
      # actual compiled bytes match too.
      postPatch = ''
        cat >> Cargo.toml <<EOF

        [patch."https://github.com/forecast-bio/crosslink.git"]
        crosslink = { path = "${sources.crosslink}/crosslink" }
        EOF
      '';

      cargoHash = "sha256-w3WqkEybEyMpawr4somXSp8dC9YXWJvd7/eBrbFAe6s=";

      nativeBuildInputs = [ pkgs.pkg-config ];
      buildInputs = [ pkgs.sqlite ];

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
