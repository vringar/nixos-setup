{pkgs, ...}: {
  isNormalUser = true;
  description = "Sash";

  # Deliberately not in wheel: no sudo, and no trusted-user access to the nix
  # daemon. networkmanager is the one group needed to join a wifi network from
  # the desktop applet, and grants nothing else.
  extraGroups = ["networkmanager"];

  # Firefox and Steam are enabled system-wide in modules/desktop.nix and the
  # sz3 block respectively, so they are already on this account's PATH.
  packages = with pkgs; [
    chromium
    google-chrome
    libreoffice
    thunderbird
  ];
}
