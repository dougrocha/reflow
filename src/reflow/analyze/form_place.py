"""Putting the planned fields and groups into the page's nodes, in reading order (see analyze.forms)."""

from __future__ import annotations

from collections.abc import Sequence

from reflow.model import HEADING_ROLES, Node, Role

PLACE_SLACK = 3.0  # a heading ends at most this far inside a control's top and is still above it (its line box is taller than its ink)


def place_flow(nodes: list[Node], items: Sequence[Node], anchored: Sequence[Node] = ()) -> list[Node]:
    """Put fields and groups into the page's nodes, in reading order. Each goes after the last
    node above it in its column (or before the first below it); controls on one row keep their
    left-to-right order. An item that would come right after an `anchored` figure (a signature,
    placed directly before the line it signs) goes before the figure instead."""
    placed: list[Node] = []
    for item in sorted(items, key=lambda n: (round(n.bbox[1] / 4), n.bbox[0])):
        same_row = [n for n in placed if abs(n.bbox[1] - item.bbox[1]) < 4 and n in nodes]
        if same_row:
            at = nodes.index(same_row[-1]) + 1
        else:
            in_column = [i for i, n in enumerate(nodes) if n.bbox
                         and min(n.bbox[2], item.bbox[2]) > max(n.bbox[0], item.bbox[0])]
            above = [i for i in in_column if nodes[i].bbox[3] <= item.bbox[1] + (
                PLACE_SLACK if nodes[i].role in HEADING_ROLES else 1)]
            below = [i for i in in_column if nodes[i].bbox[1] >= item.bbox[1]]
            # Controls already placed above (or on the row of) this one come before it.
            above += [nodes.index(n) for n in placed if n.bbox[1] <= item.bbox[1] + 4]
            # An item whose left edge is inside the text of its own rows is not at a column edge:
            # the text left of it on those rows is read first ("LMP:" before its boxes).
            left, top, _, bottom = item.bbox or (0.0, 0.0, 0.0, 0.0)
            on_rows = [(i, n.bbox) for i, n in enumerate(nodes) if n.bbox
                       and n.role not in (Role.FIELD, Role.GROUP)
                       and n.bbox[1] < bottom and n.bbox[3] > top]
            if any(box[0] < left < box[2] for _, box in on_rows):
                above += [i for i, box in on_rows if box[0] < left]
            above.sort()
            at = above[-1] + 1 if above else below[0] if below else len(nodes)
        if at and any(nodes[at - 1] is f for f in anchored):
            at -= 1
        nodes.insert(at, item)
        placed.append(item)
    return nodes
