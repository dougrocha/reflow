"""Columns and reading order: gutters, the recursive XY cut, and the form rows
that span them.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from statistics import median

from reflow.analyze.constants import (
    CENTRE_TOL,
    HEADING_SCALE,
    SHORT_BLOCK_WORDS,
    SPLIT_COLUMNS,
    SPLIT_NONE,
    SPLIT_SOFT,
)
from reflow.extract.lines import Line
from reflow.model import BBox


# Vertical whitespace crossed by no line separates columns when it is this wide (points): the
# 8 pt, less a margin for rounding of the glyph boxes. A narrower gap (down
# to MIN_GUTTER_EM font sizes: a directory's narrow columns) must have text on both sides, since
# table cells sit just as close together.
WIDE_GUTTER = 7.5
MIN_GUTTER_EM = 0.5
# Horizontal whitespace at least this many body-line heights tall separates bands.
MIN_BAND_GAP = 1.2
# Lines whose left edges sit this close (points) share a column's left edge.
COLUMN_EDGE_TOL = 1.0


# A column run (the lines between the lines crossing a soft gutter) needs this many lines
# on each side of the gutter. A soft gutter needs this many lines on its smaller side,
# counted over its column runs.
MIN_RUN_SIDE = 3
MIN_COLUMN_SIDE = 3


# Column text has words on each side: a side whose lines have fewer words (median) is a
# table's cells (numbers, single labels), which read row by row. Two-word lines are not: the
# address and contact lines of a directory ("Phoenix, AZ", "Website: www.x.com") are columns.
MIN_COLUMN_WORDS = 2


# A crossing line this share of the region's width is a full-width break between bands; a
# shorter one is text of a column (a title, a form row), kept with the column it mostly overlaps.
FULL_WIDTH_SHARE = 0.6


# A gutter that partial lines cross (lines that are neither full-width breaks nor part of a band)
# is not a gutter: column text does not run across one, so a form or a table whose text crosses
# a strip at many places is not split there.
MAX_PARTIAL_CROSS = 0.1


# A form row: two short labels on one baseline, one on each side of a gutter. Tops and bottoms
# agree within this share of the line height. The right label must have no right-side text
# within FORM_ROW_GAP font sizes above or below it (it stands alone in its column).
FORM_ROW_TOL = 0.25
FORM_ROW_GAP = 0.5


def min_gutter(lines: list[Line]) -> float:
    """The narrowest gutter that separates columns among these lines (see MIN_GUTTER_EM)."""
    return MIN_GUTTER_EM * median(l.size for l in lines) if lines else 0.0


def _column_gutter(lines: list[Line], gaps: list[tuple[float, float]]
                   ) -> tuple[float, float] | None:
    """The widest of the clear gaps that separates two columns of text (see WIDE_GUTTER)."""
    best: tuple[float, float] | None = None
    for lo, hi in gaps:
        if hi - lo < WIDE_GUTTER:
            left = [l for l in lines if l.x1 <= lo + 0.01]
            right = [l for l in lines if l.x0 >= hi - 0.01]
            if min(_median_words(left), _median_words(right)) < MIN_COLUMN_WORDS:
                continue
        if best is None or hi - lo > best[1] - best[0]:
            best = (lo, hi)
    return best


def empty_gaps(intervals: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]:
    """Empty stretches between the union of `intervals`, at least `min_gap` wide."""
    gaps = []
    intervals = sorted(intervals)
    end = intervals[0][1]
    for start, stop in intervals[1:]:
        if start - end >= min_gap:
            gaps.append((end, start))
        end = max(end, stop)
    return gaps


def _in_ruled(line: Line, ruled: Sequence[BBox]) -> bool:
    cx, cy = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
    return any(b[0] - 2 <= cx <= b[2] + 2 and b[1] - 2 <= cy <= b[3] + 2 for b in ruled)


def _gutter_runs(lines: list[Line], lo: float, hi: float, ruled: Sequence[BBox] = ()
                 ) -> tuple[list[list[Line]], list[list[Line]], dict[int, str]]:
    """The lines that cross the strip (lo, hi) or sit in a ruled table, grouped into breaks,
    and the runs of the other lines between the breaks: run k holds the lines whose middle sits
    below k breaks. A break is a full-width line (see FULL_WIDTH_SHARE), a line of a ruled
    table, or a band of consecutive crossing lines (a multi-line title). Any other crossing line
    mostly overlaps one column: it stays in that column's run, and `sides` maps its id to "L" or
    "R". Each run is a stretch of the region where the strip is clear."""
    def crossing(line: Line) -> bool:
        return line.x1 > lo + 1 and line.x0 < hi - 1

    crossers = sorted((l for l in lines if crossing(l)), key=lambda l: (l.top, l.x0))
    x_min = min(l.x0 for l in lines)
    x_max = max(l.x1 for l in lines)
    gap = MIN_BAND_GAP * median(l.height for l in lines)
    # A crossing line with a crossing neighbour close above or below is part of a band.
    near = set()
    for a, b in zip(crossers, crossers[1:]):
        if b.top - a.bottom <= gap:
            near.update((id(a), id(b)))
    centred = _centred_lines(lines, gap)
    sides: dict[int, str] = {}
    is_break: dict[int, bool] = {}
    for l in crossers:
        full = l.x1 - l.x0 >= FULL_WIDTH_SHARE * (x_max - x_min)
        if full or id(l) in near:
            is_break[id(l)] = True
        else:
            is_break[id(l)] = False
            left = max(0.0, min(l.x1, lo) - max(l.x0, x_min))
            right = max(0.0, min(l.x1, x_max) - max(l.x0, hi))
            sides[id(l)] = "L" if left >= right else "R"
    for l in lines:
        if id(l) in centred:
            is_break[id(l)] = True
    # Consecutive breaks in reading order, close together vertically, form one unit.
    ordered = sorted((l for l in lines if id(l) in is_break and is_break[id(l)]),
                     key=lambda l: (l.top, l.x0))
    units: list[list[Line]] = []
    prev: Line | None = None
    for l in ordered:
        if prev is not None and l.top - prev.bottom <= gap:
            units[-1].append(l)
        else:
            units.append([l])
        prev = l
    bottoms = [max(u.bottom for u in unit) for unit in units]
    runs: list[list[Line]] = [[] for _ in range(len(units) + 1)]
    for line in lines:
        if not (id(line) in is_break and is_break[id(line)]):
            runs[bisect_right(bottoms, (line.top + line.bottom) / 2)].append(line)
    return units, runs, sides


def _centred_lines(lines: list[Line], gap: float) -> set[int]:
    """Ids of lines that form a centred block: lines whose middles sit on one axis, close
    together vertically, with different left edges, and at least two of them set larger than the
    body text: a multi-line title with its sub-line. Body text is not centred."""
    out: set[int] = set()
    body = median(l.size for l in lines)
    by_centre = sorted(lines, key=lambda l: (l.x0 + l.x1) / 2)
    cluster: list[Line] = []
    clusters: list[list[Line]] = []
    for l in by_centre:
        if cluster and (l.x0 + l.x1) / 2 - (cluster[-1].x0 + cluster[-1].x1) / 2 > CENTRE_TOL:
            clusters.append(cluster)
            cluster = []
        cluster.append(l)
    if cluster:
        clusters.append(cluster)
    for cl in clusters:
        cl.sort(key=lambda l: l.top)
        block: list[Line] = []
        for l in cl + [None]:  # type: ignore[list-item]
            if l is not None and (not block or l.top - block[-1].bottom <= gap):
                block.append(l)
                continue
            large = sum(1 for b in block if b.size >= HEADING_SCALE * body)
            if (len(block) >= 2 and large >= 2
                    and max(b.x0 for b in block) - min(b.x0 for b in block) > CENTRE_TOL):
                out.update(id(b) for b in block)
            block = [l] if l is not None else []
    return out


def _side(line: Line, lo: float, hi: float, sides: dict[int, str]) -> str | None:
    """"L" or "R": the column a line of a gutter run belongs to, or None."""
    if id(line) in sides:
        return sides[id(line)]
    if line.x1 <= lo + 1:
        return "L"
    if line.x0 >= hi - 1:
        return "R"
    return None


def _is_column_run(run: list[Line], lo: float, hi: float, sides: dict[int, str]) -> bool:
    """Whether a run of the strip (lo, hi) is two columns: enough lines on each side."""
    left = [l for l in run if _side(l, lo, hi, sides) == "L"]
    right = [l for l in run if _side(l, lo, hi, sides) == "R"]
    return (len(left) >= MIN_RUN_SIDE and len(right) >= MIN_RUN_SIDE
            # Cells beside labels (a worksheet's values, a table's numbers) read row by row.
            and min(_median_words(left), _median_words(right)) >= MIN_COLUMN_WORDS)


def _median_words(lines: list[Line]) -> float:
    return median(len(l.text.split()) for l in lines) if lines else 0.0


def xy_cut(lines: list[Line], line_height: float, ruled: Sequence[BBox] = ()
           ) -> tuple[list[Line], int]:
    """Recursive XY-cut: columns (left to right) before bands (top to bottom).
    Returns the lines in reading order and how they were split: SPLIT_NONE, SPLIT_COLUMNS
    (clean gutters), or SPLIT_SOFT (a gutter crossed by a few lines, the least reliable).
    `ruled` are the boxes of ruled tables (see drawings.MIN_RULED_RULES): their text is never split into
    columns by a soft gutter."""
    if len(lines) <= 1:
        return lines, SPLIT_NONE
    column = _column_gutter(lines, empty_gaps([(l.x0, l.x1) for l in lines], min_gutter(lines)))
    if column:
        # Split at the widest gutter; recursion handles the others.
        lo, hi = column
        left = [l for l in lines if l.x1 <= lo + 0.01]
        right = [l for l in lines if l.x0 >= hi - 0.01]
        a, ma = xy_cut(left, line_height, ruled)
        b, mb = xy_cut(right, line_height, ruled)
        rows = _form_rows(lines, lines, lo, hi, {}, ruled)
        return _join_form_rows(a, b, rows), max(SPLIT_COLUMNS, ma, mb)
    bands = empty_gaps([(l.top, l.bottom) for l in lines], MIN_BAND_GAP * line_height)
    if bands:
        out, split = [], SPLIT_NONE
        edges = [g[0] for g in bands] + [float("inf")]
        start = float("-inf")
        parts: list[list[Line]] = []
        for edge in edges:
            parts.append([l for l in lines if start <= l.top and l.bottom <= edge + 0.01])
            start = edge
        for part in _join_column_bands(parts):
            ordered, s = xy_cut(part, line_height, ruled)
            out += ordered
            split = max(split, s)
        return out, split
    soft = _soft_gutter(lines, ruled)
    if soft:
        return _split_around_spanning(lines, soft, line_height, ruled), SPLIT_SOFT
    return sorted(lines, key=lambda l: (round(l.top), l.x0)), SPLIT_NONE


def _join_column_bands(parts: list[list[Line]]) -> list[list[Line]]:
    """Bands that no line crosses between them, and that together have a clean gutter, are one
    stretch of columns: a row of headings over a row of text cards, in a page whose other lines
    run across the gutter, reads one column at a time, not all the headings and then all the
    text. Separate rows of a form are not (a "Well Sick If Sick" row over a "NICU Admit? Yes No"
    row, or "Phone # Fax #" blocks of a request form): its rows read one at a time, a question
    beside its answers. One band of the pair must hold a paragraph, lines set close above each
    other as a card's text or a two-line heading."""
    groups: list[list[Line]] = []
    prev: list[Line] = []
    for part in parts:
        if (groups and len(groups[-1]) > 1 and len(part) > 1
                and (_has_paragraph(prev) or _has_paragraph(part))):
            joined = groups[-1] + part
            if _column_gutter(joined, empty_gaps([(l.x0, l.x1) for l in joined],
                                                 min_gutter(joined))):
                groups[-1] = joined
                prev = part
                continue
        groups.append(part)
        prev = part
    return groups


def _has_paragraph(lines: list[Line]) -> bool:
    """Whether two of these lines sit close above each other in one column, as the lines of a
    paragraph do (a form's rows have more space between them: FORM_ROW_GAP)."""
    return any(0 <= _clearance(b, a) <= FORM_ROW_GAP * a.size and a.x0 < b.x1 and b.x0 < a.x1
               for a in lines for b in lines if b.top > a.top)


def _soft_gutter(lines: list[Line], ruled: Sequence[BBox] = ()) -> tuple[float, float] | None:
    """A gutter that other lines cross, in some stretch of the region. Candidates: a column
    starts at a left edge that several lines share (hi), and the gutter starts after a line's
    right edge (lo). A candidate counts the lines in its column runs (see _gutter_runs), not
    all of them: a form or a full-width heading above a two-column text crosses the gutter, but
    the text below it is still two columns. Both sides must hold enough lines of text: the
    candidate with the most lines on its smaller side wins (a sidebar's edge has many on one
    side only)."""
    if len(lines) < 2 * MIN_COLUMN_SIDE:
        return None
    xs0 = sorted(l.x0 for l in lines)
    edges = sorted({v for i, v in enumerate(xs0)
                    if bisect_right(xs0, v + COLUMN_EDGE_TOL) - i >= MIN_RUN_SIDE})
    rights = sorted({l.x1 for l in lines})
    gutter = min_gutter(lines)
    best: tuple[float, float] | None = None
    best_key: tuple[int, int, float] = (0, 0, 0.0)  # (smaller side, breaks, width)
    for hi in edges:
        # The gutter starts after the rightmost line that ends clear of it: a line that ends
        # inside the gutter's reach counts as spanning, one that ends before it does not.
        ends = [x for x in rights if x <= hi - gutter]
        if not ends:
            continue
        lo = ends[-1]
        units, runs, sides = _gutter_runs(lines, lo, hi, ruled)
        if len(sides) > MAX_PARTIAL_CROSS * len(lines):
            continue
        column = [run for run in runs if _is_column_run(run, lo, hi, sides)]
        left = [l for run in column for l in run if _side(l, lo, hi, sides) == "L"]
        right = [l for run in column for l in run if _side(l, lo, hi, sides) == "R"]
        if min(len(left), len(right)) < MIN_COLUMN_SIDE:
            continue
        if min(_median_words(left), _median_words(right)) < MIN_COLUMN_WORDS:
            continue
        # Ties on the smaller side go to the strip that cuts through the most breaks: a title
        # across the gutter is then one unit, not split from the text beside it.
        key = (min(len(left), len(right)), sum(len(u) for u in units), hi - lo)
        if key > best_key:
            best, best_key = (lo, hi), key
    return best


def _same_baseline(a: Line, b: Line) -> bool:
    tol = FORM_ROW_TOL * min(a.height, b.height)
    return abs(a.top - b.top) <= tol and abs(a.bottom - b.bottom) <= tol


def _form_rows(run: list[Line], lines: list[Line], lo: float, hi: float,
               sides: dict[int, str], ruled: Sequence[BBox] = ()) -> dict[int, list[Line]]:
    """Form rows in a column run: id(left piece) → the right pieces on its baseline. A row is a
    short label on the left and a short label on the right on one baseline ("U.S. person" and
    "Date" of a signature line). The right piece must stand alone on its side of `lines` (no
    right-side line sits right above or below it): a directory's columns run beside each other
    line by line, and their rows must not be joined. The left piece must end its block (no
    left-side line right below it): a flowchart's "No" beside a box would otherwise be read in
    the middle of the box's sentence (p17 p43)."""
    def label(line: Line) -> bool:
        # A short label of words in running type: not a figure, not a capitalised banner or
        # heading, and not a row of labels on its own baseline (a table's year headings "2024 est.
        # 2025 est." are a header row, not a form row).
        # A bold label is a form label only inside a ruled form area ("U.S. person" and "Date"
        # of the W-9 signature row): elsewhere bold is a heading or a banner.
        return (_words(line) <= SHORT_BLOCK_WORDS and sum(ch.isalpha() for ch in line.text) >= 2
                and not any(ch.isdigit() for ch in line.text) and not line.text.isupper()
                and (not line.bold or _in_ruled(line, ruled)))

    left = [l for l in run if _side(l, lo, hi, sides) == "L" and label(l)]
    rows: dict[int, list[Line]] = {}
    for piece in run:
        if _side(piece, lo, hi, sides) != "R" or not label(piece):
            continue
        mates = [a for a in left if _same_baseline(a, piece) and abs(a.size - piece.size) < 0.5
                 and not any(o is not a and _side(o, lo, hi, sides) == "L" and o.top > a.top
                             and o.x0 < a.x1 and a.x0 < o.x1
                             and _clearance(o, a) <= FORM_ROW_GAP * a.size for o in lines)]
        if not mates or any(o is not piece and _side(o, lo, hi, sides) == "R"
                            and (_same_baseline(o, piece) or _clearance(o, piece) <= FORM_ROW_GAP * piece.size)
                            for o in lines):
            continue
        rows.setdefault(id(max(mates, key=lambda a: a.x1)), []).append(piece)
    return rows


def _words(line: Line) -> int:
    return len(line.text.split())


def _clearance(a: Line, b: Line) -> float:
    """Vertical space between two lines (negative when they overlap)."""
    return max(a.top - b.bottom, b.top - a.bottom)


def _join_form_rows(left: list[Line], right: list[Line], rows: dict[int, list[Line]]
                    ) -> list[Line]:
    """A column run in reading order: the left column, with each form row's right piece read
    right after its left piece, then the rest of the right column."""
    attached = {id(p) for pieces in rows.values() for p in pieces}
    out: list[Line] = []
    for line in left:
        out.append(line)
        out += sorted(rows.get(id(line), []), key=lambda l: l.x0)
    out += [l for l in right if id(l) not in attached]
    return out


def _split_around_spanning(lines: list[Line], gutter: tuple[float, float],
                           line_height: float, ruled: Sequence[BBox] = ()) -> list[Line]:
    """Lines crossing the gutter sit between runs (see _gutter_runs). A column run reads its
    left side, then its right side (form rows joined, see _form_rows); any other run keeps the
    reading order of its own region."""
    lo, hi = gutter
    breaks, runs, sides = _gutter_runs(lines, lo, hi, ruled)
    out: list[Line] = []
    for k, run in enumerate(runs):
        if run and _is_column_run(run, lo, hi, sides):
            left = [l for l in run if _side(l, lo, hi, sides) == "L"]
            right = [l for l in run if _side(l, lo, hi, sides) == "R"]
            out += _join_form_rows(xy_cut(left, line_height, ruled)[0],
                                   xy_cut(right, line_height, ruled)[0],
                                   _form_rows(run, lines, lo, hi, sides, ruled))
        elif len(run) == len(lines):  # no line crosses the strip: nothing to split on
            out += sorted(run, key=lambda l: (round(l.top), l.x0))
        elif run:
            out += xy_cut(run, line_height, ruled)[0]
        if k < len(breaks):
            out += sorted(breaks[k], key=lambda l: (round(l.top), l.x0))
    return out
