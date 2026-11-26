"""What a table is (`TableData`) and what a cell that is a number looks like."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple

from reflow.model import BBox
from reflow.patterns import NUMBER

MIN_FILL = 0.4  # share of a table's cells that hold text or numbers (sparse schedules stay)


class TableMethod(StrEnum):
    """How a table was found; the published `attrs["method"]`."""
    RULED = "ruled"  # pdfplumber lines
    ALIGNED = "aligned"  # text alignment
    CALENDAR = "calendar"  # a printed month (analyze.calendar)
    FORM_GRID = "form-grid"  # a form's boxes, whose labels are in the controls' own boxes


class Bar(NamedTuple):
    """A form's section bar in a table cell: the box and the text of its lines."""
    box: BBox
    text: str


class TableFact(NamedTuple):
    """A review fact about a table; it becomes a Fact on the table's node."""
    code: str
    detail: str
    data: dict


@dataclass
class TableData:
    page: int
    bbox: BBox  # top-left coordinates, like the model
    rows: list[list[str | None]]  # None marks a cell merged into the one on its left
    method: TableMethod = TableMethod.RULED
    header_rows: int = 1  # leading rows that are headers (TH)
    why: str | None = None  # how the table was found, for the review trace
    cells: list[BBox] = field(default_factory=list)  # the boxes of the text lines in its cells
    # A ruled table's cell boxes, laid out like `rows` (None where the grid has no cell there).
    # With them, the lines that no cell holds can be found (see unheld_lines).
    grid: list[list[BBox | None]] = field(default_factory=list)
    # Saturated-colour rules and fills inside a ruled table's box (a chart's colour, chart_grid).
    coloured: int = 0
    # Stroked saturated-colour curves inside it: a chart's data lines (see chart_grid).
    coloured_curves: int = 0
    # The box of the printed text of each cell, laid out like `rows` (None for an empty cell).
    # Only a table that knows its cells' text (a calendar's days) fills it; the table node's
    # cells then carry these boxes.
    boxes: list[list[BBox | None]] = field(default_factory=list)
    caption: str | None = None  # the table's printed title, and its box
    caption_bbox: BBox | None = None
    # By (row, column): a calendar day's status and notes (analyze.calendar).
    days: dict[tuple[int, int], dict] = field(default_factory=dict)
    # By (row, column): the form section bars of the cells (table_nodes.mark_section_bars), and
    # the cells that hold the rest of a bar the grid cut into several.
    bars: dict[tuple[int, int], Bar] = field(default_factory=dict)
    bar_parts: set[tuple[int, int]] = field(default_factory=set)
    facts: list[TableFact] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.method = TableMethod(self.method)

    def has_cell_notes(self, i: int, j: int) -> bool:
        """Whether the cell carries a day, a bar or a bar part."""
        return (i, j) in self.days or (i, j) in self.bars or (i, j) in self.bar_parts


def looks_numeric(text: str | None) -> bool:
    return bool(text) and bool(NUMBER.match(text.strip()))
