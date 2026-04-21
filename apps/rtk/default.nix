{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-61+PNuVF8H5+9PHc3MBt8V80ieBBi8HzSC9Gc/WUSzM=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];
  nativeCheckInputs = [
    pkgs.git
    pkgs.which
  ];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';
}
