{pkgs}:
pkgs.buildNpmPackage {
  pname = "c8ctl";
  version = "4.3.0-alpha.3";

  src = pkgs.fetchurl {
    url = "https://registry.npmjs.org/@camunda8/cli/-/cli-4.3.0-alpha.3.tgz";
    hash = "sha256-UN5B2P3cZCU13omOCQ4t4Tx2Z482vRv1NlRtOQ1U7Kw=";
  };
  sourceRoot = "package";

  nodejs = pkgs.nodejs_22;
  nativeBuildInputs = [pkgs.python3];
  npmDepsFetcherVersion = 2;
  npmDepsHash = "sha256-O7J+vkVcRQw/YByOC4uoaZEBVSr9D331YU3IXe4k2CU=";
  dontNpmBuild = true;

  postPatch = ''
    cp ${./package-lock.json} package-lock.json
    ${pkgs.python3}/bin/python3 <<'PY'
    import json
    from pathlib import Path

    package_json = Path("package.json")
    data = json.loads(package_json.read_text())
    data.pop("devDependencies", None)
    package_json.write_text(json.dumps(data, indent=2) + "\n")
    PY
  '';

  meta = {
    description = "Camunda 8 CLI";
    homepage = "https://github.com/camunda/c8ctl";
    downloadPage = "https://www.npmjs.com/package/@camunda8/cli";
    license = pkgs.lib.licenses.mit;
    mainProgram = "c8ctl";
  };
}
