# Packages element-templates-cli from vringar's fork at PR #44
# (feat/query-set-subcommands: adds query and set subcommands for agentic workflows)
# https://github.com/bpmn-io/element-templates-cli/pull/44
#
# element-templates-cli imports its Modeler from bpmn-js-headless/lib/Modeler,
# which is a pre-built rollup bundle. The published 0.1.0 has a DOM-dependent
# TextRenderer; vringar/bpmn-js-headless@fix-external-label-document-dependency
# fixes this. We build that branch and inject it into node_modules before esbuild.
#
# bpmn-js-element-templates is also a git dep not yet on npm; same treatment.
{pkgs, ...}: let
  # vringar/bpmn-js-headless at fix-external-label-document-dependency.
  # Provides a DOM-free Modeler used directly by element-templates-cli.
  bpmnJsHeadless = pkgs.buildNpmPackage {
    pname = "bpmn-js-headless";
    version = "0.1.0-fix-external-label";

    src = pkgs.fetchFromGitHub {
      owner = "vringar";
      repo = "bpmn-js-headless";
      rev = "1acba2a3555c9b63aab079ec6466c18925a1597d";
      hash = "sha256-EFxEhKG24+WIcF4juIIk9dVQkfoV52fcTGV2PJt1bd4=";
    };

    npmDepsHash = "sha256-Jos2/QBAuR663FCBNfv7Uv2eRotdO99ITDawnOBx6Ds=";
    npmBuildScript = "bundle";
    makeCacheWritable = true;
  };

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
  etCliRev = "8bec16d35963de72732484cb8aba326a9c5500fb";
  etCliHash = "sha256-MDIHDsckjqrYB6zGmLiVDClMuNRZu+2hrJxTtP6nWZQ=";
  etCliNpmDepsHash = "sha256-w4YQ/V/tcWtmKFed6uT7TGHr+IqaeWBFP9D4b1Ygm0c=";

  rawSrc = pkgs.fetchFromGitHub {
    owner = "vringar";
    repo = "element-templates-cli";
    rev = etCliRev;
    hash = etCliHash;
  };

  # Strip git deps from package.json and package-lock.json so that
  # fetchNpmDeps (which runs npm ci in a FOD) doesn't try to clone over SSH.
  # We inject the pre-built packages into node_modules in preBuild instead.
  src =
    pkgs.runCommand "element-templates-cli-src" {
      nativeBuildInputs = [pkgs.jq];
    } ''
      cp -r ${rawSrc} $out
      chmod -R u+w $out

      jq 'del(.devDependencies["bpmn-js-element-templates"])
        | del(.devDependencies["bpmn-js-headless"])' \
        $out/package.json > $out/package.json.tmp
      mv $out/package.json.tmp $out/package.json

      jq '
        del(.packages[""].devDependencies["bpmn-js-element-templates"])
        | del(.packages["node_modules/bpmn-js-element-templates"])
        | del(.packages[""].devDependencies["bpmn-js-headless"])
        | del(.packages["node_modules/bpmn-js-headless"])
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

      mkdir -p node_modules/bpmn-js-headless
      cp -r ${bpmnJsHeadless}/lib/node_modules/bpmn-js-headless/. \
        node_modules/bpmn-js-headless/
    '';

    meta = with pkgs.lib; {
      description = "Apply element templates on BPMN elements in your terminal";
      homepage = "https://github.com/bpmn-io/element-templates-cli";
      license = licenses.mit;
      mainProgram = "element-templates-cli";
    };
  }
