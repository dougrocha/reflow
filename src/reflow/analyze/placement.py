"""Where nodes go among a page's blocks: tables, figures and form grids are placed at their
caption, else in reading order by position."""

from __future__ import annotations

import re

from reflow.analyze import form_grid
from reflow.analyze.figures import FigureCandidate, figure_node, label_node
from reflow.analyze.table_nodes import table_node
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData, TableMethod
from reflow.model import BBox, Node, Role, Source

TABLE_CAPTION = re.compile(r"^table\b", re.I)


# A caption this close above or below a figure (and overlapping it horizontally) is its.
CAPTION_DISTANCE = 40.0


def attach_figures(nodes: list[Node], figures: list[FigureCandidate], page: PageContent,
                    owners: dict[int, Node]) -> tuple[list[Node], list[Node]]:
    """Place each figure in reading order: a signature directly before the node of its role
    line (FigureCandidate.anchor, found in `owners`, the node of each line's block by the line's
    id), any other at its caption if it has one, else before the first block below it in the
    same column. The caption becomes the figure's child, and so does each of its labels (see
    figure_boxes), a paragraph marked figure_label, in label order. Also returns the figures
    placed at their anchor."""
    anchored: list[Node] = []
    for fig in figures:
        box = fig.bbox
        node = figure_node(fig, page.number)
        owner = owners.get(id(page.lines[fig.anchor])) if fig.anchor is not None else None
        at = next((i for i, n in enumerate(nodes) if n is owner), None)
        if at is not None:
            anchored.append(node)
        elif (cap := caption_for(box, nodes, table=False)) is not None:
            at = nodes.index(cap)
            node.children.append(nodes.pop(at))
        else:
            at = next((i for i, n in enumerate(nodes) if n.bbox and n.bbox[1] >= box[1]
                       and min(n.bbox[2], box[2]) > max(n.bbox[0], box[0])), len(nodes))
        labels = [label_node(page.lines[i], page.number) for i in fig.labels or ()]
        node.children += labels
        if any(label.source == Source.OCR for label in labels):
            node.fact("ocr-in-picture", Source.OCR,
                      f"{len(labels)} lines read inside the picture are listed under it")
        nodes.insert(at, node)
    return nodes, anchored


def attach_tables(nodes: list[Node], tables: list[TableData],
                   fields: dict[tuple[int, int, int], list[Node]] | None = None) -> list[Node]:
    """Place each table at its caption ("Table 3. ..."), else in reading order by position:
    after the last block above it in its column, else before the first block below it."""
    previous: Node | None = None  # the month grid placed last, while the tables are months
    placed: list[tuple[Node, BBox]] = []  # the tables placed so far, with their boxes
    for ti, t in enumerate(tables):
        node = table_node(t, {(i, j): f for (k, i, j), f in (fields or {}).items() if k == ti})
        if t.method == TableMethod.CALENDAR and previous is not None:
            # The months come in reading order, row by row, whatever the columns around them.
            nodes.insert(nodes.index(previous) + 1, node)
            previous = node
            continue
        cap = None if t.method == TableMethod.CALENDAR else caption_for(t.bbox, nodes, table=True)
        if cap is not None:
            at = nodes.index(cap)
            node.children.insert(0, nodes.pop(at))
        else:
            in_column = [i for i, n in enumerate(nodes) if n.bbox
                         and min(n.bbox[2], t.bbox[2]) > max(n.bbox[0], t.bbox[0])]
            above = [i for i in in_column if nodes[i].bbox[3] <= t.bbox[1] + 1]
            below = [i for i in in_column if nodes[i].bbox[1] >= t.bbox[1]]
            # A block above that reaches well left of the table spans the columns (a page
            # heading): a table at the top of a later column follows the columns left of it, so
            # it goes before the first block below it instead. Only when every block between
            # lies wholly left of the table: a wide title over a narrow table is not a column.
            span = 0.25 * (t.bbox[2] - t.bbox[0])
            if above and below and below[0] > above[-1] + 1 \
                    and nodes[above[-1]].bbox[0] < t.bbox[0] - span \
                    and all(n.bbox is None or n.bbox[2] <= t.bbox[0] + 1
                            for n in nodes[above[-1] + 1:below[0]]):
                above = []
            at = above[-1] + 1 if above else below[0] if below else len(nodes)
            # Tables side by side hang from the same block: each goes after the ones to its
            # left that are already placed there, so the columns read left to right.
            while at < len(nodes) and any(nodes[at] is p and b[2] <= t.bbox[0] + 1
                                          and b[1] < t.bbox[3] and b[3] > t.bbox[1]
                                          for p, b in placed):
                at += 1
        nodes.insert(at, node)
        placed.append((node, t.bbox))
        previous = node if t.method == TableMethod.CALENDAR else None
    return nodes


def attach_forms(nodes: list[Node], grids: list[form_grid.FormGrid]) -> list[Node]:
    """Place each form grid's nodes, together, where a table would go: after the last block above
    it in its column, else before the first block below it."""
    for grid in sorted(grids, key=lambda g: (g.bbox[1], g.bbox[0])):
        in_column = [i for i, n in enumerate(nodes) if n.bbox
                     and min(n.bbox[2], grid.bbox[2]) > max(n.bbox[0], grid.bbox[0])]
        above = [i for i in in_column if nodes[i].bbox[3] <= grid.bbox[1] + 1]
        below = [i for i in in_column if nodes[i].bbox[1] >= grid.bbox[1]]
        at = above[-1] + 1 if above else below[0] if below else len(nodes)
        nodes[at:at] = grid.nodes
    return nodes


def caption_for(fig: BBox, nodes: list[Node], table: bool) -> Node | None:
    best, best_dist = None, CAPTION_DISTANCE
    for n in nodes:
        if n.role != Role.CAPTION or not n.bbox:
            continue
        if bool(TABLE_CAPTION.match(n.text or "")) != table:
            continue
        if min(n.bbox[2], fig[2]) <= max(n.bbox[0], fig[0]):
            continue
        dist = max(n.bbox[1] - fig[3], fig[1] - n.bbox[3], 0)
        if dist < best_dist:
            best, best_dist = n, dist
    return best
