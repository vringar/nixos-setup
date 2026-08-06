# Tooling for the parts of Open WebUI that live in its database and therefore
# cannot be declared in NixOS options: plugins and their configuration.
#
# `plugins` is the pinned set; `manifest` renders a desired state for `sync`,
# which reconciles the running instance against it.
{
  lib,
  fetchurl,
  python3,
  writeText,
  writeShellApplication,
}: rec {
  plugins = import ./plugins {inherit fetchurl;};

  # Desired state as JSON. Store paths render as strings, so the plugin source
  # is referenced by path and read at run time rather than embedded here.
  manifest = selected:
    writeText "openweb-ui-plugins.json" (builtins.toJSON (
      lib.mapAttrsToList (id: plugin: {
        inherit id;
        inherit (plugin) name global valves;
        description = plugin.description or "";
        content = plugin.src;
      })
      selected
    ));

  sync = writeShellApplication {
    name = "openweb-ui-sync";
    runtimeInputs = [(python3.withPackages (ps: [ps.requests]))];
    text = ''
      exec python3 ${./sync.py} "$@"
    '';
  };
}
