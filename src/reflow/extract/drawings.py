"""A page's vector paths: the drawings (rules and boxes), the filled shapes, and the marks of
a chart."""

from __future__ import annotations

import ctypes
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow.model import BBox
from reflow.pdfobjects import displayed, fill_colour, iter_placed, to_top_left

# Drawing operations closer than this many points belong to the same figure. Only shapes that
# touch (or nearly do) are one drawing: a side tab 4 pt from a banner is a separate shape, not
# part of its box (directory pages put the county tab next to the category banners).
MERGE_GAP = 1.0


# A path no thicker than this (points) in one direction is a rule, not a shape.
RULE_THICKNESS = 1.5
# A drawing with this many rules (table grid, boxed table) is a ruled table: its text is read
# row by row, never split into columns.
MIN_RULED_RULES = 4
# Pages with more paths than this are clustered from the first ones only, to bound the cost.
MAX_PATHS = 5000


@dataclass
class Drawing:
    """A cluster of vector drawing operations (chart, diagram, table rules)."""
    bbox: BBox
    paths: int
    rules: int  # paths that are thin horizontal or vertical strokes (table/underline rules)
    fills: int = 0  # paths with a fill
    strokes: int = 0  # paths drawn with a stroke (outlines, rules, arrows, axes)
    outlines: int = 0  # stroked paths that are not rules: box outlines, arrows, curves
    # Chart evidence (see chart_marks): (kind, count) pairs such as ("bars", 6). Plain data.
    marks: tuple[tuple[str, int], ...] = ()

    @property
    def only_rules(self) -> bool:
        return self.rules == self.paths

    @property
    def fill_only(self) -> bool:
        """Solid shapes with no outlines: banners, backdrops, bars. Rules (thin lines, such
        as the underlines of directory headings) don't count as outlines; diagrams and charts
        have outlines, arrows and curves."""
        return self.outlines == 0 and self.fills > 0


class Fill(NamedTuple):
    """A path filled with a colour, as painted. Plain data, so it pickles. A later `order` is
    drawn on top, so where two fills overlap the reader sees the one with the higher order."""
    bbox: BBox  # top-left, crop-box coordinates, like every box
    rgb: tuple[int, int, int]
    shape: Literal["box", "mark"]  # "box": at most FILL_BOX_SEGMENTS segments; "mark": a drawn symbol
    order: int  # the path's place in the page's paint order
    # The outline of a straight-edged fill (a triangle, say), in order; () for a curved one,
    # whose box stands for it.
    points: tuple[tuple[float, float], ...] = ()

    def holds(self, x: float, y: float) -> bool:
        """Whether the fill paints the point: inside its box and, when it has an outline,
        inside that (even-odd)."""
        x0, y0, x1, y1 = self.bbox
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return False
        inside = False
        for (ax, ay), (bx, by) in zip(self.points, self.points[1:] + self.points[:1]):
            if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                inside = not inside
        return inside or not self.points


# The fills a page keeps (see page_drawings_and_fills): visible (alpha above 0), not white (some
# channel below FILL_WHITE), FILL_MIN_WIDTH to FILL_MAX_WIDTH points wide, at most MAX_FILLS.
FILL_WHITE = 250


FILL_MIN_WIDTH = 4.0


FILL_MAX_WIDTH = 300.0


MAX_FILLS = 400


FILL_BOX_SEGMENTS = 5  # a rectangle, or a dot of four curves: move and four segments


class SubPath(NamedTuple):
    """One subpath of a path object, in top-left page points. Plain data, so it pickles."""
    kind: str  # line, rect, polyline, curve or point (see _kind)
    box: BBox
    fill: bool
    nline: int
    ncurve: int
    vertices: tuple[tuple[float, float], ...] = ()  # filled rects, polylines, curves: grid points
    points: tuple[tuple[float, float], ...] = ()  # a filled straight-edged subpath's points, in order


# A cluster needs this many paths before it is checked for chart marks: the smallest mark
# (four bars, four dots) takes four.
MARK_MIN_PATHS = 4


MARK_GRID = 0.5  # points: sizes and vertices closer than this are one value


TICK_MAX = 12.0  # a straight stroke this short (points) is an axis tick


DOT_MAX = 15.0  # a curve this small, and nearly square, is a dot


MAX_BARS_PER_ROW = 3  # one category's series side by side (grouped bars); more is a table row


TILE_GAP = 1.0  # points: cells this close end to end tile a row


def _grid(v: float) -> float:
    return round(v / MARK_GRID) * MARK_GRID


def _kind(pts: list[tuple[float, float]], nline: int, ncurve: int) -> str:
    """Classify one subpath by its segments (pts are its points, the first being its move)."""
    if ncurve:
        return "curve"
    grid = [(_grid(x), _grid(y)) for x, y in pts]
    if len(set(grid)) <= 1:
        return "point"
    if nline == 1 and len(set(grid)) == 2:
        return "line"
    xs = {p[0] for p in grid}
    ys = {p[1] for p in grid}
    if nline >= 3 and len(xs) <= 2 and len(ys) <= 2:
        return "rect"
    if nline == 1 and len(set(grid)) <= 2:
        return "line"
    return "polyline"


def _subpaths(obj: pdfium.PdfObject, matrix: pdfium.PdfMatrix,
              to_top: Callable[[float, float], tuple[float, float]]) -> list[SubPath]:
    """The subpaths of one path object, with its points mapped to the page by `matrix`."""
    fill_mode, stroke = ctypes.c_int(), ctypes.c_int()
    pdfium_c.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(fill_mode), ctypes.byref(stroke))
    fill = fill_mode.value != 0
    groups: list[list[tuple[int, float, float]]] = []
    for i in range(pdfium_c.FPDFPath_CountSegments(obj.raw)):
        seg = pdfium_c.FPDFPath_GetPathSegment(obj.raw, i)
        x, y = ctypes.c_float(), ctypes.c_float()
        pdfium_c.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
        typ = pdfium_c.FPDFPathSegment_GetType(seg)
        if typ == pdfium_c.FPDF_SEGMENT_MOVETO or not groups:
            groups.append([])
        groups[-1].append((typ, *to_top(*matrix.on_point(x.value, y.value))))
    subs = []
    for g in groups:
        nline = sum(1 for t, _, _ in g if t == pdfium_c.FPDF_SEGMENT_LINETO)
        ncurve = sum(1 for t, _, _ in g if t == pdfium_c.FPDF_SEGMENT_BEZIERTO)
        pts = [(x, y) for _, x, y in g]
        kind = _kind(pts, nline, ncurve)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        vertices = ()
        if fill and kind in ("curve", "polyline", "rect"):
            vertices = tuple({(_grid(x), _grid(y)) for x, y in pts})
        points = tuple(pts) if fill and not ncurve else ()
        subs.append(SubPath(kind, (min(xs), min(ys), max(xs), max(ys)), fill, nline, ncurve, vertices,
                            points))
    return subs


def _bars(rects: Sequence[SubPath], same: int, vary: int) -> int:
    """Filled rectangles sharing one size (box index `same`: 3 = height, 2 = width), at least
    three distinct lengths along the other, and in rows of at most MAX_BARS_PER_ROW along the
    stacking axis (a row is one category, or one category's series side by side). Table cells
    are not bars: cells tile a row (they touch end to end), and a table row holds more cells than
    a chart has series. A zebra band has one length; thin rules are no bars."""
    groups: dict[float, list[SubPath]] = {}
    for s in rects:
        size = s.box[same] - s.box[same - 2]
        if size > RULE_THICKNESS:
            groups.setdefault(_grid(size), []).append(s)
    if not groups:
        return 0
    members = max(groups.values(), key=len)
    lengths = {_grid(s.box[vary] - s.box[vary - 2]) for s in members}
    rows: dict[float, list[SubPath]] = {}
    for s in members:
        rows.setdefault(_grid(s.box[same - 2]), []).append(s)
    if len(members) < 4 or len(lengths) < 3 or len(members) > MAX_BARS_PER_ROW * len(rows):
        return 0
    for row in rows.values():
        row.sort(key=lambda s: s.box[vary - 2])
        if any(b.box[vary - 2] - a.box[vary] <= TILE_GAP for a, b in zip(row, row[1:])):
            return 0
    return len(members)


def chart_marks(subs: Sequence[SubPath]) -> tuple[tuple[str, int], ...]:
    """The chart marks in one drawing cluster, as (kind, count) pairs (EVIDENCE.md section 3):
    bars: >= 4 filled rectangles sharing a height or width, with >= 3 lengths along the other;
    dots: >= 4 small, nearly square curves of one size;
    wedges: >= 3 filled subpaths sharing a vertex, >= 2 of them curves (a pie's wedges share
    its centre);
    ticks: >= 10 straight strokes of at most TICK_MAX points.
    A banner or backdrop has none of these: its rectangles are few, and its lines are words."""
    marks: list[tuple[str, int]] = []
    rects = [s for s in subs if s.kind == "rect" and s.fill]
    bars = max(_bars(rects, 3, 2), _bars(rects, 2, 3))
    if bars:
        marks.append(("bars", bars))
    dots = Counter((_grid(s.box[2] - s.box[0]), _grid(s.box[3] - s.box[1])) for s in subs
                   if s.kind == "curve" and abs((s.box[2] - s.box[0]) - (s.box[3] - s.box[1])) <= 1.0
                   and s.box[2] - s.box[0] <= DOT_MAX)
    if dots and dots.most_common(1)[0][1] >= 4:
        marks.append(("dots", dots.most_common(1)[0][1]))
    sharing: dict[tuple[float, float], list[SubPath]] = {}
    for s in subs:
        for p in s.vertices:
            sharing.setdefault(p, []).append(s)
    wedges = max((len(m) for m in sharing.values()
                  if len(m) >= 3 and sum(1 for s in m if s.kind == "curve") >= 2), default=0)
    if wedges:
        marks.append(("wedges", wedges))
    ticks = sum(1 for s in subs if s.kind == "line"
                and max(s.box[2] - s.box[0], s.box[3] - s.box[1]) <= TICK_MAX)
    if ticks >= 10:
        marks.append(("ticks", ticks))
    return tuple(marks)


def _fills(subs: Sequence[SubPath], rgb: tuple[int, int, int], order: int) -> list[Fill]:
    """The areas one filled path paints, one per subpath, so a path of several rectangles gives
    each its own box. A subpath inside another is a hole (an outline ring, the counter of a
    letter): the path paints only the band between them, which no box describes, so it gives
    none (a star's black outline ring would otherwise hide the star it outlines)."""
    areas = [s for s in subs if s.kind not in ("point", "line")]
    if any(a is not b and b.box[0] <= a.box[0] and b.box[1] <= a.box[1]
           and a.box[2] <= b.box[2] and a.box[3] <= b.box[3] for a in areas for b in areas):
        return []
    return [Fill(tuple(round(v, 2) for v in s.box), rgb,
                 "box" if 1 + s.nline + s.ncurve <= FILL_BOX_SEGMENTS else "mark", order,
                 tuple((round(x, 2), round(y, 2)) for x, y in s.points))
            for s in areas if FILL_MIN_WIDTH <= s.box[2] - s.box[0] <= FILL_MAX_WIDTH]


# A drawn square that may be a check box: this wide, at most SQUARE_SKEW off square.
SQUARE_MIN = 5.0
SQUARE_MAX = 17.0
SQUARE_SKEW = 1.5
# A check box some forms draw a little wider than tall ("▭"): up to this much wider than tall.
BOX_RATIO = 1.5
# A radio button a form draws as an ellipse: this many times wider than tall at most.
OVAL_RATIO = 2.2
# A curve ending this near (points) where it began is a closed outline.
OVAL_CLOSE = 0.5


def is_square(box: BBox, obj: pdfium.PdfObject, fill_mode: int) -> bool:
    """Whether a path's box is a small box that is empty: SQUARE_MIN to SQUARE_MAX points a
    side, at most SQUARE_SKEW off square (or BOX_RATIO times wider than tall), and either not
    filled or filled in a light colour. The caller checks that the path is one rectangle. A
    check box, or a bullet."""
    w, h = box[2] - box[0], box[3] - box[1]
    if not (SQUARE_MIN <= w <= SQUARE_MAX and SQUARE_MIN <= h <= SQUARE_MAX
            and (abs(w - h) <= SQUARE_SKEW or 1 < w / h <= BOX_RATIO)):
        return False
    return _light(obj, fill_mode)


def is_oval(box: BBox, obj: pdfium.PdfObject, fill_mode: int) -> bool:
    """Whether a path's box is a small empty ellipse or circle: a radio button drawn as a curve.
    The caller checks that the path is one curve, and that it is closed."""
    w, h = box[2] - box[0], box[3] - box[1]
    if not (SQUARE_MIN <= w <= SQUARE_MAX and SQUARE_MIN <= h <= SQUARE_MAX
            and 1 - SQUARE_SKEW / h <= w / h <= OVAL_RATIO):
        return False
    return _light(obj, fill_mode)


def _closed_curve(obj: pdfium.PdfObject) -> bool:
    """Whether a path's last point is where it began: an ellipse's outline closes on itself, and
    a C-shaped stroke beside some text (a bracket, a swoosh) does not."""
    n = pdfium_c.FPDFPath_CountSegments(obj.raw)
    if n < 2:
        return False
    ends = []
    for i in (0, n - 1):
        x, y = ctypes.c_float(), ctypes.c_float()
        pdfium_c.FPDFPathSegment_GetPoint(pdfium_c.FPDFPath_GetPathSegment(obj.raw, i),
                                          ctypes.byref(x), ctypes.byref(y))
        ends.append((x.value, y.value))
    return abs(ends[0][0] - ends[1][0]) <= OVAL_CLOSE and abs(ends[0][1] - ends[1][1]) <= OVAL_CLOSE


def _light(obj: pdfium.PdfObject, fill_mode: int) -> bool:
    if fill_mode:
        colour = fill_colour(obj.raw)
        if colour and colour[3] and min(colour[:3]) < FILL_WHITE:
            return False
    return True


def page_drawings_and_fills(page: pdfium.PdfPage,
                            squares: list[BBox] | None = None,
                            ovals: list[BBox] | None = None
                            ) -> tuple[list[Drawing], list[Fill]]:
    """Cluster the page's vector drawing operations, and keep its coloured fills in paint order.

    Boxes within MERGE_GAP points of each other are merged. Pages with more than
    MAX_PATHS paths are clustered from the first MAX_PATHS only, to bound the cost. With a
    `squares` list, the boxes of the page's drawn squares (see is_square) are added to it, and with
    an `ovals` list those of its drawn ellipses (see is_oval), which
    the clusters hide: a form's check boxes sit inside the one cluster that its rules make.
    """
    # [x0, top, x1, bottom, paths, rules, fills, strokes, outlines, members]: members are the
    # (path object, matrix to the page) pairs whose subpaths give the cluster's chart marks.
    clusters: list[list] = []
    fills: list[Fill] = []
    left, bottom_edge, right, top_edge = page.get_cropbox()
    rotation = page.get_rotation()

    def to_top(x: float, y: float) -> tuple[float, float]:
        return displayed((x - left, top_edge - y, x - left, top_edge - y),
                         right - left, top_edge - bottom_edge, rotation)[:2]

    for k, (obj, box, matrix) in enumerate(iter_placed(page, {pdfium_c.FPDF_PAGEOBJ_PATH})):
        if k >= MAX_PATHS:
            break
        x0, top, x1, bottom = to_top_left(page, box)
        fill_mode, stroke = ctypes.c_int(), ctypes.c_int()
        pdfium_c.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(fill_mode), ctypes.byref(stroke))
        is_stroke = stroke.value != 0
        if (squares is not None and is_stroke and is_square((x0, top, x1, bottom), obj, fill_mode.value)
                and [s.kind for s in _subpaths(obj, matrix, to_top)] == ["rect"]):
            squares.append((round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)))
        elif (ovals is not None and is_stroke and is_oval((x0, top, x1, bottom), obj, fill_mode.value)
                and [s.kind for s in _subpaths(obj, matrix, to_top)] == ["curve"]
                and _closed_curve(obj)):
            ovals.append((round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)))
        # A single straight segment (move + line) is a rule however wide its stroke: the box
        # includes the stroke width, so a 5 pt separator line looks like a 10 pt tall shape.
        straight = is_stroke and pdfium_c.FPDFPath_CountSegments(obj.raw) <= 2
        rule = straight or min(x1 - x0, bottom - top) <= RULE_THICKNESS
        if fill_mode.value and len(fills) < MAX_FILLS:
            colour = fill_colour(obj.raw)
            rgb = colour[:3] if colour else (0, 0, 0)
            if colour and colour[3] and min(rgb) < FILL_WHITE:
                fills += _fills(_subpaths(obj, matrix, to_top), rgb, k)[:MAX_FILLS - len(fills)]
        clusters.append([x0, top, x1, bottom, 1, int(rule), int(fill_mode.value != 0),
                         int(is_stroke), int(is_stroke and not rule), [(obj, matrix)]])

    merged = True
    while merged:
        merged = False
        out: list[list[float]] = []
        for c in clusters:
            for o in out:
                if (c[0] <= o[2] + MERGE_GAP and o[0] <= c[2] + MERGE_GAP
                        and c[1] <= o[3] + MERGE_GAP and o[1] <= c[3] + MERGE_GAP):
                    o[0], o[1] = min(o[0], c[0]), min(o[1], c[1])
                    o[2], o[3] = max(o[2], c[2]), max(o[3], c[3])
                    o[4] += c[4]
                    o[5] += c[5]
                    o[6] += c[6]
                    o[7] += c[7]
                    o[8] += c[8]
                    o[9].extend(c[9])
                    merged = True
                    break
            else:
                out.append(c)
        clusters = out
    drawings = []
    for c in clusters:
        marks: tuple[tuple[str, int], ...] = ()
        if c[4] >= MARK_MIN_PATHS and c[5] < c[4]:  # enough paths, and not only rules
            marks = chart_marks([s for obj, matrix in c[9] for s in _subpaths(obj, matrix, to_top)])
        drawings.append(Drawing((round(c[0], 2), round(c[1], 2), round(c[2], 2), round(c[3], 2)),
                                int(c[4]), int(c[5]), int(c[6]), int(c[7]), int(c[8]), marks=marks))
    return drawings, fills
