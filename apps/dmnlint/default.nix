# Packages dmnlint (https://github.com/bpmn-io/dmnlint).
# Source is tracked in npins; update with `npins update dmnlint`.
# If npmDepsHash is stale, set it to "" and rebuild — Nix will print the correct value.
{
  pkgs,
  sources,
}:
pkgs.buildNpmPackage {
  pname = "dmnlint";
  version =
    (builtins.fromJSON (builtins.readFile "${sources.dmnlint}/package.json")).version;

  src = sources.dmnlint;

  npmDepsHash = "sha256-pbslgjCqoQLc7sUVCFyOb99d8ltvWL0rPuX69bF/XGM=";

  dontNpmBuild = true;

  meta = with pkgs.lib; {
    description = "Lint DMN diagrams based on configurable lint rules";
    homepage = "https://github.com/bpmn-io/dmnlint";
    license = licenses.mit;
    mainProgram = "dmnlint";
  };
}
