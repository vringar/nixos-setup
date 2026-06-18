# Packages c8ctl-plugin-model from camunda/c8ctl-plugin-model.
# Source is tracked in npins; update with `npins update c8ctl-plugin-model`.
# If npmDepsHash is stale, set it to "" and rebuild — Nix will print the correct value.
{
  pkgs,
  sources,
}:
pkgs.buildNpmPackage {
  pname = "c8ctl-plugin-model";
  version =
    (builtins.fromJSON (builtins.readFile "${sources.c8ctl-plugin-model}/package.json")).version;

  src = sources.c8ctl-plugin-model;

  nodejs = pkgs.nodejs_22;
  npmDepsHash = "sha256-1YWJb1iNkU9SloMUstPkpZNzo6d0zpN7YsG3aIlg6LU=";

  meta = with pkgs.lib; {
    description = "c8ctl plugin for modeling BPMN processes from the CLI";
    homepage = "https://github.com/camunda/c8ctl-plugin-model";
    license = licenses.mit;
  };
}
