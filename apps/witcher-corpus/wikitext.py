"""A small wikitext parser: source → AST → markdown.

Wikitext nests — templates inside link labels inside templates — so it cannot
be handled by substitution passes: every regex that balances one construct
breaks on another nested inside it. This is a recursive-descent parser, meaning
one function per construct, each consuming from a shared cursor and calling the
others for whatever nests inside. The call stack does the bookkeeping that
depth counters approximate.

The renderer is deliberately separate from the parser: the parser is faithful
to the source, and all the judgement about what is noise (citations, galleries,
game-mechanics templates) lives in `render`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# --- AST -------------------------------------------------------------------


@dataclass
class Text:
    value: str


@dataclass
class Arg:
    """A template argument: `name=value`, or positional when name is None."""

    name: str | None
    value: list["Node"]


@dataclass
class Template:
    name: str
    args: list[Arg] = field(default_factory=list)


@dataclass
class Link:
    """An internal link. `label` is None for [[Target]]."""

    target: str
    label: list["Node"] | None = None


@dataclass
class ExtLink:
    url: str
    label: list["Node"] = field(default_factory=list)


@dataclass
class Tag:
    name: str
    children: list["Node"] = field(default_factory=list)


@dataclass
class Format:
    """Bold (`'''`) or italic (`''`)."""

    kind: str
    children: list["Node"] = field(default_factory=list)


@dataclass
class Heading:
    level: int
    children: list["Node"] = field(default_factory=list)


@dataclass
class ListItem:
    markers: str
    children: list["Node"] = field(default_factory=list)


@dataclass
class Table:
    pass


@dataclass
class Break:
    pass


Node = (
    Text | Template | Link | ExtLink | Tag | Format | Heading | ListItem | Table | Break
)

# Where a plain text run has to stop and the parser look at the source again.
MARKER = re.compile(r"<!--|\{\{|\}\}|\[\[|\]\]|\{\||\|\}|\[|<|'''''|'''|''|\n|\||=")

# Tags whose entire contents are dropped: citations, image galleries and the
# tab widgets Fandom wraps around alternate artwork.
DROP_TAGS = {"ref", "gallery", "tabber", "imagemap", "references", "poem", "score"}
# Only tags that never have a body. `ref` must not be here: it usually wraps
# its citation, and treating it as empty would leak that citation into the
# prose as if it were a sentence.
SELF_CLOSING = {"br", "hr"}

# Returned when the parser consumed input that produces no node, so callers can
# tell "handled, emit nothing" apart from "not mine, try something else".
HANDLED = object()


class Parser:
    """Recursive descent over wikitext.

    Every `parse_*` method assumes the cursor sits on its opening delimiter and
    leaves it just past the closing one. `stop` carries the delimiters that the
    *enclosing* construct cares about, so a nested parse knows to hand control
    back rather than run off the end.
    """

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    # -- helpers

    def eof(self) -> bool:
        return self.pos >= len(self.text)

    def peek(self, token: str) -> bool:
        return self.text.startswith(token, self.pos)

    def at_line_start(self) -> bool:
        return self.pos == 0 or self.text[self.pos - 1] == "\n"

    # -- entry point

    def parse(self) -> list[Node]:
        return self.parse_nodes(stop=())

    def parse_nodes(self, stop: tuple[str, ...]) -> list[Node]:
        nodes: list[Node] = []
        while not self.eof():
            if any(self.peek(s) for s in stop):
                break
            node = self.parse_one(stop)
            if node is not None:
                nodes.append(node)
        return nodes

    def parse_one(self, stop: tuple[str, ...]) -> Node | None:
        if self.peek("<!--"):
            end = self.text.find("-->", self.pos)
            self.pos = len(self.text) if end < 0 else end + 3
            return None
        if self.peek("{{"):
            return self.parse_template()
        if self.peek("[["):
            return self.parse_link()
        if self.peek("{|"):
            return self.parse_table()
        if self.peek("<"):
            node = self.try_parse_tag()
            if node is HANDLED:
                return None
            if node is not None:
                return node
        if self.peek("'''''") or self.peek("'''") or self.peek("''"):
            return self.parse_format()
        if self.peek("[") and re.match(r"\[(?:https?:)?//", self.text[self.pos :]):
            return self.parse_extlink()
        if self.at_line_start():
            if self.peek("="):
                node = self.try_parse_heading()
                if node is not None:
                    return node
            if self.text[self.pos] in "*#:;":
                return self.parse_listitem()
        return self.parse_text(stop)

    # -- constructs

    def parse_text(self, stop: tuple[str, ...]) -> Node:
        start = self.pos
        # Always advance at least one character, otherwise a marker we do not
        # handle here (a stray `}}`, say) would spin forever.
        self.pos += 1
        match = MARKER.search(self.text, self.pos)
        self.pos = len(self.text) if match is None else match.start()
        return Text(self.text[start : self.pos])

    def parse_template(self) -> Node:
        self.pos += 2
        name_nodes = self.parse_nodes(stop=("|", "}}"))
        name = flatten_text(name_nodes).strip()
        args: list[Arg] = []
        while self.peek("|"):
            self.pos += 1
            args.append(self.parse_arg())
        if self.peek("}}"):
            self.pos += 2
        return Template(name, args)

    def parse_arg(self) -> Arg:
        """Parse one `|name=value` or `|value` up to the next `|` or `}}`."""
        start = self.pos
        nodes = self.parse_nodes(stop=("|", "}}", "="))
        if self.peek("="):
            name = flatten_text(nodes).strip()
            self.pos += 1
            value = self.parse_nodes(stop=("|", "}}"))
            return Arg(name, value)
        # No `=` at this level: re-read as a positional value so that any `=`
        # nested deeper (inside a link label) is kept verbatim.
        self.pos = start
        return Arg(None, self.parse_nodes(stop=("|", "}}")))

    def parse_link(self) -> Node:
        self.pos += 2
        target_nodes = self.parse_nodes(stop=("|", "]]"))
        target = flatten_text(target_nodes).strip()
        label: list[Node] | None = None
        while self.peek("|"):
            self.pos += 1
            # Later pipes are image options; the last segment is the caption.
            label = self.parse_nodes(stop=("|", "]]"))
        if self.peek("]]"):
            self.pos += 2
        return Link(target, label)

    def parse_extlink(self) -> Node:
        self.pos += 1
        match = re.match(r"\S+", self.text[self.pos :])
        url = match.group(0) if match else ""
        self.pos += len(url)
        label = self.parse_nodes(stop=("]",))
        if self.peek("]"):
            self.pos += 1
        return ExtLink(url, label)

    def parse_table(self) -> Node:
        # Tables are layout for game statistics; skip to the matching `|}`,
        # counting nested tables so an inner one does not end the outer.
        depth = 0
        while not self.eof():
            if self.peek("{|"):
                depth += 1
                self.pos += 2
            elif self.peek("|}"):
                depth -= 1
                self.pos += 2
                if depth == 0:
                    break
            else:
                self.pos += 1
        return Table()

    def try_parse_tag(self) -> Node | None:
        match = re.match(r"<(/?)([A-Za-z][\w-]*)([^>]*?)(/?)>", self.text[self.pos :])
        if match is None:
            return None
        closing, name, _attrs, self_closed = match.groups()
        name = name.lower()
        if closing:
            # A stray close tag: consume it and emit nothing. Signalling
            # HANDLED matters — falling through would let the text rule eat the
            # `<` of whatever tag comes next.
            self.pos += match.end()
            return HANDLED
        self.pos += match.end()
        if self_closed or name in SELF_CLOSING:
            return Break() if name == "br" else Tag(name)
        close = re.compile(rf"</{name}\s*>", re.I)
        found = close.search(self.text, self.pos)
        if found is None:
            # Unclosed tag: treat the rest as its content rather than failing.
            children = self.parse_nodes(stop=())
            return Tag(name, children)
        inner = Parser(self.text[self.pos : found.start()])
        children = inner.parse()
        self.pos = found.end()
        return Tag(name, children)

    def parse_format(self) -> Node:
        for token, kind in (("'''''", "bi"), ("'''", "b"), ("''", "i")):
            if self.peek(token):
                self.pos += len(token)
                # Emphasis does not survive a blank line in wikitext, and
                # unbalanced quotes are common, so bound it at the line end.
                children = self.parse_nodes(stop=(token, "\n"))
                if self.peek(token):
                    self.pos += len(token)
                return Format(kind, children)
        raise AssertionError("parse_format called off a quote")

    def try_parse_heading(self) -> Node | None:
        match = re.match(r"(={2,6})(.+?)\1\s*(?=\n|$)", self.text[self.pos :])
        if match is None:
            return None
        level = len(match.group(1))
        inner = Parser(match.group(2))
        self.pos += match.end()
        return Heading(level, inner.parse())

    def parse_listitem(self) -> Node:
        match = re.match(r"[*#:;]+", self.text[self.pos :])
        markers = match.group(0)
        self.pos += len(markers)
        children = self.parse_nodes(stop=("\n",))
        return ListItem(markers, children)


def flatten_text(nodes: list[Node]) -> str:
    """Concatenate only the literal text of a node list, for names and targets."""
    out: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            out.append(node.value)
        elif isinstance(node, Format):
            out.append(flatten_text(node.children))
    return "".join(out)


def parse(text: str) -> list[Node]:
    return Parser(text).parse()
