{lib, ...}: {
  nixpkgs.hostPlatform = "aarch64-linux";

  networking.hostName = "t20";

  boot.loader.grub.enable = false;
  boot.loader.generic-extlinux-compatible.enable = true;

  boot.initrd.availableKernelModules = ["mmc_block" "xhci_pci" "usbhid"];
  boot.supportedFilesystems = lib.mkForce ["vfat" "ext4"];

  hardware.enableRedistributableFirmware = true;

  fileSystems."/" = {
    device = "/dev/disk/by-label/NIXOS_SD";
    fsType = "ext4";
  };

  fileSystems."/boot/firmware" = {
    device = "/dev/disk/by-label/FIRMWARE";
    fsType = "vfat";
    options = ["nofail" "noatime"];
  };
}
