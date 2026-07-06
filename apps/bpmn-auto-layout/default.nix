# Packages bpmn-auto-layout from bpmn-io/bpmn-auto-layout.
# Source is tracked in npins; update with `npins update bpmn-auto-layout`.
# If npmDepsHash is stale, set it to "" and rebuild — Nix will print the correct value.
{
  pkgs,
  sources,
  scriptSrc,
}:
pkgs.buildNpmPackage {
  pname = "bpmn-auto-layout";
  version =
    (builtins.fromJSON (builtins.readFile "${sources.bpmn-auto-layout}/package.json")).version;

  src = sources.bpmn-auto-layout;

  nodejs = pkgs.nodejs_22;
  npmDepsHash = "sha256-qnXmPQzIapXpPb41yTwWcaxmdpTZqLdKGHIUcCftqxc=";

  dontNpmBuild = true;

  nativeBuildInputs = [pkgs.makeWrapper];

  postInstall = ''
    # Remove dangling workspace symlink from upstream monorepo that Nix rejects.
    rm -f $out/lib/node_modules/bpmn-auto-layout/node_modules/example

    install -D -m755 ${scriptSrc} $out/bin/bpmn-auto-layout
    wrapProgram $out/bin/bpmn-auto-layout \
      --prefix NODE_PATH : $out/lib/node_modules
  '';

  meta = with pkgs.lib; {
    description = "Automatically layout BPMN diagrams (regenerates bpmndi coordinates)";
    homepage = "https://github.com/bpmn-io/bpmn-auto-layout";
    license = licenses.mit;
    mainProgram = "bpmn-auto-layout";
  };
}
