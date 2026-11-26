"""Repairs to the rows of an aligned table before its columns are found: a row of figures read as
one line, words glued across a column gap, a value stacked over two lines or centred between
two rows, and budget line codes run into their labels.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from itertools import combinations

from reflow.patterns import CAPTION_START, CODE_START, NUMBER, YEAR_HEADING_START
from reflow.extract.lines import Line
from reflow.analyze.tables.cells import CELL_MARKS, LABEL_GROUP, Cell, Row, cluster, read_cell
from reflow.text_quality import squash

CURRENCY_SIGNS = frozenset("$€£¥")


MIN_MERGED_FIGURES = 3  # a line of this many figures only is a row read as one line




def _figure_width(text: str) -> float:
    """Width of a figure in em, between a proportional and a Helvetica-like font: digits are
    wider than the commas and points between them."""
    return sum(0.26 if ch in ".,%" else 0.53 for ch in text)




CODE_COLUMN_TOL = 2.0  # codes this close to one left edge are one code column


def split_codes(lines: list[Line]) -> list[Line]:
    """A line that starts with a row code ("0799 Total direct obligations") gets the code split
    off as its own segment, when several lines start with codes at the same left edge."""
    starts = [l for l in lines if CODE_START.match(squash(l.text))]
    edges = [e for group in cluster((l.x0 for l in starts), CODE_COLUMN_TOL)
             if len(group) >= 3 for e in group]
    if not edges:
        return lines
    out: list[Line] = []
    for line in lines:
        m = CODE_START.match(squash(line.text))
        if m and YEAR_HEADING_START.match(squash(line.text)[m.end():]):
            m = None  # "2023 actual": a column heading, not a row code
        if m and not squash(line.text)[m.end():][:1].isupper():
            m = None  # "1992 and beyond …", "504 very low-income": running text, not a row
        if m and any(abs(line.x0 - e) <= CODE_COLUMN_TOL for e in edges):
            code = m.group(1)
            x1 = round(line.x0 + 0.56 * len(code) * line.size, 2)
            rest = squash(line.text)[m.end():]
            out.append(replace(line, bbox=(line.x0, line.top, x1, line.bottom), text=code))
            out.append(replace(line, bbox=(min(line.x1, x1 + 0.3 * line.size), line.top,
                                           line.x1, line.bottom), text=rest))
        else:
            out.append(line)
    return out


# A footnote flag standing for a missing figure: "N" (not available), "X", "NA", "S".
FLAG = re.compile(r"^[A-Z]{1,2}$")


def _figures_of(words: list[str]) -> list[str]:
    """The words of a line, a currency sign standing alone joined to the figure after it."""
    out: list[str] = []
    for word in words:
        if out and out[-1] in CURRENCY_SIGNS:
            out[-1] += " " + word
        else:
            out.append(word)
    return out


SIGN_REACH = 5.0  # font sizes: a currency sign stands this far before its figure, at most


def join_signs(lines: list[Line]) -> list[Line]:
    """A currency sign that is a line of its own joins the figure to its right on the same
    baseline ("$" ... "8,426,474"): a spreadsheet sets the sign at the left edge of the cell and
    the figure at its right, with a gap that grows with the cell's width. Another line between
    them, or a gap wider than SIGN_REACH, leaves the sign alone."""
    signs = [l for l in lines if l.text.strip() in CURRENCY_SIGNS and not l.rotated]
    if not signs:
        return lines
    taken: dict[int, Line] = {}
    for sign in signs:
        row = [l for l in lines if l is not sign and not l.rotated and l.x1 > sign.x0
               and min(sign.bottom, l.bottom) - max(sign.top, l.top) > 0.5 * sign.height]
        beyond = sorted((l for l in row if l.x0 >= sign.x1 - 0.5), key=lambda l: l.x0)
        nearest = beyond[0] if beyond else None
        if (nearest is not None and NUMBER.match(nearest.text.strip())
                and nearest.x0 - sign.x1 <= SIGN_REACH * sign.size and id(nearest) not in taken):
            taken[id(nearest)] = sign
    joined = {id(sign) for sign in taken.values()}
    return [taken[id(l)].joined(l) if id(l) in taken else l for l in lines if id(l) not in joined]


def split_merged(lines: list[Line], minimum: int = MIN_MERGED_FIGURES,
                  flags: bool = False) -> list[Line]:
    """A line that holds a row's figures only ("5,131.6 6,126.9 5,362.2"), or a checklist's marks
    only ("● ● ●"): the extractor ran the columns of the row together. It is cut into one line
    per figure. Each figure is as wide as its characters; the gaps between them share what is
    left of the line's width."""
    out: list[Line] = []
    for line in lines:
        words = _figures_of(line.text.split())
        marks = len(words) >= 2 and all(w in CELL_MARKS for w in words)  # "● ● ●"
        if not marks and (len(words) < minimum or line.rotated or not all(
                NUMBER.match(w) or (flags and FLAG.match(w)) for w in words)
                or not any(NUMBER.match(w) for w in words)):
            out.append(line)
            continue
        widths = [_figure_width(w) * line.size for w in words]
        gap = (line.x1 - line.x0 - sum(widths)) / (len(words) - 1)
        if gap < 0.5 * line.size:  # no room for column gaps: one figure per word is a guess
            out.append(line)
            continue
        x = line.x0
        for word, w in zip(words, widths):
            out.append(replace(line, bbox=(round(x, 2), line.top, round(x + w, 2), line.bottom),
                               text=word))
            x += w + gap
    return out


# Glyph widths in thousandths of an em, a serif (Times) and a sans (Helvetica) face: the width
# of a word in a line whose only known geometry is its box and its size. A face of the same
# kind is close enough for the room between words to be told from the words themselves.
def _widths(table: dict[int, str]) -> dict[str, int]:
    return {ch: w for w, chars in table.items() for ch in chars}


SERIF_WIDTHS = _widths({
    250: " ,.", 278: "/:;ijlt\\", 333: "!'()-I[]`fr’‘", 389: "Js", 444: "?acez“”", 469: "^",
    480: "{}", 500: "#$*0123456789_bdghknopquvxy–†‡", 541: "~", 556: "FPST", 564: "+<=>×±",
    611: "ELTZ", 667: "BCR", 722: "ADGHKNOQUVXYw", 778: "&m", 833: "%", 889: "M", 921: "@",
    944: "W", 1000: "—…"})


SANS_WIDTHS = _widths({
    222: "'ijl`’‘", 260: "|", 278: " !,./:;I[]\\ft", 333: "()-r“”", 334: "{}", 355: '"',
    389: "*", 469: "^", 500: "Jcksvxyz", 556: "#$?0123456789_abdeghnopqu–", 584: "+<=>~×±",
    611: "FTZ", 667: "&ABEKPSVXY", 722: "CDHNRUw", 778: "GOQ", 833: "Mm", 889: "%",
    944: "W", 1000: "—…", 1015: "@"})


SANS_FONT = re.compile(r"arial|helvetica|verdana|calibri|sans|univers|frutiger|myriad|segoe|"
                       r"tahoma|gothic|futura|optima", re.I)


CUT_TOL = 0.4  # line heights: a piece of a cut line may miss its column's cell by this much


STACK_TOL = 2.0  # points: two header lines stacked on one centre line up this closely


NEIGHBOUR_GAP = 1.0  # rows this many line heights apart are neighbours in one table


def _word_widths(line: Line) -> tuple[list[str], list[float], float]:
    """The words of a line, the width of each in points from the glyph widths of a serif or
    sans face at the line's size, and the width of a space (the extractor keeps no glyph
    positions). A face wider than the table (bold) can only make a line look more spaced
    than it is, so the widths are scaled down when they overrun the line's box."""
    widths = SANS_WIDTHS if SANS_FONT.search(line.font or "") else SERIF_WIDTHS
    default = 556 if widths is SANS_WIDTHS else 500
    words = line.text.split()
    scale = line.size / 1000
    ws = [sum(widths.get(ch, default) for ch in word) * scale for word in words]
    space = widths[" "] * scale
    ink = sum(ws) + (len(words) - 1) * space
    if ink > line.x1 - line.x0 > 0:
        shrink = (line.x1 - line.x0) / ink
        ws, space = [w * shrink for w in ws], space * shrink
    return words, ws, space


MIN_COLUMN_GAP = 0.3  # em: the room a cut claims beyond a space between its words


MAX_GROUPINGS = 3000  # ways to group a line's words into pieces that are worth trying


def _group_words(line: Line, cells: list[Cell], tol: float
                 ) -> list[tuple[str, float, float]] | None:
    """The words of a line grouped into one piece per cell of `cells` (a neighbouring row's
    cells inside the line's span, left to right), each piece with the box it is placed in.
    The line must have room for the column gaps: its box less its glyphs and one space per
    word boundary leaves MIN_COLUMN_GAP em per cut, else its words are one phrase with
    ordinary spaces ("COCO trainval" over two columns is one heading). Among the groupings,
    the one whose pieces sit nearest the centres of their cells wins; a piece must overlap
    its cell, and pieces never close up tighter than a space. The first piece starts at the
    line's left edge and the last ends at its right edge. None when no grouping fits."""
    words, ws, space = _word_widths(line)
    n, m = len(words), len(cells)
    if n < m or m < 2:
        return None
    room = (line.x1 - line.x0) - sum(ws) - (n - 1) * space
    if room < (m - 1) * MIN_COLUMN_GAP * line.size:
        return None
    if math.comb(n - 1, m - 1) > MAX_GROUPINGS:
        return None
    best: tuple[float, list[tuple[str, float, float]]] | None = None
    for cuts in combinations(range(1, n), m - 1):
        bounds = [0, *cuts, n]
        pieces: list[tuple[str, float, float]] = []
        cost, end = 0.0, line.x0 - space
        for k in range(m):
            a, b = bounds[k], bounds[k + 1]
            width = sum(ws[a:b]) + (b - a - 1) * space
            cell = cells[k]
            if k == 0:
                x0 = line.x0
            elif k == m - 1:
                x0 = line.x1 - width
                if x0 < end + space - tol:
                    pieces = []
                    break
            else:
                x0 = max(cell.cx - width / 2, end + space)
            if x0 + width <= cell.x0 - tol or x0 >= cell.x1 + tol:
                pieces = []
                break  # the piece misses its cell
            cost += abs(x0 + width / 2 - cell.cx)
            pieces.append((" ".join(words[a:b]), x0, x0 + width))
            end = x0 + width
        if pieces and (best is None or cost < best[0]):
            best = (cost, pieces)
    return best[1] if best else None


def _neighbours(rows: list[Row], i: int) -> bool:
    """Whether a row of one cell sits between two rows of several cells, close enough above
    and below to be a line of their table (not a line of text under it)."""
    row = rows[i]
    above = rows[i - 1] if i else None
    below = rows[i + 1] if i + 1 < len(rows) else None
    close = NEIGHBOUR_GAP * row.height
    return (above is not None and below is not None and len(above.cells) >= 2
            and len(below.cells) >= 2 and row.top - above.bottom <= close
            and below.top - row.bottom <= close)


MIN_STRICT_CELLS = 6  # cells a lone line must fall into when only one row of cells is near


def _cut_cell(cell: Cell, rows: list[Row], i: int, strict: bool,
              tops: list[float] | None = None, bottoms: list[float] | None = None
              ) -> list[Cell] | None:
    """A cell of several words cut into the columns of a neighbouring row: one piece per cell
    of that row inside this cell's span (see _group_words). The rows nearby are tried from
    the one with most cells inside the span, nearest first. With `strict` (a line on its own
    next to one row of cells, which may be text under a table), the row must have
    MIN_STRICT_CELLS cells inside the span and each piece must be of the kind of the cell it
    falls into (a figure under a figure, words under words). None when nothing cuts.
    `tops` and `bottoms` are the rows' own (a row's top and bottom are a min and a max over
    its cells; split_glued keeps them for the whole pass)."""
    if len(cell.text.split()) < 2 or CAPTION_START.match(cell.text):
        return None
    tol = CUT_TOL * cell.height
    if tops is None or bottoms is None:
        tops, bottoms = [r.top for r in rows], [r.bottom for r in rows]
    reach = 3 * cell.height
    low, high = bottoms[i] + reach, tops[i] - reach
    near = [j for j in range(len(rows)) if j != i and tops[j] <= low and bottoms[j] >= high]
    candidates = []
    for j in near:
        inside = [c for c in rows[j].cells if cell.x0 < c.cx < cell.x1
                  and c.x1 - c.x0 < cell.x1 - cell.x0]
        if len(inside) >= (MIN_STRICT_CELLS if strict else 2) \
                and all(b.x0 > a.x1 for a, b in zip(inside, inside[1:])):
            candidates.append((-len(inside), abs(rows[j].top - rows[i].top), inside))
    for _, _, inside in sorted(candidates, key=lambda c: c[:2]):
        grouped = _group_words(cell.line, inside, tol)
        if grouped is None:
            continue
        pieces = [p for text, x0, x1 in grouped if (p := read_cell(replace(
            cell.line, bbox=(round(x0, 2), cell.top, round(x1, 2), cell.bottom), text=text)))]
        if len(pieces) == len(grouped) and not (
                strict and any(p.numeric != c.numeric for p, c in zip(pieces, inside))):
            return pieces
    return None


def split_glued(rows: list[Row]) -> list[Row]:
    """Cells the extractor ran together across columns ("RPN+RCNN ResNet-101 dense", "# layers
    # params", "mAP areo bike" over three columns of figures), cut at the columns of the rows
    around them (see _cut_cell). A cell of a row with other cells is always a candidate; a
    line on its own only between two rows of cells, or next to one when its pieces match that
    row's cells in kind. Cutting one cell can expose the columns another needs, so this
    repeats until nothing changes."""
    for _ in range(4):
        changed = False
        tops, bottoms = [r.top for r in rows], [r.bottom for r in rows]
        for i, row in enumerate(rows):
            if len(row.cells) == 1:
                if _neighbours(rows, i):
                    strict = False
                elif any(len(rows[j].cells) >= 2 and abs(rows[j].top - row.top) <= 2.5 * row.height
                         for j in (i - 1, i + 1) if 0 <= j < len(rows)):
                    strict = True
                else:
                    continue
            else:
                strict = False
            cells: list[Cell] = []
            for cell in row.cells:
                pieces = _cut_cell(cell, rows, i, strict, tops, bottoms)
                if pieces:
                    changed = True
                    cells += pieces
                else:
                    cells.append(cell)
            row.cells = sorted(cells, key=lambda c: c.x0)
            tops[i], bottoms[i] = row.top, row.bottom
        if not changed:
            break
    return rows


def merge_stacked(rows: list[Row]) -> list[Row]:
    """Two-line column headings: a row of labels over a second row of labels, each label
    centred on the one above it ("LOC" over "method", "LOC error" over "on GT CLS"). The two
    rows are one header row, each label joined to the one under it; a label on its own
    centred between the two lines ("testing") belongs to it too. Only the first two such rows
    of a block merge: a third row that lines up the same way is a table of centred words,
    which stays as it is."""
    out: list[Row] = []
    i = 0
    while i < len(rows):
        a = rows[i]
        pair = _stacked_pair(rows, i) if len(a.cells) >= 2 else None
        if pair is None:
            out.append(a)
            i += 1
            continue
        j, between = pair
        b = rows[j]
        cells = []
        for ca, cb in zip(a.cells, b.cells):
            box = (min(ca.x0, cb.x0), ca.top, max(ca.x1, cb.x1), cb.bottom)
            joined = read_cell(replace(ca.line, bbox=box, text=f"{ca.text} {cb.text}"))
            cells.append(joined if joined is not None else ca)
        cells += [c for k in between for c in rows[k].cells]
        out.append(Row(sorted(cells, key=lambda c: c.x0)))
        i = j + 1
    return out


def _stacked_pair(rows: list[Row], i: int) -> tuple[int, list[int]] | None:
    """The row under `rows[i]` that stacks on it label for label, with the indexes of the
    single-label rows between them that lie inside the band, or None."""
    a = rows[i]
    if any(c.numeric for c in a.cells):
        return None
    if i and len(rows[i - 1].cells) >= 2 and a.top - rows[i - 1].bottom <= NEIGHBOUR_GAP * a.height:
        return None  # not the first row of its block
    between: list[int] = []
    for j in range(i + 1, len(rows)):
        b = rows[j]
        if b.top - a.bottom > NEIGHBOUR_GAP * a.height and not between:
            return None
        if len(b.cells) == 1 and not b.cells[0].numeric and b.top >= a.top - STACK_TOL:
            between.append(j)
            continue
        if not _stacks(a, b) or any(rows[k].bottom > b.bottom + STACK_TOL for k in between):
            return None
        if any(min(c.x1, o.x1) > max(c.x0, o.x0) for k in between for c in rows[k].cells
               for o in a.cells + b.cells):
            return None  # the lone label sits over a column of the pair: a heading, not a peer
        nxt = next((k for k in range(j + 1, len(rows)) if len(rows[k].cells) >= 2), None)
        if nxt is not None and rows[nxt].top - b.bottom <= NEIGHBOUR_GAP * b.height \
                and _stacks(b, rows[nxt]):
            return None  # three rows that line up: centred columns of words
        return j, between
    return None


def _stacks(a: Row, b: Row) -> bool:
    """Whether every label of row b is centred under a label of row a, one for one, and
    continues it: a second line of a heading starts in lower case ("LOC" / "method"). Rows
    of centred labels in capitals ("32×32" over "1+2n") are rows of their own."""
    if len(a.cells) != len(b.cells) or any(c.numeric for c in b.cells):
        return False
    return all(abs(ca.cx - cb.cx) <= STACK_TOL and min(ca.x1, cb.x1) > max(ca.x0, cb.x0)
               and cb.text[:1].islower() for ca, cb in zip(a.cells, b.cells))


def merge_centred_values(rows: list[Row]) -> list[Row]:
    """A label set on two lines with its values centred between them: "Aspirin for the
    Prevention of Cardiovascular Disease:" / "● ● ● ●" / "Preventive Medication". The three
    rows are one, its label the two lines joined. The value row has no label, the lines touch
    it above and below, and the second line starts on the first one's left edge."""
    out: list[Row] = []
    i = 0
    while i < len(rows):
        if i + 2 < len(rows) and _centred_between(rows[i], rows[i + 1], rows[i + 2]):
            a, values, b = rows[i], rows[i + 1], rows[i + 2]
            first, second = a.cells[0], b.cells[0]
            box = (min(first.x0, second.x0), first.top, max(first.x1, second.x1), second.bottom)
            label = read_cell(replace(first.line, bbox=box, text=f"{first.text} {second.text}"))
            out.append(Row([label or first, *values.cells]))
            i += 3
            continue
        out.append(rows[i])
        i += 1
    return out


def _centred_between(a: Row, values: Row, b: Row) -> bool:
    if len(a.cells) != 1 or len(b.cells) != 1 or a.cells[0].numeric or b.cells[0].numeric:
        return False
    if not values.cells or not all(c.numeric for c in values.cells):
        return False
    first, second = a.cells[0], b.cells[0]
    if abs(first.x0 - second.x0) > LABEL_GROUP or first.text.rstrip().endswith("."):
        return False
    if any(c.x0 < max(first.x1, second.x1) for c in values.cells):
        return False  # the values sit beside the label, not under it
    height = max(first.height, second.height)
    return (b.top - a.bottom <= 0.6 * height
            and a.top + a.bottom < values.top + values.bottom < b.top + b.bottom
            and values.top < a.bottom + 0.3 * height and values.bottom > b.top - 0.3 * height)
