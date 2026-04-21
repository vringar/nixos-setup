# Packages element-templates-cli from vringar's fork at PR #44
# (feat/query-set-subcommands: adds query and set subcommands for agentic workflows)
# https://github.com/bpmn-io/element-templates-cli/pull/44
#
# The fork depends on a sibling GitHub fork of bpmn-js-element-templates
# (feat/export-util-subpath), which is not on npm yet. We build it separately
# and inject it into node_modules before the esbuild bundling step.
{pkgs, ...}: let
  # vringar/bpmn-js-element-templates at feat/export-util-subpath
  # Needed because the PR uses subpath exports not yet in the published 2.23.0.
  bpmnJsElementTemplates = pkgs.buildNpmPackage {
    pname = "bpmn-js-element-templates";
    version = "2.23.0-vringar";

    src = pkgs.fetchFromGitHub {
      owner = "vringar";
      repo = "bpmn-js-element-templates";
      rev = "aa9e6c9dd0bb17fbebea99392d8264c2d6c6cf14";
      hash = "sha256-R7Dy7qhSh2hbgjJsJWWy5wVQAhjmYNPtDRW3SCYl90s=";
    };

    npmDepsHash = "sha256-UMXxBAcSkwwY6FBNv63hpsi9dFFgbOCJPv0v97CWD20=";
    npmBuildScript = "build";

    env.PUPPETEER_SKIP_DOWNLOAD = "1";
  };

  # Update by setting both hashes to "" and rebuilding.
  etCliRev = "feat/query-set-subcommands";
  etCliHash = "sha256-MDIHDsckjqrYB6zGmLiVDClMuNRZu+2hrJxTtP6nWZQ=";
  etCliNpmDepsHash = "sha256-WkYlgSsaHbmE0nvO857WBKltTKmAB1kcROqaDTKoyJc=";

  rawSrc = pkgs.fetchFromGitHub {
    owner = "vringar";
    repo = "element-templates-cli";
    rev = etCliRev;
    hash = etCliHash;
  };

  # Strip the git dep from package.json and package-lock.json so that
  # fetchNpmDeps (which runs npm ci in a FOD) doesn't try to clone over SSH.
  # We inject the pre-built package into node_modules in preBuild instead.
  src =
    pkgs.runCommand "element-templates-cli-src" {
      nativeBuildInputs = [pkgs.jq];
    } ''
      cp -r ${rawSrc} $out
      chmod -R u+w $out

      jq 'del(.devDependencies["bpmn-js-element-templates"])' \
        $out/package.json > $out/package.json.tmp
      mv $out/package.json.tmp $out/package.json

      jq '
        del(.packages[""].devDependencies["bpmn-js-element-templates"])
        | del(.packages["node_modules/bpmn-js-element-templates"])
      ' $out/package-lock.json > $out/package-lock.json.tmp
      mv $out/package-lock.json.tmp $out/package-lock.json
    '';
in
  pkgs.buildNpmPackage {
    pname = "element-templates-cli";
    version = "0.5.0-pr44";
    inherit src;

    npmDepsHash = etCliNpmDepsHash;
    npmBuildScript = "build";

    preBuild = ''
      mkdir -p node_modules/bpmn-js-element-templates
      cp -r ${bpmnJsElementTemplates}/lib/node_modules/bpmn-js-element-templates/. \
        node_modules/bpmn-js-element-templates/
    '';

    meta = with pkgs.lib; {
      description = "Apply element templates on BPMN elements in your terminal";
      homepage = "https://github.com/bpmn-io/element-templates-cli";
      license = licenses.mit;
      mainProgram = "element-templates-cli";
    };
  }
