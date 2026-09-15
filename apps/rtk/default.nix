{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-cgRtXTd75uKInBnf6dP6e4KHyA2IP9lLEKwVzGq16gg=";

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
  checkFlags = [
    "--test-threads=1"
    # Waits on a PATH shim being exec'd by a spawned child; never observes
    # the shim run inside the Nix build sandbox (works outside it).
    "--skip=signalled_run_still_prints_captured_output"
  ];
}
