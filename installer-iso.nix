# Build an x86_64 NixOS installer ISO for headless installs (first user: sz2).
#
# On top of the stock minimal installer it adds:
# - the hosts' login keys authorized for root, so the install can be driven
#   over SSH
# - tailscaled, so the live system can join the tailnet with `sudo tailscale up`
# - sz1's nix-serve as a substituter, so nixos-install fetches what sz1 has
#   already built instead of compiling on the target
#
# Build with (links result-installer):
#   scripts/build-installer.py
#
# Flash with (run without arguments to list USB disks):
#   scripts/burn-usb.py result-installer/iso/*.iso /dev/sdX
let
  sources = import ./npins;
  pkgs = import sources.nixpkgs {system = "x86_64-linux";};
in
  (pkgs.nixos ({modulesPath, ...}: {
    imports = [
      (modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix")
    ];

    users.users.root.openssh.authorizedKeys.keyFiles = import ./home-manager/files/ssh/login-keys.nix;

    services.tailscale.enable = true;

    # Same cache and key as t20's block in hive.nix. nix-serve is only open on
    # sz1's LAN NIC, so this helps at home and is a harmless miss elsewhere.
    nix.settings = {
      extra-substituters = ["http://sz1.fritz.box:5000"];
      extra-trusted-public-keys = ["sz1.fritz.box:CB1Zd3dpBNECfzeGVpkDNJYds4O/eKJhV2Tlx2NGqEc="];
    };
  }))
  .config
  .system
  .build
  .isoImage
