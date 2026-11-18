# Packages @camunda8/c8ctl-plugin-process-os from the npm tarball.
# The tarball ships prebuilt JS and has no runtime dependencies, so it is
# installed as-is. Bump version + hash together.
{pkgs}:
pkgs.stdenvNoCC.mkDerivation rec {
  pname = "c8ctl-plugin-process-os";
  version = "0.6.0";

  src = pkgs.fetchurl {
    url = "https://registry.npmjs.org/@camunda8/${pname}/-/${pname}-${version}.tgz";
    hash = "sha256-+yO7b5rUadMGjr85sJmEwRPMpLa8BJ9mg9JRB0PoVsA=";
  };
  sourceRoot = "package";

  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p $out/lib/node_modules/@camunda8/${pname}
    cp -r . $out/lib/node_modules/@camunda8/${pname}
    runHook postInstall
  '';

  meta = {
    description = "c8ctl plugin exposing the ProcessOS lifecycle as CLI commands";
    homepage = "https://github.com/camunda/c8ctl-plugin-process-os";
    license = pkgs.lib.licenses.asl20;
  };
}
