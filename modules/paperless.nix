# Paperless-ngx: the household document archive. Bank statements that the bank
# deletes after a year, the flat purchase contract, anything that arrives on
# paper or by mail and has to outlive the place it arrived in.
#
# On sz1 because OCR wants CPU and the documents want the ZFS pool. Reach is
# deliberately confined to the LAN — this holds financial records, and the
# wildcard already terminating TLS on t20 would otherwise put them one config
# line away from the open internet.
{
  config,
  lib,
  pkgs,
  ...
}: let
  # Restated from the exporter's own option rather than hardcoded, so the
  # service and the unit that mirrors its output cannot drift apart.
  exportDir = config.services.paperless.exporter.directory;

  smbMount = "/mnt/fritz-nas";

  # The share root is the FritzBox's own internal flash, not the USB SSD — the
  # SSD appears inside the share under its volume label. Writing to the root
  # silently fills up the router instead of the disk, which is exactly what
  # happened the first time.
  ssdRoot = "${smbMount}/My_Passport/Stefan";
  mirror = "${ssdRoot}/paperless";
in {
  imports = [./fritz-nas.nix];

  my.fritzNas = {
    enable = true;
    mountPoint = smbMount;
    # Root-owned: the only consumer is the backup timer below, and the mirror
    # holds the same financial records as the archive itself.
    uid = 0;
    gid = 0;
    fileMode = "0600";
    dirMode = "0700";
  };

  age.secrets.paperless-admin.file = ../secrets/paperless-admin.age;

  services.paperless = {
    enable = true;

    # Bound to every interface and then fenced off by the interface-scoped
    # firewall rule below — the same shape as nix-serve in hive.nix, and for
    # the same reason: openFirewall would also open it on wg-sect.
    address = "0.0.0.0";
    port = 28981;

    passwordFile = config.age.secrets.paperless-admin.path;

    # Brings up Tika and Gotenberg, which is what makes .eml and Office files
    # consumable: dragging a mail out of Thunderbird into the web uploader
    # lands it with sender, subject and date as real metadata instead of as an
    # opaque blob. Both listen on loopback only, so neither needs a firewall
    # rule. Note this only works because the database is SQLite: the module
    # leaves PrivateNetwork on for the task queue when it manages PostgreSQL
    # locally, which would cut the queue off from Gotenberg.
    configureTika = true;

    settings = {
      PAPERLESS_OCR_LANGUAGE = "deu+eng";

      # The escape hatch. Paperless is a nice front-end, but the archive has to
      # outlive it, so the media tree is laid out to be navigable by a human
      # with a file browser and no database.
      PAPERLESS_FILENAME_FORMAT = "{created_year}/{correspondent}/{created}_{title}";

      PAPERLESS_CONSUMER_RECURSIVE = true;
      PAPERLESS_CONSUMER_SUBDIRS_AS_TAGS = true;
    };

    exporter = {
      # A portable dump — original files plus a manifest — rather than a copy of
      # the live data directory. It restores onto a different machine and a
      # different Paperless version, which a database file does not.
      enable = true;
      onCalendar = "01:30";
    };
  };

  # enp4s0 is the LAN NIC. Machines on the house WLAN reach sz1 through it;
  # wg-sect and tailscale0 deliberately do not appear here.
  networking.firewall.interfaces."enp4s0".allowedTCPPorts = [
    28981 # paperless-ngx web UI
  ];

  # configureTika brings up services.gotenberg, whose LibreOffice-backed
  # conversions needed working around here. One of the two bugs is now fixed
  # upstream; the remaining one is below.
  #
  # The converter bug is gone. The gotenberg package used to wire classic
  # `unoconv` 0.9.0 in as UNOCONVERTER_BIN_PATH, and that calls
  # `unohelper.absolutize`, removed from LibreOffice 26.2's python bindings, so
  # every conversion died with `AttributeError: ... 'absolutize'` (Gotenberg
  # 500). nixpkgs now vendors Gotenberg's own maintained `unoconverter` 0.4.0
  # instead, which does not call it, and no longer exposes `unoconv` as an
  # overridable argument at all - so the sed that used to live here is both
  # unnecessary and impossible to express.

  # Bug B - the listener won't start. Gotenberg runs LibreOffice as a directly-
  # supervised `--accept=socket;urp;` listener via LIBREOFFICE_BIN_PATH. nixpkgs
  # soffice.bin exits 81 (LibreOffice's "restart me") on the first run of a fresh
  # UserInstallation while it stages the bundled-extension layer; that first run
  # takes ~15s in the hardened unit and races Gotenberg's 20s "process first start"
  # deadline (503). The `soffice`/oosplash launcher would absorb the 81, but it
  # forks soffice.bin as a grandchild, so Gotenberg loses the process it supervises.
  #
  # So bake a ready profile at build time and have the wrapper drop it into
  # Gotenberg's fresh UserInstallation: soffice.bin then starts clean in one run
  # (no 81, no oosplash) and is exec'd as Gotenberg's own direct child.
  #
  # mkForce because the module defines this key; unlike UNOCONVERTER_BIN_PATH,
  # gotenberg's preFixup does NOT --set it, so the systemd-env value wins.
  systemd.services.gotenberg.environment.LIBREOFFICE_BIN_PATH = let
    prog = "${config.services.gotenberg.libreoffice.package.unwrapped}/lib/libreoffice/program";

    # A first soffice.bin run against a fresh UserInstallation initialises the
    # profile and exits 81 (restart requested); keep the resulting profile. Assert
    # it actually initialised - registrymodifications plus the bundled-extension
    # layer whose staging triggers the 81 - not merely that `$out/user` exists.
    profile = pkgs.runCommand "gotenberg-lo-profile" {} ''
      export HOME="$(mktemp -d)"
      mkdir -p "$out"
      ${prog}/soffice.bin --headless --norestore \
        "-env:UserInstallation=file://$out" \
        --convert-to pdf ${pkgs.writeText "seed" "seed"} --outdir "$HOME/o" \
        > /dev/null 2>&1 || true
      test -f "$out/user/registrymodifications.xcu" && test -d "$out/user/extensions/bundled" \
        || { echo "gotenberg-lo-profile: LibreOffice profile did not initialise" >&2; exit 1; }
    '';

    wrapper = pkgs.writeShellScript "soffice-foreground" ''
      set -eu
      ui=
      for arg in "$@"; do
        case "$arg" in
          -env:UserInstallation=file://*) ui="''${arg#-env:UserInstallation=file://}" ;;
        esac
      done
      # Seed Gotenberg's fresh (empty) UserInstallation from the baked profile so
      # soffice.bin skips the slow first-run. The store copy is read-only and
      # LibreOffice writes lock/config into its profile at runtime, so make it
      # writable.
      if [ -n "$ui" ] && [ ! -e "$ui/user" ]; then
        mkdir -p "$ui"
        cp -a ${profile}/user "$ui/user"
        chmod -R u+w "$ui/user"
      fi
      cd ${prog}
      export SAL_ENABLE_FILE_LOCKING=1
      exec ${prog}/soffice.bin "$@"
    '';
  in
    lib.mkForce "${wrapper}";

  # zfs-auto-snapshot only touches datasets carrying com.sun:auto-snapshot=true,
  # which is set by hand on zpool/paperless alone (see docs/paperless.md). The
  # SMB mirror is a flat latest-state copy, so these snapshots are the only
  # thing standing between a bad bulk import and a permanently mangled archive.
  services.zfs.autoSnapshot.enable = true;

  # Appended, not assigned: the module's own OnSuccess list restarts the
  # paperless services the exporter shut down, and unitOption merges list-valued
  # definitions by concatenation. Overwriting it would leave Paperless down
  # after every nightly export.
  systemd.services.paperless-exporter.unitConfig.OnSuccess = [
    "paperless-backup-smb.service"
  ];

  systemd.services.paperless-backup-smb = {
    description = "Mirror the Paperless export to the FritzBox SMB share";

    # No wantedBy and no startAt: this is driven purely by the exporter's
    # OnSuccess, so a failed or partial export can never overwrite a good
    # mirror. Runs as root, which is what the mount's uid=0 expects.
    unitConfig.RequiresMountsFor = [smbMount];
    serviceConfig.Type = "oneshot";

    enableStrictShellChecks = true;
    script = ''
      # document_exporter writes the manifest as part of a completed run. Its
      # absence means the directory is not an export, and mirroring it with
      # --delete would trade a good backup for a broken one.
      if [ ! -f ${exportDir}/manifest.json ]; then
        echo "no manifest.json in ${exportDir} - refusing to mirror" >&2
        exit 1
      fi

      # mkdir -p would happily recreate the whole path on the router's internal
      # flash if the SSD were unplugged, quietly mirroring to the wrong disk.
      # Require the SSD's own directory to already exist instead.
      if [ ! -d ${ssdRoot} ]; then
        echo "${ssdRoot} is missing - is the SSD mounted on the FritzBox? refusing to mirror" >&2
        exit 1
      fi

      ${pkgs.coreutils}/bin/mkdir -p ${mirror}

      # The share carries no POSIX metadata, so ownership and permissions
      # cannot be preserved and trying would fail on every file.
      # --modify-window absorbs the two-second timestamp granularity that
      # FAT-derived filesystems report.
      exec ${pkgs.rsync}/bin/rsync \
        --recursive --links --times --delete \
        --no-perms --no-owner --no-group --modify-window=2 \
        ${exportDir}/ ${mirror}/
    '';
  };
}
