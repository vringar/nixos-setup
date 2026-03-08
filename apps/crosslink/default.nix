{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "crosslink";
  version = "0-unstable";

  src = sources.crosslink;
  sourceRoot = "source/crosslink";

  cargoHash = "sha256-rsfnNsXrDVVTi5FYN85/XAniqo9nLIWRR1OI1iuXw3s=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];

  nativeCheckInputs = [pkgs.git pkgs.which];
}
