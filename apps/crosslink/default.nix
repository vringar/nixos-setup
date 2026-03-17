{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "crosslink";
  version = "0-unstable";

  src = sources.crosslink;
  sourceRoot = "source/crosslink";

  cargoHash = "sha256-MKVShGieuHWreLMnkwSF/mSbB8l7FTcV29Rg+t7X6rs=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];

  nativeCheckInputs = [pkgs.git pkgs.which];
}
