#!/usr/bin/env python3
"""Verify that a Paperless export is complete and intact.

A green `paperless-backup-smb` says rsync exited zero. It does not say the
export it copied was whole, and it does not say the bytes on the far end still
match the bytes that went in. Those are the two ways a backup is quietly
useless, and both are checkable without Paperless: manifest.json records every
document's exported filename alongside the SHA-256 the archive was built from.

Deliberately stdlib-only and Django-free, so it runs against a copy of the
export on any machine - including a rescue system where Paperless is exactly
what you no longer have.

    sudo scripts/check-paperless-export.py /mnt/fritz-nas/My_Passport/Stefan/paperless

Existence of every referenced file is checked by default. Add --checksums to
also hash the contents, which is the check that actually proves the copy is
faithful - expect it to be slow over SMB.

Exit status is 0 only when nothing is wrong, so this can become a timer later.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

DOCUMENT_MODEL = "documents.document"

# documents/settings.py - the keys the exporter adds to each document record.
EXPORTED_FILE = "__exported_file_name__"
EXPORTED_THUMBNAIL = "__exported_thumbnail_name__"
EXPORTED_ARCHIVE = "__exported_archive_name__"

# Original and archive files each carry their own recorded digest; thumbnails
# are regenerable and have none, so they are only checked for existence.
CHECKSUMMED = [(EXPORTED_FILE, "checksum"), (EXPORTED_ARCHIVE, "archive_checksum")]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(root):
    manifest = root / "manifest.json"
    if not manifest.is_file():
        sys.exit(f"{manifest} does not exist - this is not a Paperless export")
    try:
        return json.loads(manifest.read_text())
    except json.JSONDecodeError as exc:
        sys.exit(f"{manifest} is not valid JSON ({exc}) - the export is truncated")


def check_documents(root, records, verify_checksums):
    """Return (problems, referenced paths) for every document in the manifest."""
    problems = []
    referenced = set()

    for record in records:
        fields = record.get("fields", {})
        title = fields.get("title") or f"pk={record.get('pk')}"

        for key in (EXPORTED_FILE, EXPORTED_THUMBNAIL, EXPORTED_ARCHIVE):
            name = record.get(key)
            if not name:
                continue
            path = root / name
            referenced.add(path)
            if not path.is_file():
                problems.append(f"missing: {name} ({title})")

        if not verify_checksums:
            continue

        for key, checksum_field in CHECKSUMMED:
            name = record.get(key)
            expected = fields.get(checksum_field)
            if not name or not expected:
                continue
            path = root / name
            if not path.is_file():
                continue  # already reported above
            actual = sha256(path)
            if actual != expected:
                problems.append(
                    f"corrupt: {name} ({title})\n"
                    f"    manifest {expected}\n"
                    f"    on disk  {actual}",
                )

    return problems, referenced


def find_orphans(root, referenced):
    """Files present but unreferenced - the fingerprint of a partial delete."""
    orphans = []
    for path in root.rglob("*"):
        if path.is_file() and path.name != "manifest.json" and path not in referenced:
            orphans.append(path.relative_to(root))
    return orphans


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", type=Path, help="directory holding manifest.json")
    parser.add_argument(
        "--checksums",
        action="store_true",
        help="hash every file and compare against the manifest (slow over SMB)",
    )
    args = parser.parse_args()

    root = args.export_dir
    if not root.is_dir():
        sys.exit(f"{root} is not a directory")

    manifest = load_manifest(root)
    documents = [r for r in manifest if r.get("model") == DOCUMENT_MODEL]
    print(f"Manifest: {len(manifest)} records, {len(documents)} documents")
    if not documents:
        print("No documents in the manifest - nothing has been exported yet")

    problems, referenced = check_documents(root, documents, args.checksums)
    mode = "existence and checksums" if args.checksums else "existence only"
    print(f"Checked {len(referenced)} files ({mode})")

    orphans = find_orphans(root, referenced)
    if orphans:
        print(f"\n{len(orphans)} unreferenced file(s) present:")
        for orphan in orphans[:10]:
            print(f"  {orphan}")
        if len(orphans) > 10:
            print(f"  ... and {len(orphans) - 10} more")
        print("  (harmless on their own, but a partial sync looks like this)")

    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print("\nOK - every referenced file is present" + (" and matches" if args.checksums else ""))
    if not args.checksums:
        print("Note: contents were not verified. Re-run with --checksums for that.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
