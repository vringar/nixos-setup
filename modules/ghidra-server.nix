# NixOS module for the Ghidra collaborative reverse-engineering server.
#
# Ghidra 12.x uses the YAJSW service wrapper (bundled at
# Ghidra/Features/GhidraServer/data/yajsw-*) rather than the generic
# launch.sh/GhidraLauncher framework used by other Ghidra tools.
# We generate a mutable server.conf at startup and invoke YAJSW directly.
#
# After first boot, manage users with ghidra-svrAdmin (exposed by this module):
#   ghidra-svrAdmin -add <username>
#   ghidra-svrAdmin -list
#   ghidra-svrAdmin -users
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

  keystorePath = "/var/lib/ghidra-server/keystore.p12";

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

    # GhidraServer args: [-ip <host>] [-a<auth>] [-p<port>] <repository_path>
    printf '%s\n' \
      ${lib.optionalString cfg.tailscaleCert.enable
      "'wrapper.app.parameter.1=-ip ${cfg.tailscaleCert.hostname}'"} \
      'wrapper.app.parameter.2=-a${toString cfg.authMode}' \
      'wrapper.app.parameter.3=-p${toString cfg.port}' \
      'wrapper.app.parameter.4=${cfg.reposDir}' \
      >> /var/lib/ghidra-server/server.conf

    ${lib.optionalString cfg.tailscaleCert.enable ''
      # Keystore for the Tailscale-provisioned certificate.
      printf '%s\n' \
        'wrapper.java.additional.9=-Dghidra.keystore=${keystorePath}' \
        'wrapper.java.additional.10=-Dghidra.password=' \
        >> /var/lib/ghidra-server/server.conf
    ''}
  '';

  # Provisions (or renews) the Tailscale certificate and converts to PKCS#12.
  certScript = pkgs.writeShellScript "ghidra-server-cert" ''
    set -euo pipefail
    CERT=/var/lib/ghidra-server/cert.pem
    KEY=/var/lib/ghidra-server/key.pem

    ${pkgs.tailscale}/bin/tailscale cert \
      --cert-file "$CERT" \
      --key-file  "$KEY" \
      ${cfg.tailscaleCert.hostname}

    # tailscale cert.pem is a bundle (leaf + intermediates).
    # openssl pkcs12 -export only takes the first cert from -in,
    # so pass the chain explicitly via -certfile so Java can send the
    # full chain during TLS and clients can validate against ISRG Root X1.
    LEAF=/tmp/ghidra-leaf.pem
    CHAIN=/tmp/ghidra-chain.pem
    ${pkgs.openssl}/bin/openssl x509 -in "$CERT" -out "$LEAF"
    ${pkgs.gawk}/bin/awk '/-----BEGIN CERTIFICATE-----/{n++} n>1{print}' "$CERT" > "$CHAIN"

    ${pkgs.openssl}/bin/openssl pkcs12 -export \
      -out      ${keystorePath} \
      -inkey    "$KEY" \
      -in       "$LEAF" \
      -certfile "$CHAIN" \
      -passout  pass:

    rm -f "$LEAF" "$CHAIN"

    chmod 640 ${keystorePath} "$CERT" "$KEY"
    chown ghidra-server:ghidra-server ${keystorePath} "$CERT" "$KEY"
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

    tailscaleCert = {
      enable = lib.mkEnableOption "Tailscale-provisioned TLS certificate for Ghidra Server";

      hostname = lib.mkOption {
        type = lib.types.str;
        description = "Tailscale MagicDNS FQDN (e.g. t20.example.ts.net). Used for cert provisioning and the -ip server flag.";
      };
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

    # Certificate provisioning — only active when tailscaleCert is enabled.
    systemd.services.ghidra-server-cert = lib.mkIf cfg.tailscaleCert.enable {
      description = "Provision Tailscale TLS certificate for Ghidra Server";
      after = ["tailscaled.service" "network-online.target"];
      wants = ["network-online.target"];
      # Run at boot and after each timer renewal.
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${certScript}";
        # On renewal, restart ghidra-server so it picks up the new keystore.
        ExecStartPost = "${pkgs.systemd}/bin/systemctl try-restart ghidra-server.service";
      };
    };

    # Renew ~2 weeks before the 90-day Let's Encrypt expiry.
    systemd.timers.ghidra-server-cert = lib.mkIf cfg.tailscaleCert.enable {
      wantedBy = ["timers.target"];
      description = "Renew Tailscale TLS certificate for Ghidra Server";
      timerConfig = {
        OnCalendar = "Mon *-*-* 03:00:00";
        Persistent = true;
      };
    };

    systemd.services.ghidra-server = {
      description = "Ghidra Server";
      wantedBy = ["multi-user.target"];
      after = ["network.target"]
        ++ lib.optional cfg.tailscaleCert.enable "ghidra-server-cert.service";
      requires = lib.optional cfg.tailscaleCert.enable "ghidra-server-cert.service";

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

    # svrAdmin wrapper that uses the generated server.conf rather than the
    # read-only Nix store copy (which points at the wrong repos directory).
    environment.systemPackages = [
      (pkgs.writeShellScriptBin "ghidra-svrAdmin" ''
        exec sudo -u ghidra-server \
          ${cfg.package}/lib/ghidra/support/launch.sh fg jre svrAdmin 128M \
          '-DUserAdmin.invocation=svrAdmin -Djava.awt.headless=true' \
          ghidra.server.ServerAdmin \
          /var/lib/ghidra-server/server.conf \
          "$@"
      '')
    ];

    networking.firewall.allowedTCPPorts = [cfg.port];
  };
}
