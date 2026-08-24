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


class FilesApi:
    """Serves the collection's file listing the way Open WebUI does."""

    def __init__(self, files):
        self.files = files
        self.requested = []

    def get(self, path):
        self.requested.append(path)
        page = int(path.rsplit("page=", 1)[1])
        start = (page - 1) * 30
        return {"items": self.files[start : start + 30], "total": len(self.files)}


def uploaded(name, digest, file_id=None):
    return {
        "id": file_id or f"file-{name}",
        "filename": name,
        "meta": {"name": name, "data": {"corpus_hash": digest}},
    }


def test_existing_files_reads_the_listing_endpoint_not_the_detail():
    """The detail endpoint never populates `files`; reading it saw an empty
    collection and re-uploaded the corpus on every run."""
    api = FilesApi([uploaded("a.md", "hash-a")])
    reconcile.existing_files(api, "col-1")
    assert api.requested == ["/api/v1/knowledge/col-1/files?page=1"]


def test_existing_files_maps_name_to_id_and_corpus_hash():
    api = FilesApi([uploaded("a.md", "hash-a"), uploaded("b.md", "hash-b")])
    assert reconcile.existing_files(api, "col-1") == {
        "a.md": ("file-a.md", "hash-a"),
        "b.md": ("file-b.md", "hash-b"),
    }


def test_existing_files_walks_every_page():
    api = FilesApi([uploaded(f"{n}.md", f"h{n}") for n in range(65)])
    assert len(reconcile.existing_files(api, "col-1")) == 65


def test_existing_files_tolerates_missing_metadata():
    """A file uploaded outside the reconciler has no corpus_hash; it must read
    as changed rather than crash."""
    api = FilesApi([{"id": "x", "filename": "loose.md", "meta": {"name": "loose.md"}}])
    assert reconcile.existing_files(api, "col-1") == {"loose.md": ("x", "")}
