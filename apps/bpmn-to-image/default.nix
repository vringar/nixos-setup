# Packages bpmn-to-image (https://github.com/bpmn-io/bpmn-to-image).
# Source is tracked in npins; update with `npins update bpmn-to-image`.
# If npmDepsHash is stale, set it to "" and rebuild — Nix will print the correct value.
#
# Renders a .bpmn file to SVG/PNG via headless Chromium + bpmn-js. Wrapped with
# PUPPETEER_EXECUTABLE_PATH pointed at a Nix-provided Chromium so it runs without
# puppeteer's own ~200MB first-run browser download.
{
  pkgs,
  sources,
}:
pkgs.buildNpmPackage {
  pname = "bpmn-to-image";
  version = (builtins.fromJSON (builtins.readFile "${sources.bpmn-to-image}/package.json")).version;

  src = sources.bpmn-to-image;

  npmDepsHash = "sha256-McpACPU5BrKO3ueaiwrCEZ4ahuzwvgh/OZKfIc24/Go=";

  dontNpmBuild = true;

  env.PUPPETEER_SKIP_DOWNLOAD = "1";

  nativeBuildInputs = [pkgs.makeWrapper];

  postInstall = ''
    wrapProgram $out/bin/bpmn-to-image \
      --set PUPPETEER_EXECUTABLE_PATH ${pkgs.lib.getExe pkgs.chromium}
  '';

  meta = with pkgs.lib; {
    description = "Convert BPMN 2.0 diagrams to PDF, SVG or PNG images via headless Chromium";
    homepage = "https://github.com/bpmn-io/bpmn-to-image";
    license = licenses.mit;
    mainProgram = "bpmn-to-image";
  };
}
