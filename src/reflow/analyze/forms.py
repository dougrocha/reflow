"""Form fields in the reading flow: bind each widget to its printed label, group the related
checkboxes, and decide where each control goes (a ruled table's cell, or the text flow).

The rules, all from geometry because few forms say more than a tooltip:
- A text field or combo is labelled by the printed text just above it (up to three stacked
  lines, starting within the field's width), else by the text just left of it on its row.
  A worksheet row prints `entry text, leader dots, line number, box`: the line number ("1a",
  "10") names the line, not what it asks, so the label is the entry text the line number sits
  after, with the lines of the entry that wrap above it.
- A checkbox or radio is labelled by the text just right of it on its row.
- Checkboxes sharing a name stem, row or stack, and radios sharing a name, form a group;
  the question above or to the left is the legend. Missing names become review facts.
- A label moves into the control only where that cannot lose or duplicate text: a flow line
  is taken out of the flow, a table cell's text is cut by the exact label (or the whole plan
  for that label is dropped). A label left in its cell (text left of a field, the text beside
  a lone checkbox) stays as text, and the control takes it as its accessible name.
- A caption printed over its own blank line ("DATE: ______") labels the box on the blank, and
  the underscores the box replaces are left out. A question printed after a group's options
  ("□ Yes □ No Is this ...?") is the group's legend, not part of the last option's label.
- A group's checkboxes become radios where the form says only one can be chosen: a Yes / No (or
  Y / N, or "Are requested" / "Are not requested") pair, or a group under a printed "Check only
  one". Each radio still names its own PDF checkbox field (the viewer fills each from its own
  box), so only boxes that are separate fields qualify. A Y / N pair that shares a row with
  other options is its own group. A group with no legend of its own that sits under the same
  section title as another group is part of that group; a group nothing names has no fieldset.
Text is never removed unless it is found whole, so every printed character appears once (but
for the underscores a box replaces).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from reflow.analyze import form_grid
from reflow.analyze.drawn_lines import split_drawn_lines
from reflow.analyze.form_common import Claim, FormPlan, norm
from reflow.analyze.form_cuts import (
    apply_cuts,
    split_blank_lines,
    split_captions,
    split_option_questions,
    split_option_rows,
)
from reflow.analyze.form_groups import CHOOSE_ONE
from reflow.analyze.form_labels import is_line_number, starts_lower
from reflow.analyze.form_place import place_flow
from reflow.analyze.form_plan import _Planner
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData

__all__ = [
    "CHOOSE_ONE",
    "Claim",
    "FormPlan",
    "PageForms",
    "apply_cuts",
    "is_line_number",
    "norm",
    "place_flow",
    "plan_forms",
    "plan_page_forms",
    "split_blank_lines",
    "split_captions",
    "split_option_questions",
    "split_option_rows",
    "starts_lower",
]


def plan_forms(page: PageContent, lines: Sequence[Line], flow: Sequence[Line],
               tables: Sequence[TableData]) -> FormPlan:
    """The page's fields placed and labelled. `lines` are all its text lines (the geometry
    labels are found in), `flow` those left in the text flow, `tables` its tables."""
    return _Planner(page, lines, flow, tables).plan()


@dataclass
class PageForms:
    """What the form stages leave of a page: its lines (`kept` for the text-loss guard, `lines`
    for the flow), its tables, the field plan and the form grids read from the form regions."""
    kept: list[Line]
    lines: list[Line]
    tables: list[TableData]
    plan: FormPlan | None
    grids: list[form_grid.FormGrid]


def _read_grids(regions: list[form_grid.Region], plan: FormPlan | None, first: int) -> list[form_grid.FormGrid]:
    """Each form region as its nodes (see analyze.form_grid): its text lines, and the fields and
    fieldsets the form planner placed in its boxes (the planner's tables from `first` on)."""
    grids = []
    for k, region in enumerate(regions, first):
        mine = [n for (t, i, j), nodes in sorted(plan.cells.items()) if t == k for n in nodes] if plan else []
        grids.append(form_grid.build(region, plan.consumed if plan else set(), mine))
    if plan:
        plan.cells = {at: nodes for at, nodes in plan.cells.items() if at[0] < first}
    return grids


def plan_page_forms(page: PageContent, regions: list[form_grid.Region], region_lines: list[Line],
                    kept: list[Line], lines: list[Line], tables: list[TableData]) -> PageForms:
    """Form fields and form grids of one page. `regions` are the form-layout grids found among the
    page's ruled tables and `region_lines` the lines inside them; `kept` are the lines still in
    play after the tables, `lines` those left in the flow. Region lines join both for the
    planner and leave them again once their grid is read."""
    plan = None
    if regions:
        in_play = {id(l) for l in kept} | {id(l) for l in region_lines}
        in_flow = {id(l) for l in lines} | {id(l) for l in region_lines}
        kept = [l for l in page.lines if id(l) in in_play]
        lines = [l for l in page.lines if id(l) in in_flow]
        # Two boxes' words that the text layer ran into one line are cut at the wall between them.
        walls = form_grid.wall_cuts(regions, kept)
        kept, lines = apply_cuts(kept, walls), apply_cuts(lines, walls)
    grids: list[form_grid.FormGrid] = []
    if page.widgets:
        # The words a drawn box's text layer ran together around it, cut at the box.
        drawn = split_drawn_lines(page, kept)
        kept, lines = apply_cuts(kept, drawn), apply_cuts(lines, drawn)
        cuts = split_captions(page, kept)  # captions joined across neighbouring cells
        cuts |= split_option_rows(page, kept)  # a question run into its Yes / No boxes
        # A question run into the last option's label, and a caption run into the blank line its box
        # replaces; a line another rule already cut keeps that cut.
        cuts = split_option_questions(page, kept) | split_blank_lines(page, kept) | cuts
        kept, lines = apply_cuts(kept, cuts), apply_cuts(lines, cuts)
        # A form region's boxes go to the planner as a grid of their own, and its section bars stay
        # out of the labels it takes.
        for region in regions:
            region.lines = [l for l in kept if form_grid.in_region(l, region)]
        bars = {id(l) for region in regions for l in form_grid.bars(region)}
        walls = [form_grid.as_table(region) for region in regions]
        plan = plan_forms(page, [l for l in kept if id(l) not in bars],
                          [l for l in lines if id(l) not in bars], tables + walls)
        tables = plan.tables[:len(tables)]
        lines = [l for l in lines if id(l) not in plan.consumed]
        grids = _read_grids(regions, plan, len(tables))
    elif regions:
        for region in regions:
            region.lines = [l for l in kept if form_grid.in_region(l, region)]
        grids = _read_grids(regions, None, len(tables))
    if grids:
        kept = [l for l in kept if not form_grid.in_any_region(l, regions)]
        lines = [l for l in lines if not form_grid.in_any_region(l, regions)]
    return PageForms(kept, lines, tables, plan, grids)
