{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "crosslink";
  version = "0-unstable";

  src = sources.crosslink;
  sourceRoot = "source/crosslink";

  cargoHash = "sha256-BMqlaiZn8ve4nNkiSCi5t6x4NxvPn+I0xrVkRZ3D+CU=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];

  nativeCheckInputs = [pkgs.git pkgs.which];
}
