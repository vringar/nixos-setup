"""AST → markdown, plus the queries the converter runs over a parsed page.

Kept apart from the parser: the parser is faithful to the source, and every
judgement about what is noise lives here, as data rather than control flow.
"""
from __future__ import annotations

import re

from wikitext import (
    Arg,
    Break,
    ExtLink,
    Format,
    Heading,
    Link,
    ListItem,
    Node,
    Table,
    Tag,
    Template,
    Text,
    flatten_text,
)

# Templates whose payload is prose and should be kept, unwrapped.
UNWRAP = {"small", "quote", "cquote", "lang", "transl", "ill", "nihil", "'"}
# Tags whose contents are dropped wholesale (see DROP_TAGS in the parser for
# the ones already discarded during parsing).
DROP_TAGS = {"ref", "gallery", "tabber", "imagemap", "references", "score"}
EMPHASIS_TAGS = {"i": "*", "em": "*", "b": "**", "strong": "**"}
DROP_LINK_PREFIX = re.compile(r"^\s*(file|image|category|media)\s*:", re.I)


def render(nodes: list[Node]) -> str:
    return "".join(_render_node(n) for n in nodes)


def _render_node(node: Node) -> str:
    if isinstance(node, Text):
        return node.value
    if isinstance(node, Format):
        marks = {"b": "**", "i": "*", "bi": "***"}[node.kind]
        inner = render(node.children).strip()
        return f"{marks}{inner}{marks}" if inner else ""
    if isinstance(node, Link):
        if DROP_LINK_PREFIX.match(node.target):
            return ""
        return render(node.label).strip() if node.label else node.target
    if isinstance(node, ExtLink):
        return render(node.label).strip()
    if isinstance(node, Heading):
        # Markdown reserves h1 for the page title.
        level = min(node.level, 5)
        return f"\n{'#' * level} {render(node.children).strip()}\n"
    if isinstance(node, ListItem):
        indent = "  " * (len(node.markers) - 1)
        bullet = "1." if node.markers.endswith("#") else "-"
        body = render(node.children).strip()
        return f"{indent}{bullet} {body}\n" if body else ""
    if isinstance(node, Tag):
        if node.name in DROP_TAGS:
            return ""
        if node.name in EMPHASIS_TAGS:
            inner = render(node.children).strip()
            mark = EMPHASIS_TAGS[node.name]
            return f"{mark}{inner}{mark}" if inner else ""
        return render(node.children)
    if isinstance(node, Break):
        return "\n"
    if isinstance(node, Table):
        return ""
    if isinstance(node, Template):
        return _render_template(node)
    # Python has no exhaustiveness check over the Node union, so a variant
    # added to the parser without an arm here would silently render as empty
    # and delete text from the corpus. Fail instead.
    raise TypeError(f"no render arm for node type {type(node).__name__}")


def _render_template(node: Template) -> str:
    name = node.name.strip().lower()
    if name in UNWRAP:
        positional = [a for a in node.args if a.name is None]
        if positional:
            return render(positional[-1].value)
        return ""
    if name == "year":
        for arg in node.args:
            if arg.name == "year":
                return flatten_text(arg.value).strip()
        positional = [a for a in node.args if a.name is None]
        return flatten_text(positional[0].value).strip() if positional else ""
    # Everything else — infoboxes, spoiler banners, source abbreviations,
    # navigation boxes — is chrome around the prose, not prose.
    return ""


def find_infobox(nodes: list[Node]) -> Template | None:
    """First Infobox template anywhere in the tree."""
    for node in nodes:
        if isinstance(node, Template) and node.name.strip().lower().startswith("infobox"):
            return node
        for child in _children(node):
            found = find_infobox(child)
            if found is not None:
                return found
    return None


def find_categories(nodes: list[Node]) -> list[str]:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, Link) and re.match(r"^\s*category\s*:", node.target, re.I):
            out.append(node.target.split(":", 1)[1].strip())
        for child in _children(node):
            out.extend(find_categories(child))
    return out


def drop(nodes: list[Node], predicate) -> list[Node]:
    """Copy of the tree with nodes matching `predicate` removed."""
    kept: list[Node] = []
    for node in nodes:
        if predicate(node):
            continue
        if isinstance(node, (Format, Heading, ListItem, Tag)):
            node.children = drop(node.children, predicate)
        elif isinstance(node, ExtLink):
            node.label = drop(node.label, predicate)
        elif isinstance(node, Link) and node.label is not None:
            node.label = drop(node.label, predicate)
        elif isinstance(node, Template):
            node.args = [Arg(a.name, drop(a.value, predicate)) for a in node.args]
        kept.append(node)
    return kept


def _children(node: Node) -> list[list[Node]]:
    if isinstance(node, (Format, Heading, ListItem, Tag)):
        return [node.children]
    if isinstance(node, ExtLink):
        return [node.label]
    if isinstance(node, Link):
        return [node.label] if node.label else []
    if isinstance(node, Template):
        return [a.value for a in node.args]
    return []


def infobox_fields(box: Template) -> dict[str, str]:
    """Named infobox arguments, rendered to plain text."""
    fields: dict[str, str] = {}
    for arg in box.args:
        if arg.name is None:
            continue
        key = arg.name.strip().lower().replace("_", " ")
        value = tidy(render(arg.value))
        value = re.sub(r"\s*\n\s*", ", ", value).strip(" ,")
        if key and value and len(key) < 30:
            fields[key] = value
    return fields


def tidy(text: str) -> str:
    """Collapse the whitespace that dropped templates and tags leave behind."""
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
