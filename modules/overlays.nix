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
            # uefi-firmware-parser 1.16 added a setuptools-scm build-backend
            # requirement that the nixpkgs expression doesn't provide, so its
            # build fails with "Missing dependencies: setuptools-scm>=8.0".
            # Pulled in via ghidra → ghidraninja-ghidra-scripts → binwalk.
            # Remove once nixpkgs ships the fix.
            uefi-firmware-parser = pyprev.uefi-firmware-parser.overridePythonAttrs (old: {
              build-system = (old.build-system or []) ++ [pyfinal.setuptools-scm];
              env = (old.env or {}) // {SETUPTOOLS_SCM_PRETEND_VERSION = old.version;};
            });
          })
        ];
    })
  ];
}
