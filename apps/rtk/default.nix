{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-UzTZOHdh/NuWtraPaZ75xsLBcdLSWHQGcE6gSp3AHDY=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];
  nativeCheckInputs = [
    pkgs.git
    pkgs.which
  ];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';

  # The tracking tests write to one tracker DB and then assert their own
  # record is still inside a small get_recent(N) window. preCheck points the
  # whole check phase at a single HOME, so parallel test threads share that
  # DB and evict each other's records: which test loses the race varies by
  # version. Serializing removes the race for all of them rather than
  # skipping them one at a time, and costs little -- the suite is ~1s.
  checkFlags = ["--test-threads=1"];
}
