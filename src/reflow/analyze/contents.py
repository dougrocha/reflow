"""A printed table of contents as one list, nested by its printed sections.

A contents page mixes what the line reader finds in four shapes: entries (a title, a dot leader
and a page label), the headings that name its sections ("FINANCIAL SECTION"), group labels that
carry no page ("Basic Financial Statements:") and entries that wrap onto a second line. Left
alone they publish as headings, paragraphs, bullet lists and a few paragraphs that still hold
their leaders. `fold_contents` takes a page that holds at least MIN_ENTRIES entries, and that is
more than section headings over lists of entries (which read well as they are), and sets the
run from its first entry to its last, with the section headings and bullets directly above it, as
one list: every entry keeps its words and its page label (the leader is not a word), every label
is an item of its own with the entries under it, and a page's own column label ("Page", set
over the page numbers) stays a paragraph.

The nesting follows the page: an item indented past the one above it is its child. A section
heading owns the items that follow it at its own indent, as the sections of a contents page are
not indented past their entries, and stops at the next section heading.
"""

from __future__ import annotations

from reflow.analyze.headings import demote
from reflow.leaders import parse_contents_entry
from reflow.model import Node, Role, SUBHEADING_ROLES

MIN_ENTRIES = 4         # entries a page needs to be a contents page
MAX_LABEL_WORDS = 20    # a longer line is prose, not a title or a label: the page is not folded
LABEL_WORDS = 8         # a group label above the first entry is this short
INDENT = 3.0            # points: an item set this far right of the one above is its child
FOLDABLE = SUBHEADING_ROLES | {Role.P, Role.LI}


def _entry(node: Node) -> tuple[str, str] | None:
    """(title, page label) of a node that is an entry: one set as such by the layout, or one
    whose lines wrapped (a paragraph or a bullet that still ends in a leader and a label)."""
    if node.role not in FOLDABLE or not node.text:
        return None
    if node.attrs.get("contents") and node.attrs.get("page_ref"):
        return node.text, str(node.attrs["page_ref"])
    return parse_contents_entry(node.text)


def _x(node: Node) -> float:
    return float(node.attrs.get("x", node.bbox[0] if node.bbox else 0.0))


def _column_label(node: Node, left: float, right: float, size: float) -> bool:
    """A word or two set over the page numbers: it starts in the right half of the entries' width
    and ends at their right edge ("Page")."""
    return (node.role in SUBHEADING_ROLES | {Role.P} and bool(node.text) and len(node.text.split()) <= 2
            and node.bbox is not None and node.bbox[0] > (left + right) / 2
            and node.bbox[2] >= right - 3 * size)


def _leading_label(node: Node, below: Node) -> bool:
    """A group label just above the entries ("Financial Statements:"): a short paragraph, no
    sentence, set left of the item under it, which it names."""
    text = (node.text or "").strip()
    return (node.role == Role.P and _entry(node) is None and 0 < len(text.split()) <= LABEL_WORDS
            and not text.endswith(".") and _x(node) + INDENT < _x(below))


def _style(node: Node) -> tuple[float | None, bool]:
    return node.attrs.get("font_size"), node.role in SUBHEADING_ROLES


def fold_contents(nodes: list[Node]) -> list[Node]:
    """`nodes` (one page's, in reading order) with the page's printed contents folded into one
    list, or `nodes` itself when the page is no contents page."""
    entries = [i for i, n in enumerate(nodes) if _entry(n)]
    if len(entries) < MIN_ENTRIES:
        return nodes
    first, last = entries[0], entries[-1]
    run = nodes[first:last + 1]
    if (any(n.role not in FOLDABLE or n.attrs.get("index_letter") for n in run)
            or any(_entry(n) is None and len(n.text.split()) > MAX_LABEL_WORDS for n in run)):
        return nodes
    boxes = [n.bbox for n in nodes if _entry(n) and n.bbox]
    if not boxes:
        return nodes
    left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
    size = float(next((n.attrs["size"] for n in nodes if _entry(n) and "size" in n.attrs), 10.0))

    # The section headings above the first entry: those set like a heading inside the run, and
    # the bullets that are labels of it. The column label above them is a paragraph.
    inside = {_style(n) for n in run if n.role in SUBHEADING_ROLES}
    start = first
    while start > 0:
        above = nodes[start - 1]
        if above.role in SUBHEADING_ROLES and _style(above) in inside and not _column_label(
                above, left, right, size):
            start -= 1
        elif above.role == Role.LI and not above.attrs.get("index_letter"):
            start -= 1
        elif _leading_label(above, nodes[start]) and not _column_label(above, left, right, size):
            start -= 1
        else:
            break
    # A page that is already section headings over lists of entries reads well as it is.
    if all(n.role in SUBHEADING_ROLES or (n.role == Role.LI and n.attrs.get("contents")
                                  and n.attrs.get("page_ref")) for n in nodes[start:last + 1]):
        return nodes
    for n in nodes[:start]:
        if _column_label(n, left, right, size) and n.role in SUBHEADING_ROLES:
            demote(n, "the column label of a contents page: it names the page numbers")

    items: list[Node] = []
    sections: set[int] = set()  # id() of the items that were headings: the printed sections
    for n in nodes[start:last + 1]:
        entry = _entry(n)
        section = n.role in SUBHEADING_ROLES and entry is None
        attrs = {k: v for k, v in n.attrs.items() if k not in ("font_size", "style_level", "label_caps",
                                                                 "set_apart", "index")}
        attrs.update(contents=True, x=_x(n))
        if section:
            sections.add(id(n))
        if entry:
            n.set_text(entry[0])
            attrs["page_ref"] = entry[1]
        attrs.setdefault("size", size)
        attrs["why"] = [*attrs.get("why", []),
                        "contents entry: title and page label, the leader dropped" if entry
                        else "contents label: names the entries under it"]
        n.role, n.attrs = Role.LI, attrs
        items.append(n)
    # The headings of the run are gone from the outline: nothing above them reads their level.
    head = Node(Role.LIST, page=items[0].page, bbox=items[0].bbox,
                confidence=min(n.confidence for n in items),
                attrs={"ordered": False, "contents": True})
    head.children = _nest(items, sections)
    return [*nodes[:start], head, *nodes[last + 1:]]


def _nest(items: list[Node], sections: set[int]) -> list[Node]:
    """The items as the tree the page indents them into: each under the nearest earlier item set
    left of it, and under the section heading before it when set at the same indent."""
    top: list[Node] = []
    stack: list[Node] = []
    for item in items:
        x = _x(item)
        while stack:
            above = stack[-1]
            if x > _x(above) + INDENT or (
                    id(above) in sections and id(item) not in sections
                    and x >= _x(above) - INDENT):
                break
            stack.pop()
        if stack:
            parent = stack[-1]
            lists = [c for c in parent.children if c.role == Role.LIST]
            if lists:
                lists[-1].children.append(item)
            else:
                parent.children.append(Node(Role.LIST, page=item.page, bbox=item.bbox,
                                            confidence=parent.confidence, children=[item],
                                            attrs={"ordered": False, "contents": True}))
        else:
            top.append(item)
        stack.append(item)
    return top
