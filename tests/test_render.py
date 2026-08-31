"""Unit tests for the generic queries render.py runs over a parsed page."""

import importlib.util
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).parent.parent / "apps" / "witcher-corpus"
sys.path.insert(0, str(_APP))
_spec = importlib.util.spec_from_file_location("render", _APP / "render.py")
render = importlib.util.module_from_spec(_spec)
sys.modules["render"] = render
_spec.loader.exec_module(render)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Fandom templates one infobox per game, so the bare name never
        # matched the numbered ones the wiki actually uses.
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
        # Underscores normalise for every kind, so the emitted type is
        # consistent rather than carrying the template's spelling.
        ("Infobox real_person", "real person"),
        ("Infobox film_tv", "film tv"),
    ],
)
def test_infobox_kind_normalises_away_the_game(raw, expected):
    assert render.infobox_kind(raw) == expected
