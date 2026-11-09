# Packages feel-mcp-server from camunda/mcp monorepo.
# Calls an external FEEL evaluation API (feel.upgradingdave.com) via stdio MCP.
{
  pkgs,
  sources,
  ...
}: let
  python3Packages = pkgs.python3Packages;
in
  python3Packages.buildPythonPackage {
    pname = "feel-mcp-server";
    version = "1.0.0";

    pyproject = true;

    src = "${sources.mcp}/feel-mcp-server";

    build-system = with python3Packages; [
      setuptools
      wheel
    ];

    dependencies = with python3Packages; [
      mcp
      httpx
      pydantic
    ];

    meta = with pkgs.lib; {
      description = "MCP Server for FEEL expression validation using live API";
      homepage = "https://github.com/camunda/mcp";
      license = licenses.asl20;
      mainProgram = "feel-mcp-server";
    };
  }
