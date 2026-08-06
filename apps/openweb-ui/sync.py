#!/usr/bin/env python3
"""Apply a plugin manifest to Open WebUI.

Open WebUI keeps plugins ("functions") in its database, so they cannot be
declared outright. This makes their *content and configuration* declarative:
the manifest is the desired state, and each run creates what is missing,
updates what changed and leaves the rest alone. A second run is a no-op.

The toggle endpoints flip a boolean rather than setting one, so activation is
read first and only changed when it actually differs — calling toggle blindly
would disable a plugin on every second run.

Usage: sync.py <manifest.json>
Environment: OPEN_WEBUI_URL, OPEN_WEBUI_TOKEN
"""
from __future__ import annotations

import json
import os
import sys

import requests

TIMEOUT = 60
REQUIRED_PATHS = [
    "/api/v1/functions/",
    "/api/v1/functions/create",
    "/api/v1/functions/id/{id}/update",
    "/api/v1/functions/id/{id}/toggle",
    "/api/v1/functions/id/{id}/toggle/global",
    "/api/v1/functions/id/{id}/valves/update",
]


class OpenWebUI:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def call(self, method: str, path: str, **kwargs):
        response = self.session.request(
            method, f"{self.base}{path}", timeout=TIMEOUT, **kwargs
        )
        if not response.ok:
            raise RuntimeError(
                f"{method} {path} -> {response.status_code}: {response.text[:400]}"
            )
        return response.json() if response.content else None

    def check(self) -> None:
        spec = self.call("GET", "/openapi.json") or {}
        missing = [p for p in REQUIRED_PATHS if p not in (spec.get("paths") or {})]
        if missing:
            raise RuntimeError(
                "Open WebUI's API no longer provides: "
                + ", ".join(missing)
                + " — the plugin sync needs updating for this version"
            )


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        manifest = json.load(fh)

    token = os.environ.get("OPEN_WEBUI_TOKEN", "")
    if not token:
        print("OPEN_WEBUI_TOKEN is unset", file=sys.stderr)
        return 2
    api = OpenWebUI(os.environ.get("OPEN_WEBUI_URL", "http://127.0.0.1:8080"), token)
    api.check()

    installed = {f["id"]: f for f in api.call("GET", "/api/v1/functions/") or []}

    for plugin in manifest:
        plugin_id = plugin["id"]
        with open(plugin["content"], encoding="utf-8") as fh:
            content = fh.read()
        form = {
            "id": plugin_id,
            "name": plugin["name"],
            "content": content,
            "meta": {"description": plugin.get("description", ""), "manifest": {}},
        }
        current = installed.get(plugin_id)
        if current is None:
            api.call("POST", "/api/v1/functions/create", json=form)
            print(f"{plugin_id}: created", file=sys.stderr)
            current = {"is_active": False, "is_global": False}
        elif current.get("content") != content or current.get("name") != form["name"]:
            api.call("POST", f"/api/v1/functions/id/{plugin_id}/update", json=form)
            print(f"{plugin_id}: updated", file=sys.stderr)

        if plugin.get("valves"):
            api.call(
                "POST", f"/api/v1/functions/id/{plugin_id}/valves/update",
                json=plugin["valves"],
            )

        # These endpoints toggle rather than set, so compare first.
        if not current.get("is_active"):
            api.call("POST", f"/api/v1/functions/id/{plugin_id}/toggle", json={})
            print(f"{plugin_id}: enabled", file=sys.stderr)
        if plugin.get("global") and not current.get("is_global"):
            api.call("POST", f"/api/v1/functions/id/{plugin_id}/toggle/global", json={})
            print(f"{plugin_id}: enabled globally", file=sys.stderr)

    print(f"{len(manifest)} plugin(s) in sync", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
