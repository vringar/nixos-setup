{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = "0.34.0";

  src = sources.rtk;

  cargoHash = "sha256-b+4q5xf+g5MNZ/c0AwRF9vUQGKIbTezt3dE4VQIVQPE=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];
  nativeCheckInputs = [pkgs.git pkgs.which];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';
}
