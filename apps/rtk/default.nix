{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-tgW6il/xLxt/xwhUBJ4MNVnk0JSZ7iFjJaEobj5+H4o=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];
  nativeCheckInputs = [
    pkgs.git
    pkgs.which
  ];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';

  # Flaky: parallel test threads share one tracker DB, so a concurrent
  # test can push this test's record out of the "recent 10" window
  # before it reads back.
  checkFlags = ["--skip=core::tracking::tests::test_tracker_record_and_recent"];
}
