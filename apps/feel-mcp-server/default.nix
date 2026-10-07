# Packages feel-mcp-server from camunda/mcp monorepo.
# Calls an external FEEL evaluation API (feel.upgradingdave.com) via stdio MCP.
{
  pkgs,
  sources,
  ...
}: let
  python3Packages = pkgs.python3Packages;

  # feel-mcp-server pins mcp==2.2.0 and uses the 2.x MCPServer API; nixpkgs
  # still ships 1.x. Drop these overrides once nixpkgs catches up.
  mcpVersion = "2.2.0";
  mcpSrc = pkgs.fetchFromGitHub {
    owner = "modelcontextprotocol";
    repo = "python-sdk";
    tag = "v${mcpVersion}";
    hash = "sha256-nnpNXnQiFSGx5KQBDXHCOH0wE3cznlmI6s7vtchcj3A=";
  };

  # 2.x splits the wire types into a sibling package in the same repo.
  mcp-types = python3Packages.buildPythonPackage {
    pname = "mcp-types";
    version = mcpVersion;
    pyproject = true;
    src = mcpSrc;
    sourceRoot = "${mcpSrc.name}/src/mcp-types";
    build-system = with python3Packages; [
      hatchling
      uv-dynamic-versioning
    ];
    dependencies = with python3Packages; [
      pydantic
      typing-extensions
    ];
    pythonImportsCheck = ["mcp_types"];
  };

  mcp = python3Packages.mcp.overrideAttrs (old: {
    version = mcpVersion;
    src = mcpSrc;
    # The inherited test selection targets 1.x.
    doCheck = false;
    doInstallCheck = false;
    nativeCheckInputs = [];
    pythonRelaxDeps = [];
    propagatedBuildInputs = with python3Packages; [
      anyio
      httpx2
      jsonschema
      mcp-types
      opentelemetry-api
      pydantic
      pyjwt
      python-multipart
      sse-starlette
      starlette
      typing-extensions
      typing-inspection
      uvicorn
    ];
  });
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

    dependencies =
      [mcp]
      ++ (with python3Packages; [
        httpx
        pydantic
      ]);

    meta = with pkgs.lib; {
      description = "MCP Server for FEEL expression validation using live API";
      homepage = "https://github.com/camunda/mcp";
      license = licenses.asl20;
      mainProgram = "feel-mcp-server";
    };
  }
