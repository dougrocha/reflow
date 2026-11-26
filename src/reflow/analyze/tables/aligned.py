"""Aligned tables: tables without a full grid (booktabs rules, short underlines, or whitespace
alone), found from the text lines.

Consecutive rows whose cells line up in columns make a table. Numbers align on their right edge,
labels on their left edge, and dot leaders after a label are ignored. Layout columns of the page
are kept apart, so two schedules side by side never merge into one table. A scanned page's
grids come from reflow.analyze.tables.scanned.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Sequence

from reflow.patterns import (
    CAPTION_START,
    CODE_CELL,
    NUMBER,
    SCHEDULE_HEAD,
    YEAR_CELL,
    YEAR_HEADING_ANY,
    YEAR_LABEL,
)
from reflow import geometry
from reflow.extract.lines import Line
from reflow.analyze.tables.cells import (
    CELL_MARKS,
    HEADER_TOL,
    LABEL_GROUP,
    MIN_NUMBER_CELLS,
    ROW_OVERLAP,
    Cell,
    Column,
    Row,
    assign_column,
    find_columns,
    group_rows,
    read_cell,
)
from reflow.analyze.tables.rows import (
    CURRENCY_SIGNS,
    NEIGHBOUR_GAP,
    join_signs,
    merge_centred_values,
    merge_stacked,
    split_codes,
    split_glued,
    split_merged,
)
from reflow.analyze.tables.scanned import MAX_CELL_WORDS, ocr_grid_tables
from reflow.extract.tables.cells import MIN_FILL, TableData, TableMethod, looks_numeric
from reflow.leaders import is_contents_entry
from reflow.model import BBox

MAX_HEADING_WORDS = 8  # a label-only row inside a table (a sub-heading) is at most this long


LIST_LABEL = re.compile(r"^(\(?[a-z]{1,3}[.)]|\(?[ivx]{1,4}[.)])\s")  # "a. " or "(iv) "


LIST_NUMBER = re.compile(r"^\d{1,2}[.)]\s")  # "6. " or "12) "


MIN_LAYOUT_GUTTER = 6.0  # a gap between layout columns, in points


GUTTER_CROSSINGS = 0.02  # share of lines a layout gutter may be crossed by (rules, strays)


CENTRAL = 0.12        # a layout gutter lies within this share of the page width of its middle


GUTTER_ROUNDING = 0.1  # a gutter may fall this many points short of MIN_LAYOUT_GUTTER (edge rounding)


BAND_GUTTER_DRIFT = 2 * MIN_LAYOUT_GUTTER  # a band's gutter may sit this far from the page's


MIN_ROW_SEGMENTS = 3  # segments on one baseline that make a table row beside a full-width line


MAX_ROW_GAP = 2.5     # rows further apart than this many row heights end a table


MIN_TABLE_ROWS = 3


MIN_WORD_TABLE_ROWS = 5  # a table of words only (no numbers): a header and at least four rows


MAX_WRAPPED_SHARE = 0.2  # rows starting mid-sentence, in a table of words


MAX_JUNK_SHARE = 0.2  # cells that are a mark or a fragment, not text or a number


HEADING_MAX_WORDS = 6  # a heading over a table's columns is a label, not a sentence


SENTENCE_BREAK = re.compile(r"[.!?](\s|$)")


MIN_NUMBER_SHARE = 0.4  # a number column is filled in at least this share of the table's rows


# A table with no heading above its figures is kept when this many rows (and this share of its
# rows) open with a budget line code: the rows continue a schedule whose headings are a page back.
MIN_CODED_ROWS = 3


MIN_CODED_SHARE = 0.5


MIN_HEADLESS_YEARS = 3  # a schedule's year columns: actual, then two estimates


def layout_gutter(lines: Sequence[Line], width: float,
                   rules: Sequence[BBox] = ()) -> tuple[float, float] | None:
    """The gap between the page's two layout columns: a central strip that (almost) no line
    crosses, with lines on both sides. A table never spans it, but a ruled table's own column
    gap can look like one: `rules` (the boxes of the page's ruling strokes) tell them apart."""
    if not lines or width <= 0:
        return None
    n = int(width) + 2
    cover = [0] * (n + 2)
    for line in lines:
        # A unit cell counts as crossed only when the line covers more than half of it, so a
        # line that overhangs a gap by a hundredth of a point does not close the gap.
        first = max(0, math.floor(line.x0 - 0.5) + 1)
        end = min(n, math.ceil(line.x1 - 0.5))
        if first < end:
            cover[first] += 1
            cover[end] -= 1
    allowed = max(1, round(GUTTER_CROSSINGS * len(lines)))
    best: tuple[float, float] | None = None
    level, start = 0, None
    for x in range(n + 1):
        level += cover[x]
        if level <= allowed:
            if start is None:
                start = x
            continue
        if start is not None:
            lo, hi = start, x
            start = None
            # The cover array works in whole points; the gap's real edges are the nearest
            # line ends on each side, so a 6 pt gutter is not measured as 5.
            left_ends = [l.x1 for l in lines if l.x1 <= lo + 0.5]
            right_starts = [l.x0 for l in lines if l.x0 >= hi - 0.5]
            if len(left_ends) < 5 or len(right_starts) < 5:
                continue
            right = [l for l in lines if l.x0 >= hi - 0.5]
            if sum(_marks_only(l.text) for l in right) > 0.5 * len(right):
                continue  # marks beside labels ("● ● ●"): a checklist's columns, not the page's
            if (not any(l.ocr for l in right)
                    and sum(_figures_only(l.text) for l in right) > FIGURE_SIDE * len(right)):
                continue  # figures and years only: more columns of one table, not a layout column
            lo_f, hi_f = max(left_ends), min(right_starts)
            mid = (lo_f + hi_f) / 2
            if _ruled_rows_cross(lines, mid, rules):
                continue  # the gap between a ruled table's stub column and its other columns
            if not any(l.ocr for l in right) and _figure_rows_cross(lines, mid):
                continue  # the gap between two of a table's columns, under their headings
            if hi_f - lo_f >= MIN_LAYOUT_GUTTER - GUTTER_ROUNDING and abs(mid - width / 2) <= CENTRAL * width:
                if best is None or abs(mid - width / 2) < abs((best[0] + best[1]) / 2 - width / 2):
                    best = (lo_f, hi_f)  # the gap nearest the middle of the page
    return best


def _ruled_rows_cross(lines: Sequence[Line], mid: float, rules: Sequence[BBox]) -> bool:
    """Whether a ruled table's rows cross `mid`: inside the box of some rules, at least
    MIN_TABLE_ROWS rows of MIN_ROW_SEGMENTS or more lines on one baseline have lines on both
    sides of it, and they hold at least half of the box's lines. The rules frame a table whose
    columns lie either side of the gap, so the gap is a column gap, not a layout gutter."""
    for box in rules:
        inside = [l for l in lines if geometry.inside(l.bbox, box, slack=2)]
        across = [row for row in _baseline_rows(inside) if len(row) >= MIN_ROW_SEGMENTS
                  and any(l.x1 <= mid for l in row) and any(l.x0 >= mid for l in row)]
        if len(across) >= MIN_TABLE_ROWS and sum(map(len, across)) >= len(inside) / 2:
            return True
    return False


def _figure_rows_cross(lines: Sequence[Line], mid: float) -> bool:
    """Whether a table's rows carry figures across `mid`, with or without ruling: at least
    MIN_TABLE_ROWS rows of MIN_ROW_SEGMENTS or more lines on one baseline have lines on both
    sides of it and only figures on its right, and they hold at least half of the lines. The
    headings over a table's columns are words, so a side's share of figures (FIGURE_SIDE) can
    miss a short table of many columns (audit p82: seven columns, the gap between the third and
    the fourth in the middle of a landscape page); its rows still cross the gap."""
    across = [row for row in _baseline_rows(lines) if len(row) >= MIN_ROW_SEGMENTS
              and any(l.x1 <= mid for l in row)
              and (right := [l for l in row if l.x0 >= mid])
              and all(_figures_only(l.text) for l in right)]
    return len(across) >= MIN_TABLE_ROWS and sum(map(len, across)) >= len(lines) / 2


def _baseline_rows(lines: Sequence[Line]) -> list[list[Line]]:
    """The lines grouped by baseline, in the order of their baselines."""
    rows: list[list[Line]] = []
    for l in sorted(lines, key=lambda l: (l.top + l.bottom, l.x0)):
        for row in rows:
            ref = row[0]
            if abs((l.top + l.bottom) - (ref.top + ref.bottom)) / 2 <= ROW_OVERLAP * max(
                    l.height, ref.height):
                row.append(l)
                break
        else:
            rows.append([l])
    return rows


# Share of a side's lines that are bare figures: it holds no words of its own. A scan's lines are
# read by OCR, whose columns the scanned-grid reader sets (see tables.scanned): they keep the gutter.
FIGURE_SIDE = 0.8


NO_FIGURE = re.compile(r"^n/a$", re.I)  # a cell that stands where a figure would


def _figures_only(text: str) -> bool:
    """Whether a line is a figure ("1,234", "$ 5", "(12)"), an "N/A" in place of one, a lone
    currency sign or a heading of the year columns ("FY 2023")."""
    words = text.split()
    return bool(words) and all(NUMBER.match(w) or NO_FIGURE.match(w) or w in CURRENCY_SIGNS
                               or w in ("FY", "FY.") for w in words)


def _marks_only(text: str) -> bool:
    words = text.split()
    return bool(words) and all(w in CELL_MARKS for w in words)


def _split_at(lines: list[Line], gutter: tuple[float, float]) -> list[list[Line]]:
    mid = (gutter[0] + gutter[1]) / 2
    return [[l for l in lines if (l.x0 + l.x1) / 2 < mid],
            [l for l in lines if (l.x0 + l.x1) / 2 >= mid]]


def _crosses_centre(line: Line, width: float) -> bool:
    """A line that runs across the whole strip where a layout gutter may lie (see CENTRAL):
    a full-width table row or caption. No gutter in that strip can lie under it."""
    return line.x0 <= width * (0.5 - CENTRAL) and line.x1 >= width * (0.5 + CENTRAL)


def _layout_regions(lines: list[Line], width: float, rules: Sequence[BBox] = ()
                    ) -> list[list[Line]]:
    """The layout columns of the page. A page-wide gutter splits it in two. Without one, a
    full-width line with no line beside it (a caption, a full-width paragraph line) ends a
    band of the page, and each band is split at the gutter found within it, if it has one.
    Full-width table rows have cells beside them, so they stay in their band; where a band
    is split, a row goes to the side its left edge is on, with its labels."""
    gutter = layout_gutter(lines, width, rules)
    if gutter is not None:
        return _split_at(lines, gutter)
    full = [l for l in lines if _crosses_centre(l, width)]
    # A full-width line of a table (a row the extractor read as one line, under a row of
    # cells) is not a band's end: it stays with its rows. A caption always ends a band.
    ends = [l for l in full if not any(o is not l and o.top < l.bottom and o.bottom > l.top
                                       for o in lines)
            and (CAPTION_START.match(l.text.strip()) or not _beside_row(l, lines))]
    if not ends:
        return [lines]
    ends_ids = {id(l) for l in ends}
    bottoms = sorted(l.bottom for l in ends)
    bands: dict[int, list[Line]] = {}
    for l in lines:
        if id(l) not in ends_ids:
            bands.setdefault(bisect.bisect_right(bottoms, l.top), []).append(l)
    # The layout columns are the same down the page: the band with most lines sets the
    # gutter, and a band whose gutter lies elsewhere is one column (a gap between a table's
    # stub labels and its figures, ResNet Table 9: the table rows are all the band holds).
    gutters = {k: layout_gutter(band, width, rules) for k, band in bands.items()}
    found = [k for k, g in gutters.items() if g is not None]
    page_mid = None
    if found:
        g = gutters[max(found, key=lambda k: len(bands[k]))]
        page_mid = (g[0] + g[1]) / 2
    regions = [ends]
    for k, band in bands.items():
        gutter = gutters[k]  # table rows across the band count as crossings
        if gutter is None or abs((gutter[0] + gutter[1]) / 2 - page_mid) > BAND_GUTTER_DRIFT:
            regions.append(band)
        else:
            mid = (gutter[0] + gutter[1]) / 2
            regions += [[l for l in band if l.x0 < mid], [l for l in band if l.x0 >= mid]]
    return regions


def _beside_row(line: Line, lines: list[Line]) -> bool:
    """Whether a row of MIN_ROW_SEGMENTS segments on one baseline lies within a line height
    above or below `line`, under its span: the line is a row of that table."""
    near = [o for o in lines if o is not line and o.x0 < line.x1 and o.x1 > line.x0
            and (0 <= line.top - o.bottom <= NEIGHBOUR_GAP * line.height
                 or 0 <= o.top - line.bottom <= NEIGHBOUR_GAP * line.height)]
    for o in near:
        same = [p for p in near if abs((p.top + p.bottom) - (o.top + o.bottom)) / 2
                <= ROW_OVERLAP * max(p.height, o.height)]
        if len(same) >= MIN_ROW_SEGMENTS:
            return True
    return False


def aligned_tables(page: int, lines: Sequence[Line], width: float = 0.0,
                   rules: Sequence[BBox] = ()) -> list[TableData]:
    """Tables found from text alignment alone: one per run of aligned rows, in each layout
    column of the page. `lines` are the page's text lines, artifacts removed; `width` is the
    page width (0 to keep the page as one layout column). `rules` are the boxes of the page's
    ruling strokes: a table of words only (no numbers) counts when rules frame it."""
    usable = [l for l in lines if l.text.strip() and not l.rotated]
    tables, held = ocr_grid_tables(page, usable)
    usable = [l for l in usable if id(l) not in held]  # the grid's lines are not read twice
    for region in _layout_regions(usable, width, rules):
        tables += _region_tables(page, region, rules, width)
    return tables


def _region_tables(page: int, lines: list[Line], rules: Sequence[BBox], width: float = 0.0
                   ) -> list[TableData]:
    lines = [l for l in split_codes(split_merged(join_signs(lines)))
             if not is_contents_entry(l.text)]  # TOC lines: prose
    cells = [c for c in (read_cell(l) for l in lines) if c is not None]
    rows = merge_centred_values(split_glued(merge_stacked(group_rows(cells))))
    units: list[Row] = []  # rows that can belong to a table, and the rows that end one
    for i, row in enumerate(rows):
        above = rows[i - 1] if i else None
        if len(row.cells) >= 2 and units and above is not None and _wraps_row(row, above):
            # A wrapped label beside values: its words and values join the row above.
            row.kind = "cont"
            units[-1].extra.extend(row.cells)
            continue
        if len(row.cells) >= 2:
            row.kind = "table"
        elif len(row.cells) == 1 and above is not None and _is_continuation(row, above):
            # A wrapped label: its text joins the cell of the row it continues.
            row.kind = "cont"
            units[-1].extra.append(row.cells[0])
            continue
        elif len(row.cells) == 1 and _is_heading(row.cells[0]):
            row.kind = "sub"  # a heading inside a table
        else:
            row.kind = "prose"
        units.append(row)
    return _runs(page, units, rules, width)


def _label(row: Row) -> Cell | None:
    """The row's label: its leftmost cell that is text, not a number (a row code is a number)."""
    return min((c for c in row.cells if not c.numeric), key=lambda c: c.x0, default=None)


def _values(row: Row) -> list[Cell]:
    """Numbers to the right of the label: the figures of the row. A row code sits left of it."""
    label = _label(row)
    return [c for c in row.cells if c.numeric and label is not None and c.x0 > label.x1]


def _wraps_row(row: Row, above: Row) -> bool:
    """A row that is the second line of the label above it, with its values beside it. Two
    shapes: a line that starts in lower case under a label (values or not), or, with values on
    this line only, a label that starts in capitals under a label-only row of the table (a
    label that wraps before its figures: "Section 514 … Revitalization" / "Modifications 8")."""
    label, above_label = _label(row), _label(above)
    if label is None or above_label is None or LIST_LABEL.match(label.text):
        return False  # "b. So-called trust …" is a list item, not a wrapped label
    if above.kind not in ("table", "sub", "cont"):
        return False
    if abs(label.x0 - above_label.x0) > LABEL_GROUP:
        return False
    gap = row.top - above.bottom
    if gap > 0.6 * max(label.height, above.height):
        return False
    if label.text[:1].islower():
        if _heads_same_columns(row, above):
            return False  # a header line over a header line: its cells sit in the same columns
        # A lower-case line continues a label. Under a row with figures, only a label of its own
        # does: a line with figures of its own is the next row ("ours (ILSVRC'15)" 8.9 9.0).
        return not _values(above) or not _values(row)
    # Capitals: only under a label-only table row (a row code and a label, no figures), when
    # this line has the figures and no row code of its own (a code starts a new row), and the
    # first line does not end a clause.
    if above.kind != "table" or any(c.x0 > above_label.x1 for c in above.cells) \
            or not _values(row):
        return False  # the row above has no cell beside its label
    if any(c.numeric and c.x0 < label.x0 for c in row.cells):
        return False
    return not above_label.text.rstrip().endswith((":", ".", ";"))


def _heads_same_columns(row: Row, above: Row) -> bool:
    """Both rows carry cells beyond their labels, and a cell of this row shares its horizontal
    span with a cell of the row above: two header lines of one table, not a wrapped label."""
    label, above_label = _label(row), _label(above)
    beyond = [c for c in row.cells if c is not label]
    above_beyond = [c for c in above.cells if c is not above_label]
    return any(min(c.x1, a.x1) - max(c.x0, a.x0) > 0 for c in beyond for a in above_beyond)


def _is_heading(cell: Cell) -> bool:
    """A label on a line of its own inside a table: a few words, no sentence. Captions and
    sentences are prose, which ends a table."""
    text = cell.text
    words = len(text.split())
    if cell.numeric or words > MAX_HEADING_WORDS or CAPTION_START.match(text):
        return False
    return not (text.endswith(".") and words > 2)


def _is_continuation(row: Row, prev: Row) -> bool:
    """A single text cell right under a row, close enough to be its wrapped text: it starts in
    lower case, or the row above was cut off mid-phrase (no closing punctuation)."""
    cell = row.cells[0]
    gap = row.top - prev.bottom
    if cell.numeric or gap > 0.6 * max(cell.height, prev.height) or cell.text.endswith(":"):
        return False  # a heading ("Outlays, gross:") is a row of its own
    if prev.kind not in ("table", "sub", "cont") or cell.text[:1].isdigit():
        return False  # a new numbered or coded row is never wrapped text
    if all(c.numeric for c in prev.cells):
        return False  # a row of figures or marks alone ("18 30 40") has no label to wrap
    if cell.text[:1] in "*†‡§":
        return False  # "*Recommended for some women …": the note under the table
    values = _values(prev)
    if values and cell.x1 > min(v.x0 for v in values):
        return False  # a wrapped label stays left of the figures: this line is a footnote
    if cell.text[:1].islower():
        return True
    label = max((c for c in prev.cells if not c.numeric), key=lambda c: len(c.text),
                default=prev.cells[0])
    return not label.text.rstrip().endswith((":", ".", ")", ";"))


def _runs(page: int, units: list[Row], rules: Sequence[BBox], width: float = 0.0
          ) -> list[TableData]:
    """Split the rows into runs: consecutive rows with no prose between them and no large gap.
    Each run is one candidate table, with its own columns."""
    runs: list[list[Row]] = []
    current: list[Row] = []
    prev: Row | None = None
    for row in units:
        if row.kind == "prose":
            if current:
                runs.append(current)
            current, prev = [], None
            continue
        if prev is not None and row.top - prev.bottom > MAX_ROW_GAP * max(row.height, prev.height):
            if current:
                runs.append(current)
            current = []
        if _is_schedule_header(row) and sum(r.kind == "table" for r in current) >= 2:
            runs.append(current)  # a new schedule, with its own columns
            current = []
        current.append(row)
        prev = row
    if current:
        runs.append(current)
    return [t for run in runs if (t := _table(page, run, rules, width)) is not None]


def _is_schedule_header(row: Row) -> bool:
    """The heading row of a schedule: "Identification code …" and the year labels above its
    columns. A schedule is one table, so a new heading starts a new one."""
    return not any(c.numeric for c in row.cells) and any(
        SCHEDULE_HEAD.search(c.text) for c in row.cells)


def _coded_rows(run: list[Row]) -> int:
    """Table rows that open with a budget line code ("234011 Guaranteed …", "3510 Budget
    authority …"): a schedule continued from the page before, whose column headings are not on
    this page. The code is the leftmost cell of the row, so a figure further right is not one."""
    count = 0
    for row in run:
        if row.kind == "table" and CODE_CELL.match(min(row.cells, key=lambda c: c.x0).text):
            count += 1
    return count


def _heading_rows(head: list[Row], columns: list[Column]) -> list[dict[int, list[Cell]]]:
    """Label-only lines above a table's first row, as header rows of its grid. They are header
    rows only when one of them spans two or more columns ("top-5 localization err" over the
    val and test columns). Lines that share no column share a header row ("method" beside it).
    A column a heading spans, but does not start, maps to no cells: a colspan."""
    placed: list[list[tuple[Cell, list[int]]]] = []
    for row in head:
        line: list[tuple[Cell, list[int]]] = []
        for cell in row.cells + row.extra:  # a heading's second line joins it as a continuation
            # A sentence above the table (running text that ends a paragraph) is not a heading.
            if len(cell.text.split()) > HEADING_MAX_WORDS or SENTENCE_BREAK.search(cell.text):
                return []
            over = [i for i, col in enumerate(columns)
                    if min(cell.x1, col.hi) - max(cell.x0, col.lo) > 0]
            if len(over) < 2:
                k = assign_column(cell, columns)
                if k is None:
                    return []
                over = [k]
            line.append((cell, over))
        placed.append(line)
    if not any(len(over) >= 2 for line in placed for _, over in line):
        return []
    rows: list[dict[int, list[Cell]]] = []
    current: dict[int, list[Cell]] = {}
    for line in placed:
        keys = {k for _, over in line for k in over}
        if current and keys & set(current):
            rows.append(current)
            current = {}
        for cell, over in line:
            current[over[0]] = [cell]
            for k in over[1:]:
                current[k] = []
    if current:
        rows.append(current)
    return rows


def _table(page: int, run: list[Row], rules: Sequence[BBox], width: float = 0.0) -> TableData | None:
    # A heading ending in a colon ("Total:") over the figures labels them.
    lead_label = bool(run) and run[0].kind == "sub" and run[0].cells[0].text.endswith(":")
    head: list[Row] = []  # label-only lines above the first row of the table
    while run and run[0].kind == "sub":
        head.append(run[0])
        run = run[1:]
    while run and run[-1].kind == "sub":
        run = run[:-1]
    words_only = not any(c.numeric for r in run for c in r.cells)
    if words_only and any(r.kind == "sub" for r in run):
        return None  # a line of its own inside a block of words: a list or a layout, not a table
    table_rows = sum(r.kind == "table" for r in run)
    # A row of year labels over the figures ("2023 actual  2024 est.  2025 est.") heads a
    # schedule: one row of figures under it is a table, and each year column needs one figure.
    schedule = bool(run) and run[0].kind == "table" and sum(
        bool(YEAR_LABEL.match(c.text.strip())) for c in run[0].cells) >= 2
    # Figures under a heading with a colon ("Total:", "Offsets against gross budget authority
    # and outlays:") are a table: the heading labels them, so they need no year labels above
    # them (a schedule that continues into the next column has none).
    short_block = lead_label and table_rows == 2 and len(run) == 2
    if len(run) < (2 if short_block or schedule else MIN_TABLE_ROWS) or table_rows < 2:
        return None
    if words_only and len(run) < MIN_WORD_TABLE_ROWS:
        return None  # a short block of words in two columns is a layout, not a table
    # Figures on a wrapped line of a row (its extra cells) define columns too: a label that
    # wraps before its figures puts them on the wrapped line.
    columns = find_columns([c for r in run if r.kind == "table"
                        for c in r.cells + [e for e in r.extra if e.numeric]],
                       years=[c for c in run[0].cells if YEAR_LABEL.match(c.text.strip())]
                       if schedule else ())
    if schedule and columns:
        # The stub heading of a schedule ("Identification code …") labels the rows under it: a
        # column of its own on the left, where the row labels sit. A figure is no label, so the
        # stub's column does not need a second cell to repeat its edge.
        first_figure = min((col.lo for col in columns if col.kind == "num"), default=None)
        for cell in run[0].cells:
            if cell.numeric or YEAR_LABEL.match(cell.text.strip()) or first_figure is None \
                    or cell.x1 > first_figure or assign_column(cell, columns) is not None:
                continue
            columns.append(Column("text", [cell.x0], [cell]))
        columns.sort(key=lambda col: col.lo)
    # A number column is a column of figures: filled in a fair share of the rows. Stray
    # numbers at a line end (footnotes, years, list numbers) don't make one.
    need = MIN_NUMBER_CELLS if short_block else max(MIN_NUMBER_CELLS + 1,
                                                   MIN_NUMBER_SHARE * table_rows)
    # A sparse number column is kept when the heading row labels it (a year with few values).
    heading = [c for c in run[0].cells if not c.numeric] if not any(
        c.numeric for c in run[0].cells) else []
    columns = [c for c in columns if c.kind == "text" or len(c.cells) >= need
               or any(h.x1 >= c.lo - HEADER_TOL and h.x0 <= c.hi + HEADER_TOL for h in heading)]
    if not columns:
        return None
    placed = _heading_rows(head, columns)
    # The rows above the first row with a figure in it are the table's headings.
    first_figures = next((i for i, r in enumerate(run) if any(c.numeric for c in r.cells)), len(run))
    for ri, row in enumerate(run):
        by: dict[int, list[Cell]] = {}
        spanning: list[Cell] = []
        for cell in row.cells + row.extra:
            k = assign_column(cell, columns)
            if k is not None:
                by.setdefault(k, []).append(cell)
            elif any(ch.isalnum() for ch in cell.text):
                spanning.append(cell)
        # A heading over several columns in a row of headings ("COCO train" over "@.5" and
        # "@[.5, .95]") spans the columns it covers, when nothing else in the row is in them.
        # Any other text in no column would vanish from the page: the run stays prose.
        for cell in spanning:
            over = [i for i, col in enumerate(columns)
                    if min(cell.x1, col.hi) - max(cell.x0, col.lo) > 0]
            if len(over) == 1 and over[0] not in by and columns[over[0]].kind == "num" and (
                    ri < first_figures or NO_FIGURE.match(cell.text.strip())):
                by[over[0]] = [cell]  # a heading centred over its figures, or an "N/A" among them
                continue
            if len(over) < 2 or any(c.numeric for c in row.cells) or any(k in by for k in over):
                return None
            by[over[0]] = [cell]
            for k in over[1:]:
                by[k] = []
        placed.append(by)
    used = sorted({k for by in placed for k in by})
    cols = [columns[k] for k in used]
    if len(cols) < 2:
        return None  # one column of values is a list
    numeric = any(col.kind == "num" for col in cols)
    text_cols = sum(col.kind == "text" for col in cols)
    if not numeric and text_cols < 2:
        return None  # a single run of labels is a list, not a table

    index = {k: i for i, k in enumerate(used)}
    grid: list[list[str | None]] = []
    boxes: list[BBox] = []
    for by in placed:
        line: list[str | None] = [""] * len(used)
        for k, cs in by.items():
            # No cells: a column a heading spans. None marks it merged into the cell on its left.
            line[index[k]] = " ".join(c.text for c in sorted(cs, key=lambda c: c.x0)) if cs else None
            boxes += [c.line.bbox for c in cs]
        grid.append(line)
    if _looks_like_prose(run, grid) or _looks_like_contacts(grid) or _looks_like_index(grid) \
            or _looks_like_bullets(grid) or _looks_like_merged_values(grid):
        return None
    cells_total = sum(len(line) for line in grid)
    if cells_total and sum(1 for line in grid for t in line if t) < MIN_FILL * cells_total:
        return None  # mostly empty cells: a layout of text blocks, not a table
    words = [len(t.split()) for line in grid for t in line if t and not looks_numeric(t)]
    if words and sum(words) / len(words) > MAX_CELL_WORDS:
        return None  # long sentences in columns: running text, not a table
    # A label column of sentences is running text beside the table, even when its figures
    # sit in the rows (a paragraph's lines share the rows of a table on the right).
    # A numbered or lettered item ("6. Sole proprietorship …") is a list entry, not a sentence.
    labels = [line[0].split() for line in grid if line[0] and not looks_numeric(line[0])
              and not LIST_LABEL.match(line[0]) and not LIST_NUMBER.match(line[0])]
    if cols[0].kind == "text" and labels and sum(map(len, labels)) / len(labels) > MAX_CELL_WORDS:
        return None
    header_rows = 0
    # In a grid of marks the figures over the columns are headings too ("18 30 40" over "●").
    marked = any(t in CELL_MARKS for line in grid for t in line if t)
    for line in grid:  # year labels over the numbers: rows with no number, above the body
        if any(line[i] and cols[i].kind == "num" for i in range(len(cols))) \
                and not any(looks_numeric(v) and not YEAR_CELL.match(v) or v in CELL_MARKS
                            for v in line):
            header_rows += 1
        elif marked and not line[0] and all(not v or looks_numeric(v) for v in line[1:]):
            header_rows += 1
        else:
            break
    if header_rows == 0 and grid and not any(looks_numeric(v) for v in grid[0]):
        header_rows = 1
    labelled = any(grid[0][i] and cols[i].kind == "num" for i in range(len(cols))) if grid else False
    coded = _coded_rows(run)
    headless_ok = (coded >= MIN_CODED_ROWS and coded >= MIN_CODED_SHARE * table_rows
                   and not any(YEAR_HEADING_ANY.search(t) for line in grid for t in line if t))
    # Two schedules side by side are never one table: a header-less schedule lies in one column.
    headless_ok = headless_ok and not (width and min(b[0] for b in boxes) < width / 2
                                       < max(b[2] for b in boxes))
    # With no year headings, a figure's year is known only when every year has its column: a
    # schedule with an empty year column would read its figures one year off.
    years = sum(col.kind == "num" and not all(CODE_CELL.match(c.text) for c in col.cells)
                for col in cols)
    headless_ok = headless_ok and years >= MIN_HEADLESS_YEARS
    if numeric and not lead_label and (header_rows == 0 or not labelled) and not headless_ok:
        return None  # figures with no label above them: not a table of figures
    bbox = (round(min(b[0] for b in boxes), 2), round(min(b[1] for b in boxes), 2),
            round(max(b[2] for b in boxes), 2), round(max(b[3] for b in boxes), 2))
    # A backdrop that bleeds off both edges of the page (a tinted panel, with the dividers drawn
    # on it merged into one drawing) frames no table.
    framed = any(geometry.overlap(bbox, r) >= 0.5 * geometry.area(bbox) for r in rules
                 if not (width and r[0] < 0 and r[2] > width))
    if not numeric and not framed:
        return None  # words in columns with no rules around them: a directory, not a table
    return TableData(page, bbox, grid, method=TableMethod.ALIGNED, header_rows=header_rows, cells=boxes,
                     why=_describe(grid, cols, header_rows))


def _looks_like_prose(run: list[Row], grid: list[list[str | None]]) -> bool:
    """Columns of running text: most rows start mid-sentence (lower case), or the cells are
    fragments and stray marks. Labels and figures in a table don't look like that."""
    labels: list[str] = []
    lower: list[bool] = []  # a label that starts mid-sentence, on a row with no figures of its own
    body = [r for r in run if r.kind == "table"]
    for row in body[1:] if body and all(not c.numeric for c in body[0].cells) else body:
        texts = [c.text for c in row.cells if not c.numeric]  # the header row is not a label
        if texts:
            labels.append(texts[0])
            # A wrapped label is a single cell: a line of words over columns is a header line.
            lower.append(len(row.cells) == 1 and not _values(row))
    wrapped = sum(1 for t, bare in zip(labels, lower)
                  if bare and t[:1].islower() and not LIST_LABEL.match(t))
    if labels and wrapped > MAX_WRAPPED_SHARE * len(labels):
        return True
    cells = [t for line in grid for t in line if t]
    junk = sum(1 for t in cells if not NUMBER.match(t) and t not in CELL_MARKS
               and sum(ch.isalnum() for ch in t) < 2)
    return bool(cells) and junk > MAX_JUNK_SHARE * len(cells)


# Contact and provider text: phone numbers, ZIP codes after a state, credentials in brackets,
# web addresses. A directory of these is a list of listings, not a table.
CONTACT_CELL = re.compile(
    r"\(\d{3}\)\s?\d{3}-\d{4}|\b[A-Z]{2} \d{5}\b|\bwebsite:|\blanguage:|\bwww\.|"
    r"\.(com|org|gov)\b|\b(MD|DO|ANP|GNP|FNP|PA|NP|DDS|PhD|PharmD|RN)\s?\([FM]\)",
    re.I)


MAX_CONTACT_SHARE = 0.08  # share of a table's cells that may be contact text


def _looks_like_contacts(grid: list[list[str | None]]) -> bool:
    cells = [t for line in grid for t in line if t]
    hits = sum(1 for t in cells if CONTACT_CELL.search(t))
    return hits >= 2 and hits > MAX_CONTACT_SHARE * len(cells)


INDEX_LEADER = re.compile(r"\.\s?\.\s?\.")  # a dot leader left inside a cell: an index entry


def _looks_like_index(grid: list[list[str | None]]) -> bool:
    """An index or listing: entries with dot leaders and page numbers. Leaders at the end of a
    label are cut before the cell is built, so a leader still in a cell is an entry, not data."""
    return any(t and INDEX_LEADER.search(t) for line in grid for t in line)


def _looks_like_merged_values(grid: list[list[str | None]]) -> bool:
    """A cell that holds several figures and nothing else ("41.5 48.4 49.9 51.1 53.8"): the
    columns of a row were read as one line, so the table's structure is not known."""
    for line in grid:
        for t in line:
            words = t.split() if t else []
            if len(words) >= 3 and all(NUMBER.match(w) for w in words):
                return True
    return False


def _looks_like_bullets(grid: list[list[str | None]]) -> bool:
    """Bulleted items in the cells: a list of sentences beside other text, not a table."""
    return sum(1 for line in grid for t in line if t and t.startswith("•")) >= 2


def _describe(grid: list[list[str | None]], cols: list[Column], header_rows: int) -> str:
    edges = [col.edges[-1] for col in cols if col.kind == "num"]
    right = f"; numbers right-aligned at x {', '.join(f'{e:g}' for e in edges)}" if edges else ""
    headers = f"{header_rows} header row{'' if header_rows == 1 else 's'}"
    return f"aligned columns: {len(grid)} rows × {len(cols)} columns, {headers}{right}"
