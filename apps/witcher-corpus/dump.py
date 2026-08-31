"""Streaming articles out of a MediaWiki XML export.

Generic: nothing here knows which wiki the dump came from. Exports are far
larger than memory — the Witcher one is 116MB of XML — so pages are parsed
incrementally and released as they go.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterator

MW = "{http://www.mediawiki.org/xml/export-0.11/}"


@dataclass(frozen=True)
class Article:
    title: str
    wikitext: str


def articles(path: str) -> Iterator[Article]:
    """Yield the main-namespace, non-redirect pages of a dump, in file order.

    Namespace 0 is article space; the rest is templates, categories, user
    pages and other machinery. Redirects hold no text of their own.
    """
    for _, el in ET.iterparse(path, events=("end",)):
        # `end` fires for every element, but only a completed <page> may be
        # cleared: clearing anything else discards the <ns>, <revision> and
        # <text> children of the page still being assembled, and every page
        # then reads as empty.
        if el.tag != MW + "page":
            continue
        try:
            if (el.findtext(MW + "ns") or "") != "0":
                continue
            if el.find(MW + "redirect") is not None:
                continue
            revision = el.find(MW + "revision")
            text = (revision.findtext(MW + "text") if revision is not None else "") or ""
            yield Article(el.findtext(MW + "title") or "", text)
        finally:
            el.clear()
