{config, ...}: let
  cfg = config.my;
in {
  config.home-manager.users.${cfg.username} = {pkgs, ...}: {
    home.packages = [
      ((pkgs.ghidra.overrideAttrs (prev: {
          pname = prev.pname + "withName";
          postInstall =
            (prev.postInstall or "")
            + ''
              cat <<'EOF' >>$out/lib/ghidra/support/launch.properties
              # Username
              VMARGS=-Duser.name=${cfg.username}
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
  };
}
