{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = "v0.35.0";

  src = sources.rtk;

  cargoHash = "sha256-r/PCA15MsmERCq3z8nObxdbX3KijsrInxsgJ6aqRVc4=";

  nativeBuildInputs = [pkgs.pkg-config];
  buildInputs = [pkgs.sqlite];
  nativeCheckInputs = [pkgs.git pkgs.which];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';
}
