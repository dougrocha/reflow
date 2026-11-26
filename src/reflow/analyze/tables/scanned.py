"""Grids of scanned words: tables on OCR'd pages.

A scanned table has no vector rules and no figures to frame it, and its OCR rows are skewed:
the cells of one row sit on baselines a few points apart, and the rows pack tighter than the
lines are tall. Rows from baselines alone then split or fuse. The first column of such a
table is a stub of labels that repeat on one left edge, one per row: each stub label anchors
a row, and every other cell joins the row of the nearest label. A header, which may be split
over several lines, sits just above the first row.
"""

from __future__ import annotations

import bisect
import math
import re
from statistics import median
from collections.abc import Iterable, Sequence
from dataclasses import replace

from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.analyze.tables.cells import LEFT_TOL, Cell, ink, read_cell
from reflow.extract.tables.cells import TableData, TableMethod

MAX_CELL_WORDS = 8  # average words in a label cell of an unruled table: more is running text



MIN_GRID_ROWS = 3  # stub labels (rows) that make a grid of scanned words


MIN_GRID_COLS = 4  # columns, the stub included


GRID_FILL = 0.7  # share of the body cells that hold text


GRID_COLUMN_SHARE = 0.6  # a column of the grid has a cell in this share of the rows


GRID_HEADER_GAP = 1.0  # header lines closer than this many line heights are one header


GRID_BODY_GAP = 1.6  # ... and the header lies this close above the first row


GRID_ROW_SPLIT = 0.4  # a clear band this tall (line heights) between lines is a header-row break


GRID_SKEW = 0.6  # a column's cells sit this close (line heights, on average) to their row's label


GRID_SENTENCES = 0.3  # share of the cells that may be sentences before the grid is prose


MAX_GRID_ROWS = 80  # a stub longer than this is not read (keeps the work per page small)


MAX_GRID_STUBS = 12  # stub columns tried on one page


RULE_TAIL = re.compile(r"^(.*_)\s+([^\s_][^_]{0,13})$")  # "foot ____ ft": a cell, a rule, a cell


SENTENCE_END = re.compile(r"\w[.!?]$")


SECTION_TITLE = re.compile(r"^\d+[.)]\s")


GRID_EMPTY_ROW = 0.5  # cost of a row a column leaves empty, in row pitches


GRID_FAR_ROWS = 3.0  # an end label this many pitches from the rest is not a row


GRID_COLUMN_TOL = 6.0  # cell left edges this close are one column


GRID_LABELLED = 0.6  # share of the columns the header must name


MAX_GRID_HEADER_WORDS = 4  # a header cell is a label, not a sentence


HYPHEN_END = re.compile(f"[-{SOFT_HYPHEN}]$")


RULE_RUNS = re.compile(r"\s*(?:_\s*){2,}|\s*-{3,}\s*")  # leader fragments left inside a cell


def ocr_grid_tables(page: int, lines: Sequence[Line]) -> tuple[list[TableData], set[int]]:
    """Tables of words found in scanned text (OCR lines) by their stub column, with the ids of
    the lines they hold. Only OCR text is read this way: a text layer that was typeset has
    its own alignment to go by, and aligned words there are prose until a rule frames them."""
    origin: dict[int, int] = {}  # a piece cut from a fused line → the line it came from
    work: list[Line] = []
    for l in lines:
        pieces = _cut_rule_tail(l) if l.ocr and not l.rotated else [l]
        origin.update({id(p): id(l) for p in pieces})
        work += pieces
    cells = [c for l in work if l.ocr and not l.rotated and (c := read_cell(l)) is not None
             and any(ch.isalnum() for ch in c.text)]
    if len(cells) < MIN_GRID_ROWS * MIN_GRID_COLS:
        return [], set()
    tables: list[TableData] = []
    held: set[int] = set()
    for stubs in stub_columns(cells):
        free = [c for c in cells if id(c.line) not in held]
        stubs = [s for s in stubs if id(s.line) not in held]
        if len(stubs) < MIN_GRID_ROWS:
            continue
        found = _ocr_grid(page, stubs, free, work)
        if found is not None:
            table, used = found
            tables.append(table)
            held |= {origin[i] for i in used}
    return tables, held


def _cut_rule_tail(line: Line) -> list[Line]:
    """An OCR line that ends in a short token after a rule of underscores ("foot (or mile)
    _____ ____ ft (or mi)") is two cells: the scan's cell rule and then the next cell. The
    cut is by the text's width. The line is cut only for the grid reading; a line that ends
    up in no grid stays whole in the text."""
    m = RULE_TAIL.match(line.text.strip())
    if m is None or len(m[2].split()) > 3:
        return [line]
    run = re.search(r"[_\s]+$", m[1])
    if run is None or run[0].count("_") < 3:
        return [line]
    text = line.text.strip()
    x = line.x0 + (line.x1 - line.x0) * ink(m[1] + " ") / max(ink(text), 1e-6)
    return [replace(line, bbox=(line.x0, line.top, round(x, 2), line.bottom), text=m[1]),
            replace(line, bbox=(round(x, 2), line.top, line.x1, line.bottom), text=m[2])]


def stub_columns(cells: list[Cell]) -> list[list[Cell]]:
    """Groups of text cells on one left edge, at least MIN_GRID_ROWS of them, left to right."""
    text = sorted((c for c in cells if not c.numeric), key=lambda c: c.x0)
    groups: list[list[Cell]] = []
    for c in text:
        if groups and c.x0 - groups[-1][-1].x0 <= LEFT_TOL:
            groups[-1].append(c)
        else:
            groups.append([c])
    # Few stub columns are tried, the longest first, so a page of scattered text stays cheap.
    big = sorted((g for g in groups if len(g) >= MIN_GRID_ROWS), key=len, reverse=True)
    return [sorted(g, key=lambda c: c.cy)
            for g in sorted(big[:MAX_GRID_STUBS], key=lambda g: g[0].x0)]


def _joined(cells: Iterable[Cell]) -> str:
    """The text of cells read top to bottom, a word cut at a line end ("Abbrevia-" over "tion")
    mended, leader fragments dropped."""
    text = ""
    for c in sorted(cells, key=lambda c: (round(c.cy / 4), c.x0)):
        part = RULE_RUNS.sub(" ", c.text).strip()
        if not part:
            continue
        if text and HYPHEN_END.search(text) and part[:1].islower():
            text = HYPHEN_END.sub("", text) + part
        else:
            text = f"{text} {part}".strip()
    return text


def _trim_stubs(stubs: list[Cell]) -> list[Cell]:
    """A title or a note on the stub's left edge, far above or below the labels (more than
    GRID_FAR_ROWS times their usual pitch, and several lines), is not one of them."""
    stubs = list(stubs)
    while len(stubs) > MIN_GRID_ROWS:
        for end, near in ((0, 1), (-1, -2)):
            rest = stubs[1:] if end == 0 else stubs[:-1]
            pitch = [b.cy - a.cy for a, b in zip(rest, rest[1:])]
            gap = abs(stubs[near].cy - stubs[end].cy)
            if gap > GRID_FAR_ROWS * max(median(pitch), stubs[end].height):
                del stubs[end]
                break
        else:
            return stubs
    return stubs


def _ocr_grid(page: int, stubs: list[Cell], cells: list[Cell], lines: Sequence[Line]
              ) -> tuple[TableData, set[int]] | None:
    stubs = _trim_stubs(stubs)
    if len(stubs) > MAX_GRID_ROWS:
        return None
    height = median([c.height for c in stubs])
    pitch = [b.cy - a.cy for a, b in zip(stubs, stubs[1:])]
    if min(pitch) < 0.6 * height:
        return None  # labels that overlap vertically are not one per row
    step = min(median(pitch), 2.5 * height)
    bounds = [(a.cy + b.cy) / 2 for a, b in zip(stubs, stubs[1:])]
    first, last = stubs[0].cy - 0.5 * step, stubs[-1].cy + 0.75 * step
    edge = max(s.x0 for s in stubs) + LEFT_TOL
    stub_ids = {id(s.line) for s in stubs}
    body = [c for c in cells if id(c.line) not in stub_ids and c.x0 > edge
            and first <= c.cy <= last]
    if not body:
        return None
    # The columns right of the stub: cells on one left edge, in a fair share of the rows.
    body.sort(key=lambda c: c.x0)
    groups: list[list[Cell]] = []
    for c in body:
        if groups and c.x0 - groups[-1][-1].x0 <= GRID_COLUMN_TOL:
            groups[-1].append(c)
        else:
            groups.append([c])
    need = max(MIN_GRID_ROWS, math.ceil(GRID_COLUMN_SHARE * len(stubs)))
    columns = [g for g in groups
               if len({bisect.bisect(bounds, c.cy) for c in g}) >= need]
    if len(columns) + 1 < MIN_GRID_COLS:
        return None
    spans = [(min(s.x0 for s in stubs), median([s.x1 for s in stubs]))] + [
        (median([c.x0 for c in g]), median([c.x1 for c in g])) for g in columns]
    starts = [lo for lo, _ in spans]

    def column_of(x0: float) -> int:  # the last column that starts at or left of the cell
        return max(0, bisect.bisect(starts, x0 + GRID_COLUMN_TOL) - 1)

    grid: list[list[list[Cell]]] = [[[] for _ in spans] for _ in stubs]
    for i, s in enumerate(stubs):
        grid[i][0].append(s)
    by_column: list[list[Cell]] = [[] for _ in spans]
    for c in body:
        by_column[column_of(c.x0)].append(c)
    if by_column[0]:
        return None  # text between the stub and its first column belongs to no cell
    for k in range(1, len(spans)):
        placed = _place_column(by_column[k], stubs, step)
        if placed is None:
            return None  # a cell far from every row, or rows it cannot fill: the rows are not read
        if placed and sum(abs(c.cy - stubs[i].cy) for c, i in placed) / len(placed) \
                > GRID_SKEW * height:
            return None  # the column does not follow the rows of the stub
        for c, i in placed:
            grid[i][k].append(c)
    rows: list[list[str | None]] = [[_joined(cs) for cs in row] for row in grid]
    filled = sum(1 for row in rows for t in row if t)
    if filled < GRID_FILL * len(rows) * len(spans):
        return None  # mostly empty: text blocks side by side, not a table
    words = [len(t.split()) for row in rows for t in row if t]
    if sum(words) / len(words) > MAX_CELL_WORDS:
        return None
    full = [t for row in rows for t in row if t]
    if sum(1 for t in full if SENTENCE_END.search(t) and len(t.split()) >= 2) \
            > GRID_SENTENCES * len(full):
        return None  # cells that end like sentences: columns of prose

    head = _grid_header(cells, grid, spans, edge - LEFT_TOL, height)
    if head is None:
        return None
    header_rows, header_cells = head
    body_cells = [c for row in grid for cs in row for c in cs]
    used = {id(c.line) for c in body_cells + header_cells}
    boxes = [c.line.bbox for c in body_cells + header_cells]
    bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
    for l in lines:  # a line with text inside the box that no cell holds would vanish
        cx, cy = (l.x0 + l.x1) / 2, (l.top + l.bottom) / 2
        if id(l) not in used and bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3] \
                and any(ch.isalnum() for ch in l.text):
            return None
    grid_rows = header_rows + rows
    why = (f"scanned words in columns: {len(grid_rows)} rows × {len(spans)} columns, "
           f"{len(header_rows)} header rows, rows anchored on the labels at x {starts[0]:g}")
    return (TableData(page, tuple(round(v, 2) for v in bbox), grid_rows, method=TableMethod.ALIGNED,
                      header_rows=len(header_rows), cells=boxes, why=why), used)


def _place_column(cells: list[Cell], stubs: list[Cell], reach: float
                  ) -> list[tuple[Cell, int]] | None:
    """The row of each cell of one column, in order down the column, each within `reach` of
    its row's label. Skew moves a cell off its row's baseline, so the nearest label is not
    always its row; the order down the column is the surer guide. Rows never run backwards
    down a column, and each row left empty costs GRID_EMPTY_ROW of a row pitch, so a cell
    that sits between two rows goes where it leaves no gap. None when no assignment keeps
    every cell within reach: the rows cannot be told apart, and the table is not read."""
    cells = sorted(cells, key=lambda c: c.cy)
    n, r = len(cells), len(stubs)
    if n == 0:
        return []
    inf = math.inf
    gap = GRID_EMPTY_ROW * reach

    def cost(i: int, j: int) -> float:
        d = abs(cells[i].cy - stubs[j].cy)
        return d if d <= reach else inf

    best = [[inf] * r for _ in range(n)]
    came = [[-1] * r for _ in range(n)]
    for j in range(r):
        best[0][j] = cost(0, j) + gap * j
    for i in range(1, n):
        for j in range(r):
            here = cost(i, j)
            if here == inf:
                continue
            for pj in range(j + 1):
                skipped = j - pj - 1 if pj < j else 0
                total = best[i - 1][pj] + here + gap * skipped
                if total < best[i][j]:
                    best[i][j], came[i][j] = total, pj
    j = min(range(r), key=lambda j: best[n - 1][j] + gap * (r - 1 - j))
    if best[n - 1][j] == inf:
        return None
    rows = [0] * n
    for i in range(n - 1, -1, -1):
        rows[i] = j
        j = came[i][j]
    return list(zip(cells, rows))


def _grid_header(cells: list[Cell], grid: list[list[list[Cell]]],
                 spans: list[tuple[float, float]], left: float, height: float
                 ) -> tuple[list[list[str | None]], list[Cell]] | None:
    """The header lines above the first row, as header rows: lines that touch or nearly touch
    in a chain upward from the first row, each short and over the columns. Lines are cut into
    rows where a clear band lies between them; a line of a row that is not the last spans from
    its column to the one before the next line's (a group heading, "Metric" over its unit and
    abbreviation); the last row names the columns, the lines of one column joined. None when
    that row names too few columns."""
    held = {id(c.line) for row in grid for cs in row for c in cs}
    first = min(c.top for cs in grid[0] for c in cs)
    right = max(c.x1 for row in grid for cs in row for c in cs) + 3 * LEFT_TOL
    # A header may run past its column, up to where the next one starts.
    starts = [lo for lo, _ in spans]
    reach = list(zip(starts, starts[1:] + [right]))
    above = sorted((c for c in cells if id(c.line) not in held and c.bottom <= first + 0.3 * c.height
                    and c.cx >= left and c.x0 <= right
                    and any(c.x1 >= lo - LEFT_TOL and c.x0 <= hi for lo, hi in reach)),
                   key=lambda c: -c.bottom)
    chain: list[Cell] = []
    top = first
    for c in above:
        limit = GRID_BODY_GAP if not chain else GRID_HEADER_GAP
        if top - c.bottom > limit * max(c.height, height) \
                or len(c.text.split()) > MAX_GRID_HEADER_WORDS \
                or (SENTENCE_END.search(c.text) and len(c.text.split()) >= 2) \
                or SECTION_TITLE.match(c.text):
            break  # a sentence or a numbered title ends the header
        chain.append(c)
        top = min(top, c.top)
    if not chain:
        return None
    chain.sort(key=lambda c: (c.top, c.x0))
    bands: list[list[Cell]] = []
    reach = 0.0
    for c in chain:
        if bands and c.top - reach < GRID_ROW_SPLIT * c.height:
            bands[-1].append(c)
            reach = max(reach, c.bottom)
        else:
            bands.append([c])
            reach = c.bottom

    def column_at(c: Cell) -> int:
        def gap(k: int) -> float:
            lo, hi = spans[k]
            return max(lo - c.cx, c.cx - hi, 0.0)
        return min(range(len(spans)), key=lambda k: (gap(k), abs(c.cx - sum(spans[k]) / 2)))

    out: list[list[str | None]] = []
    for n, band in enumerate(bands):
        row: list[str | None] = [""] * len(spans)
        by: dict[int, list[Cell]] = {}
        for c in band:
            by.setdefault(column_at(c), []).append(c)
        ks = sorted(by)
        for i, k in enumerate(ks):
            row[k] = _joined(by[k])
            if n < len(bands) - 1:  # a group heading spans up to the next heading's column
                end = ks[i + 1] if i + 1 < len(ks) else len(spans)
                for j in range(k + 1, end):
                    row[j] = None
        out.append(row)
    if sum(1 for t in out[-1] if t) < GRID_LABELLED * len(spans):
        return None
    return out, chain
