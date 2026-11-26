"""The cells, rows and columns that the aligned and scanned readers build from text lines."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from itertools import combinations

from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.patterns import LEADER_TAIL, NUMBER, NUMBER_WITH_NOTE
from reflow.text_quality import squash

# Marks that fill a cell of a checklist grid on their own ("●" under each age it applies to).
CELL_MARKS = frozenset("●○◆✓✔✗✘")  # not boxes: "■ I moved out …" is a form (see analyze.forms)

MIN_GLYPH_WIDTH = 1.5  # a single character narrower than this (points) is a kerning fragment

RIGHT_TOL = 3.0       # numbers that share a right edge within this are one column (points)

LEFT_TOL = 3.0        # labels that share a left edge within this are one column

LABEL_GROUP = 12.0    # label edges this close form one column: indents within a label column

HEADER_TOL = 5.0      # a header may sit a little off the right edge of the numbers below it

ROW_OVERLAP = 0.45    # cells share a row when their centres are this fraction of a line apart

MIN_NUMBER_CELLS = 2  # a numeric column needs this many numbers to count


@dataclass
class Cell:
    line: Line
    text: str
    x0: float
    x1: float  # right edge, leader dots excluded
    top: float
    bottom: float
    numeric: bool

    @property
    def height(self) -> float: return self.bottom - self.top

    @property
    def cy(self) -> float: return (self.top + self.bottom) / 2

    @property
    def cx(self) -> float: return (self.x0 + self.x1) / 2


@dataclass
class Row:
    cells: list[Cell]
    kind: str = "prose"  # "table", "sub" (label-only heading), "cont" (wrapped label), "prose"
    extra: list[Cell] = field(default_factory=list)  # wrapped label lines of this row

    @property
    def top(self) -> float: return min(c.top for c in self.cells + self.extra)

    @property
    def bottom(self) -> float: return max(c.bottom for c in self.cells + self.extra)

    @property
    def height(self) -> float: return max(c.height for c in self.cells)


@dataclass
class Column:
    kind: str  # "num" (right edges of numbers) or "text" (left edges of labels)
    edges: list[float] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)

    @property
    def lo(self) -> float: return min(c.x0 for c in self.cells)

    @property
    def hi(self) -> float: return max(c.x1 for c in self.cells)


def ink(text: str) -> float:
    """Rough width of a string in em: dots and spaces are narrow."""
    return sum(0.28 if ch in ". ·…_" else 0.55 for ch in text)


def read_cell(line: Line) -> Cell | None:
    text = squash(line.text)
    stripped = LEADER_TAIL.sub("", text).strip()
    if not stripped or not any(ch.isalnum() or ch in "–—−-" + SOFT_HYPHEN or ch in CELL_MARKS
                               for ch in stripped):
        return None
    x0, x1 = line.x0, line.x1
    if len(stripped) == 1 and x1 - x0 < MIN_GLYPH_WIDTH:
        return None  # one letter-spaced glyph of a code ("5 2 6"), not a cell of its own
    numeric = bool(NUMBER.match(stripped)) or stripped in CELL_MARKS  # a mark is a value
    if stripped != text:
        # The leader is as wide as its dots: scale the box down to the text that is left.
        x1 = x0 + (x1 - x0) * ink(stripped) / max(ink(text), 1e-6)
    elif (m := NUMBER_WITH_NOTE.match(stripped)):
        # "7.54 (7.72±0.16)": a value and its note. The value sets the right edge.
        numeric = True
        x1 = x0 + (x1 - x0) * ink(m[1]) / max(ink(stripped), 1e-6)
    return Cell(line, stripped, x0, round(x1, 2), line.top, line.bottom, numeric)


def group_rows(cells: list[Cell]) -> list[Row]:
    """Cells on one baseline make a row, in reading order."""
    groups: list[list[Cell]] = []
    for cell in sorted(cells, key=lambda c: (c.cy, c.x0)):
        for group in groups:
            ref = group[0]
            if abs(cell.cy - ref.cy) <= ROW_OVERLAP * max(cell.height, ref.height):
                group.append(cell)
                break
        else:
            groups.append([cell])
    rows = [Row(sorted(g, key=lambda c: c.x0)) for g in groups]
    return sorted(rows, key=lambda r: r.top)


def cluster(values: Iterable[float], tol: float) -> list[list[float]]:
    groups: list[list[float]] = []
    for v in sorted(values):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


def find_columns(cells: list[Cell], years: Sequence[Cell] = ()) -> list[Column]:
    """Columns from the edges that repeat: right edges of numbers, left edges of labels. A
    column needs at least two cells. A single figure is a column only under a year label in
    `years` (a schedule's year heading over its one row of figures). Only cells of multi-cell
    rows define columns, so prose lines don't make a column of their own."""
    columns: list[Column] = []
    num = [c for c in cells if c.numeric]
    for group in cluster((c.x1 for c in num), RIGHT_TOL):
        members = [c for c in num if group[0] - RIGHT_TOL <= c.x1 <= group[-1] + RIGHT_TOL]
        headed = any(abs(y.x1 - group[-1]) <= HEADER_TOL for y in years)
        if len(members) >= MIN_NUMBER_CELLS or (members and headed):
            columns.append(Column("num", group, members))
    text = [c for c in cells if not c.numeric]
    labels: list[Column] = []
    for group in cluster((c.x0 for c in text), LABEL_GROUP):
        members = [c for c in text if group[0] - 0.01 <= c.x0 <= group[-1] + 0.01]
        if len(members) >= 2:
            labels.append(Column("text", group, members))
    # Two groups whose spans overlap are one column unless a row holds a cell of each. Label
    # groups that never share a row are one column of centred or ragged labels: they merge.
    merged = True
    while merged:
        merged = False
        for a, b in combinations(labels, 2):
            if spans_overlap(a, b) and not share_row(a, b):
                labels.remove(a)
                labels.remove(b)
                labels.append(Column("text", sorted(a.edges + b.edges), a.cells + b.cells))
                merged = True
                break
    # A group inside another's span that never shares a row with it (a label edge inside a
    # number column: its headings or notes) is dropped; assign_column places its cells by their
    # centres. The one with more cells stays, numbers first.
    kept: list[Column] = []
    for col in sorted(columns + labels, key=lambda col: (col.kind != "num", -len(col.cells))):
        if all(not spans_overlap(col, k) or share_row(col, k) for k in kept):
            kept.append(col)
    return sorted(kept, key=lambda col: col.lo)


def spans_overlap(a: Column, b: Column) -> bool:
    return a.hi > b.lo and b.hi > a.lo


def share_row(a: Column, b: Column) -> bool:
    """Whether some row holds a cell of each column: two columns, not one."""
    return any(ca is not cb and abs(ca.cy - cb.cy) <= ROW_OVERLAP * max(ca.height, cb.height)
               for ca in a.cells for cb in b.cells)


def assign_column(cell: Cell, columns: list[Column]) -> int | None:
    """The column a cell belongs to, by its edges, else by the share of its width it covers."""
    if cell.numeric:
        for i, col in enumerate(columns):
            if col.kind == "num" and any(abs(cell.x1 - e) <= RIGHT_TOL for e in col.edges):
                return i
    for i, col in enumerate(columns):
        if col.kind == "text" and col.edges[0] - LEFT_TOL <= cell.x0 <= col.edges[-1] + LEFT_TOL:
            return i
    for i, col in enumerate(columns):  # a header over numbers, its right edge a little off
        if col.kind == "num" and any(abs(cell.x1 - e) <= HEADER_TOL for e in col.edges):
            return i
    centre = (cell.x0 + cell.x1) / 2
    for i, col in enumerate(columns):  # a centred header or dash: the span its centre falls in
        if col.kind == "num" and col.lo - LEFT_TOL <= centre <= col.hi + LEFT_TOL:
            return i
    best, best_share = None, 0.3
    width = max(cell.x1 - cell.x0, 1e-6)
    for i, col in enumerate(columns):  # otherwise the column whose span covers most of it
        share = max(0.0, min(cell.x1, col.hi) - max(cell.x0, col.lo)) / width
        if share > best_share:
            best, best_share = i, share
    return best
