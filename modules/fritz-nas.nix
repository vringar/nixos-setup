# The USB SSD hanging off the FritzBox, exported over SMB.
#
# Two machines want it for different reasons: sz1 mirrors Paperless exports
# there from an unattended timer running as root, sz3 browses it interactively
# as the login user. The share and the credentials are the same; only the
# synthesised ownership differs, which is what the options below express.
# modules/local-llm.nix notes that a second consumer is when to introduce
# options — this is that second consumer.
#
# sz3 is not wired up yet: it still carries the inline mount in
# hardware/sz3.nix pointing at an out-of-band /etc/secrets/smb. Migrating it
# needs its host key in secrets/secrets.nix and a rekey of the secret below,
# neither of which can be verified while the machine is away. See
# docs/paperless.md.
{
  config,
  lib,
  ...
}: let
  cfg = config.my.fritzNas;
in {
  options.my.fritzNas = {
    enable = lib.mkEnableOption "the FritzBox SMB share";

    mountPoint = lib.mkOption {
      type = lib.types.str;
      example = "/mnt/fritz-nas";
      description = "Absolute path to mount the share at.";
    };

    uid = lib.mkOption {
      type = lib.types.int;
      default = 0;
      description = ''
        Owner of every file in the mount. The share carries no POSIX metadata,
        so CIFS synthesises ownership client-side: this is a property of the
        mount, not of anything stored on the server.
      '';
    };

    gid = lib.mkOption {
      type = lib.types.int;
      default = 0;
      description = "Group of every file in the mount. See {option}`uid`.";
    };

    fileMode = lib.mkOption {
      type = lib.types.str;
      default = "0600";
      description = "Synthesised permission bits for files. See {option}`uid`.";
    };

    dirMode = lib.mkOption {
      type = lib.types.str;
      default = "0700";
      description = "Synthesised permission bits for directories. See {option}`uid`.";
    };
  };

  config = lib.mkIf cfg.enable {
    # Encrypted to the host key rather than to the user key that
    # modules/wg-sect.nix hand-decrypts: the consumers here are mount and timer
    # units with no way to wait for /home to be unlocked.
    age.secrets.smb-fritznas.file = ../secrets/smb-fritznas.age;

    fileSystems.${cfg.mountPoint} = {
      device = "//fritz.box/FRITZ.NAS";
      fsType = "cifs";
      options = [
        # Mount on first access rather than at boot. A router that is rebooting,
        # or an SSD that has been unplugged, then fails whichever unit touched
        # the path instead of hanging the boot on an unreachable network share.
        "x-systemd.automount"
        "noauto"
        "x-systemd.idle-timeout=60"
        "x-systemd.device-timeout=5s"
        "x-systemd.mount-timeout=5s"

        "credentials=${config.age.secrets.smb-fritznas.path}"

        # The FritzBox does not hand out unique inode numbers, so let the client
        # generate them instead of trusting the server's.
        "noserverino"

        "uid=${toString cfg.uid}"
        "gid=${toString cfg.gid}"
        "file_mode=${cfg.fileMode}"
        "dir_mode=${cfg.dirMode}"
      ];
    };
  };
}
