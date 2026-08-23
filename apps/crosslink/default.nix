{
  pkgs,
  sources,
  doCheck ? true,
}:
pkgs.rustPlatform.buildRustPackage {
  pname = "crosslink";
  version = "0-unstable";

  src = sources.crosslink;
  sourceRoot = "source/crosslink";

  cargoHash = "sha256-GJ76eg/YHsUcl8/u9YRDX92uPe7Mja3Y6yANO8pATp4=";

  nativeBuildInputs = [
    pkgs.pkg-config
    pkgs.installShellFiles
  ];
  buildInputs = [pkgs.sqlite];

  inherit doCheck;
  # nextest instead of cargo test: same green-tests gate, but per-test timing
  # in the log (the suite's cost is concentrated in a few huge proptests).
  useNextest = true;

  nativeCheckInputs = [
    pkgs.git
    pkgs.which
    # provider_hooks tests spawn the agent hook scripts with python3; without
    # it the spawn fails ENOENT rather than reporting a hook mismatch.
    pkgs.python3
  ];

  # The db proptests run 8-18 min each at proptest's default 256 cases and
  # dominate the ~50 min suite. 64 cases keeps the deploy gate meaningful at
  # roughly a quarter of the wall time; upstream CI still runs full strength.
  preCheck = ''
    export PROPTEST_CASES=64
  '';

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
