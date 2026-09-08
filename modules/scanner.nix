# Network scan into Paperless. The Epson XP-970 speaks eSCL (AirScan), so
# sane-airscan finds it over the mDNS that desktop.nix's avahi already
# advertises - no USB cable, no per-device driver. `scan-to-paperless` pulls a
# flatbed scan and drops a PDF straight into Paperless' consume directory,
# where the `scanner/` subdir becomes the `scanner` tag
# (PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS in paperless.nix).
#
# sz1-only: it is the one host with both the Paperless archive and LAN reach to
# the printer. The XP-970 has no ADF, so the default is a single flatbed page
# (`-m` loops the glass for multi-page; see scripts/scan-to-paperless.py).
{pkgs, ...}: let
  # Pin the printer by its stable FritzBox hostname and turn auto-discovery
  # off, so the ONLY scanner sane-airscan ever offers is this one device -
  # reached over a name that survives DHCP-lease and IPv6-prefix changes, not
  # the transient 192.168.x lease. mkSaneConfig symlinks each backend's
  # airscan.conf last-wins, and this file is ordered after sane-airscan in
  # extraBackends, so it replaces the package's shipped example.
  #
  # eSCL endpoint verified at https://EPSON79D3A6.fritz.box:443/eSCL
  # (ScannerCapabilities -> 200, MakeAndModel "EPSON XP-970 Series").
  airscanConf = pkgs.writeTextFile {
    name = "airscan-paperless.conf";
    destination = "/etc/sane.d/airscan.conf";
    text = ''
      [devices]
        "Epson XP-970 (fritz.box)" = https://EPSON79D3A6.fritz.box:443/eSCL, escl

      [options]
        discovery = disable
    '';
  };
in {
  # The eSCL/WSD backend. hardware.sane puts a `scanimage` with this backend
  # registered onto the system PATH; the wrapper below deliberately does NOT
  # bundle its own sane-backends, because an unconfigured one would not have
  # the airscan DLL registered and so would never discover the printer.
  hardware.sane = {
    enable = true;
    # airscanConf after sane-airscan so its airscan.conf wins the merge.
    extraBackends = [pkgs.sane-airscan airscanConf];
    # The integrated webcam shows up through SANE's v4l backend as a "scanner".
    # Drop the backend entirely so it can never be selected and a scan can
    # never accidentally capture the camera.
    disabledDefaultBackends = ["v4l"];
  };

  environment.systemPackages = [
    (pkgs.writeShellApplication {
      name = "scan-to-paperless";
      # img2pdf and python3 are the wrapper's own dependencies; scanimage is
      # left to resolve from the inherited PATH so it is the hardware.sane one.
      runtimeInputs = [pkgs.img2pdf pkgs.python3];
      text = ''exec python3 ${../scripts/scan-to-paperless.py} "$@"'';
    })
  ];

  # scan-to-paperless runs on the desktop as the human user, but the consume
  # directory is paperless-owned. Give the scanner drop-dir the `scanner` group
  # (the SANE access group vringar joins below) with setgid + group-write, so a
  # desktop scan can deposit into Paperless' inbox without sudo. paperless still
  # owns the dir, so it reads and then removes each file as it consumes it; the
  # setgid bit makes deposited files inherit the `scanner` group.
  systemd.tmpfiles.rules = [
    "d /var/lib/paperless/consume/scanner 2770 paperless scanner - -"
  ];
  users.users.vringar.extraGroups = ["scanner"];
}
