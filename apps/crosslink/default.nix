{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "crosslink";
  version = "0-unstable";

  src = sources.crosslink;
  sourceRoot = "source/crosslink";

  cargoHash = "sha256-cAZi6atfZWXOmt05jwiREQIrTh/Gm2VOf81HEgtZTyc=";

  nativeBuildInputs = [pkgs.pkg-config pkgs.installShellFiles];
  buildInputs = [pkgs.sqlite];

  nativeCheckInputs = [pkgs.git pkgs.which];

  postInstall = ''
    bash ${./generate-completions.sh} $out/bin/crosslink > _crosslink
    installShellCompletion --zsh --name _crosslink _crosslink
  '';
}
