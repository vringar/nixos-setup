# Paperless-ngx document archive on sz1

## Context

Buying a flat produced a pile of bank and legal documents that a folder tree
models badly, and the bank deletes downloadable statements after a year while
no longer sending paper. The archive needs tags and metadata rather than a
hierarchy, full-text search over scanned PDFs, and a place to keep archived
mail. Paperless-ngx covers all four.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Host | sz1 | OCR wants CPU; the documents want the ZFS pool. The Pi3 has 1 GB of RAM and an SD card. |
| Packaging | `services.paperless` | First-class module in the pinned nixpkgs; keeps state declarative. |
| Database | SQLite | Single user, a few thousand documents. Also load-bearing: the module only drops `PrivateNetwork` on the task queue when it is *not* managing PostgreSQL locally, and the queue must reach Gotenberg. |
| Office/mail parsing | `configureTika = true` | The only way `.eml` and `.docx` become consumable. Pulls Chromium and LibreOffice. |
| Reach | LAN only | Financial records. t20's `*.home.zabka.it` wildcard faces the internet; this deliberately does not use it. |
| Primary storage | `zpool/paperless`, 100 G quota | ZFS quotas bound a dataset and its descendants, and the export lives inside the mountpoint — so the quota covers roughly 2× the document volume. |
| Backup | Exporter → rsync mirror to FritzBox SMB | The exporter emits a portable dump that restores onto a different machine and a different Paperless version. A copy of the live data directory does not. |
| Versioning | ZFS snapshots, not the mirror | The SMB copy is a flat latest-state mirror, so a bad import would propagate. Snapshots are what makes that recoverable. |

## Non-goals

- **Offsite copy.** Both copies live in the flat. Deferred on cost; the design
  takes a restic leg without rework.
- **Automated bank statement fetching.** FinTS/HBCI scraping is fragile.
  Statements are downloaded by hand and uploaded through the web UI.
- **IMAP auto-fetch.** Paperless can poll a mailbox on a schedule. Not
  configured at first — mail is dragged out of Thunderbird by hand. Enabling it
  later is a UI job, not a Nixlang one: mail accounts and rules are database
  rows, and `document_exporter` serialises them (`mail_accounts`,
  `mail_rules`), so the nightly backup already restores them.

  One consequence to enable it knowingly: `document_exporter` writes sensitive
  fields in cleartext unless given `--passphrase`, so the mail account password
  ends up readable in `manifest.json` — and therefore on the SMB mirror. There
  is no `--passphrase-file`, so passing one without leaking it into the world
  readable Nix store means overriding the exporter's script to read an agenix
  `EnvironmentFile`. Judged acceptable instead: the share is authenticated
  behind FritzBox NAS credentials, the realistic exposure is physical access to
  the SSD, and that is trusted. Use a mailbox-scoped app password so revoking
  it does not mean rotating the primary mail password. Revisit if the mirror
  ever moves somewhere less trusted.

## Architecture

```
   web upload (LAN: sz1, sz3 on the house WLAN, phone)
               |
               v
   services.paperless on sz1        :28981, firewall-scoped to enp4s0
     SQLite + redis
     tika + gotenberg (loopback)    .eml, .docx, .odt
     tesseract deu+eng
               |
               |  /var/lib/paperless -> zpool/paperless (quota + auto-snapshot)
               |
   01:30  paperless-exporter.service
               |  writes export/ (originals + manifest.json)
               |  OnSuccess=
               v
   paperless-backup-smb.service
               |  rsync --delete
               v
   //fritz.box/FRITZ.NAS/My_Passport/Stefan/paperless/   (CIFS automount,
                                                          plain mirror)
```

`paperless-backup-smb` is driven only by the exporter's `OnSuccess`, never by
its own timer, so a failed export cannot overwrite a good mirror. It also
refuses to run if `manifest.json` is missing — `rsync --delete` from an empty
directory would otherwise erase the backup.

The target is inside `My_Passport/`, not at the share root: the root is the
FritzBox's own internal flash and the USB SSD is mounted under its volume label
within the share. The unit refuses to run when that directory is absent rather
than letting `mkdir -p` recreate the path on the router's flash, which would
mirror to the wrong disk without any error.

## Manual prerequisites

### P1 — create the dataset

Not declarative: same reasoning as `zpool/llm`. The quota and the snapshot
property have to exist before the service first starts.

```bash
sudo zfs create -o quota=100G -o com.sun:auto-snapshot=true zpool/paperless
```

`services.zfs.autoSnapshot.enable` is set in `modules/paperless.nix`, but
zfs-auto-snapshot only touches datasets carrying that property, so this is the
line that actually opts the archive in.

Then hand the dataset root to the service user:

```bash
sudo systemd-tmpfiles --create
```

`zfs create` leaves the dataset root as `root:root`, and the module's tmpfiles
rules cannot fix that during the deploy that first mounts it: on a switch the
activation script runs tmpfiles *before* the new mount unit starts, so it
chowns the directory underneath the mountpoint and the dataset root that lands
on top of it keeps root's ownership. `paperless-secret-key.service` is the
first thing to notice, failing with an exit code 1 it does not explain.

Re-running tmpfiles once the mount is up applies the same rules to the real
directory. A reboot fixes it equally well — on boot the mount is part of
`local-fs.target`, which tmpfiles orders itself after — but a reboot is a poor
trade for one idempotent command.

### P2 — real SMB credentials

`secrets/smb-fritznas.age` ships with a placeholder and the mount will fail
until it is replaced. From `secrets/`:

```bash
cd secrets && agenix -e smb-fritznas.age
```

Contents are a cifs `credentials(5)` file:

```
username=<fritzbox user>
password=<fritzbox password>
```

### P3 — the admin password

`secrets/paperless-admin.age` holds a generated 28-character password. Put it
in the password manager. To change it:

```bash
cd secrets && agenix -e paperless-admin.age
```

Note the module's warning: renaming the superuser later leaves the old one in
place. Change the password, not the name.

## Deploy

```bash
colmena build            # gate
bash rebuild.sh
```

## Verification

1. **Service up.** `systemctl status paperless-web` and browse to
   `http://sz1.fritz.box:28981`. Log in as `admin`.
2. **OCR.** Upload a real bank statement. The document should become
   full-text searchable, and German text should come out with correct
   umlauts — that is what proves `deu` reached tesseract.
3. **Mail.** Drag a message out of Thunderbird onto the uploader. Confirm
   sender became the correspondent, subject the title, and the send date the
   created date. This is the check that Tika and Gotenberg are actually wired
   up; without them the file lands as an opaque blob.
4. **Export.** `sudo systemctl start paperless-exporter`, then confirm
   `/var/lib/paperless/export/manifest.json` exists.
5. **Mirror.** The exporter triggers the backup itself; confirm with
   `systemctl status paperless-backup-smb` and `sudo ls /mnt/fritz-nas/My_Passport/Stefan/paperless` (root-owned mount).
6. **Restore.** The only check that matters. See below.

Once the IMAP fetcher is configured, `scripts/check-paperless-mail.py` reports
what it has actually seen:

```bash
sudo paperless-manage shell < scripts/check-paperless-mail.py
```

It reads `paperless_mail.ProcessedMail` rather than logs, because a working
fetcher finding nothing and a fetcher broken by a changed password both show up
as "no new documents" in the UI.

Set the IMAP port explicitly. `MailAccount.imap_port` is `blank=True,
null=True`, so neither the model, the serialiser, nor the web form requires it,
and a missing port reaches the mail client as `None` — which fails as a
connection timeout rather than as a validation error. 993 for SSL, 143 for
STARTTLS or no encryption.

The test button runs in the web server, not the task queue, so its output is in
`journalctl -u paperless-web`. Note also that the two mail loggers diverge:
`paperless_mail` (the connection handling and the test button) writes to
`mail.log`, while the scheduled task's own logger is `paperless.mail.tasks`,
which lands in `paperless.log`. Messages like "no rules enabled for account X"
are in the latter.

## Restore

An unverified backup is not a backup. Restoring reads the mirror, so it also
proves the SMB leg end to end.

```bash
sudo -u paperless paperless-manage document_importer /mnt/fritz-nas/My_Passport/Stefan/paperless
```

Against a scratch instance rather than the live one — `document_importer`
expects an empty database. To rebuild from nothing: create the dataset, deploy,
then import before uploading anything.

If Paperless itself is ever gone, the documents are still readable without it:
`PAPERLESS_FILENAME_FORMAT` lays the media tree out as
`{created_year}/{correspondent}/{created}_{title}`, navigable in any file
browser.

## Follow-ups

### sz3 migration

`hardware/sz3.nix` still mounts the share inline, with
`credentials=/etc/secrets/smb` — a file that exists on the machine but not in
this repo, so that mount is not reproducible from a clean install.
`modules/fritz-nas.nix` is where it should go. Blocked on the machine being
away; when it returns:

1. `ssh-keyscan -t ed25519 sz3` and add the key to `secrets/secrets.nix`
2. Add `sz3` to `"smb-fritznas.age".publicKeys` and rekey: `cd secrets && agenix -r`
3. Delete the `fileSystems."/run/media/stefan/fritzbox"` block from
   `hardware/sz3.nix`, and set on sz3:

   ```nix
   my.fritzNas = {
     enable = true;
     mountPoint = "/run/media/stefan/fritzbox";
     uid = 1000;
     gid = 100;
     fileMode = "0644";
     dirMode = "0755";
   };
   ```

4. Deploy sz3 and confirm the share still mounts.

### Deferred

- **Offsite third copy.** A restic repo to Backblaze B2 or a Hetzner Storage
  Box, encrypted client-side. Slots in beside the SMB mirror.
- **Tailscale access.** Add `tailscale0` to the firewall rule in
  `modules/paperless.nix` if the archive is ever wanted from outside the house.
- **Pi5 migration.** If Paperless moves to a Pi5, sz1 becomes the pull target
  rather than the host, and this module moves nearly unchanged. Blocked on
  Steam Link, which is armhf-only and unpackaged on NixOS; Moonlight
  (`moonlight-qt`, aarch64) plus `services.sunshine` on sz1 is the replacement.
