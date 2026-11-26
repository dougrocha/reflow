"""Ruled tables: pdfplumber's "lines" strategy finds tables drawn with a full grid of rules.

It is slow (~6x pypdfium2), so it only runs on candidate pages: pages with enough vector drawing
operations to hold a ruled table. Run it on the normalized copy (see reflow.ingest.load.normalize).
This is the part of table detection the extraction stage runs; the rest runs in the analysis
(reflow.analyze.tables).
"""

from __future__ import annotations

import ctypes
import math
from collections.abc import Iterable, Sequence
from contextlib import ExitStack, closing
from pathlib import Path

import pdfplumber
from pdfplumber.table import Table
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow import geometry
from reflow.extract import pagecontent
from reflow.extract.chars import font_size
from reflow.extract.drawings import MIN_RULED_RULES, Drawing
from reflow.extract.grid import find_ruled_tables, shown_edges, wider_rule_groups
from reflow.extract.redraws import Redraws
from reflow.extract.tables.cells import TableData, TableMethod, looks_numeric
from reflow.extract.text import hidden_indices
from reflow.model import BBox
from reflow.text_quality import squash

def ruled_header_rows(rows: list[list[str | None]]) -> int:
    """1, or 2 when the first row is a banner across the table ("BLUEVALUE RATES") over a row
    of column names ("AGE", "Plan A" …) with figures under them."""
    if len(rows) < 3 or sum(1 for c in rows[0] if c) != 1:
        return 1
    names, body = rows[1], [c for row in rows[2:] for c in row if c]
    if not all(names) or any(looks_numeric(c) for c in names):
        return 1
    return 2 if body and sum(map(looks_numeric, body)) >= 0.5 * len(body) else 1


MIN_CURVE_POINTS = 20  # a data series has many points; a chevron or an arrow has a few


def _saturated(color: object) -> bool:
    """An RGB or CMYK colour whose channels differ widely: a colour, not grey or black."""
    if not isinstance(color, (list, tuple)) or len(color) not in (3, 4):
        return False
    return max(color[:3]) - min(color[:3]) >= 0.25


def _coloured(page, box: BBox) -> int:
    """The saturated-colour rules, lines and fills whose boxes lie inside `box`."""
    x0, top, x1, bottom = box
    return sum(1 for o in (*page.rects, *page.curves, *page.lines)
               if o["x0"] >= x0 - 2 and o["x1"] <= x1 + 2 and o["top"] >= top - 2
               and o["bottom"] <= bottom + 2
               and (_saturated(o.get("non_stroking_color")) or _saturated(o.get("stroking_color"))))


def _coloured_curves(page, box: BBox) -> int:
    """The stroked, unfilled, saturated-colour curves of at least MIN_CURVE_POINTS points whose
    boxes lie inside `box`: a chart's data lines. A ruled table's rules are straight lines and
    its fills are rectangles, so it has none; a diagram's filled or short arrows do not count."""
    x0, top, x1, bottom = box
    return sum(1 for o in page.curves
               if o["x0"] >= x0 - 2 and o["x1"] <= x1 + 2 and o["top"] >= top - 2
               and o["bottom"] <= bottom + 2 and not o.get("fill")
               and len(o.get("pts") or ()) >= MIN_CURVE_POINTS
               and _saturated(o.get("stroking_color")))


def candidate_pages(drawings_by_page: dict[int, list[Drawing]]) -> list[int]:
    return [n for n, drawings in drawings_by_page.items()
            if sum(d.rules for d in drawings) >= MIN_RULED_RULES]


def find_tables(path: Path, pages: Iterable[int],
                contents: dict[int, pagecontent.PlumberPage] | None = None
                ) -> dict[int, list[TableData]]:
    """The ruled tables on these pages. pdfplumber parses each page here (the cost of table
    finding); with a `contents` dict, the same parse also fills it with the page's
    PageContent for the tag tree and the links, so no later reader parses the page again."""
    found: dict[int, list[TableData]] = {}
    with pdfplumber.open(path) as pdf, ExitStack() as stack:
        text_pdf = None
        for n in pages:
            page = pdf.pages[n - 1]
            hidden: _Hidden | None = None   # read when the page has a table
            edges = None                    # the page's shown edges, read for its first table
            try:
                if contents is not None:
                    contents[n] = pagecontent.PlumberPage.from_page(page, words=bool(page.page_obj.annots))
                for table in _between_bands(page, find_ruled_tables(page)):
                    if edges is None:
                        edges = shown_edges(page)
                    complete = _complete_open_sides(page, table, edges)
                    if text_pdf is None:
                        text_pdf = stack.enter_context(pdfium.PdfDocument(path))
                    if hidden is None:
                        hidden = _Hidden.of(text_pdf, n, page)
                    if complete is not table and hidden.any_in(page, complete.bbox):
                        rows = [[_clean(c) for c in row] for row in _extract(page, complete, hidden)]
                        table = complete
                    elif complete is not table:
                        with closing(text_pdf[n - 1]) as text_page:
                            with closing(text_page.get_textpage()) as text:
                                height = text_page.get_height()
                                redrawn = _redrawn(text)
                                rows = [[_clean(_bounded_text(
                                    text, (b[0], height - b[3], b[2], height - b[1]), redrawn))
                                    if b else None
                                    for b in row.cells] for row in complete.rows]
                        table = complete
                    else:
                        rows = [[_clean(c) for c in row] for row in _extract(page, table, hidden)]
                    if len(rows) < 2 or max(len(r) for r in rows) < 2:
                        continue
                    if not any(c for row in rows for c in row):
                        continue
                    if is_entry_box(rows):
                        continue  # form boxes for digits: no data to tabulate
                    x0, top, x1, bottom = table.bbox
                    cols = max(len(r) for r in rows)
                    grid = [[None if c is None else tuple(round(v, 2) for v in c) for c in row.cells]
                            for row in table.rows]
                    lead = _header_block(rows, grid)
                    rows_, grid_ = _join_block_rows(rows[lead:], grid[lead:])
                    rows, grid = rows[:lead] + rows_, grid[:lead] + grid_
                    heads = lead or ruled_header_rows(rows)
                    found.setdefault(n, []).append(TableData(
                        n, (round(x0, 2), round(top, 2), round(x1, 2), round(bottom, 2)), rows,
                        method=TableMethod.RULED, header_rows=heads, grid=grid,
                        coloured=_coloured(page, table.bbox),
                        coloured_curves=_coloured_curves(page, table.bbox),
                        why=f"ruled lines: {len(rows)} rows × {cols} columns, "
                            f"{heads} header row{'' if heads == 1 else 's'}"))
            finally:
                page.close()
    return found


def _names(a: str, b: str) -> bool:
    """Whether two cell texts are one name: equal, or one is a run of whole words of the
    other ("Procedures" under a bar, "Periodontal Procedures" under the next)."""
    short, long = sorted((a.split(), b.split()), key=len)
    return any(long[k:k + len(short)] == short for k in range(len(long) - len(short) + 1))


def _repeats(first: dict[int, str], row: dict[int, str]) -> bool:
    """Whether `row` names two columns as `first` does, or is `first` whole. One shared value
    is no sign (a "$0" under a band, in a table with no header)."""
    shared = sum(x in first and _names(first[x], name) for x, name in row.items())
    return shared >= 2 or (shared == 1 and row == first)


def _past_band(row, is_band: bool, end: float) -> list[BBox]:
    """The cells of a row that stay in the table: all of them, or for a band only those that run
    on below it, cut to start where the band ends."""
    cells = [c for c in row.cells if c is not None]
    if not is_band:
        return cells
    return [(c[0], end, c[2], c[3]) for c in cells if c[3] > end + 0.01]


def _between_bands(page, tables: Iterable[Table]) -> list[Table]:
    """The tables without their bands. A band is a row with no text in any of its cells, lower
    than the smallest glyph in the table, so no line of its text fits in it: a coloured bar
    drawn over a table or between its groups of rows, which the finder reads as a row because
    the bar's edges are rules to it. The grid may cut the bar into several cells (where a rule
    meets it) or leave it one cell across the table; either way a band is no row. Under a band,
    a row that names two columns as the table's first row does (or repeats a first row of one
    name) starts a second table (the same columns again, under their own bar), when each part
    keeps two rows or more."""
    out: list[Table] = []
    for table in tables:
        x0, top, x1, bottom = table.bbox
        rows = table.rows
        inside = [((c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2, c["bottom"] - c["top"])
                  for c in page.chars if not c["text"].isspace()]
        inside = [m for m in inside if x0 <= m[0] < x1 and top <= m[1] < bottom]
        glyph = min((m[2] for m in inside), default=0.0)
        # A band's extent is as far down as its shortest cell goes: a cell of the row that runs
        # on below it (a header over two rows, beside a bar over the rest) is not part of it.
        ends = [min(c[3] for c in row.cells if c is not None) for row in rows]
        band = [ends[i] - row.bbox[1] < glyph
                and not any(row.bbox[1] <= m[1] < ends[i] for m in inside)
                for i, row in enumerate(rows)]
        kept = [i for i in range(len(rows)) if not band[i]]
        if len(kept) == len(rows) or not kept:
            out.append(table)
            continue
        names = [{round(box[0]): squash(text) for box, text in zip(row.cells, texts, strict=True)
                  if box is not None and text and text.strip()}
                 for row, texts in zip(rows, _extract(page, table), strict=True)]
        parts: list[list[int]] = []
        for i in kept:
            again = i and band[i - 1] and parts and _repeats(names[parts[0][0]], names[i])
            if again or not parts:
                parts.append([])
            parts[-1].append(i)
        if any(len(part) < 2 for part in parts):
            parts = [kept]
        for part in parts:
            first = part[0]
            while first and band[first - 1]:
                first -= 1
            out.append(Table(table.page, [
                c for i in range(first, part[-1] + 1) for c in _past_band(rows[i], band[i], ends[i])]))
    return out


# Two origins this close (points) are one glyph's: pdfium's and pdfminer's arithmetic differ
# in the last digits.
ORIGIN_SLACK = 0.05


class _Hidden:
    """The glyphs of a page that its lines leave out as hidden (text.hidden_indices), keyed by
    their text and their origin in pdfminer's device space: pdfium's origin in PDF user space
    through the matrix pdfminer starts the page with (PDFPageInterpreter.process_page, from the
    page's resolved MediaBox and /Rotate). pdfplumber's char `matrix` ends at that origin, so a
    char is matched to its glyph whatever the page's rotation or boxes. A key a visible glyph
    shares (the same letter drawn hidden and shown at one spot) is left alone: it keeps text."""

    def __init__(self, hidden: list[tuple[str, float, float]],
                 shown: list[tuple[str, float, float]] = ()):
        self.cells: dict[tuple[str, int, int], list[tuple[float, float]]] = {}
        for ch, x, y in hidden:
            if not any(t == ch and abs(x - sx) < ORIGIN_SLACK and abs(y - sy) < ORIGIN_SLACK
                       for t, sx, sy in shown):
                self.cells.setdefault((ch, round(x), round(y)), []).append((x, y))

    @classmethod
    def of(cls, pdf: pdfium.PdfDocument, n: int, plumbed) -> _Hidden:
        """The hidden glyphs of page n, for the pdfplumber page `plumbed`."""
        with closing(pdf[n - 1]) as page:
            hidden = set(hidden_indices(page))
            if not hidden:
                return cls([])
            x0, y0, x1, y1 = plumbed.page_obj.mediabox
            ctm = {90: (0, -1, 1, 0, -y0, x1), 180: (-1, 0, 0, -1, x1, y1),
                   270: (0, 1, -1, 0, y1, -x0)}.get(plumbed.page_obj.rotate,
                                                    (1, 0, 0, 1, -x0, -y0))
            a, b, c, d, e, f = ctm
            x, y = ctypes.c_double(), ctypes.c_double()
            found: tuple[list, list] = ([], [])
            with closing(page.get_textpage()) as text:
                for i in range(text.count_chars()):
                    ch = chr(pdfium_c.FPDFText_GetUnicode(text.raw, i))
                    if ch.isspace() or not pdfium_c.FPDFText_GetCharOrigin(
                            text.raw, i, ctypes.byref(x), ctypes.byref(y)):
                        continue
                    found[i in hidden].append((ch, a * x.value + c * y.value + e,
                                               b * x.value + d * y.value + f))
            return cls(found[1], found[0])

    def __contains__(self, char: dict) -> bool:
        if not self.cells:
            return False
        x, y, ch = char["matrix"][4], char["matrix"][5], char["text"]
        return any(abs(x - hx) < ORIGIN_SLACK and abs(y - hy) < ORIGIN_SLACK
                   for i in (-1, 0, 1) for j in (-1, 0, 1)
                   for hx, hy in self.cells.get((ch, round(x) + i, round(y) + j), ()))

    def any_in(self, page, box: BBox) -> bool:
        """Whether a hidden glyph is centred in `box` (pdfplumber's frame)."""
        x0, top, x1, bottom = box
        return bool(self.cells) and any(
            x0 <= (c["x0"] + c["x1"]) / 2 < x1 and top <= (c["top"] + c["bottom"]) / 2 < bottom
            and c in self for c in page.chars)


def _extract(page, table, hidden: _Hidden | None = None) -> list[list[str | None]]:
    """table.extract(x_tolerance_ratio=WORD_GAP), with the same text. Table.extract tests every
    char of the page against every row and then every cell of the row, and works out each char's
    centre every time. Here the centres are worked out once, only the chars inside the table are
    tested, and a cell tests only the chars of its row. The test is Table.extract's (centre at or
    after the box's left and top edge, before its right and bottom edge), and the text of a cell
    is pdfplumber's extract_text, with words split at a gap of pagecontent.WORD_GAP font sizes.
    A `hidden` char is text the page's lines leave out as hidden (_Hidden), and is left out
    here too. A glyph drawn again in place is read once (reflow.extract.redraws)."""
    x0, top, x1, bottom = table.bbox
    inside = []
    redraws = Redraws()
    for c in page.chars:
        h, v = (c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2
        if (x0 <= h < x1 and top <= v < bottom and not (hidden is not None and c in hidden)
                and not redraws.seen(c["text"], (c["x0"], c["top"], c["x1"], c["bottom"]),
                                     c["size"] or 0.0)):
            inside.append((h, v, c))
    out: list[list[str | None]] = []
    for row in table.rows:
        left, upper, right, lower = row.bbox
        in_row = [m for m in inside if left <= m[0] < right and upper <= m[1] < lower]
        texts: list[str | None] = []
        for cell in row.cells:
            if cell is None:
                texts.append(None)
                continue
            cx0, ctop, cx1, cbottom = cell
            chars = [m[2] for m in in_row if cx0 <= m[0] < cx1 and ctop <= m[1] < cbottom]
            texts.append(pdfplumber.utils.extract_text(
                chars, x_tolerance_ratio=pagecontent.WORD_GAP) if chars else "")
        out.append(texts)
    return out


def _redrawn(text: pdfium.PdfTextPage) -> dict[int, tuple[float, float]]:
    """The glyphs pdfium gives again in place (reflow.extract.redraws), by textpage index, with
    the centre of each glyph's box."""
    raw, redraws, found = text.raw, Redraws(), {}
    matrix = pdfium_c.FS_MATRIX()
    for i in range(text.count_chars()):
        ch = chr(pdfium_c.FPDFText_GetUnicode(raw, i))
        if ch.isspace():
            continue
        obj = pdfium_c.FPDFText_GetTextObject(raw, i)
        box = text.get_charbox(i)
        if redraws.seen(ch, box, font_size(raw, i, matrix),
                        ctypes.addressof(obj.contents) if obj else None):
            found[i] = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    return found


def _bounded_text(text: pdfium.PdfTextPage, rect: BBox,
                  redrawn: dict[int, tuple[float, float]]) -> str:
    """pdfium's text in `rect` (PDF coordinates: left, bottom, right, top). When a glyph drawn
    again in place lies in it, the text is the glyphs whose centre lies in it, in pdfium's order,
    without the redraws: a gap between them (whitespace, or a glyph outside) is one space."""
    left, bottom, right, top = rect
    if not any(left <= x < right and bottom <= y < top for x, y in redrawn.values()):
        return text.get_text_bounded(left, bottom, right, top)
    out: list[str] = []
    gap = False
    for i in range(text.count_chars()):
        if i in redrawn:
            continue
        ch = chr(pdfium_c.FPDFText_GetUnicode(text.raw, i))
        if not ch.isspace():
            x0, y0, x1, y1 = text.get_charbox(i)
            if left <= (x0 + x1) / 2 < right and bottom <= (y0 + y1) / 2 < top:
                if gap and out:
                    out.append(" ")
                out.append(ch)
                gap = False
                continue
        gap = True
    return "".join(out)


def _complete_open_sides(page, table, edges=None):
    """Recover borderless outside columns from repeated horizontal-rule endpoints. The rules
    are the ones the finder reads (grid.shown_edges), not every side of every fill: the border
    of a fill lying on its own colour is no rule."""
    _, top, _, bottom = table.bbox
    for _, _, group in wider_rule_groups(shown_edges(page) if edges is None else edges,
                                         table.bbox):
        if abs(min(e["top"] for e in group) - top) > 1 \
                or abs(max(e["top"] for e in group) - bottom) > 1:
            continue
        # Keep the exact endpoints: rounding them could crop a rule out of the page.
        left = min(e["x0"] for e in group)
        right = max(e["x1"] for e in group)
        crop = page.crop((left, top, right, bottom))
        candidates = find_ruled_tables(crop, {"explicit_vertical_lines": [left, right]})
        for candidate in candidates:
            if all(abs(a - b) <= 1 for a, b in
                   zip(candidate.bbox, (left, top, right, bottom))) \
                    and any(_stacked_block(a.cells, b.cells)
                            for a, b in zip(candidate.rows, candidate.rows[1:])):
                return candidate
    return table


def _stacked_block(above: Sequence[BBox | None], below: Sequence[BBox | None]) -> bool:
    """A spanning data cell above subdivisions, beside stubs as tall as both parts."""
    a = [b for b in above if b is not None]
    b = [b for b in below if b is not None]
    if not a or len(b) < 2:
        return False
    end = max(box[3] for box in a)
    split = min(box[1] for box in b)
    if abs(max(box[3] for box in b) - end) > 0.01:
        return False
    # One record, not several: outside the spanning cell no rule may cut across the block.
    # The cells beside it must run through the split (ResNet Table 1: the layer-name and
    # output-size stubs). A ruled boundary there means separate records that merely sit
    # beside a tall label or a merged heading (a ledger's category cell, a directory entry).
    def spans(box: BBox) -> bool:
        if abs(box[3] - split) > 0.01 or sum(
                box[0] <= part[0] + 0.01 and box[2] >= part[2] - 0.01 for part in b) < 2:
            return False
        beside = [o for o in a if o[2] <= box[0] + 0.01 or o[0] >= box[2] - 0.01]
        return bool(beside) and all(o[3] >= end - 0.01 for o in beside) \
            and len(beside) == len(a) - 1
    return any(spans(box) for box in a)


def _header_block(rows: list[list[str | None]], grid: list[list[BBox | None]]) -> int:
    """2 when the table's first two rows are a header of two levels: a heading over several
    columns ("Areas of Access", "What You Will Pay") above the names of those columns, beside
    stubs ("User Roles") as tall as both rows. Every cell of both rows has text, and none of
    the names is a figure. The rows stay two, each cell with its own box and the spans that
    follow from the grid (colspan across the columns, rowspan for the stubs): joined into one
    row, the heading would be the first column's name and span nothing. Else 0."""
    if len(rows) < 3 or not _stacked_block(grid[0], grid[1]):
        return 0
    texts = [t for row, boxes in zip(rows[:2], grid[:2], strict=True)
             for t, box in zip(row, boxes, strict=True) if box is not None]
    names = [t for t, box in zip(rows[1], grid[1], strict=True) if box is not None]
    return 2 if all(texts) and not any(looks_numeric(t) for t in names if t) else 0


def _join_block_rows(rows: list[list[str | None]], grid: list[list[BBox | None]]
                     ) -> tuple[list[list[str | None]], list[list[BBox | None]]]:
    """Keep stacked cells in one logical row when their vertical bands overlap.

    A partial rule can divide the right-hand cells while a stub spans the whole block.
    The model has colspans but no rowspans, so retain that block as one row.
    """
    out: list[list[str | None]] = []
    boxes: list[list[BBox | None]] = []
    bottom = -math.inf
    for row, band in zip(rows, grid):
        have = [b for b in band if b is not None]
        top, end = min(b[1] for b in have), max(b[3] for b in have)
        if not boxes or top >= bottom - 0.01 or not _stacked_block(boxes[-1], band):
            out.append(list(row))
            boxes.append(list(band))
        else:
            for j, (text, box) in enumerate(zip(row, band)):
                if box is None:
                    continue
                previous = boxes[-1][j]
                out[-1][j] = " ".join(t for t in (out[-1][j], text) if t)
                boxes[-1][j] = geometry.union([previous, box]) if previous else box
        bottom = max(bottom, end)
    return out, boxes


def is_entry_box(rows: list[list[str | None]]) -> bool:
    """A form's entry boxes (SSN or EIN digits): at most a label, and empty cells below it.
    A dash in a box is a separator between digit groups, not data."""
    def empty(c: str | None) -> bool:
        return not c or set(c) <= set("-–—−")
    filled = [c for row in rows for c in row if not empty(c)]
    return len(filled) <= 1 or all(empty(c) for row in rows[1:] for c in row)


def _clean(cell: str | None) -> str | None:
    if cell is None:
        return None
    return squash(cell)
