# NixOS module for the Ghidra collaborative reverse-engineering server.
#
# Ghidra 12.x uses the YAJSW service wrapper (bundled at
# Ghidra/Features/GhidraServer/data/yajsw-*) rather than the generic
# launch.sh/GhidraLauncher framework used by other Ghidra tools.
# We generate a mutable server.conf at startup and invoke YAJSW directly.
#
# After first boot, add users with:
#   GHIDRA=$(systemctl cat ghidra-server | grep -oP '/nix/store/[^/]+-ghidra-[^/]+')
#   sudo -u ghidra-server $GHIDRA/lib/ghidra/server/svrAdmin -add <username>
{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.ghidra-server;

  ghidraHome = "${cfg.package}/lib/ghidra";
  dataDir = "${ghidraHome}/Ghidra/Features/GhidraServer/data";
  classpathFrag = "${dataDir}/classpath.frag";
  # YAJSW is bundled with Ghidra — version matches the Ghidra release.
  wrapperJar = "${dataDir}/yajsw-stable-13.18/wrapper.jar";

  # Convert "256M" / "1G" to an integer number of MB for wrapper.java.maxmemory.
  maxMemoryMB =
    if lib.hasSuffix "G" cfg.maxMemory
    then toString ((lib.toInt (lib.removeSuffix "G" cfg.maxMemory)) * 1024)
    else lib.removeSuffix "M" cfg.maxMemory;

  # Script that generates /var/lib/ghidra-server/server.conf from the
  # read-only Nix-store template, substituting our configured values.
  setupConf = pkgs.writeShellScript "ghidra-server-setup-conf" ''
    set -euo pipefail
    install -m 640 ${cfg.package}/lib/ghidra/server/server.conf \
      /var/lib/ghidra-server/server.conf

    ${pkgs.gnused}/bin/sed -i \
      -e 's|ghidra.repositories.dir=.*|ghidra.repositories.dir=${cfg.reposDir}|' \
      -e 's|wrapper.java.maxmemory=.*|wrapper.java.maxmemory=${maxMemoryMB}|' \
      -e 's|wrapper.logfile=.*|wrapper.logfile=/var/lib/ghidra-server/wrapper.log|' \
      -e 's|wrapper.working.dir=.*|wrapper.working.dir=/var/lib/ghidra-server|' \
      -e '/^wrapper\.app\.parameter\./d' \
      /var/lib/ghidra-server/server.conf

    # GhidraServer args: [-a<auth>] [-p<port>] <repository_path>
    printf '%s\n' \
      'wrapper.app.parameter.1=-a${toString cfg.authMode}' \
      'wrapper.app.parameter.2=-p${toString cfg.port}' \
      'wrapper.app.parameter.3=${cfg.reposDir}' \
      >> /var/lib/ghidra-server/server.conf
  '';
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
      default = "256M";
      description = "Maximum JVM heap for the GhidraServer process (e.g. 256M, 1G). Pi 3 has 1 GB RAM total.";
    };

    jvmArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Extra JVM flags for the YAJSW wrapper process.";
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
        StateDirectory = "ghidra-server ghidra-server/repos";
        StateDirectoryMode = "0750";

        # YAJSW substitutes these env vars for ${...} references in server.conf.
        Environment = [
          "ghidra_home=${ghidraHome}"
          "classpath_frag=${classpathFrag}"
          "wrapper_tmpdir=/tmp"
          "java=${pkgs.jdk21}/bin/java"
        ];

        # Regenerate server.conf on every start so config changes take effect.
        ExecStartPre = "${setupConf}";

        # Invoke the YAJSW wrapper directly (bypassing ghidraSvr which
        # hard-codes the conf path to the read-only Nix store).
        ExecStart = lib.concatStringsSep " " ([
            "${pkgs.jdk21}/bin/java"
            "-Xmx128m"
          ]
          ++ cfg.jvmArgs
          ++ [
            "-Djna_tmpdir=/tmp"
            "-Djava.io.tmpdir=/tmp"
            "-jar"
            wrapperJar
            "-c"
            "/var/lib/ghidra-server/server.conf"
          ]);

        Restart = "on-failure";
        RestartSec = "10s";
      };
    };

    networking.firewall.allowedTCPPorts = [cfg.port];
  };
}
