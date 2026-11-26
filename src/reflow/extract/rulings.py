"""Which pages can hold a ruled table, read from pdfium's path objects.

pdfplumber finds a ruled table from the edges of the page's lines, rectangles and curves, but
getting them costs a pdfminer parse of the whole page (about 0.3 s on a dense page: almost all
of the time serial extraction takes). pdfium already holds the page's paths, and walking their
segments is cheap. This module rebuilds the edge list pdfplumber would make (pdfminer's reading
of a path: `ml` is a line, a closed square `mlllh` a rectangle, the rest a curve whose edges
join its end points) and runs pdfplumber's own table finder (`TableFinder`: edge merging,
intersections, cells; `grid` finds the cells faster) over it. `worth_parsing` says whether that
finds a table.

It is a filter, not a replacement: a page it passes is parsed by pdfplumber as before, so the
tables, their text and their colours are pdfplumber's. A page it rejects has no table for
pdfplumber to find. It is written to err on the side of passing: coordinates that agree within
SLACK count as equal (pdfplumber needs them exactly equal; pdfium and pdfminer round
differently), and the control points of curves are not edges for pdfplumber but are harmless here.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow.extract.grid import RuledFinder, wider_rule_groups
from reflow.pdfobjects import iter_placed

SLACK = 0.01   # points: coordinates this close are equal (pdfium rounds to float32)

type Point = tuple[float, float]


@dataclass
class _Page:
    """The part of a pdfplumber page that TableFinder reads with the default settings."""
    edges: list[dict]
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def _edge(kind: str, x0: float, x1: float, top: float, bottom: float, orientation: str | None
          ) -> dict:
    return {"object_type": kind, "x0": x0, "x1": x1, "top": top, "bottom": bottom,
            "y0": -bottom, "y1": -top, "doctop": top, "width": x1 - x0, "height": bottom - top,
            "orientation": orientation}


def _subpaths(obj, matrix) -> Iterator[tuple[list[Point], bool]]:
    """(points, closed) of each subpath of a path object, in page space with y up. Points are
    the ends of the segments: a Bezier curve gives its end point, as pdfminer reads it."""
    x, y = ctypes.c_float(), ctypes.c_float()
    pts: list[Point] = []
    closed = False
    bezier = 0
    for i in range(pdfium_c.FPDFPath_CountSegments(obj.raw)):
        seg = pdfium_c.FPDFPath_GetPathSegment(obj.raw, i)
        kind = pdfium_c.FPDFPathSegment_GetType(seg)
        if kind == pdfium_c.FPDF_SEGMENT_MOVETO:
            if pts:
                yield pts, closed
            pts, closed, bezier = [], False, 0
        if kind == pdfium_c.FPDF_SEGMENT_BEZIERTO:
            bezier += 1
            keep = bezier % 3 == 0
        else:
            bezier, keep = 0, True
        if keep:
            pdfium_c.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
            pts.append(matrix.on_point(x.value, y.value))
        if pdfium_c.FPDFPathSegment_GetClose(seg):
            closed = True
    if pts:
        yield pts, closed


def _near(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) <= SLACK and abs(a[1] - b[1]) <= SLACK


def _segment_edge(kind: str, a: Point, b: Point) -> dict | None:
    (x0, y0), (x1, y1) = a, b
    # pdfplumber's y runs down; only differences matter here, so use -y.
    top, bottom = sorted((-y0, -y1))
    left, right = sorted((x0, x1))
    if abs(y1 - y0) <= SLACK:
        return _edge(kind, left, right, top, top, "h")
    if abs(x1 - x0) <= SLACK:
        return _edge(kind, left, left, top, bottom, "v")
    return None


def page_edges(page: pdfium.PdfPage) -> list[dict]:
    """The edges pdfplumber would make from the page's paths (see the module note)."""
    edges: list[dict] = []
    for obj, _, matrix in iter_placed(page, {pdfium_c.FPDF_PAGEOBJ_PATH}):
        for pts, closed in _subpaths(obj, matrix):
            if closed and not _near(pts[0], pts[-1]):
                pts.append(pts[0])
            if len(pts) < 2:
                continue
            if len(pts) == 2 or (len(pts) == 3 and closed and _near(pts[0], pts[2])):
                # A line. pdfplumber calls it horizontal when its two ends are level, vertical
                # otherwise (a slanted line too, with the box of its two ends).
                (x0, y0), (x1, y1) = pts[:2]
                top, bottom = sorted((-y0, -y1))
                left, right = sorted((x0, x1))
                level = abs(y1 - y0) <= SLACK
                edges.append(_edge("line", left, right, top, top if level else bottom,
                                   "h" if level else "v"))
            elif len(pts) == 5 and _near(pts[0], pts[4]) and (
                    (_near_x(pts[0], pts[1]) and _near_y(pts[1], pts[2])
                     and _near_x(pts[2], pts[3]) and _near_y(pts[3], pts[0]))
                    or (_near_y(pts[0], pts[1]) and _near_x(pts[1], pts[2])
                        and _near_y(pts[2], pts[3]) and _near_x(pts[3], pts[0]))):
                xs, ys = [p[0] for p in pts], [-p[1] for p in pts]
                left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
                edges += [_edge("rect_edge", left, right, top, top, "h"),
                          _edge("rect_edge", left, right, bottom, bottom, "h"),
                          _edge("rect_edge", left, left, top, bottom, "v"),
                          _edge("rect_edge", right, right, top, bottom, "v")]
            else:
                for a, b in zip(pts, pts[1:]):
                    if (edge := _segment_edge("curve_edge", a, b)) is not None:
                        edges.append(edge)
    return edges


def _near_x(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) <= SLACK


def _near_y(a: Point, b: Point) -> bool:
    return abs(a[1] - b[1]) <= SLACK


def worth_parsing(page: pdfium.PdfPage) -> bool:
    """True when pdfplumber could return a ruled table for this page: its table finder, run on
    the page's rulings, finds a table of at least two rows and two columns (the least
    tables.find_tables keeps), or one that ruled._complete_open_sides might widen."""
    edges = page_edges(page)
    for table in RuledFinder(_Page(edges)).tables:
        rows = len({c[1] for c in table.cells})
        cols = len({c[0] for c in table.cells})
        if (rows >= 2 and cols >= 2) or wider_rule_groups(edges, table.bbox):
            return True
    return False


def ruled_pages(path: Path, pages: Iterable[int]) -> list[int]:
    """The pages among these (1-based) whose rulings make a table: the ones pdfplumber is
    worth parsing."""
    pages = list(pages)
    if not pages:
        return []
    found: list[int] = []
    doc = pdfium.PdfDocument(path)
    try:
        for n in pages:
            page = doc[n - 1]
            try:
                if worth_parsing(page):
                    found.append(n)
            finally:
                page.close()
    finally:
        doc.close()
    return found
