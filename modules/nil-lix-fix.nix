{...}: {
  # The Lix module overlays top-level `nix` with Lix. `nil` links the nix C++
  # libraries and, at build time, dumps its builtins database (the `builtins.*`
  # names + hover docs it serves to the editor) from whatever `nix` it is given.
  # We deliberately let that be Lix, so nil's builtins knowledge matches the
  # interpreter this host actually runs.
  #
  # The catch: nil's `crates/builtin` `tests::sanity` and its `ide::hover`
  # expect-tests hardcode upstream CppNix's doc strings, and Lix's docs have
  # diverged: `builtins.attrNames` gained an "O(l n log n)" complexity note and
  # `builtins.head` gained "Has constant time complexity.", so those tests fail
  # against Lix.
  #
  # Rather than disable the tests or force nil onto CppNix, we keep them
  # running (they guard against real breakage in Lix's builtins dump) and patch
  # the fixtures to Lix's current strings. This is intentionally high-maintenance:
  # the patch is tightly coupled to both Lix's doc text and nil's source layout,
  # so it fails loudly on the next Lix/nil bump — the desired early warning.
  nixpkgs.overlays = [
    (final: prev: {
      nil = prev.nil.overrideAttrs (old: {
        patches = (old.patches or []) ++ [./nil-attrnames-doc.patch];
      });
    })
  ];
}
