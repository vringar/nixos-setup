# Single SSD, GPT:
#   SZ2_BOOT  1 GiB ESP (vfat)
#   SZ2_LUKS  rest, LUKS2 container holding btrfs with the root subvolume @
# Everything is addressed by GPT partition label, so this file did not have to
# wait for the disk to be formatted. Swap is zram, not a partition.
{
  name,
  config,
  lib,
  modulesPath,
  ...
}: {
  imports = [
    (modulesPath + "/installer/scan/not-detected.nix")
  ];

  networking.hostName = name;
  boot.loader.systemd-boot = {
    enable = true;
    configurationLimit = 10;
  };
  boot.loader.efi.canTouchEfiVariables = true;

  # Detected by nixos-generate-config on the machine (Intel i5-7200U).
  boot.initrd.availableKernelModules = ["xhci_pci" "nvme" "usb_storage" "sd_mod" "rtsx_pci_sdmmc"];
  boot.kernelModules = ["kvm-intel"];
  hardware.cpu.intel.updateMicrocode = lib.mkDefault config.hardware.enableRedistributableFirmware;

  boot.initrd.luks.devices.root.device = "/dev/disk/by-partlabel/SZ2_LUKS";

  fileSystems."/" = {
    device = "/dev/mapper/root";
    fsType = "btrfs";
    options = ["subvol=@" "compress=zstd" "noatime"];
  };

  fileSystems."/boot" = {
    device = "/dev/disk/by-partlabel/SZ2_BOOT";
    fsType = "vfat";
    options = ["fmask=0077" "dmask=0077"];
  };

  zramSwap.enable = true;

  networking.useDHCP = lib.mkDefault true;
  nixpkgs.hostPlatform = lib.mkDefault "x86_64-linux";
}
