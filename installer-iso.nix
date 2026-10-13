# Build an x86_64 NixOS installer ISO for headless installs (first user: sz2).
#
# On top of the stock minimal installer it adds:
# - SSH public keys authorized for root, so the install can be driven over SSH
# - tailscaled, so the live system can join the tailnet with `sudo tailscale up`
# - sz1's nix-serve as a substituter, so nixos-install fetches what sz1 has
#   already built instead of compiling on the target
#
# Build with (passes every public key from ~/.ssh, links result-installer):
#   scripts/build-installer.py
#
# Flash with (run without arguments to list USB disks):
#   scripts/burn-usb.py result-installer/iso/*.iso /dev/sdX
{
  # Newline-separated OpenSSH public keys. scripts/build-installer.py passes
  # the keys from ~/.ssh; a plain `nix-build installer-iso.nix` gets the repo key.
  authorizedKeys ? builtins.readFile ./home-manager/files/ssh/github_key.pub,
}: let
  sources = import ./npins;
  pkgs = import sources.nixpkgs {system = "x86_64-linux";};
in
  (pkgs.nixos ({
    modulesPath,
    lib,
    ...
  }: {
    imports = [
      (modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix")
    ];

    users.users.root.openssh.authorizedKeys.keys =
      lib.filter (key: key != "") (lib.splitString "\n" authorizedKeys);

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
