# Node.js library dependencies for cherry-picked process-os skills.
# Built from the lock files shipped inside process-os; update by running
# `npins update process-os` and setting npmDepsHash values back to "" to
# trigger a re-hash on the next build.
#
# Output is a flat node_modules directory; set NODE_PATH to $out so that
# skill scripts can require() these packages without network access.
{
  pkgs,
  sources,
}: let
  nodeModulesFrom = {
    pname,
    src,
    npmDepsHash,
  }:
    pkgs.buildNpmPackage {
      inherit pname src npmDepsHash;
      version = "0";
      nodejs = pkgs.nodejs_22;
      dontNpmBuild = true;
      installPhase = ''
        runHook preInstall
        cp -r node_modules $out
        runHook postInstall
      '';
    };

  compatDeps = nodeModulesFrom {
    pname = "lint-camunda-compat-deps";
    src = "${sources."process-os"}/skills/lint-camunda-compat";
    npmDepsHash = "sha256-fEePOggnSt9vlagqnX5tujjsM2pq4fNyNnaXuaNZk/Q=";
  };

  formsDeps = nodeModulesFrom {
    pname = "lint-forms-deps";
    src = "${sources."process-os"}/skills/lint-forms";
    npmDepsHash = "sha256-CzaZFHbnkS29uD0xE8k6PJQJ94BlSZIck2pZTxEYXcU=";
  };

  autoLayoutDeps = nodeModulesFrom {
    pname = "bpmn-auto-layout-deps";
    src = "${sources."process-os"}/skills/bpmn-generate/scripts";
    npmDepsHash = "sha256-ETOAZLb2KlmpTMJ2nkdTSi9tEQdXwu0oT0qpRBJM03A=";
  };
in
  pkgs.symlinkJoin {
    name = "process-os-skills-node-modules";
    paths = [compatDeps formsDeps autoLayoutDeps];
  }
