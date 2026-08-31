"""What counts as Witcher lore.

Every judgement specific to this wiki lives here, as data. The rest of the
pipeline reads a dump, parses wikitext and writes markdown without knowing
what any of it is about.

A page is kept unless some rule drops it. Rules are tried in order and the
first that applies decides, so a rule's position is its precedence -- the
relationship that used to be a paragraph of English next to a regex.

`unless_category` is how an exception is written: a page the wiki files as a
game item is still dropped as a stat block, *unless* it also sits in a
category of in-world writing, in which case the drop does not apply.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Callable

SOURCE_BASE = "https://witcher.fandom.com/wiki/"

# Infobox kinds that describe game mechanics rather than the world. Items
# alone are a third of the wiki -- crafting diagrams, armour, relics -- and
# would drown retrieval in stat blocks. Pages with no infobox are kept: that
# is where the concept articles live (Elder Blood, Signs, historical events).
MECHANICS_INFOBOX = frozenset({
    "item", "quest", "gwent", "gwent card", "achievement", "needed", "tb",
    "tb battle", "merchant", "trophy", "mutagen", "diagram", "weapon", "armor",
    "potion", "bomb", "oil", "card", "book", "crafting",
})

MECHANICS_CATEGORY = re.compile(
    r"crafting diagram|gwent|thronebreaker card|quest item|relic|armor|"
    r"witcher gear|achievement|trophy",
    re.I,
)
# Categories the wiki keeps for its own bookkeeping. Deliberately absent:
# "pages with ...", which are MediaWiki tracking categories and say nothing
# about content -- "Pages with tables" alone removed 177 articles, Geralt of
# Rivia among them, because a long well-developed page contains a table.
HOUSEKEEPING_CATEGORY = re.compile(
    r"cut content|disambiguation|stub|subpages|images?$",
    re.I,
)
# Pages about the games as software: patch notes, release updates, skill
# trees, community tooling. The test is whether the page exists inside the
# fiction, so in-world writing stays even when a game is the only place it
# appears.
#
# Rejected, each having taken lore with it:
#   "romance cards"   -- the wiki files the character there, so it removed
#                        Triss Merigold. Card pages go via gwent above.
#   "combat"          -- removed Sign and Witcher fighting styles, which
#                        describe the world, not a control scheme.
#   "premium modules" -- The Price of Neutrality and Side Effects are story
#                        adventures, so their content is narrative.
OUT_OF_WORLD_CATEGORY = re.compile(
    r"patch(es)?$|updates?$|character development$|^modding$|^guides$|^add-ons$",
    re.I,
)
# In-world writing that the wiki files as an item, because in the games it is
# one: books, letters, scrolls and the contract notices pinned to notice
# boards. What is written on them is diegetic, so it survives the stat-block
# rule -- though not the housekeeping rules or the stub threshold.
DIEGETIC_CATEGORY = re.compile(
    r"notice board postings$|letters and reports$|\bbooks$|letters$|scrolls$",
    re.I,
)

# Infobox fields that are asset filenames rather than facts.
ASSET_FIELD = frozenset(
    {"image", "coa", "flag", "geo map", "city map", "px", "width", "imagebg"}
)
# Below this much prose a page is a stub -- a title and a sentence fragment,
# which only adds retrieval noise.
MIN_PROSE = 400


class Page:
    """The facts a rule may consult about one article.

    `prose` is a thunk: rendering costs real time over 100k pages, so it runs
    only if a rule actually asks how long the prose is, which by then means
    every cheaper rule has already declined to drop the page.
    """

    def __init__(
        self, title: str, kind: str, categories: list[str], prose: Callable[[], str]
    ) -> None:
        self.title = title
        self.kind = kind
        self.categories = categories
        self._prose = prose

    @functools.cached_property
    def prose_length(self) -> int:
        return len(self._prose())


@dataclass(frozen=True)
class Drop:
    """Drop a page when every stated condition holds and no exception applies."""

    why: str
    category: re.Pattern | None = None
    kind: frozenset[str] | None = None
    prose_under: int | None = None
    unless_category: re.Pattern | None = None

    def applies(self, page: Page) -> bool:
        if self.category is not None and not _matches(self.category, page.categories):
            return False
        if self.kind is not None and page.kind not in self.kind:
            return False
        if self.unless_category is not None and _matches(
            self.unless_category, page.categories
        ):
            return False
        # Last, so the thunk stays unevaluated unless everything else matched.
        if self.prose_under is not None and page.prose_length >= self.prose_under:
            return False
        return True


def _matches(pattern: re.Pattern, categories: list[str]) -> bool:
    return any(pattern.search(c) for c in categories)


RULES: tuple[Drop, ...] = (
    Drop("game-mechanics category", category=MECHANICS_CATEGORY),
    Drop("wiki housekeeping", category=HOUSEKEEPING_CATEGORY),
    Drop("about the games, not the world", category=OUT_OF_WORLD_CATEGORY),
    Drop(
        "game-mechanics stat block",
        kind=MECHANICS_INFOBOX,
        unless_category=DIEGETIC_CATEGORY,
    ),
    Drop("stub", prose_under=MIN_PROSE),
)


def verdict(page: Page) -> str | None:
    """Return the reason this page is dropped, or None to keep it."""
    for rule in RULES:
        if rule.applies(page):
            return rule.why
    return None


def source_url(title: str) -> str:
    return SOURCE_BASE + title.replace(" ", "_")


def facts(fields: dict[str, str]) -> dict[str, str]:
    """The infobox fields worth stating, asset filenames removed."""
    return {k: v for k, v in fields.items() if k not in ASSET_FIELD}
