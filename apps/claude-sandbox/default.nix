{pkgs}: let
  python3 = pkgs.python3;
  python3WithShtab = python3.withPackages (ps: [ps.shtab]);
in
  pkgs.stdenv.mkDerivation {
    pname = "claude-sandbox";
    version = "0.1.0";

    src = ./.;

    nativeBuildInputs = [python3WithShtab];

    buildInputs = [python3];

    dontConfigure = true;

    buildPhase = ''
      # Generate zsh completions from source (before substitution — parser doesn't use placeholders)
      ${python3WithShtab}/bin/python3 -c "
      import importlib.util, shtab
      spec = importlib.util.spec_from_file_location('claude_sandbox', 'claude-sandbox.py')
      mod = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(mod)
      print(shtab.complete(mod.create_parser(), 'zsh'))
      " > _claude-sandbox

      substitute claude-sandbox.py claude-sandbox \
        --replace-fail '@bwrap@' '${pkgs.bubblewrap}/bin/bwrap' \
        --replace-fail '@nix_shell@' '${pkgs.nix}/bin/nix-shell' \
        --replace-fail '@bash@' '${pkgs.bashInteractive}/bin/bash' \
        --replace-fail '@python3@' '${python3}/bin/python3'
    '';

    checkPhase = ''
      ${python3}/bin/python3 -m py_compile claude-sandbox
    '';

    doCheck = true;

    installPhase = ''
      install -Dm755 claude-sandbox $out/bin/claude-sandbox
      install -Dm644 _claude-sandbox $out/share/zsh/site-functions/_claude-sandbox
    '';
  }
