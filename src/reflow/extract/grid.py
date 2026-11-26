"""pdfplumber's table finder, with its cell search made linear in the size of a ruling's row.

`pdfplumber.table.intersections_to_cells` finds, for every intersection of the rulings, the first
rectangle that has it as its top-left corner. It copies the rest of the sorted points for each
one, scans them for the points below and to the right, and rebuilds two sets of edge boxes for
every pair it tests (and evaluates a `typing` annotation each time), so a page with thousands of
intersections spends seconds there: about 2.4 s of the 21 s that p17 takes to extract at `-j 1`.

`cells_of` returns the same cells in the same order. It indexes the points by column and by
row once, keeps each point's edge boxes as sets, and walks the same pairs in the same order, so
the first rectangle it finds is the one pdfplumber finds. `RuledFinder` is `TableFinder` with
that function in the one place `TableFinder.__init__` calls it. Everything else (the
intersections, the grouping of cells into tables, the text of a cell) is pdfplumber's.

So are the edges, less the ones nobody can see (`shown_edges`): pdfplumber takes every side of
every filled rectangle for a rule, also the sides of a fill painted on a larger fill of its own
colour.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np
from pdfplumber.table import (
    Table,
    TableFinder,
    TableSettings,
    cells_to_tables,
    edges_to_intersections,
)
from pdfplumber.utils import line_to_edge, obj_to_bbox, rect_to_edges

type Point = tuple[float, float]
type Cell = tuple[float, float, float, float]


def cells_of(intersections: Mapping[Point, Mapping[str, Any]]) -> list[Cell]:
    """The cells `pdfplumber.table.intersections_to_cells(intersections)` returns, in its order."""
    points = sorted(intersections)
    verticals = {p: {obj_to_bbox(e) for e in intersections[p]["v"]} for p in points}
    horizontals = {p: {obj_to_bbox(e) for e in intersections[p]["h"]} for p in points}

    def connects(a: Point, b: Point) -> bool:
        if a[0] == b[0] and not verticals[a].isdisjoint(verticals[b]):
            return True
        return a[1] == b[1] and not horizontals[a].isdisjoint(horizontals[b])

    # `points` ascends by x then y, so a column ascends by y and a row by x.
    columns: dict[float, list[Point]] = {}
    rows: dict[float, list[Point]] = {}
    for p in points:
        columns.setdefault(p[0], []).append(p)
        rows.setdefault(p[1], []).append(p)
    column_at = {p: k for group in columns.values() for k, p in enumerate(group)}
    row_at = {p: k for group in rows.values() for k, p in enumerate(group)}

    cells: list[Cell] = []
    for point in points:
        below = columns[point[0]][column_at[point] + 1:]
        right = rows[point[1]][row_at[point] + 1:]
        for lower in below:
            if not right:
                break
            if not connects(point, lower):
                continue
            for beside in right:
                if not connects(point, beside):
                    continue
                corner = (beside[0], lower[1])
                if corner in intersections and connects(corner, beside) and connects(corner, lower):
                    cells.append((point[0], point[1], corner[0], corner[1]))
                    break
            else:
                continue
            break
    return cells


COVER_SLACK = 0.01  # points: two edges this close are one edge
COVER_CHUNK = 256   # fills compared with all the others at a time, to bound the memory
# A fill of at least this share of the page is the page's background, or a panel under the whole
# layout, not a cell's shading: every cell filled with that colour would lie "on its own colour"
# and lose its edges, and with them its rows and columns. It covers nothing.
BACKGROUND_SHARE = 0.5


def _colour(colour: object) -> object:
    return tuple(colour) if isinstance(colour, (list, tuple)) else colour


def covered_fills(rects: Sequence[Mapping[str, Any]], page_area: float = 0.0) -> set[int]:
    """The indexes of the filled, unstroked rectangles that paint nothing anyone can see:
    each lies wholly on a fill of its own colour, with no fill of another colour painted on
    that one before it (`rects` is in paint order). Word shades a cell with two such fills,
    the cell and the text area inside its padding. Of two fills with one box, the later is
    the covered one. A fill of `BACKGROUND_SHARE` of `page_area` or more covers nothing."""
    filled = [k for k, r in enumerate(rects) if r.get("fill")]
    if len(filled) < 2:
        return set()
    at = np.array(filled)
    box = np.array([[rects[k][side] for side in ("x0", "top", "x1", "bottom")] for k in filled],
                   dtype=float)
    names: dict[object, int] = {}
    colour = np.array([names.setdefault(_colour(rects[k].get("non_stroking_color")), len(names))
                       for k in filled])
    # Whether each fill may cover another: not a background (area 0 means the page is unknown).
    may_cover = ((box[:, 2] - box[:, 0]) * (box[:, 3] - box[:, 1]) < BACKGROUND_SHARE * page_area
                 if page_area > 0 else np.ones(len(filled), dtype=bool))
    covered: set[int] = set()
    for start in range(0, len(filled), COVER_CHUNK):
        inner = slice(start, start + COVER_CHUNK)
        on = ((colour[inner, None] == colour[None, :]) & may_cover[None, :]
              & (box[None, :, 0] <= box[inner, None, 0] + COVER_SLACK)
              & (box[None, :, 1] <= box[inner, None, 1] + COVER_SLACK)
              & (box[None, :, 2] >= box[inner, None, 2] - COVER_SLACK)
              & (box[None, :, 3] >= box[inner, None, 3] - COVER_SLACK))
        for i, j in zip(*np.nonzero(on), strict=True):
            i += start
            if i == j or rects[at[i]].get("stroke") or int(at[i]) in covered:
                continue
            if j > i:
                # Painted after it. With the same box it is the covered one; a larger one
                # hides this fill, whatever was painted in between.
                if np.all(np.abs(box[i] - box[j]) <= COVER_SLACK):
                    continue
            else:
                between = slice(j + 1, i)
                if np.any((colour[between] != colour[i])
                          & (box[between, 0] < box[i, 2]) & (box[between, 2] > box[i, 0])
                          & (box[between, 1] < box[i, 3]) & (box[between, 3] > box[i, 1])):
                    continue  # it shows again on what was painted over the larger fill
            covered.add(int(at[i]))
    return covered


def shown_edges(page: Any) -> list[Mapping[str, Any]]:
    """`page.edges`, without the sides of the fills that `covered_fills` names: the border of
    one colour inside itself is no rule. A page without rectangles of its own (the stand-in
    of reflow.extract.rulings) keeps its edges."""
    rects: Sequence[dict[str, Any]] = getattr(page, "rects", ())
    x0, top, x1, bottom = page.bbox
    covered = covered_fills(rects, (x1 - x0) * (bottom - top))
    if not covered:
        return page.edges
    return [*map(line_to_edge, page.lines),
            *(e for k, r in enumerate(rects) if k not in covered for e in rect_to_edges(r)),
            *page.curve_edges]


class RuledFinder(TableFinder):
    """pdfplumber's TableFinder, with `cells_of` finding the cells and `shown_edges` its
    rules."""

    def __init__(self, page: Any, settings: Any = None) -> None:
        self.settings = TableSettings.resolve(settings)
        # get_edges reads the edges and the box of `self.page`.
        shown: Any = SimpleNamespace(edges=shown_edges(page), bbox=page.bbox)
        self.page = shown
        self.edges = self.get_edges()
        self.page = page
        self.intersections = edges_to_intersections(
            self.edges, self.settings.intersection_x_tolerance,
            self.settings.intersection_y_tolerance)
        self.cells = cells_of(self.intersections)
        self.tables = [Table(self.page, group) for group in cells_to_tables(self.cells)]


def wider_rule_groups(edges: Iterable[Mapping[str, Any]], bbox: tuple[float, float, float, float]
                      ) -> list[tuple[float, float, list[Mapping[str, Any]]]]:
    """The groups of three or more horizontal rules that span a table of box `bbox` and run
    further out than it does, as (left, right, rules), the largest group first: where a
    borderless outside column may sit (ruled._complete_open_sides)."""
    x0, top, x1, bottom = bbox
    groups: dict[tuple[float, float], list[Mapping[str, Any]]] = {}
    for rule in edges:
        if (rule.get("orientation") == "h" and top - 1 <= rule["top"] <= bottom + 1
                and rule["x0"] <= x0 + 1 and rule["x1"] >= x1 - 1):
            groups.setdefault((round(rule["x0"], 1), round(rule["x1"], 1)), []).append(rule)
    return [(left, right, group)
            for (left, right), group in sorted(groups.items(), key=lambda item: -len(item[1]))
            if len(group) >= 3 and not (left >= x0 - 1 and right <= x1 + 1)]


def find_ruled_tables(page: Any, settings: Any = None) -> list[Table]:
    """`page.find_tables(settings)`, for a pdfplumber page or a crop of one."""
    return RuledFinder(page, settings).tables
