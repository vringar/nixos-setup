{pkgs}:
pkgs.stdenv.mkDerivation {
  pname = "open-plc-utils";
  version = "0-unstable-2025-02-19";

  src = pkgs.fetchFromGitHub {
    owner = "qca";
    repo = "open-plc-utils";
    rev = "46c3506453c15b873fd6ed3e76c9872cea5e143a";
    hash = "sha256-KjvJFCYClNloOtPwFi1mU9xIoiYYcz7oIfw+9y3RtN4=";
  };

  # The Makefiles try to chown to root and set SUID bits during install,
  # which doesn't work in the Nix sandbox. We do a manual install instead.
  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    make all
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall

    # Install binaries
    mkdir -p $out/bin
    for dir in ether key mdio mme nvm pib plc ram serial slac; do
      find "$dir" -maxdepth 1 -executable -type f -exec install -m 0755 {} $out/bin/ \;
    done

    # Install man pages
    mkdir -p $out/share/man/man1
    find . -name '*.1' -exec install -m 0444 {} $out/share/man/man1/ \;

    # Install HTML documentation
    mkdir -p $out/share/doc/open-plc-utils
    cp -r docbook/*.html $out/share/doc/open-plc-utils/
    cp -r docbook/*.png $out/share/doc/open-plc-utils/ 2>/dev/null || true
    cp -r docbook/*.css $out/share/doc/open-plc-utils/ 2>/dev/null || true

    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "Qualcomm Atheros open-source HomePlug AV powerline toolkit";
    homepage = "https://github.com/qca/open-plc-utils";
    license = licenses.bsd3;
    platforms = platforms.linux;
  };
}
