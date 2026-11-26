"""What the form planner's parts share: the tuning constants, a label claim, the plan it returns and
the small helpers more than one part reads (see analyze.forms for what the planner does)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from reflow.extract.forms import Widget
from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData
from reflow.model import Node
from reflow.text_quality import squash

STACK_GAP = 3.0  # lines this close, left-aligned, are one label
MAX_LABEL_LINES = 3
ROW_TOLERANCE = 3.0  # boxes whose tops differ by less than this (pt) are on one row
NUMERAL_ROW = re.compile(r"\d{1,3}(?:\s+\d{1,3}){2,}")  # a chart's numbers, one over each of its boxes (teeth 1 2 3 ...)


def norm(text: str) -> str:
    return squash(text.replace(SOFT_HYPHEN, ""))


@dataclass
class Claim:
    """A printed line (or stack of lines) taken as a widget's label or a group's legend."""
    lines: list[Line]
    how: str  # "above", "left", "right", "legend", or "cell" (a table cell's whole text)
    whole: str | None = None  # exact text for a cell or one inline option label
    part: int | None = None  # an inline option label: its index among the line's marker-split parts
    shared: bool = False  # a legend that another group already took: this one only names itself by it

    @property
    def text(self) -> str:
        return self.whole if self.whole is not None else norm(" ".join(l.text for l in self.lines))


@dataclass
class FormPlan:
    tables: list[TableData]  # the page's tables, with consumed labels cut out of their cells
    consumed: set[int] = field(default_factory=set)  # id() of flow lines that became labels
    cells: dict[tuple[int, int, int], list[Node]] = field(default_factory=dict)
    flow: list[Node] = field(default_factory=list)  # fields and groups that sit in the text flow


def _trim_markers(after: list[str]) -> list[str]:
    """The texts after a line's first box glyph without the empty ones at the end: a box at the
    end of the run belongs to the next option, whose label is printed on its own."""
    while after and not after[-1].strip():
        after = after[:-1]
    return after


def _labels_first(parts: list[str]) -> bool:
    """A run that prints each label before its box ("Yes □ No □"): every text but the last has
    words, and nothing follows the last box."""
    return all(part.strip() for part in parts[:-1]) and not parts[-1].strip()


def _cell_at(tables: Sequence[TableData], point: tuple[float, float]
             ) -> tuple[int, int, int] | None:
    """(table, row, column) of the smallest grid cell holding the point."""
    best, best_area = None, float("inf")
    x, y = point
    for ti, t in enumerate(tables):
        for i, row in enumerate(t.grid):
            for j, box in enumerate(row):
                if box and box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                    area = (box[2] - box[0]) * (box[3] - box[1])
                    if area < best_area:
                        best, best_area = (ti, i, j), area
    return best


def _tooltip(text: str) -> str:
    """Ignore numeric codes and single tokens that look like internal field identifiers."""
    text = norm(text)
    if not text or all(token.isdigit() for token in text.split()):
        return ""
    if " " not in text and text.isupper() and len(text) <= 8:
        return ""
    if " " not in text and not re.fullmatch(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", text):
        return ""
    return text


def _is_section_title(line: Line) -> bool:
    """A bold numbered title ("3. PROPERTY IDENTIFICATION INFORMATION") or bold line of
    capitals heads its section, whatever form group sits under it."""
    text = line.text.strip()
    return line.bold and (bool(re.match(r"^(?:section\s+)?\d{1,2}\.\s+\S", text, re.I))
                          or (text.isupper() and len(text.split()) >= 2 and len(text) <= 60))


class PlannerState:
    """What the parts of the planner read of the page: the planner's __init__ sets each (see _Planner)."""
    page: PageContent
    lines: list[Line]
    tables: list[TableData]
    widgets: list[Widget]
    place: dict[int, tuple[int, int, int] | None]
    option_labels: dict[int, Claim]
    taken: set[int]  # id() of lines already claimed

