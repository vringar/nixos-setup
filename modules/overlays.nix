# System-wide nixpkgs overlays.
#
# Keep this scoped to upstream-bug workarounds and local package tweaks. Each
# entry should note why it exists and when it can be dropped.
{...}: {
  nixpkgs.overlays = [
    (final: prev: {
      pythonPackagesExtensions =
        prev.pythonPackagesExtensions
        ++ [
          (pyfinal: pyprev: {
            # nixpkgs marks posthog broken while mcp >= 2 (meta.broken =
            # versionAtLeast mcp.version "2"): upstream pins the test-only
            # extra to mcp<2, and the posthog.mcp integration tests do not
            # run against mcp 2. mcp is only a check input (peer dependency of
            # posthog.mcp, not imported by `import posthog`), so skip those
            # tests and clear the flag. Remove once nixpkgs' posthog is
            # compatible with mcp 2 (broken condition gone).
            posthog = pyprev.posthog.overridePythonAttrs (old: {
              disabledTestPaths = (old.disabledTestPaths or []) ++ ["posthog/test/mcp"];
              meta = old.meta // {broken = false;};
            });
          })
        ];
    })
  ];
}
