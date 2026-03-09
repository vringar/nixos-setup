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
