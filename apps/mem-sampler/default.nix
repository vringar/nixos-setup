# Memory sampling and after-the-fact reconstruction for sz1.
#
# The kernel exposes memory as live gauges and since-boot counters, neither of
# which remembers anything. Reading them after an incident describes the present,
# and reading a counter once describes nothing at all -- so a past outage is only
# reconstructable if something wrote the numbers down as they went.
#
# Two entry points, deliberately split by dependency weight: the collector runs
# resident and holds to the standard library alone, while the report is a
# short-lived process that may pull in the plotting stack.
{
  lib,
  python3,
  python3Packages,
}:
python3Packages.buildPythonApplication {
  pname = "mem-sampler";
  version = "1.0.0";
  format = "other";

  src = ./.;

  # Only the report needs these. The collector imports nothing outside the
  # standard library, which is what lets it be the one process that must not
  # fail while the machine is under memory pressure.
  propagatedBuildInputs = with python3Packages; [
    matplotlib
    numpy
  ];

  nativeCheckInputs = [python3Packages.pytest];

  installPhase = ''
    runHook preInstall
    install -Dm755 mem_sampler.py $out/bin/mem-sampler
    install -Dm755 mem_report.py $out/bin/mem-report
    runHook postInstall
  '';

  # The parsers are the part that silently rots when a kernel changes a format,
  # so they are exercised in the build sandbox rather than trusted.
  checkPhase = ''
    runHook preCheck
    MEM_SAMPLER_SRC=$PWD pytest ${../../tests/test_mem_sampler.py} -q
    runHook postCheck
  '';

  meta = with lib; {
    description = "Sample kernel memory gauges into the journal and plot them back";
    mainProgram = "mem-sampler";
    platforms = platforms.linux;
    license = licenses.mit;
  };
}
