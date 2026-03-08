{
  pkgs,
  sources,
}:
pkgs.python3Packages.buildPythonApplication {
  pname = "cpitd";
  version = "0-unstable";
  pyproject = true;

  src = sources.cpitd;

  build-system = with pkgs.python3Packages; [
    setuptools
    setuptools-scm
  ];

  dependencies = with pkgs.python3Packages; [
    click
    pygments
    tomli
  ];

  env.SETUPTOOLS_SCM_PRETEND_VERSION = "0.2.2";
}
