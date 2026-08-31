#!/usr/bin/env python3
"""Convert a Fandom XML dump into one markdown file per article.

The corpus feeds Open WebUI's retrieval, so the goal is prose a model can
quote from, not a faithful wiki rendering. This file only wires the pieces
together: `dump` streams articles, `wikitext` and `render` turn them into
markdown, `policy` decides which ones are lore, and `document` lays the
survivors out.

Usage: wikitext-to-markdown.py <dump.xml> <outdir>
"""
from __future__ import annotations

import collections
import os
import sys

import policy
from document import build, slugify
from dump import articles
from render import (
    find_categories,
    find_infobox,
    infobox_fields,
    infobox_kind,
    render,
    tidy,
)
from wikitext import parse


def prose_of(body: str) -> str:
    """The body minus its headings, which is what the stub threshold measures."""
    return "\n".join(
        line for line in body.splitlines() if line.strip() and not line.startswith("#")
    )


def convert(title: str, wikitext: str) -> tuple[str | None, str]:
    """Return (markdown, reason). Markdown is None when the policy drops it."""
    tree = parse(wikitext)
    categories = find_categories(tree)

    box = find_infobox(tree)
    kind = infobox_kind(box.name) if box is not None else ""

    # The infobox is re-emitted as facts below, and every other template
    # renders empty, so the tree can be rendered as-is. Deferred, because a
    # page the policy drops on cheaper grounds is never rendered at all.
    body: list[str] = []

    def rendered() -> str:
        if not body:
            body.append(tidy(render(tree)))
        return prose_of(body[0])

    dropped = policy.verdict(policy.Page(title, kind, categories, rendered))
    if dropped is not None:
        return None, dropped

    return (
        build(
            title=title,
            kind=kind,
            categories=categories,
            fields=policy.facts(infobox_fields(box)) if box is not None else {},
            body=body[0],
            source=policy.source_url(title),
        ),
        "kept",
    )


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    dump, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)

    tally: collections.Counter[str] = collections.Counter()
    written = failed = 0
    seen: set[str] = set()
    for article in articles(dump):
        try:
            md, reason = convert(article.title, article.wikitext)
        except Exception as exc:  # noqa: BLE001 — one bad page must not stop the run
            print(f"failed: {article.title}: {exc}", file=sys.stderr)
            failed += 1
            continue
        tally[reason] += 1
        if md is None:
            continue
        name = slugify(article.title)
        if name in seen:
            name = f"{name}-{written}"
        seen.add(name)
        with open(os.path.join(outdir, name + ".md"), "w", encoding="utf-8") as fh:
            fh.write(md)
        written += 1

    # Which rule removed how much. Without this the pipeline reports only a
    # total, and a filter that silently stops matching -- or starts matching
    # far too much -- looks exactly like a normal run.
    for reason, count in tally.most_common():
        print(f"  {count:6d}  {reason}", file=sys.stderr)
    print(f"wrote {written}, failed {failed}", file=sys.stderr)

    # A drastic drop means the dump layout changed and the filters stopped
    # matching; fail loudly rather than shipping an empty knowledge base.
    if written < 1000 or failed > written // 100:
        print("unexpected conversion outcome — check the dump layout", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
