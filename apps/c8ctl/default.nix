{pkgs}:
pkgs.buildNpmPackage {
  pname = "c8ctl";
  version = "3.2.0-alpha.1";

  src = pkgs.fetchurl {
    url = "https://registry.npmjs.org/@camunda8/cli/-/cli-3.2.0-alpha.1.tgz";
    hash = "sha256-FTjzR/HUdqTECDxtRrPT0nw0RXXSqtnE904WMilP2Ag=";
  };
  sourceRoot = "package";

  nodejs = pkgs.nodejs_22;
  nativeBuildInputs = [pkgs.python3];
  npmDepsFetcherVersion = 3;
  npmDepsHash = "sha256-oGCXMrHPgRJBCGUO6DPeLJ0sO6K35p06f79ep59Mb+Y=";
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
