{
  pkgs,
  config,
  lib,
  ...
}: let
  jvmOpts = lib.concatStringsSep " " [
    "-Duser.name=${config.home.username}"
    # SslRMIClientSocketFactory sets endpointIdentificationAlgorithm=HTTPS by default,
    # which fails for servers with self-signed certs lacking proper SAN/CN
    # (e.g. SECT TU Berlin's CN=GhidraServer). Disable RMI hostname verification.
    "-Djdk.rmi.ssl.client.enableEndpointIdentification=false"
    # Self-signed server cert, added on top of the OS trust store (ghidra.cacerts would replace it).
    "-Dghidra.unix.default.cacerts=${./ghidra-sect.pem}"
  ];
in {
  home.packages = [
    (pkgs.symlinkJoin {
      name = "ghidra-${pkgs.ghidra.version}";
      paths = [
        (pkgs.ghidra.withExtensions (p:
          with p; [
            machinelearning
            sleighdevtools
            ghidraninja-ghidra-scripts
            ret-sync
          ]))
      ];
      nativeBuildInputs = [pkgs.makeWrapper];
      postBuild = ''
        for bin in $out/bin/*; do
          realBin=$(readlink -f "$bin")
          [ -f "$realBin" ] && [ -x "$realBin" ] || continue
          rm "$bin"
          makeWrapper "$realBin" "$bin" \
            --set-default JDK_JAVA_OPTIONS ${lib.escapeShellArg jvmOpts}
        done
      '';
    })
  ];
}
