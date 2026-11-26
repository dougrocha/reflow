"""Why a ruled table is not a table: a chart's gridlines, or the page's decorative frame."""

from __future__ import annotations

from collections.abc import Sequence

from reflow import geometry
from reflow.extract.drawings import Drawing
from reflow.extract.lines import Line
from reflow.extract.tables.cells import MIN_FILL, TableData, looks_numeric

# Chart marks that make a grid a chart's, with the count each needs. Ticks and bars do not: a
# real table's cell rules are short strokes too (an IRS tax table has over a hundred), and a
# zebra-shaded table has repeated bars (EPA p789). Seven small icons in a legend table are
# dots too (Pub 17 p7), so dots need more than a chart_marks' four.
CHART_GRID_MARKS = {"dots": 10, "wedges": 3}


# A form's ruled grid has circles for its choices and a logo's curves: dots and wedges too. Its
# cells hold labels and no figure at all, where a chart's own table holds its values.
MIN_LABEL_CELLS = 20


MIN_COLOURED = 10  # saturated-colour rules and fills in a sparse grid: a chart's colour


MIN_CHART_CURVES = 3  # coloured data lines through a grid, however full its cells are


def chart_grid(table: TableData, drawings: Sequence[Drawing]) -> str | None:
    """Why a ruled table is a chart's gridlines rather than a table, or None. Two kinds of
    evidence make it a chart's (each is checked on the corpus):
    - a drawing with dots or wedges (chart marks, see CHART_GRID_MARKS) around it: a chart's
      own small table of values (census pp 23 and 27: their cells are not sparse). A grid of
      labels with no figure in it is a form's, its choice circles the dots (MIN_LABEL_CELLS);
    - a sparse grid (fill below MIN_FILL, the test the aligned tables pass) with coloured
      rules and fills inside it: a coloured chart whose labels sit outside the grid (census
      pp 13, 19, 20, 22 and 25). A sparse worksheet in black and white is kept (Pub 17 p33:
      fill 0.38, no colour); so is a zebra-shaded table (EPA p789: bars, no colour);
    - coloured data curves through the grid (MIN_CHART_CURVES): a chart whose axis values fill
      every cell (resnet p5 Figure 4, p8 Figure 6). A table has no coloured curves; a phase
      diagram's filled arrows are not data (NASA SE handbook p34)."""
    if table.method != "ruled":
        return None
    cells = [c for row in table.rows for c in row if c is not None]  # None: a merged cell
    fill = sum(1 for c in cells if c) / len(cells) if cells else 1.0
    sparse = fill < MIN_FILL
    area = geometry.area(table.bbox)
    marks = [m for d in drawings if area and geometry.overlap(table.bbox, d.bbox) >= 0.9 * area
             for m in d.marks if m[0] in CHART_GRID_MARKS and m[1] >= CHART_GRID_MARKS[m[0]]]
    labels = [c for c in cells if c and c.strip()]
    if marks and not (len(labels) >= MIN_LABEL_CELLS and not any(map(looks_numeric, labels))):
        return f"gridlines inside a chart ({', '.join(f'{n} {k}' for k, n in marks)})"
    if sparse and table.coloured >= MIN_COLOURED:
        return (f"gridlines of a chart: {round(100 * fill)}% of its cells hold text, "
                f"{table.coloured} coloured rules and fills")
    if table.coloured_curves >= MIN_CHART_CURVES:
        return (f"gridlines of a chart: {table.coloured_curves} coloured data curves through "
                f"its {round(100 * fill)}%-filled cells")
    return None


PAGE_FRAME_AREA = 0.95  # a ruled box this share of the page (or more) is the page's border


PAGE_FRAME_CELL = 0.6   # ... and one of its cells holds this share of its text lines


def page_frame(table: TableData, page_w: float, page_h: float,
               lines: Sequence[Line]) -> str | None:
    """Why a ruled table is the page's decorative frame rather than a table, or None: its box
    covers the page and one cell holds most of the text lines inside it (the rest of the page's
    rules are the table's other, nearly empty cells). A full-page form spreads its text over
    many cells, so no one cell holds PAGE_FRAME_CELL of it."""
    if table.method != "ruled" or not table.grid:
        return None
    page = (0.0, 0.0, page_w, page_h)
    if geometry.overlap(table.bbox, page) < PAGE_FRAME_AREA * geometry.area(page):
        return None
    inside = [l for l in lines if geometry.inside(l.bbox, table.bbox, slack=2)]
    if not inside:
        return None
    biggest = max((sum(1 for l in inside if geometry.centre_in(l.bbox, box))
                   for row in table.grid for box in row if box is not None), default=0)
    if biggest < PAGE_FRAME_CELL * len(inside):
        return None
    share = round(100 * biggest / len(inside))
    return f"the page's frame: one cell holds {share}% of the {len(inside)} text lines around it"
