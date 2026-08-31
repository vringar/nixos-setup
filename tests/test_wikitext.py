"""Regression tests for the wikitext parser's bracket handling.

The bug these pin was silent and total: a template argument containing an
external link absorbed every byte after it, so the page parsed without error
and rendered to nothing. 245 articles left the corpus that way.
"""

import importlib.util
import sys
from pathlib import Path

_APP = Path(__file__).parent.parent / "apps" / "witcher-corpus"
sys.path.insert(0, str(_APP))
_spec = importlib.util.spec_from_file_location("wikitext", _APP / "wikitext.py")
wikitext = importlib.util.module_from_spec(_spec)
sys.modules["wikitext"] = wikitext
_spec.loader.exec_module(wikitext)

Template = wikitext.Template


def test_external_link_ends_at_its_closing_bracket():
    nodes = wikitext.parse("[http://example.com/ Label] after")
    assert "after" in wikitext.flatten_text(nodes)


def test_external_link_inside_a_template_does_not_eat_the_page():
    """The exact shape that emptied Ciri, Yennefer and Vesemir.

    Their infoboxes carry `|voice = [http://imdb.../ Actor]`, and the link
    label ran past its own `]` and swallowed the entire article body.
    """
    text = "{{Infobox|voice = [http://imdb.com/name/nm1/ Jo Wyatt]}}\nBODY TEXT"
    nodes = wikitext.parse(text)
    assert isinstance(nodes[0], Template), "the infobox must close"
    assert "BODY TEXT" in wikitext.flatten_text(nodes[1:])


def test_several_links_in_one_argument_each_terminate():
    text = "{{Infobox|voice = [http://a.test/ A]<br/>[http://b.test/ B]}}\nBODY"
    nodes = wikitext.parse(text)
    assert isinstance(nodes[0], Template)
    assert "BODY" in wikitext.flatten_text(nodes[1:])


def test_a_lone_closing_bracket_is_kept_as_text():
    """`]` outside a link is ordinary text, not a token to be dropped."""
    assert "]" in wikitext.flatten_text(wikitext.parse("a ] b"))
