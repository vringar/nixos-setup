# Home-manager module for the per-user crossbridge supervisor.
#
# Adapted from upstream's nix/module.nix, but as a home-manager module
# rather than a NixOS one — crossbridge is a per-user daemon, and ai.nix
# is the natural home for AI/agent tooling.
#
# Differences from upstream:
#   - Hard-wires apps/crossbridge (assertion + crosslink path patch); no
#     `package` option to override.
#   - Translated to home-manager's INI-style systemd unit schema.
#
# Servers and clients are deliberately not wrapped — they're per-repo and
# started manually (typically in a tmux pane).
{
  config,
  lib,
  pkgs,
  ...
}: let
  sources = import ../npins;
  crossbridge = import ../apps/crossbridge {inherit pkgs sources;};
  cfg = config.services.crossbridge-supervisor;
in {
  options.services.crossbridge-supervisor = {
    enable = lib.mkEnableOption "crossbridge per-user supervisor (peer-group socket coordinator)";

    socketRoot = lib.mkOption {
      type = lib.types.str;
      default = "%t/crossbridge";
      description = ''
        Runtime directory under which the supervisor binds its register
        socket (`''${socketRoot}/register.socket`) and creates per-peer slug
        subdirectories. The supervisor wipes this directory on startup.

        The default uses the systemd specifier `%t`, which expands to
        `$XDG_RUNTIME_DIR` (typically `/run/user/$UID`). `RuntimeDirectory`
        is also set to `crossbridge` so systemd creates and tears down the
        directory with the unit.

        Exposed to the supervisor via `CROSSBRIDGE_SOCKET_ROOT`. Servers
        and clients run by the same user must use the same value.
      '';
    };

    logLevel = lib.mkOption {
      type = lib.types.str;
      default = "crossbridge_supervisor=info";
      description = "RUST_LOG filter string for the supervisor.";
    };
  };

  config = lib.mkIf cfg.enable {
    systemd.user.services.crossbridge-supervisor = {
      Unit = {
        Description = "Crossbridge per-user supervisor";
      };

      Install = {
        WantedBy = ["default.target"];
      };

      Service = {
        Type = "simple";
        ExecStart = "${crossbridge}/bin/crossbridge-supervisor";
        Restart = "on-failure";
        RestartSec = "5s";

        RuntimeDirectory = "crossbridge";
        RuntimeDirectoryMode = "0700";

        Environment = [
          "RUST_LOG=${cfg.logLevel}"
          "CROSSBRIDGE_SOCKET_ROOT=${cfg.socketRoot}"
        ];
      };
    };
  };
}
