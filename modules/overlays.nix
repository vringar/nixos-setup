# System-wide nixpkgs overlays.
#
# Keep this scoped to upstream-bug workarounds and local package tweaks. Each
# entry should note why it exists and when it can be dropped.
{...}: {
  nixpkgs.overlays = [
    (final: prev: {
      # nixpkgs' lixPackageSets.git builds Lix at bc9fb560ac2d, but its version
      # is >= 2.96, so common-lix.nix passes `-Dmimalloc=disabled`, which that
      # source does not define: meson fails with `Unknown option: "mimalloc"`.
      # Drop the flag; mimalloc is off either way. Remove once the git package
      # points at a Lix revision that has the option.
      lixPackageSets =
        prev.lixPackageSets
        // {
          git = prev.lixPackageSets.git.overrideScope (
            lixFinal: lixPrev: {
              lix = lixPrev.lix.overrideAttrs (old: {
                mesonFlags = builtins.filter (f: !(prev.lib.hasInfix "mimalloc" f)) old.mesonFlags;
              });
            }
          );
        };
    })
    (final: prev: {
      # buildPythonPackage enables __structuredAttrs by default (nixpkgs
      # #543976), so makeWrapperArgs reaches makeWrapper as an array. The
      # open-webui expression (pkgs/by-name/op/open-webui/package.nix:242)
      # still writes it as one string, "--set FRONTEND_BUILD_DIR <path>", which
      # makeWrapper rejects: `makeWrapper doesn't understand the arg --set ...`.
      # Split it into separate arguments. Remove once nixpkgs #575801
      # (treewide: fix python makeWrapperArgs with __structuredAttrs) or an
      # equivalent open-webui fix lands (tracked in nixpkgs #576478).
      open-webui = prev.open-webui.overrideAttrs (old: {
        makeWrapperArgs = ["--set" "FRONTEND_BUILD_DIR" "${old.passthru.frontend}/share/open-webui"];
      });
    })
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
