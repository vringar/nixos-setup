#!/usr/bin/env python3
"""Convert a Fandom XML dump into one markdown file per article.

The corpus feeds Open WebUI's retrieval, so the goal is prose a model can quote
from, not a faithful wiki rendering. Parsing and rendering live in wikitext.py
and render.py; this file is the dump reader and the curation policy.

Usage: wikitext-to-markdown.py <dump.xml> <outdir>
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET

from render import find_categories, find_infobox, infobox_fields, render, tidy
from wikitext import Template, parse

MW = "{http://www.mediawiki.org/xml/export-0.11/}"

# Page types that are game mechanics rather than lore. Items alone are a third
# of the wiki (crafting diagrams, armor, relics) and would drown retrieval in
# stat blocks. Pages with no infobox are kept: that is where the concept
# articles live (Elder Blood, Signs, historical events).
SKIP_INFOBOX = {
    "item", "quest", "gwent", "gwent card", "achievement", "needed", "tb",
    "merchant", "trophy", "mutagen", "diagram", "weapon", "armor", "potion",
    "bomb", "oil", "card", "book", "crafting",
}
SKIP_CATEGORY = re.compile(
    r"crafting diagram|gwent|thronebreaker card|quest item|relic|armor|"
    r"witcher gear|cut content|achievement|trophy|disambiguation|stub|"
    r"pages with|subpages|images?$",
    re.I,
)
# Infobox fields that are asset filenames rather than facts.
SKIP_FIELD = {"image", "coa", "flag", "geo map", "city map", "px", "width", "imagebg"}
# Below this much prose a page is a stub — a title and a sentence fragment,
# which only adds retrieval noise.
MIN_PROSE = 400


def yaml_scalar(value: str) -> str:
    """Quote a scalar so titles with colons, quotes or brackets stay valid YAML."""
    value = value.replace("\n", " ").strip()
    if re.search(r"""[:#\[\]{},&*?|<>=!%@`'"]""", value) or not value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s-]", "", s).strip().replace(" ", "-")
    return re.sub(r"-{2,}", "-", s)[:120] or "untitled"


def convert(title: str, wikitext: str) -> str | None:
    """Return the markdown for one article, or None if it should be skipped."""
    tree = parse(wikitext)

    box = find_infobox(tree)
    kind = ""
    fields: dict[str, str] = {}
    if box is not None:
        kind = box.name.strip().lower().removeprefix("infobox").strip(" _")
        if kind in SKIP_INFOBOX:
            return None
        fields = infobox_fields(box)

    categories = find_categories(tree)
    if any(SKIP_CATEGORY.search(c) for c in categories):
        return None

    # The infobox is re-emitted as facts below, and every other template
    # renders empty, so the tree can be rendered as-is.
    body = tidy(render(tree))

    prose = "\n".join(
        line for line in body.splitlines() if line.strip() and not line.startswith("#")
    )
    if len(prose) < MIN_PROSE:
        return None

    # Frontmatter is provenance, not search material: Open WebUI ingests it as
    # plain text and only the first chunk of a document carries it. Anything
    # that must be retrievable is repeated in the body — hence the title being
    # both a field and the H1, and the facts staying a body section.
    url = "https://witcher.fandom.com/wiki/" + title.replace(" ", "_")
    out = ["---", f"title: {yaml_scalar(title)}"]
    if kind:
        out.append(f"type: {yaml_scalar(kind)}")
    if categories:
        out.append("categories:")
        out += [f"  - {yaml_scalar(c)}" for c in categories[:12]]
    out += [f"source: {url}", "---", "", f"# {title}", ""]

    facts = [
        f"- **{key}:** {value}"
        for key, value in fields.items()
        if key not in SKIP_FIELD and len(value) < 400
    ]
    if facts:
        out += ["## Facts", ""] + facts + [""]
    out.append(body)
    return "\n".join(out) + "\n"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    dump, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)

    written = skipped = failed = 0
    seen: set[str] = set()
    for _, el in ET.iterparse(dump, events=("end",)):
        if el.tag != MW + "page":
            continue
        try:
            if (el.findtext(MW + "ns") or "") != "0" or el.find(MW + "redirect") is not None:
                continue
            title = el.findtext(MW + "title") or ""
            rev = el.find(MW + "revision")
            text = (rev.findtext(MW + "text") if rev is not None else "") or ""
            try:
                md = convert(title, text)
            except Exception as exc:  # noqa: BLE001 — one bad page must not stop the run
                print(f"failed: {title}: {exc}", file=sys.stderr)
                failed += 1
                continue
            if md is None:
                skipped += 1
                continue
            name = slugify(title)
            if name in seen:
                name = f"{name}-{written}"
            seen.add(name)
            with open(os.path.join(outdir, name + ".md"), "w", encoding="utf-8") as fh:
                fh.write(md)
            written += 1
        finally:
            el.clear()

    print(f"wrote {written}, skipped {skipped}, failed {failed}", file=sys.stderr)
    # A drastic drop means the dump layout changed and the filters stopped
    # matching; fail loudly rather than shipping an empty knowledge base.
    if written < 1000 or failed > written // 100:
        print("unexpected conversion outcome — check the dump layout", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
