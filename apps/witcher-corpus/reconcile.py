#!/usr/bin/env python3
"""Reconcile a corpus directory into an Open WebUI knowledge collection.

Open WebUI keeps collections in its database, so they cannot be declared in
Nix. This makes their *contents* declarative anyway: the corpus directory is
the desired state, and each run uploads what is missing, replaces what changed
and removes what is no longer wanted. A second run should report all zeros —
that is the check that change detection works.

The diff is computed server-side. `sync/diff` takes a manifest of checksums,
compares it against the collection's real attachment table and answers what is
added, modified and deleted. Doing it here instead meant reading the
collection back and comparing by hand, which depended on knowing which of
several endpoints reports contents truthfully — one of them silently reports
every collection as empty. `sync/diff` also writes nothing, so it doubles as a
dry run: it can be asked what a run would do without doing it.

Checksums are SHA-256 of the raw bytes. Deliberately not sent as upload
metadata: letting the server hash what it actually received makes the
comparison an end-to-end integrity check, so a truncated upload disagrees on
the next run and is re-sent rather than silently accepted.

Usage: reconcile.py [--dry-run] <collection-name> <corpus-dir>
Environment: OPEN_WEBUI_URL, OPEN_WEBUI_TOKEN
"""
from __future__ import annotations

import hashlib
import os
import sys

import requests

TIMEOUT = 300
# Attaching a batch embeds every file in it before the request returns — CPU
# work at Nice=15, competing with inference. Timing out client-side does not
# stop the server, it only abandons work that is still running and leaves the
# uploads unattached, so this is deliberately generous.
PROCESS_TIMEOUT = 1800
# Attaching files one request at a time is the slow part; the API takes a list.
# Small enough that one batch stays well inside PROCESS_TIMEOUT, and that the
# progress line moves often enough to tell a slow run from a stuck one.
BATCH = 25


class OpenWebUI:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def _call(self, method: str, path: str, timeout: int = TIMEOUT, **kwargs):
        response = self.session.request(
            method, f"{self.base}{path}", timeout=timeout, **kwargs
        )
        if not response.ok:
            raise RuntimeError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}"
            )
        return response.json() if response.content else None

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body, timeout: int = TIMEOUT):
        return self._call("POST", path, json=body, timeout=timeout)

    def upload(self, filename: str, content: bytes) -> dict:
        # Embedding runs in the background so the call returns promptly; inline
        # processing would make every one of thousands of uploads wait on CPU
        # sentence-transformers work.
        return self._call(
            "POST",
            "/api/v1/files/",
            params={"process": "true", "process_in_background": "true"},
            files={"file": (filename, content, "text/markdown")},
        )


# The paths this script depends on. Open WebUI ships an OpenAPI spec but no
# SDK; generating a client for 6 of its ~476 endpoints would vendor thousands
# of lines and still only match whichever version it was generated from.
# Checking the live spec instead turns an upgrade that moves an endpoint into
# one clear error, up front, rather than a 404 midway through an upload run.
# This checks that the paths still exist, not that they still behave the same —
# `paged` and the diff shape check below cover the responses we parse.
REQUIRED_PATHS = [
    "/api/v1/knowledge/",
    "/api/v1/knowledge/create",
    "/api/v1/knowledge/{id}/sync/diff",
    "/api/v1/knowledge/{id}/sync/cleanup",
    "/api/v1/knowledge/{id}/files/batch/add",
    "/api/v1/files/",
]

DIFF_KEYS = ("added", "modified", "deleted", "unmodified_count")


def check_api(api: "OpenWebUI") -> None:
    spec = api.get("/openapi.json") or {}
    missing = [p for p in REQUIRED_PATHS if p not in (spec.get("paths") or {})]
    if missing:
        raise RuntimeError(
            "Open WebUI's API no longer provides: "
            + ", ".join(missing)
            + " — the reconciler needs updating for this version"
        )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paged(api: OpenWebUI, path: str):
    """Yield every item from a list endpoint, one page at a time.

    List responses are an envelope, `{"items": [...], "total": N}`, served 30
    at a time. An unpaged GET therefore returns the first page and looks like
    the whole answer, which would make this create a second collection with a
    duplicate name once 30 of them exist.
    """
    page = 1
    seen = 0
    while True:
        payload = api.get(f"{path}?page={page}")
        if not isinstance(payload, dict) or "items" not in payload:
            raise RuntimeError(
                f"GET {path} did not return a paginated envelope — Open WebUI "
                "changed the list shape and the reconciler needs updating"
            )
        items = payload.get("items") or []
        if not items:
            return
        yield from items
        seen += len(items)
        if seen >= (payload.get("total") or 0):
            return
        page += 1


def ensure_collection(api: OpenWebUI, name: str) -> str:
    for item in paged(api, "/api/v1/knowledge/"):
        if item.get("name") == name:
            return item["id"]
    created = api.post(
        "/api/v1/knowledge/create",
        {
            "name": name,
            "description": "Managed by reconcile.py — edits in the UI are overwritten",
        },
    )
    return created["id"]


def read_corpus(corpus_dir: str) -> dict[str, bytes]:
    wanted: dict[str, bytes] = {}
    for entry in sorted(os.listdir(corpus_dir)):
        if entry.endswith(".md"):
            with open(os.path.join(corpus_dir, entry), "rb") as fh:
                wanted[entry] = fh.read()
    return wanted


def manifest(wanted: dict[str, bytes]) -> list[dict]:
    """The corpus as the server wants to see it.

    `path` is the directory within the collection; the corpus is flat, so it is
    empty for every file. It is part of the identity the server diffs on, so it
    has to match what the uploads actually produce — files attached without a
    directory index as root.
    """
    return [
        {
            "filename": filename,
            "path": "",
            "checksum": digest(content),
            "size": len(content),
        }
        for filename, content in wanted.items()
    ]


def diff(api: OpenWebUI, collection_id: str, wanted: dict[str, bytes]) -> dict:
    payload = api.post(
        f"/api/v1/knowledge/{collection_id}/sync/diff",
        {"manifest": manifest(wanted)},
    )
    if not isinstance(payload, dict) or any(k not in payload for k in DIFF_KEYS):
        raise RuntimeError(
            "sync/diff did not return the expected shape — Open WebUI changed "
            "the sync contract and the reconciler needs updating"
        )
    return payload


def attach(api: OpenWebUI, collection_id: str, batch: list[dict]) -> None:
    api.post(
        f"/api/v1/knowledge/{collection_id}/files/batch/add",
        batch,
        timeout=PROCESS_TIMEOUT,
    )


def main() -> int:
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    name, corpus_dir = argv
    token = os.environ.get("OPEN_WEBUI_TOKEN", "")
    if not token:
        print("OPEN_WEBUI_TOKEN is unset", file=sys.stderr)
        return 2
    api = OpenWebUI(os.environ.get("OPEN_WEBUI_URL", "http://127.0.0.1:8080"), token)
    check_api(api)

    wanted = read_corpus(corpus_dir)
    if not wanted:
        print(f"no .md files in {corpus_dir}", file=sys.stderr)
        return 1

    collection_id = ensure_collection(api, name)
    plan = diff(api, collection_id, wanted)

    todo = [entry["filename"] for entry in plan["added"]] + [
        entry["filename"] for entry in plan["modified"]
    ]
    # Superseded copies and files no longer in the corpus. Removed after the
    # uploads rather than before: a failed run then leaves a stale copy to be
    # replaced next time, instead of a gap where a document used to be.
    obsolete = [entry["stale_file_id"] for entry in plan["modified"]] + [
        entry["file_id"] for entry in plan["deleted"]
    ]

    if dry_run:
        # sync/diff writes nothing, so everything above this point was
        # read-only. Reporting here answers "what would a run do" without
        # spending hours finding out.
        print(
            f"{name}: would upload {len(todo)}, remove {len(obsolete)}; "
            f"{len(wanted)} desired, {plan['unmodified_count']} already present",
            file=sys.stderr,
        )
        return 0

    pending: list[dict] = []
    for index, filename in enumerate(todo, 1):
        uploaded = api.upload(filename, wanted[filename])
        pending.append({"file_id": uploaded["id"]})
        if len(pending) >= BATCH:
            attach(api, collection_id, pending)
            pending = []
            print(f"  {index}/{len(todo)} uploaded", file=sys.stderr, flush=True)
    if pending:
        attach(api, collection_id, pending)

    if obsolete:
        # Removes the vector entries and the stored blob too, which detaching
        # on its own does not.
        api.post(
            f"/api/v1/knowledge/{collection_id}/sync/cleanup",
            {"file_ids": obsolete, "dir_ids": plan.get("rmdir") or []},
            timeout=PROCESS_TIMEOUT,
        )

    print(
        f"{name}: {len(todo)} uploaded, {len(obsolete)} removed, "
        f"{len(wanted)} desired, {plan['unmodified_count']} already present",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
