# Build the initial SD card image for the Raspberry Pi 3.
# Produces a minimal image with SSH access pre-configured.
#
# Build with (requires aarch64 QEMU emulation or native aarch64 builder):
#   nix-build pi-image.nix
#
# Flash with:
#   sudo dd if=result of=/dev/sdX bs=4M status=progress conv=fsync
#
# After first boot: set deployment.targetHost in hive.nix and use
# `colmena apply --on rpi` to apply the full config.
let
  sources = import ./npins;
  pkgs = import sources.nixpkgs {
    system = "aarch64-linux";
    config.allowUnfree = true;
  };
in
  (pkgs.nixos ({
    modulesPath,
    lib,
    ...
  }: {
    imports = [
      (modulesPath + "/installer/sd-card/sd-image-aarch64.nix")
    ];

    networking.hostName = "t20";

    hardware.enableRedistributableFirmware = true;
    boot.supportedFilesystems = lib.mkForce ["vfat" "ext4"];

    fileSystems."/" = {
      device = "/dev/disk/by-label/NIXOS_SD";
      fsType = "ext4";
    };

    services.openssh = {
      enable = true;
      settings = {
        PasswordAuthentication = false;
        KbdInteractiveAuthentication = false;
      };
    };

    users.users.vringar = {
      isNormalUser = true;
      extraGroups = ["wheel" "networkmanager"];
      openssh.authorizedKeys.keyFiles = [./home-manager/files/ssh/github_key.pub];
    };

    security.sudo.wheelNeedsPassword = false;

    networking.networkmanager.enable = true;
    time.timeZone = "Europe/Berlin";

    nix.settings.experimental-features = "nix-command flakes";

    system.stateVersion = "25.05";
  }))
  .config
  .system
  .build
  .sdImage
