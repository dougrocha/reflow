"""Sections: nest the flat reading order under its headings, once.

Contents nesting (render.toc.toc_entries), chunking (render.chunks.opens_top_section) and "Show on
page" (the page sidecar's `sections`, render.sidecars) all read the SECTION nodes.

Shape: a SECTION has role SECTION, page None, bbox None, text None, attrs {"level": n};
its first child is the heading (heading_of), the rest are
the nodes up to the next heading of the same or a higher level, with lower-level sections
nested. Nodes before the first heading stay at the top level. Artifacts stay where they
are (they are filtered by every consumer already). Sections cross pages freely; the
renderer splits them per page (see render.chunks).

Consumers read `doc.flow()` (reflow.model.Document.flow), not `doc.root.children`.
"""

from __future__ import annotations

from reflow.model import BBox, Node, Role, make_id

HEADING_LEVELS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def nest_sections(nodes: list[Node]) -> list[Node]:
    """`nodes` (the flat reading order) regrouped under SECTION nodes. Pure: returns a new
    top-level list, the content nodes themselves are shared, not copied. H1 does not open a
    section (it is the title); it closes every open one."""
    top: list[Node] = []
    stack: list[tuple[int, Node]] = []
    for node in nodes:
        level = HEADING_LEVELS.get(node.role.value)
        if level == 1:
            stack.clear()
            top.append(node)
            continue
        if level is None:
            (stack[-1][1].children if stack else top).append(node)
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        section = Node(Role.SECTION, id=make_id(node.page, None, f"section:{node.text}"),
                       attrs={"level": level}, children=[node])
        (stack[-1][1].children if stack else top).append(section)
        stack.append((level, section))
    return top


def heading_of(section: Node) -> Node:
    """The section's heading node (its first child)."""
    if (section.role != Role.SECTION or not section.children
            or section.children[0].role.value not in HEADING_LEVELS):
        raise ValueError(f"SECTION {section.id!r} must have a heading as its first child")
    return section.children[0]


def _content(section: Node, page: int):
    """The boxes on `page` of the section's nodes: leaf blocks whole, sub-sections opened."""
    for child in section.children:
        if child.role == Role.SECTION:
            yield from _content(child, page)
        elif child.role != Role.ARTIFACT and child.page == page and child.bbox is not None:
            yield child.bbox


def section_region(section: Node, page: int) -> BBox | None:
    """The union of the section's descendants' boxes on `page` (what "Show on page"
    outlines), or None when the section has nothing on that page. Written into the page
    sidecar (render.sidecars) so the client does no arithmetic."""
    boxes = list(_content(section, page))
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def insert_by_page(children: list[Node], node: Node, page: int) -> None:
    """Put `node` before the first node (in reading order) that is on a later page than
    `page`, inside the section that holds it; at the end when there is none."""
    def place(items: list[Node]) -> bool:
        for k, item in enumerate(items):
            if item.role == Role.SECTION:
                if heading_of(item).page is not None and heading_of(item).page > page:
                    items.insert(k, node)
                    return True
                if place(item.children):
                    return True
            elif item.page is not None and item.page > page:
                items.insert(k, node)
                return True
        return False
    if not place(children):
        children.append(node)
