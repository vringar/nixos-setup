"""Unit tests for the curation policy in wikitext-to-markdown.py.

The converter decides what counts as lore, and getting that wrong is silent:
the build still succeeds and the corpus just fills up with the wrong pages.
These pin the two filters that do the deciding.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "witcher-corpus"
# The converter imports render and wikitext as siblings, so the package
# directory has to be importable before the module itself is loaded.
sys.path.insert(0, str(_APP))
_spec = importlib.util.spec_from_file_location(
    "wikitext_to_markdown", _APP / "wikitext-to-markdown.py"
)
w2m = importlib.util.module_from_spec(_spec)
sys.modules["wikitext_to_markdown"] = w2m
_spec.loader.exec_module(w2m)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The bug this filter had: one infobox per game, so the bare name in
        # SKIP_INFOBOX never matched the numbered ones the wiki actually uses.
        ("Infobox item1", "item"),
        ("Infobox item2", "item"),
        ("Infobox item3", "item"),
        ("Infobox quest1", "quest"),
        ("Infobox quest3", "quest"),
        # Expansions suffix the template rather than renaming it.
        ("Infobox item3/baw", "item"),
        # Thronebreaker uses an underscore, and both spellings occur.
        ("Infobox tb_battle", "tb battle"),
        ("Infobox tb battle", "tb battle"),
        # Names without a game number survive untouched.
        ("Infobox character", "character"),
        ("Infobox location", "location"),
        ("Infobox bestiary", "bestiary"),
        # Underscores normalise for lore types too, so the frontmatter is
        # consistent rather than carrying the template's spelling.
        ("Infobox real_person", "real person"),
        ("Infobox film_tv", "film tv"),
    ],
)
def test_infobox_kind_normalises_away_the_game(raw, expected):
    assert w2m.infobox_kind(raw) == expected


@pytest.mark.parametrize(
    "kind",
    ["Infobox item3", "Infobox quest1", "Infobox tb_battle", "Infobox item3/baw"],
)
def test_numbered_game_mechanics_are_skipped(kind):
    assert w2m.infobox_kind(kind) in w2m.SKIP_INFOBOX


@pytest.mark.parametrize(
    "kind", ["Infobox character", "Infobox location", "Infobox bestiary"]
)
def test_lore_infoboxes_survive(kind):
    assert w2m.infobox_kind(kind) not in w2m.SKIP_INFOBOX


@pytest.mark.parametrize(
    "category",
    [
        "The Witcher Monster Slayer updates",
        "The Witcher 3 patches",
        "The Witcher 2 patches",
        "Thronebreaker patches",
        "The Witcher character development",
        "Modding",
        "Guides",
        "Add-ons",
    ],
)
def test_gameplay_categories_are_skipped(category):
    assert w2m.SKIP_CATEGORY.search(category)


@pytest.mark.parametrize(
    "category",
    [
        "Combat spells",
        # Each of these three filtered out real lore before being dropped.
        "The Witcher combat",
        "Premium modules",
        "Magic",
        "History",
        "Culture",
        "Magical items",
        "Creatures",
        "Ranks and titles",
        "Folklore",
        "Books mentioned in the novels",
        # The wiki files romanceable characters under this, so filtering on it
        # dropped Triss Merigold. The card pages go via the gwent patterns.
        "Romance cards",
    ],
)
def test_lore_categories_survive(category):
    assert not w2m.SKIP_CATEGORY.search(category)
