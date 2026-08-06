#!/usr/bin/env python3
"""Reconcile a corpus directory into an Open WebUI knowledge collection.

Open WebUI keeps collections in its database, so they cannot be declared in
Nix. This makes their *contents* declarative anyway: the corpus directory is
the desired state, and each run uploads what is missing, replaces what changed
and removes what is no longer wanted. A second run should report all zeros —
that is the check that change detection works.

Change detection compares a content hash attached as file metadata at upload
time, rather than the server's own `hash` field, so it does not depend on which
digest Open WebUI uses internally.

Endpoints and payload shapes were read from the instance's /openapi.json.

Usage: reconcile.py <collection-name> <corpus-dir>
Environment: OPEN_WEBUI_URL, OPEN_WEBUI_TOKEN
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

import requests

TIMEOUT = 300
# Attaching files one request at a time is the slow part; the API takes a list.
BATCH = 100


class OpenWebUI:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def _call(self, method: str, path: str, **kwargs):
        response = self.session.request(
            method, f"{self.base}{path}", timeout=TIMEOUT, **kwargs
        )
        if not response.ok:
            raise RuntimeError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}"
            )
        return response.json() if response.content else None

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body):
        return self._call("POST", path, json=body)

    def delete(self, path):
        return self._call("DELETE", path)

    def upload(self, filename: str, content: bytes, metadata: dict) -> dict:
        # Embedding runs in the background so the call returns promptly; inline
        # processing would make every one of thousands of uploads wait on CPU
        # sentence-transformers work.
        return self._call(
            "POST",
            "/api/v1/files/",
            params={"process": "true", "process_in_background": "true"},
            files={"file": (filename, content, "text/markdown")},
            data={"metadata": json.dumps(metadata)},
        )


# The paths this script depends on. Open WebUI ships an OpenAPI spec but no
# SDK; generating a client for 6 of its ~476 endpoints would vendor thousands
# of lines and still only match whichever version it was generated from.
# Checking the live spec instead turns an upgrade that moves an endpoint into
# one clear error, up front, rather than a 404 midway through an upload run.
REQUIRED_PATHS = [
    "/api/v1/knowledge/",
    "/api/v1/knowledge/create",
    "/api/v1/knowledge/{id}",
    "/api/v1/knowledge/{id}/file/remove",
    "/api/v1/knowledge/{id}/files/batch/add",
    "/api/v1/files/",
]


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


def ensure_collection(api: OpenWebUI, name: str) -> str:
    for item in api.get("/api/v1/knowledge/") or []:
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


def existing_files(api: OpenWebUI, collection_id: str) -> dict[str, tuple[str, str]]:
    """Map filename -> (file id, corpus hash) for what the collection holds."""
    detail = api.get(f"/api/v1/knowledge/{collection_id}") or {}
    out: dict[str, tuple[str, str]] = {}
    for item in detail.get("files") or []:
        meta = item.get("meta") or {}
        filename = meta.get("name") or item.get("filename") or ""
        stored = meta.get("data") if isinstance(meta.get("data"), dict) else meta
        out[filename] = (item["id"], stored.get("corpus_hash", ""))
    return out


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    name, corpus_dir = sys.argv[1], sys.argv[2]
    token = os.environ.get("OPEN_WEBUI_TOKEN", "")
    if not token:
        print("OPEN_WEBUI_TOKEN is unset", file=sys.stderr)
        return 2
    api = OpenWebUI(os.environ.get("OPEN_WEBUI_URL", "http://127.0.0.1:8080"), token)
    check_api(api)

    wanted: dict[str, bytes] = {}
    for entry in sorted(os.listdir(corpus_dir)):
        if entry.endswith(".md"):
            with open(os.path.join(corpus_dir, entry), "rb") as fh:
                wanted[entry] = fh.read()
    if not wanted:
        print(f"no .md files in {corpus_dir}", file=sys.stderr)
        return 1

    collection_id = ensure_collection(api, name)
    present = existing_files(api, collection_id)

    stale = [
        (filename, file_id)
        for filename, (file_id, file_hash) in present.items()
        if filename not in wanted or file_hash != digest(wanted[filename])
    ]
    todo = [
        filename
        for filename, content in wanted.items()
        if filename not in present or present[filename][1] != digest(content)
    ]

    for filename, file_id in stale:
        api.post(f"/api/v1/knowledge/{collection_id}/file/remove", {"file_id": file_id})
        # Detaching leaves the upload behind; delete it so re-runs do not pile up.
        api.delete(f"/api/v1/files/{file_id}")

    pending: list[dict] = []
    for index, filename in enumerate(todo, 1):
        content = wanted[filename]
        uploaded = api.upload(filename, content, {"corpus_hash": digest(content)})
        pending.append({"file_id": uploaded["id"]})
        if len(pending) >= BATCH:
            api.post(f"/api/v1/knowledge/{collection_id}/files/batch/add", pending)
            pending = []
            print(f"  {index}/{len(todo)} uploaded", file=sys.stderr, flush=True)
    if pending:
        api.post(f"/api/v1/knowledge/{collection_id}/files/batch/add", pending)

    removed = sum(1 for filename, _ in stale if filename not in wanted)
    print(
        f"{name}: {len(todo)} uploaded, {removed} removed, {len(wanted)} desired, "
        f"{len(present)} were present",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
