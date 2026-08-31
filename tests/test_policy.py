"""Unit tests for the Witcher curation policy.

The policy decides what counts as lore, and getting it wrong is silent: the
build still succeeds and the corpus just fills with the wrong pages. These
pin the rules, their order, and the exception that lets in-world writing
survive being filed as a game item.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "witcher-corpus"
sys.path.insert(0, str(_APP))
_spec = importlib.util.spec_from_file_location("policy", _APP / "policy.py")
policy = importlib.util.module_from_spec(_spec)
sys.modules["policy"] = policy
_spec.loader.exec_module(policy)


def page(kind="", categories=(), prose_length=10_000, title="A page"):
    return policy.Page(title, kind, list(categories), lambda: "x" * prose_length)


# --- the rules, individually -------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        "The Witcher Monster Slayer updates",
        "The Witcher 3 patches",
        "Thronebreaker patches",
        "The Witcher character development",
        "Modding",
        "Guides",
        "Add-ons",
    ],
)
def test_out_of_world_categories_are_dropped(category):
    assert policy.verdict(page(categories=[category])) == "about the games, not the world"


@pytest.mark.parametrize(
    "category",
    [
        "Combat spells",
        "Magic",
        "History",
        "Culture",
        "Magical items",
        "Creatures",
        "Ranks and titles",
        "Folklore",
        "Books mentioned in the novels",
        # Each of these three was tried as a filter and took real lore.
        "The Witcher combat",
        "Premium modules",
        "Romance cards",
        # A MediaWiki tracking category, and the reason Geralt was missing.
        "Pages with tables",
    ],
)
def test_lore_categories_survive(category):
    assert policy.verdict(page(categories=[category])) is None


# --- the exception -----------------------------------------------------------


def test_a_stat_block_is_dropped():
    assert policy.verdict(page(kind="item")) == "game-mechanics stat block"


def test_in_world_writing_survives_being_filed_as_an_item():
    """The `unless_category` exception: an item, but what it holds is writing."""
    verdict = policy.verdict(page(kind="item", categories=["The Witcher 3 books"]))
    assert verdict is None


def test_the_exception_does_not_reach_past_the_stub_threshold():
    """Diegetic pages are still subject to every later rule."""
    short = page(kind="item", categories=["The Witcher 3 books"], prose_length=10)
    assert policy.verdict(short) == "stub"


def test_the_exception_does_not_reach_past_the_housekeeping_rules():
    """Rules before the stat-block rule are unaffected by its exception."""
    both = page(kind="item", categories=["The Witcher 3 books", "Neutral gwent cards"])
    assert policy.verdict(both) == "game-mechanics category"


# --- ordering ----------------------------------------------------------------


def test_prose_is_not_rendered_when_a_cheaper_rule_already_decided():
    """Rendering every page would cost real time over a 100k-page dump."""

    def explode():
        raise AssertionError("the policy rendered a page it had already dropped")

    assert policy.verdict(policy.Page("t", "item", [], explode)) is not None


def test_a_plain_lore_page_is_kept():
    assert policy.verdict(page(kind="character", categories=["Witchers"])) is None


# --- the split of the old single filter --------------------------------------


def test_the_category_rules_still_cover_exactly_the_old_filter():
    """The one SKIP_CATEGORY regex became three, and must not have drifted."""
    old = re.compile(
        r"crafting diagram|gwent|thronebreaker card|quest item|relic|armor|"
        r"witcher gear|cut content|achievement|trophy|disambiguation|stub|"
        r"subpages|images?$|"
        r"patch(es)?$|updates?$|character development$|"
        r"^modding$|^guides$|^add-ons$",
        re.I,
    )
    new = [
        policy.MECHANICS_CATEGORY,
        policy.HOUSEKEEPING_CATEGORY,
        policy.OUT_OF_WORLD_CATEGORY,
    ]
    samples = [
        "Crafting diagrams", "The Witcher 3 gwent", "Thronebreaker cards",
        "The Witcher 3 quest items", "Relics", "Armor", "Witcher gear",
        "Cut content", "Achievements", "Trophies", "Triss (disambiguation)",
        "Stubs", "Subpages", "Images", "The Witcher patches",
        "The Witcher Monster Slayer updates", "The Witcher character development",
        "Modding", "Guides", "Add-ons",
        # and things neither should touch
        "Magic", "History", "Witchers", "Combat spells", "Pages with tables",
        "The Witcher 3 books", "Thronebreaker letters and reports",
    ]
    for s in samples:
        assert bool(old.search(s)) == any(p.search(s) for p in new), s
