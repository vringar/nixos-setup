# NixOS module for the Ghidra collaborative reverse-engineering server.
#
# The server stores repositories under `reposDir` and listens on `port`.
# After first boot, add users with:
#   sudo -u ghidra-server ${pkgs.ghidra}/lib/ghidra/server/svrAdmin -add <username>
#
# launch.sh signature (NixOS-wrapped):
#   <mode> <java-type> <name> <max-memory> "<vmarg-list>" <app-classname> <app-args>...
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.ghidra-server;
in {
  options.services.ghidra-server = {
    enable = lib.mkEnableOption "Ghidra collaborative reverse-engineering server";

    package = lib.mkPackageOption pkgs "ghidra" {};

    port = lib.mkOption {
      type = lib.types.port;
      default = 13100;
      description = "TCP port for the Ghidra Server RMI interface.";
    };

    reposDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/ghidra-server/repos";
      description = "Directory for Ghidra repository storage.";
    };

    authMode = lib.mkOption {
      type = lib.types.ints.between 0 2;
      default = 0;
      description = "Authentication mode: 0 = none, 1 = password file, 2 = PKI.";
    };

    maxMemory = lib.mkOption {
      type = lib.types.str;
      # Pi 3 has 1 GB RAM — keep heap modest
      default = "256M";
      description = "Maximum JVM heap size passed to launch.sh (e.g. 256M, 1G).";
    };

    jvmArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = ["-Xms64m"];
      description = "Extra JVM flags passed to the Ghidra Server process.";
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.ghidra-server = {
      isSystemUser = true;
      group = "ghidra-server";
      description = "Ghidra Server daemon user";
      # Ghidra's launch.sh writes a JDK path cache to $HOME/.config/ghidra/;
      # system users default to /var/empty (read-only), causing startup failure.
      # StateDirectory creates /var/lib/ghidra-server owned by this user.
      home = "/var/lib/ghidra-server";
      createHome = false;
    };
    users.groups.ghidra-server = {};

    systemd.services.ghidra-server = {
      description = "Ghidra Server";
      wantedBy = ["multi-user.target"];
      after = ["network.target"];

      serviceConfig = {
        Type = "simple";
        User = "ghidra-server";
        Group = "ghidra-server";
        # Creates /var/lib/ghidra-server and /var/lib/ghidra-server/repos
        StateDirectory = "ghidra-server ghidra-server/repos";
        StateDirectoryMode = "0750";
        ExecStart = "${cfg.package}/lib/ghidra/support/launch.sh fg jdk GhidraServer ${cfg.maxMemory} \"${lib.concatStringsSep " " cfg.jvmArgs}\" ghidra.server.remote.GhidraServer ${cfg.reposDir} -p${toString cfg.port} -a${toString cfg.authMode}";
        Restart = "on-failure";
        RestartSec = "10s";
      };
    };

    networking.firewall.allowedTCPPorts = [cfg.port];
  };
}
