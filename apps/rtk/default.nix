{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-CFhKBzJc2/+gZDfHq7wxBWEbtHV8EF3OYa+t1b9aL8k=";

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
