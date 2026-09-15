{pkgs}:
pkgs.buildNpmPackage {
  pname = "c8ctl";
  version = "4.2.0-alpha.2";

  src = pkgs.fetchurl {
    url = "https://registry.npmjs.org/@camunda8/cli/-/cli-4.2.0-alpha.2.tgz";
    hash = "sha256-n6jq9P/0sK7ZSI80rzZErvYt3oV2pZ6cVzJ/34j0tmk=";
  };
  sourceRoot = "package";

  nodejs = pkgs.nodejs_22;
  nativeBuildInputs = [pkgs.python3];
  npmDepsFetcherVersion = 2;
  npmDepsHash = "sha256-8gUM/4skzUBR9YLnnm1L0IA0oh+Obv0caHh8tM3aHm8=";
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
