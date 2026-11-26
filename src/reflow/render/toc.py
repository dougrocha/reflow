"""The contents: every heading with its chunk and anchor (toc_entries), nested for the shell."""

from __future__ import annotations

from collections.abc import Iterator

from reflow.analyze.sections import heading_of
from reflow.model import HEADING_LEVEL, Document, Node, Role
from reflow.render.chunks import Chunk
from reflow.render.links import anchor
from reflow.text_quality import fold


def drop_repeats(entries: list[dict]) -> list[dict]:
    """`entries` without an entry that only repeats the one before it: the same text (case and
    spacing aside) at the same or a deeper level (an entity name printed at the top of two
    pages, a name and its title set as two headings). The heading stays in the transcript. The
    children of a dropped entry nest under the one before it."""
    kept: list[dict] = []
    merged: dict[str, str] = {}  # a dropped entry's id -> the entry it repeats

    def norm(entry: dict) -> str:
        return fold(entry["text"])

    for entry in entries:
        last = kept[-1] if kept else None
        if last and entry["level"] >= last["level"] and norm(entry) == norm(last):
            merged[entry["id"]] = merged.get(last["id"], last["id"])
            continue
        kept.append(entry)
    return [{**e, "parent": merged.get(e["parent"], e["parent"])} if e["parent"] in merged else e
            for e in kept]


# A heading the bookmarks leave out is listed under the bookmark before it at most this many
# levels deeper (outline_levels).
OUTLINE_NEST_DEPTH = 2


def outline_levels(heads: list[Node]) -> dict[int, int]:
    """The contents level of each heading listed when the bookmarks are the contents, by id().
    A bookmarked heading takes its bookmark's depth, the top depth at level 2; a lone first
    bookmark that holds all the others is the document's title, and its depth does not count.
    A heading the bookmarks leave out is listed when it falls under a bookmark with no
    bookmarks under it (one per chapter), up to the next bookmark: as many levels under
    that bookmark as it is under the bookmarked heading, to OUTLINE_NEST_DEPTH, or beside the
    bookmark when it is at the bookmarked heading's level or above."""
    marks = [n.attrs.get("outline") for n in heads]
    depths = [m["level"] for m in marks if m]
    top = min(depths, default=1)
    if len(depths) > 1 and depths[0] == top and depths.count(top) == 1:
        top = min(depths[1:])  # the title's bookmark, over the chapters
    following: list[int | None] = [None] * len(heads)  # the depth of the next bookmark
    nxt = None
    for i in range(len(heads) - 1, -1, -1):
        following[i] = nxt
        if marks[i]:
            nxt = marks[i]["level"]
    levels: dict[int, int] = {}
    leaf: tuple[int, int] | None = None  # (contents level, heading level) of the open leaf bookmark
    for i, (n, mark) in enumerate(zip(heads, marks)):
        if mark:
            level = min(max(mark["level"] - top + 2, 2), 6)
            levels[id(n)] = level
            ends = following[i] is None or following[i] <= mark["level"]
            leaf = (level, HEADING_LEVEL[n.role]) if ends else None
        elif leaf is not None:
            depth = HEADING_LEVEL[n.role] - leaf[1]
            if depth <= OUTLINE_NEST_DEPTH:
                levels[id(n)] = min(leaf[0] + max(depth, 0), 6)
    return levels


def cell_headings(node: Node) -> list[Node]:
    """The headings in a table's cells: the section bars of a form table."""
    if node.role != Role.TABLE:
        return []
    return [h for tr in node.children if tr.role == Role.TR for cell in tr.children
            for h in cell.children if h.role in HEADING_LEVEL]


def toc_entries(doc: Document, chunks: list[Chunk]) -> list[dict]:
    """The contents: the headings, or, when the PDF's bookmarks name them (root attrs
    "toc" == "bookmarks", set by analyze.evidence.outline_toc), the bookmarked headings
    as the page prints them, nested as the bookmarks nest (their top level is level 2,
    under the title), with the headings under a bookmark that has none under it
    (outline_levels)."""
    chunk_of = {p: c for c in chunks for p in c.pages}
    outline = doc.root.attrs.get("toc") == "bookmarks"
    entries: list[dict] = []
    by_level: list[dict] = []  # the open entries by level, for headings outside any SECTION

    def listed(n: Node) -> bool:
        return n.role in HEADING_LEVEL and n.role != Role.H1 and n.page in chunk_of

    def heads(nodes: list[Node]) -> Iterator[Node]:
        for n in nodes:
            if n.role == Role.SECTION:
                yield heading_of(n)
                yield from heads(n.children[1:])
            else:
                yield n
                yield from cell_headings(n)

    levels = outline_levels([n for n in heads(doc.root.children) if listed(n)]) if outline else {}

    def add(n: Node, parent: str | None, by_section: bool) -> dict | None:
        if not listed(n) or (outline and id(n) not in levels):
            return None
        entry = {"level": levels[id(n)] if outline else HEADING_LEVEL[n.role],
                 "text": n.text, "id": anchor(n),
                 "page": n.page, "href": f"{chunk_of[n.page].href}#{anchor(n)}"}
        if not by_section:
            while by_level and by_level[-1]["level"] >= entry["level"]:
                by_level.pop()
            parent = by_level[-1]["id"] if by_level else None
            by_level.append(entry)
        entry["parent"] = parent
        entries.append(entry)
        return entry

    def walk(nodes: list[Node], parent: str | None) -> None:
        for n in nodes:
            if n.role == Role.SECTION:
                # The bookmarks nest by their own levels, not by the sections.
                entry = add(heading_of(n), parent, by_section=not outline)
                walk(n.children[1:], entry["id"] if entry else parent)
            else:
                add(n, None, by_section=False)
                for h in cell_headings(n):
                    add(h, None, by_section=False)

    walk(doc.root.children, None)
    return drop_repeats(entries)


def nest_toc(entries: list[dict]) -> list[dict]:
    """The contents entries as the nested list the <ol> renders: each under its `parent`
    (the entry of the enclosing SECTION's heading), when that one is in `entries`."""
    nested = {e["id"]: {**e, "children": []} for e in entries}
    root: list[dict] = []
    for e in entries:
        parent = nested.get(e["parent"]) if e["parent"] else None
        (parent["children"] if parent else root).append(nested[e["id"]])
    return root
