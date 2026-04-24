# Packages bpmnlint (https://github.com/bpmn-io/bpmnlint).
# Source is tracked in npins; update with `npins update bpmnlint`.
# If npmDepsHash is stale, set it to "" and rebuild — Nix will print the correct value.
#
# Personal patch: bundles bpmnlint-plugin-camunda-ai (from sources.bpmnlint-aitools)
# and rewrites bpmnlint:recommended to include its rules, so any .bpmnlintrc that
# extends "bpmnlint:recommended" silently picks them up. Not visible to other devs.
{
  pkgs,
  sources,
}: let
  # Single source of truth for which plugin rules are active and at what severity.
  # Lives in the plugin repo; if it adds/renames rules, this picks them up on the
  # next `npins update bpmnlint-aitools` with no edits here.
  pluginRules =
    builtins.toJSON
    (builtins.fromJSON (builtins.readFile
        "${sources.bpmnlint-aitools}/bpmnlint-plugin-camunda-ai/.bpmnlintrc")).rules;
in
  pkgs.buildNpmPackage {
    pname = "bpmnlint";
    version = (builtins.fromJSON (builtins.readFile "${sources.bpmnlint}/package.json")).version;

    src = sources.bpmnlint;

    npmDepsHash = "sha256-pvQPc5mlkO+5W5l8HLYICuA6wH6BIRSxPH6px+ThYnU=";

    # bpmnlint has no build step — the CLI and lib are plain JS.
    dontNpmBuild = true;

    env.PUPPETEER_SKIP_DOWNLOAD = "1";

    nativeBuildInputs = [pkgs.makeWrapper];

    postInstall = ''
      install -d $out/lib/node_modules/bpmnlint-plugin-camunda-ai
      cp -r ${sources.bpmnlint-aitools}/bpmnlint-plugin-camunda-ai/. \
        $out/lib/node_modules/bpmnlint-plugin-camunda-ai/

      cat > $out/lib/node_modules/bpmnlint/config/recommended.js <<EOF
      const upstream = require('./recommended-upstream');
      module.exports = {
        ...upstream,
        rules: { ...upstream.rules, ...${pluginRules} },
      };
      EOF

      # bpmnlint's NodeResolver scopes require() to the user's CWD, so the bundled
      # plugin is invisible from arbitrary projects. NODE_PATH is consulted as a
      # fallback by node's module resolution — point it at our store.
      wrapProgram $out/bin/bpmnlint \
        --prefix NODE_PATH : $out/lib/node_modules
    '';

    # Stash the original recommended so the wrapper above can re-require it.
    postPatch = ''
      cp config/recommended.js config/recommended-upstream.js
    '';

    meta = with pkgs.lib; {
      description = "Validate your BPMN diagrams based on configurable lint rules";
      homepage = "https://github.com/bpmn-io/bpmnlint";
      license = licenses.mit;
      mainProgram = "bpmnlint";
    };
  }
