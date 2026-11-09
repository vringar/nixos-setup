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
            # nixpkgs' cargo-setup-hook forces crates onto system libraries
            # (AWS_LC_SYS_USE_SYSTEM, ZSTD_SYS_USE_PKG_CONFIG), but the primp
            # expression provides none of them, so maturin fails with "Can't probe
            # for zstd in pkg-config" and then "AWS_LC_SYS_USE_SYSTEM=1 requires a
            # system-installed AWS-LC". Remove once nixpkgs' primp declares them.
            primp = pyprev.primp.overridePythonAttrs (old: {
              nativeBuildInputs = (old.nativeBuildInputs or []) ++ [final.pkg-config];
              buildInputs = (old.buildInputs or []) ++ [final.zstd final.aws-lc];
            });
            # Same hook change for chromadb: its crates (libsqlite3-sys,
            # tikv-jemalloc-sys) are forced onto system libraries the expression does
            # not provide, failing with "cannot find -lsqlite3" and "Path to
            # `jemalloc` ... does not exist". Let it build the bundled ones. Remove
            # once nixpkgs' chromadb declares its system libraries.
            chromadb = pyprev.chromadb.overridePythonAttrs {dontCargoUseSystemLibs = true;};
          })
        ];
    })
  ];
}
