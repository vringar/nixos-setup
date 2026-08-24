"""Unit tests for check-owui-corpus.py's parsing and reporting."""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "check_owui_corpus",
    Path(__file__).parent.parent / "scripts" / "check-owui-corpus.py",
)
check = importlib.util.module_from_spec(_spec)
sys.modules["check_owui_corpus"] = check
_spec.loader.exec_module(check)


def write(tmp_path, text):
    path = tmp_path / "secret"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_reads_token_from_environment_file(tmp_path):
    assert check.read_token(write(tmp_path, "OPEN_WEBUI_TOKEN=sk-abc\n")) == "sk-abc"


def test_strips_quotes_like_systemd_does(tmp_path):
    assert check.read_token(write(tmp_path, 'OPEN_WEBUI_TOKEN="sk-abc"\n')) == "sk-abc"


def test_ignores_comments_blanks_and_other_keys(tmp_path):
    body = "# comment\n\nOTHER=1\nOPEN_WEBUI_TOKEN=sk-abc\n"
    assert check.read_token(write(tmp_path, body)) == "sk-abc"


def test_missing_key_reads_as_empty(tmp_path):
    assert check.read_token(write(tmp_path, "OTHER=1\n")) == ""


def page(items, total=None):
    return {"items": items, "total": total if total is not None else len(items)}


def test_finds_collection_on_a_later_page():
    pages = [
        page([{"name": str(n), "id": f"id-{n}"} for n in range(30)], total=31),
        page([{"name": "witcher-lore", "id": "wanted"}], total=31),
    ]
    assert check.find_collection(pages, "witcher-lore") == "wanted"


def test_absent_collection_is_none():
    assert check.find_collection([page([])], "witcher-lore") is None


def test_summarize_reports_attached_count_and_hash():
    payload = page(
        [{"id": "x", "filename": "a.md", "meta": {"name": "a.md", "data": {"corpus_hash": "abc123"}}}],
        total=850,
    )
    assert check.summarize(payload) == [
        "attached:  850",
        "page size: 1",
        "sample:    a.md",
        "hash:      abc123",
    ]


def test_summarize_flags_missing_metadata():
    """No corpus_hash means change detection cannot work — say so loudly."""
    payload = page([{"id": "x", "filename": "loose.md", "meta": {"name": "loose.md"}}], total=1)
    assert "MISSING" in check.summarize(payload)[-1]


def test_summarize_handles_an_empty_collection():
    assert check.summarize(page([], total=0)) == [
        "attached:  0",
        "page size: 0",
        "sample:    (none)",
    ]
