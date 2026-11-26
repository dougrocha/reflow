"""What the page stages decide about one page, kept together, and what they share across the
document.

analyze() plans every page first (blocks, figures, tables, reading order), because heading
styles are ranked and lists are linked across pages, and only then builds the nodes. A
PageContext is that plan for one page; a PlanState is what every page's planning reads and adds to.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from reflow.analyze.blocks import Block
from reflow.analyze.figures import FigureCandidate
from reflow.analyze.forms import FormPlan
from reflow.analyze.form_grid import FormGrid
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData
from reflow.model import BBox


@dataclass
class PageContext:
    page: PageContent
    blocks: list[Block]
    figures: list[FigureCandidate]  # each with why it is one and its labels
    tables: list[TableData]
    columns: int  # reading-order split (index into SPLIT_NAMES)
    form: list[BBox]  # ruled boxes that are not tables: where form labels may sit
    plan: FormPlan | None = None  # set on pages with form widgets
    guarded: set[int] = field(default_factory=set)  # id() of lines the table text-loss guard kept
    label_turned: set[int] = field(default_factory=set)  # id() of rotated lines figures keep as labels
    guarded_lines: list[Line] = field(default_factory=list)  # the lines `guarded` holds, so they can cross a process boundary
    forms: list[FormGrid] = field(default_factory=list)  # ruled grids read as forms, not tables (analyze.form_grid)


@dataclass
class PlanState:
    body: float
    reasons: dict[int, str]  # id(line) → why it was dropped
    artifacts: set[int]  # id() of dropped lines
    legend: dict[str, str]  # icon key → meaning (figures.find_legend)
    marks: dict[int, str] = field(default_factory=dict)  # id(line) → what the icon beside it means
    # Hanging indents across the whole document, by marker x (see text_offset).
    offsets: Counter[tuple[int, float]] = field(default_factory=Counter)
    styles: Counter[tuple[float, bool]] = field(default_factory=Counter)
