"""Unit tests for reconcile.py's paging over Open WebUI list responses."""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "reconcile",
    Path(__file__).parent.parent / "apps" / "witcher-corpus" / "reconcile.py",
)
reconcile = importlib.util.module_from_spec(_spec)
sys.modules["reconcile"] = reconcile
_spec.loader.exec_module(reconcile)


class FakeApi:
    """Serves a list endpoint the way Open WebUI does: 30 items per page."""

    def __init__(self, items, page_size=30):
        self.items = items
        self.page_size = page_size
        self.requested = []

    def get(self, path):
        self.requested.append(path)
        page = int(path.rsplit("page=", 1)[1])
        start = (page - 1) * self.page_size
        return {"items": self.items[start : start + self.page_size], "total": len(self.items)}


def collection(name):
    return {"id": f"id-{name}", "name": name}


def test_single_page_returns_every_item():
    api = FakeApi([collection(str(n)) for n in range(5)])
    assert len(list(reconcile.paged(api, "/api/v1/knowledge/"))) == 5
    assert api.requested == ["/api/v1/knowledge/?page=1"]


def test_walks_past_the_page_boundary():
    api = FakeApi([collection(str(n)) for n in range(70)])
    got = list(reconcile.paged(api, "/api/v1/knowledge/"))
    assert [item["name"] for item in got] == [str(n) for n in range(70)]
    assert api.requested == [f"/api/v1/knowledge/?page={n}" for n in (1, 2, 3)]


def test_empty_collection_list_terminates():
    api = FakeApi([])
    assert list(reconcile.paged(api, "/api/v1/knowledge/")) == []


def test_bare_list_response_is_rejected_loudly():
    """The 0.11 breakage: a bare list used to be iterated as if it were items."""

    class BareListApi:
        def get(self, path):
            return [collection("witcher-lore")]

    with pytest.raises(RuntimeError, match="paginated envelope"):
        list(reconcile.paged(BareListApi(), "/api/v1/knowledge/"))


def test_finds_existing_collection_on_a_later_page():
    items = [collection(str(n)) for n in range(40)] + [collection("witcher-lore")]
    api = FakeApi(items)
    assert reconcile.ensure_collection(api, "witcher-lore") == "id-witcher-lore"


class DiffApi:
    """Records the diff request and replays a canned plan."""

    def __init__(self, plan):
        self.plan = plan
        self.posted = []

    def post(self, path, body, timeout=None):
        self.posted.append((path, body, timeout))
        return self.plan


def plan(added=(), modified=(), deleted=(), unmodified=0, rmdir=()):
    return {
        "added": [{"filename": f, "path": ""} for f in added],
        "modified": [
            {"filename": f, "path": "", "stale_file_id": f"stale-{f}"} for f in modified
        ],
        "deleted": [{"file_id": i, "filename": f} for i, f in deleted],
        "mkdir": [],
        "rmdir": list(rmdir),
        "unmodified_count": unmodified,
        "directory_map": {},
    }


def test_manifest_entry_shape_matches_the_server_contract():
    """filename/path/checksum/size are all required by FileManifestEntry."""
    entries = reconcile.manifest({"a.md": b"hello"})
    assert entries == [
        {
            "filename": "a.md",
            "path": "",
            "checksum": reconcile.digest(b"hello"),
            "size": 5,
        }
    ]


def test_manifest_path_is_root_for_every_file():
    """The corpus is flat; a non-empty path would not match how uploads land,
    and path is part of the identity the server diffs on."""
    entries = reconcile.manifest({"a.md": b"x", "b.md": b"y"})
    assert {e["path"] for e in entries} == {""}


def test_checksum_is_sha256_of_raw_bytes():
    """Must equal what the server computes for the uploaded bytes, or every
    file reads as modified forever."""
    import hashlib

    entries = reconcile.manifest({"a.md": b"witcher"})
    assert entries[0]["checksum"] == hashlib.sha256(b"witcher").hexdigest()


def test_diff_posts_the_manifest_to_the_sync_endpoint():
    api = DiffApi(plan(unmodified=1))
    reconcile.diff(api, "col-1", {"a.md": b"x"})
    path, body, _ = api.posted[0]
    assert path == "/api/v1/knowledge/col-1/sync/diff"
    assert list(body) == ["manifest"]


def test_diff_rejects_an_unexpected_response_shape():
    """The old spec check only asserted paths existed, which let a changed
    response shape through as a crash mid-run."""
    api = DiffApi({"added": [], "modified": []})
    with pytest.raises(RuntimeError, match="sync contract"):
        reconcile.diff(api, "col-1", {"a.md": b"x"})


def test_diff_passes_a_well_formed_plan_through():
    api = DiffApi(plan(added=["a.md"], unmodified=7))
    got = reconcile.diff(api, "col-1", {"a.md": b"x"})
    assert got["unmodified_count"] == 7
    assert got["added"][0]["filename"] == "a.md"
