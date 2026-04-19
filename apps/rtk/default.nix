{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "rtk";
  version = sources.rtk.version;

  src = sources.rtk;

  cargoHash = "sha256-Vr1WKy+poeJnqjV7LvekC/jV1jolJDgxwNUp229EEWk=";

  nativeBuildInputs = [ pkgs.pkg-config ];
  buildInputs = [ pkgs.sqlite ];
  nativeCheckInputs = [
    pkgs.git
    pkgs.which
  ];

  preCheck = ''
    export HOME=$(mktemp -d)
  '';
}
