"""Assembling one article into a markdown document.

Generic: the caller supplies the title, the facts and where the page came
from. Nothing here decides what is worth keeping — that is the policy's job.
"""
from __future__ import annotations

import re
import unicodedata

# Frontmatter is provenance, not search material: Open WebUI ingests it as
# plain text and only the first chunk of a document carries it. Anything that
# must be retrievable is repeated in the body — hence the title being both a
# field and the H1, and the facts staying a body section.
MAX_CATEGORIES = 12
MAX_FACT = 400


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


def build(
    *,
    title: str,
    kind: str,
    categories: list[str],
    fields: dict[str, str],
    body: str,
    source: str,
) -> str:
    """Render the frontmatter, the facts list and the body as one document."""
    out = ["---", f"title: {yaml_scalar(title)}"]
    if kind:
        out.append(f"type: {yaml_scalar(kind)}")
    if categories:
        out.append("categories:")
        out += [f"  - {yaml_scalar(c)}" for c in categories[:MAX_CATEGORIES]]
    out += [f"source: {source}", "---", "", f"# {title}", ""]

    facts = [
        f"- **{key}:** {value}"
        for key, value in fields.items()
        if len(value) < MAX_FACT
    ]
    if facts:
        out += ["## Facts", ""] + facts + [""]
    out.append(body)
    return "\n".join(out) + "\n"
