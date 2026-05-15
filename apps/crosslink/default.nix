{
  pkgs,
  sources,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "crosslink";
  version = "0-unstable";

  src = sources.crosslink;
  sourceRoot = "source/crosslink";

  cargoHash = "sha256-QmeFC6ZhZ3sZVbUsDQogvqnk97kza05mX7aNACVyDTg=";

  nativeBuildInputs = [
    pkgs.pkg-config
    pkgs.installShellFiles
  ];
  buildInputs = [pkgs.sqlite];

  nativeCheckInputs = [
    pkgs.git
    pkgs.which
  ];

  # The crate embeds dashboard/dist/ via rust-embed. The React frontend is
  # built separately and dist/ is gitignored, so it is absent from source.
  # Stub a minimal index.html so the crate compiles; the dashboard route
  # serves this placeholder instead of the real SPA. sourceRoot only makes
  # source/crosslink writable, so the sibling dashboard dir needs chmod.
  postPatch = ''
    chmod -R u+w ../dashboard
    mkdir -p ../dashboard/dist
    cat > ../dashboard/dist/index.html <<'EOF'
    <!doctype html>
    <title>crosslink dashboard — not built</title>
    <p>This binary was built without the React dashboard frontend.</p>
    EOF
  '';

  postInstall = ''
    bash ${./generate-completions.sh} $out/bin/crosslink > _crosslink
    installShellCompletion --zsh --name _crosslink _crosslink
  '';
}
