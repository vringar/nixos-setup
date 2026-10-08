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
    pkgs.makeWrapper
  ];
  buildInputs = [pkgs.sqlite];

  inherit doCheck;
  # nextest instead of cargo test: same green-tests gate, but per-test timing
  # in the log (the suite's cost is concentrated in a few huge proptests).
  useNextest = true;

  nativeCheckInputs = [
    pkgs.git
    pkgs.which
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

  # The `crosslink` command this package puts on PATH is gate.py, not the
  # real binary: crosslink grew its own autonomous-agent framework (kickoff/
  # swarm/sentinel/agent/daemon/...) alongside its issue tracker, and this
  # deployment only wants the issue tracker. The real binary moves to
  # crosslink-real; gate.py forwards allowed commands to it, blocks denied
  # ones with a reason, and -- for a top-level command that's neither kept
  # nor denied -- checks known-commands.txt (generated below, against this
  # same build) to tell "crosslink added something new, go review it" apart
  # from "not a real command at all, let the real binary's own error say
  # so". See apps/crosslink/gate.py for the full policy and reasoning.
  postInstall = ''
    mv $out/bin/crosslink $out/bin/crosslink-real

    bash ${./generate-completions.sh} $out/bin/crosslink-real > _crosslink
    installShellCompletion --zsh --name _crosslink _crosslink

    mkdir -p $out/share/crosslink
    bash ${./extract-known-commands.sh} $out/bin/crosslink-real \
      > $out/share/crosslink/known-commands.txt
    # A near-empty result means crosslink's `help` output changed shape and
    # the extraction silently broke -- fail the build instead of shipping a
    # gate that can no longer tell a real new command from a hallucinated
    # one (see gate.py: an empty known-commands list makes every unreviewed
    # command look fake, forwarding it unchecked).
    known_count=$(wc -l < $out/share/crosslink/known-commands.txt)
    if [ "$known_count" -lt 20 ]; then
      echo "crosslink: extract-known-commands.sh only found $known_count" \
           "top-level commands (expected 20+) -- 'crosslink help' output" \
           "shape probably changed; fix extract-known-commands.sh" >&2
      exit 1
    fi

    install -Dm644 ${./gate.py} $out/share/crosslink/gate.py
    makeWrapper ${pkgs.python3}/bin/python3 $out/bin/crosslink \
      --add-flags $out/share/crosslink/gate.py \
      --set CROSSLINK_REAL_BIN $out/bin/crosslink-real \
      --set CROSSLINK_KNOWN_COMMANDS $out/share/crosslink/known-commands.txt
  '';
}
