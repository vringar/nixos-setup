#!/usr/bin/env python3
"""Report what the corpus reconciler sees when it looks at a knowledge collection.

This performs the same read `reconcile.py` performs, so it distinguishes a
working reconciler from one that believes the collection is empty and
therefore re-uploads the whole corpus on every run.

Reads the deployed API key, so it needs root:
    sudo python3 scripts/check-owui-corpus.py [collection-name]

The token is never printed. Standard library only — no dependency on the
reconciler's own environment.

Environment: OPEN_WEBUI_URL, OPEN_WEBUI_SECRET
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080"
DEFAULT_SECRET = "/run/agenix/open-webui-token"
TIMEOUT = 30


def read_token(path: str) -> str:
    """Pull OPEN_WEBUI_TOKEN out of a systemd EnvironmentFile.

    Same format systemd parses: KEY=VALUE per line, blanks and # comments
    ignored, surrounding quotes stripped.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "OPEN_WEBUI_TOKEN":
                return value.strip().strip("'\"")
    return ""


def get(base: str, path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


def find_collection(pages: list[dict], name: str) -> str | None:
    """Id of the collection called `name`, or None.

    Takes every page: the list is served 30 at a time, so checking only the
    first would miss a collection and report it as absent.
    """
    for page in pages:
        for item in page.get("items") or []:
            if item.get("name") == name:
                return item.get("id")
    return None


def summarize(payload: dict) -> list[str]:
    """Lines describing one page of a collection's file listing."""
    items = payload.get("items") or []
    lines = [
        f"attached:  {payload.get('total')}",
        f"page size: {len(items)}",
    ]
    if not items:
        lines.append("sample:    (none)")
        return lines

    meta = items[0].get("meta") or {}
    data = meta.get("data") if isinstance(meta.get("data"), dict) else {}
    lines.append(f"sample:    {meta.get('name') or items[0].get('filename')}")
    lines.append(
        "hash:      "
        + (
            data.get("corpus_hash")
            or "MISSING — change detection will re-upload everything"
        )
    )
    return lines


def collect_pages(base: str, path: str, token: str) -> list[dict]:
    pages, page, seen = [], 1, 0
    while True:
        payload = get(base, f"{path}?page={page}", token)
        pages.append(payload)
        items = payload.get("items") or []
        seen += len(items)
        if not items or seen >= (payload.get("total") or 0):
            return pages
        page += 1


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "witcher-lore"
    base = os.environ.get("OPEN_WEBUI_URL", DEFAULT_URL)
    secret = os.environ.get("OPEN_WEBUI_SECRET", DEFAULT_SECRET)

    try:
        token = read_token(secret)
    except OSError:
        print(f"cannot read {secret} — run me with sudo", file=sys.stderr)
        return 1
    if not token:
        print(f"no OPEN_WEBUI_TOKEN in {secret}", file=sys.stderr)
        return 1

    try:
        collection_id = find_collection(
            collect_pages(base, "/api/v1/knowledge/", token), name
        )
        if collection_id is None:
            print(f"no collection named {name!r} — the next run will create it")
            return 0

        print(f"collection: {name} ({collection_id})")
        # The listing endpoint, not the collection detail: the detail response
        # never populates `files`, which is what made every run believe the
        # collection was empty.
        page = get(base, f"/api/v1/knowledge/{collection_id}/files?page=1", token)
    except urllib.error.HTTPError as error:
        print(f"{error.code} from {base}: {error.read()[:200]!r}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"cannot reach {base}: {error}", file=sys.stderr)
        return 1

    print("\n".join(summarize(page)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
