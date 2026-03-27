{
  pkgs,
  config,
  ...
}: {
  home.packages = [
    ((pkgs.ghidra.overrideAttrs (prev: {
        pname = prev.pname + "withName";
        postInstall =
          (prev.postInstall or "")
          + ''
            cat <<'EOF' >>$out/lib/ghidra/support/launch.properties
            # Username
            VMARGS=-Duser.name=${config.home.username}
            # Trust IP SAN when connecting by hostname (server cert has IP SAN only)
            VMARGS=-Djdk.tls.trustNameService=true
            EOF
          '';
      }))
      .withExtensions (p:
        with p; [
          machinelearning
          sleighdevtools
          ghidraninja-ghidra-scripts
          ret-sync
          lightkeeper
        ]))
  ];
}
