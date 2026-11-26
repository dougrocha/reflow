"""Ruled boxes of text: a rectangle the page rules around a few blocks of text (an address box, a
"Fee / Pay to" panel, a "Key points" group) is published as one group, not as loose paragraphs.

A box is drawn one of three ways, the first two read from the page's rules:
  * a frame: one drawing of at most FRAME_PATHS stroked paths and no fill (a rectangle, or its
    four sides), FRAME_MIN wide and high;
  * a band: the only two horizontal rules of their span (within SPAN_TOL) on the page, one above
    the other, at least BAND_WIDTH wide. A span ruled three times or more is a table's rules or a
    column's separators, not a box, and a top rule with text sitting on it is an underline. On a born-digital page the rules are its vector drawings;
    on a scanned page they are the page scan's rule regions (extract.scan);
  * a fill: a tinted rectangle behind the text (a callout), which the page paints as a filled
    box with no rule round it. It is a box only when it is clearly smaller than the page, no
    other fill of the page repeats its colour and width (a table's stripes), and it holds the
    same text as any other box: no table, figure, form field or heading, MIN_LINES lines or
    more, with FILL_BLOCKS blocks, none of them a running "(continued)" banner (a
    directory tints its page banners). A rounded or irregular fill is not read as a box.

A candidate becomes a group only when it holds nothing but text: no table, figure, form field or
other drawing in it, no heading (a heading stays in the outline), and at least MIN_LINES lines with a word in them. It
is doubtful when a line crosses its edge, or when the reading order puts text from outside the box
among its own: then nothing is grouped and its first block carries a `ruled-box-doubt` fact, so
the review queue shows it.

The group is a BOX node whose bbox is the box on the page and whose children are its blocks, in
reading order. Its `name` attribute is the box's first line as printed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from reflow import geometry
from reflow.extract.drawings import Fill
from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.extract.pages import PageContent
from reflow.model import BBox, HEADING_ROLES, Node, Role, Source

# Nodes a box of text may hold; anything else in it (a table, a figure, a form field) means the
# rectangle is not a box of text.
TEXT_ROLES = {Role.P, Role.LIST, Role.LI, Role.NOTE, Role.CAPTION}
NOT_TEXT = {Role.TABLE, Role.FIGURE, Role.FIELD, Role.GROUP}

FRAME_PATHS = 4          # a rectangle drawn as one path, or as its four sides
FRAME_MIN = (36.0, 18.0)  # pt: the narrowest and lowest frame that can hold two lines
RULE_MAX = 4.0           # pt: the thickest horizontal rule of a band
RULE_MIN = 36.0          # pt: the shortest one
BAND_WIDTH = 100.0       # pt: the narrowest band: a narrower pair rules a column of figures
SPAN_TOL = 3.0           # pt: the two rules of a band start and end this close
# The page scan finds rules on an 8 pt grid (extract.scan._ink_clusters): a scanned rule's box
# is a tile or two high, and its ends are rounded to the grid.
SCAN_GRID = 8.0
BAND_MIN = 18.0          # pt: the least room between a band's rules: two lines of text
BAND_MAX = 0.35          # of the page height: a pair further apart is a header and a footer rule
PAGE_SHARE = 0.5         # of the page area: a frame this big is the page's border
FILL_PAGE_SHARE = 0.25   # of the page area: a tinted rectangle this big is a backdrop
FILL_BLOCKS = (2, 6)     # a tinted box holds a group of blocks: one is a banner or a label, more a panel
FILL_STRIPES = 3         # fills of one colour and width this many times are stripes, not callouts
MIN_LINES = 2            # a single line in a box is a label or a field, not a group
WORD = re.compile(r"[^\W\d_]{2}")  # a box of text holds words, not only figures
EDGE_SLACK = 2.0         # pt: a line or block may overhang the box this much
UNDERLINE_GAP = 3.0      # pt: a line that starts above a rule and ends this close to it, or
                         # past it, is underlined by it


@dataclass(frozen=True)
class Candidate:
    bbox: BBox
    shape: Literal["frame", "band", "fill"]
    rules: tuple[BBox, ...]   # the drawings (or scan rules) that draw it


def candidates(page: PageContent) -> list[Candidate]:
    """The page's ruled rectangles, frames first, each at most once."""
    area = page.width * page.height
    frames = [Candidate(d.bbox, "frame", (d.bbox,)) for d in page.drawings
              if 1 <= d.paths <= FRAME_PATHS and not d.fills and d.strokes == d.paths
              and (d.rules == d.paths if d.paths > 1 else d.outlines == 1)
              and d.bbox[2] - d.bbox[0] >= FRAME_MIN[0] and d.bbox[3] - d.bbox[1] >= FRAME_MIN[1]
              and geometry.area(d.bbox) < PAGE_SHARE * area]
    # Each horizontal rule with how far its box may be from the line it draws.
    tol = {b: SPAN_TOL for b in [d.bbox for d in page.drawings if d.paths == 1 and d.rules == 1]
           if b[3] - b[1] <= RULE_MAX}
    tol |= {r.bbox: SCAN_GRID for r in page.scan.regions
            if r.kind == "rule" and r.bbox[3] - r.bbox[1] <= 2 * SCAN_GRID}
    rules = sorted((b for b in tol if b[2] - b[0] >= RULE_MIN), key=lambda b: (b[1], b[0]))
    # Every horizontal rule of the page counts against a span, those inside a drawing of many
    # paths (a table's grid) too.
    spans = [b for b in [d.bbox for d in page.drawings if d.rules] if b[3] - b[1] <= RULE_MAX]
    spans += rules
    bands = []
    for i, top in enumerate(rules):
        same = [b for b in spans if abs(b[0] - top[0]) <= max(tol[top], tol.get(b, SPAN_TOL))
                and abs(b[2] - top[2]) <= max(tol[top], tol.get(b, SPAN_TOL))]
        low = next((b for b in rules[i + 1:] if b in same), None)
        if (low is None or len(set(same)) != 2 or top[2] - top[0] < BAND_WIDTH
                or not BAND_MIN <= low[1] - top[3] <= BAND_MAX * page.height
                or any(l.bbox[1] < top[1] and l.bbox[3] >= top[1] - UNDERLINE_GAP
                       and geometry.horizontal_overlap(l.bbox, top) > 0 for l in page.lines)):
            continue
        bands.append(Candidate((min(top[0], low[0]), top[1], max(top[2], low[2]), low[3]),
                               "band", (top, low)))
    return frames + bands + _fills(page, area)


def _rectangle(fill: Fill) -> bool:
    """Whether every point of the fill's outline is a corner of its box."""
    x0, y0, x1, y1 = fill.bbox
    return bool(fill.points) and all(min(abs(x - x0), abs(x - x1)) <= 0.5
                                     and min(abs(y - y0), abs(y - y1)) <= 0.5 for x, y in fill.points)


def _same(a: BBox, b: BBox) -> bool:
    return all(abs(p - q) <= EDGE_SLACK for p, q in zip(a, b))


def _fills(page: PageContent, area: float) -> list[Candidate]:
    """The page's tinted boxes: filled rectangles (Fill.shape "box") smaller than PAGE_SHARE of
    the page that no other fill repeats (stripes). The rule that draws one is the drawing of the
    fill itself (the text of a page drawn as glyph outlines is part of that drawing); any other
    drawing inside it is a picture, which `_group` refuses."""
    boxes = [f for f in page.fills if f.shape == "box" and _rectangle(f)
             and f.bbox[2] - f.bbox[0] >= FRAME_MIN[0] and f.bbox[3] - f.bbox[1] >= FRAME_MIN[1]
             and geometry.area(f.bbox) < FILL_PAGE_SHARE * area]
    shapes = Counter((f.rgb, round(f.bbox[0]), round(f.bbox[2])) for f in boxes)
    return [Candidate(f.bbox, "fill", tuple(d.bbox for d in page.drawings if _same(d.bbox, f.bbox)) or (f.bbox,))
            for f in sorted(boxes, key=lambda f: (f.bbox[1], f.bbox[0]))
            if shapes[(f.rgb, round(f.bbox[0]), round(f.bbox[2]))] < FILL_STRIPES]


def _shrunk(box: BBox, by: float = 1.0) -> BBox:
    return (box[0] + by, box[1] + by, box[2] - by, box[3] - by)


def group_boxes(nodes: list[Node], page: PageContent, artifacts: set[int]) -> list[Node]:
    """`nodes` (a page's top-level nodes in reading order) with the blocks of each ruled box of
    text wrapped in one BOX node (see the module note). A doubtful box leaves its blocks as
    they are and adds a `ruled-box-doubt` fact to the first of them."""
    found = candidates(page)
    if not found:
        return nodes
    lines = [l for l in page.lines if id(l) not in artifacts and not l.rotated]
    taken: list[BBox] = []
    for box in found:
        if any(geometry.overlap(box.bbox, t) > 0 for t in taken):
            continue
        nodes, grouped = _group(nodes, box, page, lines)
        if grouped:
            taken.append(box.bbox)
    return nodes


def _group(nodes: list[Node], box: Candidate, page: PageContent, lines: Sequence[Line]
           ) -> tuple[list[Node], bool]:
    rect, interior = box.bbox, _shrunk(box.bbox)
    flow = [i for i, n in enumerate(nodes) if n.role != Role.ARTIFACT]
    inner = [i for i in flow if nodes[i].bbox and geometry.centre_in(nodes[i].bbox, rect)]
    held = [l for l in lines if geometry.centre_in(l.bbox, rect)]
    if (not inner or len(held) < MIN_LINES or not any(WORD.search(l.text) for l in held)
            or (box.shape == "fill" and (not FILL_BLOCKS[0] <= len(inner) <= FILL_BLOCKS[1]
                                         or any(nodes[i].attrs.get("context") for i in inner)))):
        return nodes, False
    # Only text: no table, figure or control over it, no heading in it, and no drawing or ink
    # the scan found in it but its own rules (a grid, an underline to write on, a checkbox).
    if (any(n.role in NOT_TEXT and n.bbox and geometry.overlap(n.bbox, interior) > 0
            for top in nodes for n in top.walk())
            or any(geometry.overlap(w.bbox, interior) > 0 for w in page.widgets)
            or any(nodes[i].role not in TEXT_ROLES for i in inner)
            or any(b not in box.rules and geometry.overlap(b, interior) > 0
                   for b in [d.bbox for d in page.drawings] + [r.bbox for r in page.scan.regions])):
        return nodes, False
    first = nodes[inner[0]]
    doubt = None
    if any(not geometry.inside(l.bbox, rect, EDGE_SLACK) and geometry.overlap(l.bbox, interior) > 0
           for l in lines):
        doubt = "a line of text crosses the edge of the box"
    elif any(not geometry.inside(nodes[i].bbox, rect, EDGE_SLACK) for i in inner):
        doubt = "a block runs out of the box"
    elif inner != flow[flow.index(inner[0]):flow.index(inner[0]) + len(inner)]:
        doubt = "the reading order puts text from outside the box among its lines"
    if doubt:
        if box.shape == "fill":   # a tint is no rule to dispute: leave the blocks, ask no one
            return nodes, False
        first.fact("ruled-box-doubt", Source.HEURISTIC,
                   f"A ruled {box.shape} around this text was not published as a group: {doubt}",
                   bbox=geometry.rounded(rect))
        return nodes, False
    opening = min(held, key=lambda l: (l.bbox[1], l.bbox[0]))
    name = opening.text.replace(SOFT_HYPHEN, "").strip()
    group = Node(Role.BOX, page=page.number, bbox=rect,
                 attrs={"shape": box.shape, **({"name": name} if name else {}),
                        "why": ["tinted fill: it is painted behind only text" if box.shape == "fill"
                                else f"ruled {box.shape}: its rules enclose only text"]},
                 children=[nodes[i] for i in inner],
                 confidence=min(nodes[i].confidence for i in inner), source=first.source)
    at = inner[0]
    # A heading printed under the box opens what follows it, not the box: a box beside a
    # column (a mail-to panel beside the form's title) comes before that column's next heading.
    while (at > 0 and nodes[at - 1].role in HEADING_ROLES and nodes[at - 1].bbox
           and nodes[at - 1].bbox[1] >= rect[3] - EDGE_SLACK):
        at -= 1
    rest = [n for i, n in enumerate(nodes) if i not in set(inner)]
    return rest[:at] + [group] + rest[at:], True
