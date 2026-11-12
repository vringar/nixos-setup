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
    options = ["noatime"];
  };

  # SD-card wear: keep churny writes in RAM. Journal is lost on reboot; the
  # card is the scarce resource, not the logs.
  boot.tmp.useTmpfs = true;
  boot.tmp.tmpfsSize = "256M";
  services.journald.settings.Journal = {
    Storage = "volatile";
    RuntimeMaxUse = "64M";
  };

  fileSystems."/boot/firmware" = {
    device = "/dev/disk/by-label/FIRMWARE";
    fsType = "vfat";
    options = ["nofail" "noatime"];
  };
}
