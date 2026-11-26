"""Rotated text laid out upright: each direction of reading is a page of its own."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from reflow.extract.pages import PageContent
from reflow.model import HEADING_ROLES, BBox, Node, Role, make_id


# Turning a box of rotated text to upright: the text's baseline direction (Line.turn) gives the
# frame the lines read in. Turned lines are laid out as a page of their own (see rotated_nodes).
def _turn_box(box: BBox, turn: int, width: float, height: float) -> BBox:
    """Box of the page turned so that the text reads left to right: upward text (turn 1) turns
    the page clockwise, downward text (turn -1) counter-clockwise, upside-down text (turn 2)
    half a turn."""
    x0, top, x1, bottom = box
    if turn == 1:
        return (height - bottom, x0, height - top, x1)
    if turn == -1:
        return (top, width - x1, bottom, width - x0)
    return (width - x1, height - bottom, width - x0, height - top)


def _unturn_box(box: BBox, turn: int, width: float, height: float) -> BBox:
    """The inverse of _turn_box: a box in the turned frame (X, Y), back on the page (x, y)."""
    x0, y0, x1, y1 = box
    corners = []
    for big_x, big_y in ((x0, y0), (x1, y1)):
        if turn == 1:  # X = height - y, Y = x
            corners.append((big_y, height - big_x))
        elif turn == -1:  # X = y, Y = width - x
            corners.append((width - big_y, big_x))
        else:  # X = width - x, Y = height - y
            corners.append((width - big_x, height - big_y))
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _turned_frame(turn: int, width: float, height: float) -> tuple[float, float]:
    """(width, height) of the frame a turn reads in: a quarter turn swaps the sides."""
    return (height, width) if turn in (1, -1) else (width, height)


def rotated_nodes(page: PageContent, artifacts: set[int],
                  analyze: Callable[[list[PageContent]], list[Node]]) -> list[Node]:
    """Nodes of the page's rotated text. Each direction of reading is laid out as a page of its
    own, turned upright, so a sideways table reads row by row in its own order; the nodes come
    back with boxes on the page. `analyze` lays out a page of turned lines. Sideways text is never a heading: on a pseudo-page of a few
    lines a chart's axis label ("std") would pass for one, and the outline is the page's."""
    nodes: list[Node] = []
    for turn in (1, -1, 2):
        # A rotated line with no direction of its own reads upward, as the most common turn.
        group = [l for l in page.lines if l.rotated and (l.turn or 1) == turn
                 and id(l) not in artifacts]
        if not group:
            continue
        width, height = _turned_frame(turn, page.width, page.height)
        upright = [replace(l, bbox=_turn_box(l.bbox, turn, page.width, page.height),
                           rotated=False, turn=0) for l in group]
        pseudo = PageContent(page.number, width, height, upright, images=[], drawings=[])
        for node in analyze([pseudo]):
            if node.bbox is not None:
                node.bbox = _unturn_box(node.bbox, turn, page.width, page.height)
                node.id = make_id(node.page, node.bbox, node.text)
            if node.role in HEADING_ROLES:
                node.role = Role.P
            nodes.append(node)
    return nodes


def place_rotated(nodes: list[Node], rotated: list[Node]) -> list[Node]:
    """Place the rotated text's nodes in the page's reading order: before the first node that
    starts below them, else after the page's last node."""
    if not rotated:
        return nodes
    top = min((n.bbox[1] for n in rotated if n.bbox), default=0.0)
    at = next((i for i, n in enumerate(nodes) if n.bbox and n.bbox[1] > top), len(nodes))
    return nodes[:at] + rotated + nodes[at:]
