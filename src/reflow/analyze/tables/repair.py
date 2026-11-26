"""Ruled tables put right against the page's text lines (the analysis calls these, after the
aligned tables are found): a ruled table that lost lines is read again or takes them back, an
aligned table that holds more of the page replaces it, and cell words take the case they have
in the text lines.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import replace

from reflow import geometry
from reflow.extract.lines import Line
from reflow.extract.tables.cells import TableData
from reflow.analyze.tables.rows import split_merged
from reflow.model import BBox
from reflow.text_quality import squash

MIN_LOSS_SHARE = 0.1  # a ruled table that loses this share of its lines is read again


def merge_tables(ruled: list[TableData], aligned: list[TableData],
                 lines: Sequence[Line] = ()) -> list[TableData]:
    """Ruled tables, with aligned tables where they add to them. An aligned table that
    covers one ruled table and is clearly larger (a partly ruled table, its other columns
    unruled) replaces it. So does one that holds more of the text lines inside the ruled
    table than the ruled table's own cells do (`lines` are the page's text lines: a ruled
    grid whose cells came out empty or garbled). Otherwise the ruled table wins where the
    two overlap."""
    kept = list(ruled)
    for a in aligned:
        overlapping = [r for r in kept if geometry.overlap(a.bbox, r.bbox) > 0]
        if not overlapping:
            kept.append(a)
            continue
        # Larger is a partly ruled table completed. Over two ruled tables it is the two run
        # together (the BlueValue and Standard rates, one above the other): they stay two.
        larger = len(overlapping) == 1 and all(
            geometry.area(a.bbox) >= 1.2 * geometry.area(r.bbox)
            and geometry.overlap(a.bbox, r.bbox) >= 0.8 * geometry.area(r.bbox) for r in overlapping)
        if larger or (lines and all(_holds_more(a, r, lines) for r in overlapping)):
            kept = [r for r in kept if r not in overlapping] + [a]
    return sorted(kept, key=lambda t: (t.bbox[1], t.bbox[0]))


def _holds_more(aligned: TableData, ruled: TableData, lines: Sequence[Line]) -> bool:
    """Whether the aligned table holds more of the text lines in the ruled table's box than
    the ruled table does, and covers most of the box."""
    if not ruled.grid or geometry.overlap(aligned.bbox, ruled.bbox) < 0.8 * geometry.area(ruled.bbox):
        return False
    inside = [l for l in lines if geometry.inside(l.bbox, ruled.bbox)]
    lost = len(unheld_lines(ruled, inside))
    theirs = sum(1 for l in inside if any(geometry.overlap(l.bbox, c) >= 0.5 * geometry.area(l.bbox)
                                          for c in aligned.cells))
    return theirs > len(inside) - lost


def repair_ruled(table: TableData, lines: Sequence[Line], glyphs: Sequence[Line] = ()
                 ) -> TableData:
    """A ruled table with what its cells lost put back, as far as it can be: the lines no cell
    holds are added to empty cells (adopt_unheld); a table that loses a large share of its
    lines is filled again from the lines (refill_ruled), if that holds more. `lines` are the
    page's text lines, `glyphs` further marks (dashes) that a cell may hold."""
    lost = unheld_lines(table, lines)
    if not lost:
        return table
    best = adopt_unheld(table, lost)
    left = unheld_lines(best, lines)
    inside = sum(1 for l in lines if geometry.inside(l.bbox, table.bbox))
    if left and len(left) >= MIN_LOSS_SHARE * inside:
        again = refill_ruled(table, [*lines, *glyphs])
        if again is not None and len(unheld_lines(again, lines)) < len(left):
            best = again
    return best


def _grid_geometry(table: TableData) -> tuple[list[tuple[float, float] | None], list[tuple[float, int]]]:
    """The vertical band of each row of a ruled table's grid (None for a row with no cell), and
    the left edge of each column of its widest row (the columns a line is placed in)."""
    bands: list[tuple[float, float] | None] = []
    for boxes in table.grid:
        have = [b for b in boxes if b is not None]
        bands.append((min(b[1] for b in have), max(b[3] for b in have)) if have else None)
    widest = max(table.grid, key=lambda boxes: sum(b is not None for b in boxes))
    return bands, [(b[0], j) for j, b in enumerate(widest) if b is not None]


def _locate(table: TableData, bands: list[tuple[float, float] | None],
            starts: list[tuple[float, int]], line: Line) -> tuple[int, int] | None:
    """The row and column of a ruled table's grid that a line belongs to: the row whose band
    holds the line's centre, and the cell of that row it lies in, else the column of the
    widest row at that x."""
    cx, cy = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
    holding = [(band[1] - band[0], i) for i, band in enumerate(bands)
               if band and band[0] <= cy <= band[1]]
    if not holding:
        return None
    i = min(holding)[1]  # a tall cell spanning rows holds the line too: the thinnest row wins
    j = next((j for j, b in enumerate(table.grid[i]) if b is not None and b[0] <= cx <= b[2]),
             None)
    if j is None:
        # No cell of this row there: the line starts a cell right of the last cell before it
        # (a heading over the columns the rules do not reach), else at the grid's column at x.
        before = [b[2] for b in table.grid[i] if b is not None and b[2] <= cx + 1]
        edge = max(before) - 1 if before else None
        j = next((j for x, j in starts if edge is not None and edge <= x <= cx), None)
        if j is None:
            j = max(((x, j) for x, j in starts if x <= cx + 1), default=(0, None))[1]
    return None if j is None else (i, j)


def adopt_unheld(table: TableData, lost: Sequence[Line]) -> TableData:
    """A ruled table with the lines its cells did not hold added where they belong: a heading
    that sits beside the grid's own cells (a spanning heading over columns the rules do not
    reach), or a figure the cell text left out. A line is added to a cell that is empty (or
    merged into the cell on its left, where it starts a cell of its own); a line whose cell
    holds other text stays out of the table, in the text flow."""
    if not table.grid or not lost:
        return table
    bands, starts = _grid_geometry(table)
    rows = [list(r) for r in table.rows]
    grid = [list(r) for r in table.grid]
    added: list[str] = []
    for line in lost:
        at = _locate(table, bands, starts, line)
        if at is None:
            continue
        i, j = at
        if rows[i][j] or j >= len(rows[i]):
            continue
        rows[i][j] = squash(line.text)
        grid[i][j] = line.bbox
        added.append(rows[i][j])
    if not added:
        return table
    return replace(table, rows=rows, grid=grid,
                   why=(table.why or "ruled table") + f"; text outside the cells added: {added}")


def refill_ruled(table: TableData, lines: Sequence[Line]) -> TableData | None:
    """A ruled table whose cell text came out wrong (see unheld_lines), filled again from the
    page's own text lines. The rows are the grid's rows; the columns are those of the grid's
    widest row, the same for every row (the grid's cells differ from row to row where its rules
    do). Each line goes to the cell it lies in; a line of figures that runs over several cells
    is cut into its figures first (see split_merged). A line over several columns becomes a
    heading with a colspan, when those cells hold nothing else; a line that fits no row stays
    out of the table. None when the table has no grid."""
    if not table.grid:
        return None
    bands, starts = _grid_geometry(table)
    widest = max(table.grid, key=lambda boxes: sum(b is not None for b in boxes))
    columns = sorted((b[0], b[2]) for b in widest if b is not None)
    if len(columns) < 2:
        return None
    last = len(columns) - 1
    put: dict[tuple[int, int], list[Line]] = {}
    spanning: list[tuple[int, list[int], Line]] = []
    for line in split_merged([l for l in lines if geometry.inside(l.bbox, table.bbox)],
                              minimum=2, flags=True):
        cy = (line.top + line.bottom) / 2
        holding = [(band[1] - band[0], i) for i, band in enumerate(bands)
                   if band and band[0] <= cy <= band[1]]
        if not holding:
            continue
        i = min(holding)[1]
        width = max(line.x1 - line.x0, 1e-6)
        over = [j for j, (lo, hi) in enumerate(columns)
                if min(hi, line.x1) - max(lo, line.x0) >= width / 3]
        if len(over) > 1:
            spanning.append((i, over, line))  # a heading over several columns
            continue
        cx = (line.x0 + line.x1) / 2
        j = next((j for j, (lo, hi) in enumerate(columns) if lo <= cx <= hi), None)
        if j is None:
            j = max((j for j, (lo, _) in enumerate(columns) if lo <= cx), default=0)
        put.setdefault((i, min(j, last)), []).append(line)
    # A line over several columns becomes a cell with a colspan, when those cells hold nothing else.
    merged: set[tuple[int, int]] = set()
    for i, over, line in spanning:
        if not any((i, j) in put or (i, j) in merged for j in over):
            put[(i, over[0])] = [line]
            merged |= {(i, j) for j in over[1:]}
    rows: list[list[str | None]] = []
    grid: list[list[BBox | None]] = []
    for i, band in enumerate(bands):
        top, bottom = band if band else (table.bbox[1], table.bbox[1])
        out: list[str | None] = []
        out_boxes: list[BBox | None] = []
        for j, (lo, hi) in enumerate(columns):
            found = put.get((i, j))
            if found:
                found.sort(key=lambda l: (round(l.top), l.x0))
                out.append(" ".join(squash(l.text) for l in found))
                right = max(hi, max(l.x1 for l in found))
                out_boxes.append((lo, top, right, bottom))
            elif (i, j) in merged:
                out.append(None)
                out_boxes.append(None)
            else:
                out.append("")
                out_boxes.append((lo, top, hi, bottom))
        rows.append(out)
        grid.append(out_boxes)
    return replace(table, rows=rows, grid=grid, why=(table.why or "ruled table") +
                   "; cell text read again from the page's text lines")


def _alnum(text: str) -> Counter[str]:
    """The letters and digits of a text, casefolded: the cells' reader and the lines' reader may
    give a word set in small capitals in different cases ("INFORmATION", "Information")."""
    return Counter(ch for ch in text.casefold() if ch.isalnum())


def recase_cells(table: TableData, lines: Sequence[Line]) -> TableData:
    """A ruled table whose cell words take the case of the same words in the page's text lines.
    The cells' reader maps each glyph on its own and misses the text layer's /ActualText, so a
    word set in small capitals can come out in mixed case ("COVERAgE") where the text layer
    says "coverage". A word changes only when the lines over its cell spell it, casefolded, one
    way, and not the cell's way."""
    if not table.grid:
        return table
    rows = [list(r) for r in table.rows]
    changed = False
    for i, (boxes, row) in enumerate(zip(table.grid, table.rows)):
        for j, (box, text) in enumerate(zip(boxes, row)):
            if box is None or not text:
                continue
            spelt: dict[str, set[str]] = {}
            for l in lines:
                if geometry.overlap(l.bbox, box) > 0:
                    for w in l.text.split():
                        spelt.setdefault(w.casefold(), set()).add(w)
            words = []
            for w in text.split(" "):
                ways = spelt.get(w.casefold(), set())
                words.append(next(iter(ways)) if len(ways) == 1 and w not in ways else w)
            if (new := " ".join(words)) != text:
                rows[i][j] = new
                changed = True
    if not changed:
        return table
    return replace(table, rows=rows,
                   why=(table.why or "ruled table") + "; cell words take the text lines' case")


def _letters(text: str) -> str:
    """The letters and digits of a text in order, casefolded: spaces, hyphens and soft hyphens
    do not tell one reading of a cell from another."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def unscramble_cells(table: TableData, lines: Sequence[Line]) -> TableData:
    """A ruled table whose cells read in the order of the page's own text lines. The cells' reader
    (pdfplumber) puts characters whose tops are within 3 pt on one line and sorts them by x, so
    two lines set close together in a small type ("52. Phone" over "Number", with "( ) -" between
    them) come out mixed, character by character ("5 2 . P N h u o m n b e e r ( ) -"). A cell
    changes only when the text lines whose middles lie in it have the same letters and digits,
    each as many times, as the cell, in another order: the lines say the same text, read right."""
    if not table.grid:
        return table
    rows = [list(r) for r in table.rows]
    changed = False
    for i, (boxes, row) in enumerate(zip(table.grid, table.rows)):
        for j, (box, text) in enumerate(zip(boxes, row)):
            if box is None or not text or len(text) < 4:
                continue
            held = sorted((l for l in lines if geometry.centre_in(l.bbox, box)),
                          key=lambda l: (round(l.top), l.x0))
            if len(held) < 2:
                continue
            read = " ".join(squash(l.text) for l in held)
            if _letters(read) != _letters(text) and _alnum(read) == _alnum(text):
                rows[i][j] = read
                changed = True
    if not changed:
        return table
    return replace(table, rows=rows,
                   why=(table.why or "ruled table") + "; cell text in the text lines' order")


def unheld_lines(table: TableData, lines: Sequence[Line]) -> list[Line]:
    """The text lines inside a ruled table's box (by the middle of their height) that none of
    its cells holds: the grid's text extraction left them out (a cell the grid does not have, a
    row read as one line, a spanning header). Each line must find its letters and digits in the
    cells it lies on. A table with no cell boxes holds all."""
    if not table.grid:
        return []
    cells = [(box, squash(t) if t else "")
             for boxes, row in zip(table.grid, table.rows) for box, t in zip(boxes, row)
             if box is not None]
    have_in = [_alnum(t) for _, t in cells]
    lost: list[Line] = []
    for line in sorted((l for l in lines if geometry.inside(geometry.core(l.bbox), table.bbox)),
                       key=lambda l: (l.top, l.x0)):
        need = _alnum(line.text)
        if not need:
            continue
        cy = (line.top + line.bottom) / 2
        on = [i for i, (box, _) in enumerate(cells)
              if box[1] <= cy <= box[3] and min(box[2], line.x1) - max(box[0], line.x0) > 0]
        have: Counter[str] = Counter()
        for i in on:
            have += have_in[i]
        if any(need[ch] > have[ch] for ch in need):
            lost.append(line)
    return lost
