"""Month grids: a printed calendar's months as tables, found from where the lines lie.

A wall calendar prints each month as a title ("J U L Y 2 0 2 4", letters spaced), a header of
seven single letters ("S M T W T F S") and the days as two-digit lines, three or four months
across and as many down. The extraction order of those lines is not geometric, so nothing here
uses it: a header belongs to the title above it, a month ends where the next title, header or
wide line starts, and a day belongs to the column whose header letter it lies under.

The printed days are checked against the weekday arithmetic (`CalendarGrid.check`): the days of
a month are 1 to N, each in the column of its weekday. A grid that fails is still published as
printed, with a warning (the numbers are what the page says).

A colour behind a day, or a mark beside it, says what the day is, and the page's colour key says
what the colour means (`read_key`, `day_status`). A dated list under the key ("July 1-5: Staff
Holiday") names days too (`read_dated_list`). Every word written into a day's cell is printed on
the page, and keeps the box it is printed in: a key's label, or the text of a list entry. A colour
that no key entry names, or a list line the grammar cannot read, goes to review; it never becomes
a guessed label (`annotate`).
"""

from __future__ import annotations

import calendar
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from reflow import geometry
from reflow.analyze.blocks import join_lines
from reflow.extract.drawings import Fill
from reflow.extract.lines import Line
from reflow.extract.tables.cells import TableData, TableFact, TableMethod
from reflow.model import BBox
from reflow.text_quality import squash

MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
          "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER")
WEEKDAYS = "SMTWTFS"  # Sunday first
DAY = re.compile(r"\d{1,2}")
TITLE = re.compile(r"([A-Za-z]+)(\d{4})")
MAX_TITLE_GAP = 4.0     # a title lies within this many header heights above its header
MAX_PITCH_SPREAD = 1.6  # the gaps between a header's letters differ by less than this ratio
MAX_ROW_GAP = 1.8       # a gap of more row pitches than this ends a month's days
SAME_ROW = 0.5          # lines whose tops differ by less than this share of their height are one row


@dataclass(frozen=True)
class CalendarGrid:
    title: str                                  # the printed title, spacing removed: "JULY 2024"
    title_line: Line
    year: int
    month: int
    weekdays: tuple[Line, ...]                  # the 7 printed header letters, left to right
    columns: tuple[float, ...]                  # the 7 column centres (x)
    weeks: tuple[tuple[Line | None, ...], ...]  # day lines by row and column, as printed ("01")
    regions: tuple[tuple[BBox, ...], ...]       # every cell's full region, rows × 7, empty too
    bbox: BBox                                  # title, header and all regions
    lines: frozenset[int]                       # ids of every line the grid consumes
    # Days that share a cell with another day line: (row, column, line). They are consumed too,
    # and read into the cell's text, so the arithmetic sees a cell that holds two dates.
    crowded: tuple[tuple[int, int, Line], ...] = ()

    def cell_text(self, row: int, column: int) -> str:
        line = self.weeks[row][column]
        extra = [l.text.strip() for r, c, l in self.crowded if (r, c) == (row, column)]
        return " ".join(([line.text.strip()] if line else []) + extra)

    def check(self) -> str | None:
        """Why the printed days are not the month's, naming the first bad date, else None. The
        days read row by row must be 1 to N (`calendar.monthrange`) with no duplicate and no
        missing date, each in the column of its weekday (Sunday first)."""
        days = calendar.monthrange(self.year, self.month)[1]
        name = self.title
        expected = 1
        for r, row in enumerate(self.weeks):
            for c in range(7):
                text = self.cell_text(r, c)
                if not text:
                    continue
                if not text.isdigit():
                    return f"{name}: row {r + 1}, column {c + 1} holds {text!r}, not one date"
                n = int(text)
                if n < expected:
                    return f"{name}: date {n} is printed twice"
                if n > expected:
                    return f"{name}: date {expected} is missing (row {r + 1} holds {n} next)"
                if expected > days:
                    return f"{name}: date {n} does not exist"
                want = (date(self.year, self.month, n).weekday() + 1) % 7
                if c != want:
                    return (f"{name}: date {n} is in the {WEEKDAY_NAMES[c]} column, "
                            f"not {WEEKDAY_NAMES[want]}")
                expected += 1
        if expected <= days:
            return f"{name}: date {expected} is missing (the last printed is {expected - 1})"
        return None

    def to_table(self, page: int) -> TableData:
        header = [l.text.strip() for l in self.weekdays]
        rows: list[list[str | None]] = [header]
        boxes: list[list[BBox | None]] = [[l.bbox for l in self.weekdays]]
        cells = [l.bbox for l in self.weekdays]
        lefts = [self.regions[0][c][0] for c in range(7)]
        rights = [self.regions[0][c][2] for c in range(7)]
        grid: list[list[BBox | None]] = [[(lefts[c], l.top, rights[c], l.bottom)
                                          for c, l in enumerate(self.weekdays)]]
        for r, week in enumerate(self.weeks):
            rows.append([self.cell_text(r, c) for c in range(7)])
            boxes.append([l.bbox if l else None for l in week])
            cells += [l.bbox for l in week if l]
            grid.append(list(self.regions[r]))
        cells += [l.bbox for _, _, l in self.crowded]
        mismatch = self.check()
        return TableData(page, self.bbox, rows, method=TableMethod.CALENDAR, header_rows=1,
                         why=f"month grid: {self.title}, {len(self.weeks)} weeks of printed days",
                         cells=cells, grid=grid, boxes=boxes, caption=self.title,
                         caption_bbox=self.title_line.bbox,
                         facts=[TableFact("calendar-grid-mismatch", mismatch, {})] if mismatch else [])

    def to_trace(self) -> dict:
        return {"title": self.title, "bbox": list(self.bbox), "weeks": len(self.weeks),
                "mismatch": self.check()}


WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _title_of(line: Line) -> tuple[str, int, int] | None:
    """(title, year, month) when the line reads `MONTHNAME YYYY` once the spaces between
    letters are removed ("J U L Y 2 0 2 4"), else None."""
    m = TITLE.fullmatch("".join(line.text.split()))
    if not m or m.group(1).upper() not in MONTHS:
        return None
    return f"{m.group(1)} {m.group(2)}", int(m.group(2)), MONTHS.index(m.group(1).upper()) + 1


def _headers(lines: list[Line]) -> list[tuple[Line, ...]]:
    letters = sorted((l for l in lines if len(l.text.strip()) == 1 and l.text.strip() in "SMTWF"),
                     key=lambda l: (l.top, l.x0))
    rows: list[list[Line]] = []
    for l in letters:
        if rows and abs(l.top - rows[-1][0].top) <= SAME_ROW * l.height:
            rows[-1].append(l)
        else:
            rows.append([l])
    found: list[tuple[Line, ...]] = []
    for row in rows:
        row.sort(key=lambda l: l.x0)
        i = 0
        while i + 7 <= len(row):
            run = row[i:i + 7]
            centres = [(l.x0 + l.x1) / 2 for l in run]
            gaps = [b - a for a, b in zip(centres, centres[1:])]
            if ("".join(l.text.strip() for l in run) == WEEKDAYS and min(gaps) > 0
                    and max(gaps) <= MAX_PITCH_SPREAD * min(gaps)):
                found.append(tuple(run))
                i += 7
            else:
                i += 1
    return found


def _owners(titles: list[tuple[Line, str, int, int]],
            headers: list[tuple[Line, ...]]) -> list[tuple[tuple[Line, str, int, int], tuple[Line, ...]]]:
    """Each header with the title that owns it: the nearest title above whose x-centre lies in
    the header's x-extent. A title owns the nearest header below it; the other headers it was
    nearest to belong to no calendar."""
    best: dict[int, tuple[float, tuple[Line, ...]]] = {}
    pairs: dict[int, tuple[Line, str, int, int]] = {}
    for header in headers:
        x0, x1 = min(l.x0 for l in header), max(l.x1 for l in header)
        top, height = header[0].top, header[0].height
        above = [t for t in titles
                 if x0 <= (t[0].x0 + t[0].x1) / 2 <= x1 and t[0].bottom <= top + 1
                 and top - t[0].bottom <= MAX_TITLE_GAP * height]
        if not above:
            continue
        title = max(above, key=lambda t: t[0].bottom)
        gap = top - title[0].bottom
        key = id(title[0])
        if key not in best or gap < best[key][0]:
            best[key] = (gap, header)
            pairs[key] = title
    return [(pairs[k], best[k][1]) for k in pairs]


def _grid(title: tuple[Line, str, int, int], header: tuple[Line, ...], lines: list[Line],
          others: list[Line]) -> CalendarGrid | None:
    """The month under one header: its days, regions and the lines it consumes. `others` are
    the lines that end a month: other titles and the letters of other headers."""
    tline, name, year, month = title
    centres = tuple((l.x0 + l.x1) / 2 for l in header)
    pitch = statistics.median(b - a for a, b in zip(centres, centres[1:]))
    left, right = centres[0] - pitch / 2, centres[-1] + pitch / 2
    top = max(l.bottom for l in header)
    ids = {id(l) for l in header} | {id(tline)}

    def in_band(l: Line) -> bool:
        return l.x1 > left and l.x0 < right

    # The month ends at the first line below its header that is not one of its own: a title, the
    # letters of another header, or a line wider than a cell that is not a day.
    stops = {id(o) for o in others}
    ends = [l.top for l in lines + others
            if id(l) not in ids and in_band(l) and l.top >= top - 1
            and not DAY.fullmatch(l.text.strip()) and (l.x1 - l.x0 > pitch or id(l) in stops)]
    bottom = min(ends, default=float("inf"))
    days: list[tuple[Line, int]] = []
    for l in lines:
        if id(l) in ids or not DAY.fullmatch(l.text.strip()) or l.top < top - 1 or l.bottom > bottom + 1:
            continue
        cx = (l.x0 + l.x1) / 2
        col = min(range(7), key=lambda c: abs(centres[c] - cx))
        if abs(centres[col] - cx) <= pitch / 2:
            days.append((l, col))
    if not days:
        return None
    days.sort(key=lambda d: d[0].top)
    rows: list[list[tuple[Line, int]]] = []
    for d in days:
        if rows and d[0].top - rows[-1][0][0].top <= SAME_ROW * d[0].height:
            rows[-1].append(d)
        else:
            rows.append([d])
    mids = [statistics.mean((l.top + l.bottom) / 2 for l, _ in row) for row in rows]
    gaps = [b - a for a, b in zip(mids, mids[1:])]
    row_pitch = statistics.median(gaps) if gaps else 2.5 * statistics.mean(l.height for l, _ in rows[0])
    for i, gap in enumerate(gaps):
        if gap > MAX_ROW_GAP * row_pitch:
            rows, mids = rows[:i + 1], mids[:i + 1]
            break
    # A cell's region runs to the midpoints between neighbouring centres; the outer edges are half a
    # pitch beyond the first and last.
    xs = [left] + [(a + b) / 2 for a, b in zip(centres, centres[1:])] + [right]
    ys = [mids[0] - row_pitch / 2] + [(a + b) / 2 for a, b in zip(mids, mids[1:])] \
        + [min(mids[-1] + row_pitch / 2, max(bottom, mids[-1] + 1))]
    weeks: list[tuple[Line | None, ...]] = []
    crowded: list[tuple[int, int, Line]] = []
    for r, row in enumerate(rows):
        cells: list[Line | None] = [None] * 7
        for l, c in sorted(row, key=lambda d: d[0].x0):
            if cells[c] is None:
                cells[c] = l
            else:
                crowded.append((r, c, l))
        weeks.append(tuple(cells))
    regions = tuple(tuple((xs[c], ys[r], xs[c + 1], ys[r + 1]) for c in range(7))
                    for r in range(len(rows)))
    consumed = ids | {id(l) for row in rows for l, _ in row}
    box = geometry.union([tline.bbox, *(l.bbox for l in header), *(b for row in regions for b in row)])
    if box is None:
        raise ValueError('calendar: expected box is not None')
    return CalendarGrid(name, tline, year, month, header, centres, tuple(weeks), regions, box,
                        frozenset(consumed), tuple(crowded))


def find_calendars(lines: list[Line]) -> list[CalendarGrid]:
    """The page's month grids, in reading order of their titles (top to bottom, then left to
    right). Every step works on where the lines lie; none uses the order they were extracted in."""
    titles = [(l, *t) for l in lines if (t := _title_of(l))]
    owned = _owners(titles, _headers(lines)) if titles else []
    stops = [t[0] for t, _ in owned] + [l for _, h in owned for l in h]
    grids = []
    for title, header in owned:
        mine = {id(title[0])} | {id(l) for l in header}
        grid = _grid(title, header, lines, [l for l in stops if id(l) not in mine])
        if grid:
            grids.append(grid)
    return sorted(grids, key=lambda g: (round(g.title_line.top / 20), g.title_line.x0))


# --- The colour key, the days it marks, and the dated list ---

KEY_ANCHOR = re.compile(r"(colou?r\s+)?(key|legend)\s*:?", re.I)
COLOUR_TOLERANCE = 40   # per channel: a colour this close to a key colour is that colour
MIN_COLOUR_SHARE = 0.2  # a colour seen on this share of a day's coloured cell marks the day
LATTICE = 5             # sample points across and down a box
MAX_SWATCH = 3.0        # a swatch, or a list's marker, is at most this many text heights on a side
LABEL_REACH = 1.5       # a label starts within this many swatch widths right of its swatch
SAME_X = 2.0            # points: a wrapped line starts this close to the x of the line above
WRAP_GAP = 0.5          # ... less than this share of the text size below it
SAME_SIZE = 0.1         # ... in a size that differs by less than this share
LIST_GAP = 2.0          # a gap of more text sizes than this ends a group of the dated list
DASHES = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
                        "\u2014": "-", "\u2212": "-"})
_MONTH = "|".join(MONTHS)
# "July 1-5: Staff Holiday", "October 11", "December 23-January 3: Winter Break"
ENTRY = re.compile(rf"({_MONTH}) (\d{{1,2}})(?:-(?:({_MONTH}) )?(\d{{1,2}}))?(?::(.*))?", re.I)
STARTS_WITH_MONTH = re.compile(rf"({_MONTH})\b", re.I)

type Rgb = tuple[int, int, int]


@dataclass(frozen=True)
class KeyEntry:
    rgb: Rgb
    shape: str                      # "box" or "mark", as the swatch's Fill
    label: str                      # as printed, its wrapped lines joined (blocks.join_lines)
    label_boxes: tuple[BBox, ...]   # every printed line of the label
    swatch: BBox


@dataclass(frozen=True)
class Status:
    entry: KeyEntry
    evidence: BBox  # the fill that shows the key's colour or mark on the day


@dataclass(frozen=True)
class DatedEntry:
    text: str                  # as printed, its wrapped lines joined
    boxes: tuple[BBox, ...]    # every printed line of the entry
    dates: tuple[date, ...]    # every date of its range
    note: str | None           # the text after the colon, as printed; None for a date alone


@dataclass(frozen=True)
class DatedGroup:
    heading: str
    heading_box: BBox
    key: KeyEntry | None                    # the key entry its marker pairs with; None: not paired
    entries: tuple[DatedEntry, ...]
    unparsed: tuple[tuple[str, BBox], ...]  # lines of the group the grammar did not read

    @property
    def complete(self) -> bool:
        """Every line of the group was read, so it can be checked both ways."""
        return not self.unparsed


@dataclass(frozen=True)
class DatedList:
    groups: tuple[DatedGroup, ...]


def _close(a: Rgb, b: Rgb) -> bool:
    return max(abs(x - y) for x, y in zip(a, b)) <= COLOUR_TOLERANCE


def _match(rgb: Rgb, shape: str, key: Sequence[KeyEntry]) -> KeyEntry | None:
    """The key entry of this shape whose colour is close to `rgb`. None when no entry is, and
    when more than one is: a colour two entries could name is not given either name."""
    found = [e for e in key if e.shape == shape and _close(e.rgb, rgb)]
    return found[0] if len(found) == 1 else None


def _beside(line: Line, box: BBox) -> bool:
    """Whether a swatch or marker is on the line's row: they share at least half of the
    shorter one's height (a marker that only grazes the line under its heading is not its)."""
    shared = min(line.bottom, box[3]) - max(line.top, box[1])
    return shared >= 0.5 * min(line.height, box[3] - box[1])


def _wrapped(first: Line, lines: Sequence[Line], stop: frozenset[int] = frozenset()) -> list[Line]:
    """`first` and the lines wrapped under it: each starts at its x (within SAME_X), in its size
    (within SAME_SIZE), less than WRAP_GAP of that size below the line above, and is not in
    `stop` (ids of lines that start something else)."""
    out = [first]
    while True:
        prev = out[-1]
        below = [l for l in lines if id(l) not in stop and all(l is not o for o in out)
                 and abs(l.x0 - first.x0) <= SAME_X
                 and abs(l.size - first.size) <= SAME_SIZE * first.size
                 and l.top > prev.top + prev.height / 2
                 and l.top - prev.bottom < WRAP_GAP * first.size]
        if not below:
            return out
        out.append(min(below, key=lambda l: l.top))


def _rows(items: list, box, height: float) -> list:
    """Items in reading order by their `box(item)`: rows top to bottom (a row's tops differ by
    less than half `height`), each left to right."""
    rows: list[list] = []
    for item in sorted(items, key=lambda i: box(i)[1]):
        if rows and box(item)[1] - box(rows[-1][0])[1] < height / 2:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [i for row in rows for i in sorted(row, key=lambda i: box(i)[0])]


def read_key(lines: Sequence[Line], fills: Sequence[Fill],
             grids: Sequence[CalendarGrid]) -> list[KeyEntry]:
    """The page's colour key, [] when it has none. The key starts at a line that reads "COLOR
    KEY", "COLOUR KEY", "KEY" or "LEGEND" and ends at the next line at least as large (the next
    heading); only the fills and lines between are read. A swatch is a fill there, at most
    MAX_SWATCH text heights on a side, whose colour and shape also occur in a month grid: a
    decoration that marks no day is no swatch. Its label is the line starting just right of it
    (within LABEL_REACH swatch widths) beside it, with the lines wrapped under it (`_wrapped`).
    Entries come in reading order."""
    anchors = [l for l in lines if KEY_ANCHOR.fullmatch(l.text.strip())]
    if not anchors or not grids:
        return []
    anchor = min(anchors, key=lambda l: (l.top, l.x0))
    end = min((l.top for l in lines if l.top > anchor.bottom and l.size >= anchor.size),
              default=float("inf"))
    region = (float("-inf"), anchor.top, float("inf"), end)
    text = [l for l in lines if l is not anchor and geometry.centre_in(l.bbox, region)]
    if not text:
        return []
    height = statistics.median(l.height for l in text)
    in_grids = [f for f in fills if any(geometry.centre_in(f.bbox, g.bbox) for g in grids)]
    swatches = [f for f in fills if geometry.centre_in(f.bbox, region)
                and max(f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]) <= MAX_SWATCH * height
                and any(g.shape == f.shape and _close(g.rgb, f.rgb) for g in in_grids)]
    firsts: list[tuple[Fill, Line]] = []
    for f in swatches:
        reach = f.bbox[2] + LABEL_REACH * (f.bbox[2] - f.bbox[0])
        beside = [l for l in text if f.bbox[2] - SAME_X <= l.x0 <= reach and _beside(l, f.bbox)]
        if beside:
            firsts.append((f, min(beside, key=lambda l: l.x0)))
    starts = frozenset(id(l) for _, l in firsts)
    entries = []
    for f, first in firsts:
        label = _wrapped(first, text, starts - {id(first)})
        entries.append(KeyEntry(f.rgb, f.shape, join_lines([l.text for l in label]),
                                tuple(l.bbox for l in label), f.bbox))
    return _rows(entries, lambda e: e.swatch, height)


def _lattice(box: BBox) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = box
    return [(x0 + (i + 0.5) * (x1 - x0) / LATTICE, y0 + (j + 0.5) * (y1 - y0) / LATTICE)
            for i in range(LATTICE) for j in range(LATTICE)]


def _top(boxes: Sequence[Fill], x: float, y: float) -> Fill | None:
    """The fill the reader sees at a point: the one painted last of those that paint it."""
    return max((f for f in boxes if f.holds(x, y)), key=lambda f: f.order, default=None)


def cell_colours(line: Line, region: BBox, boxes: Sequence[Fill]) -> Counter[Fill]:
    """How many of a LATTICE × LATTICE sample of a day's coloured cell each fill shows. The
    coloured cell is the day's region, clipped to the box of the fills under its numeral: the
    colours behind a day may split beside the numeral rather than across it (a cell half
    yellow, half green, cut on the diagonal)."""
    under = {f for x, y in _lattice(line.bbox) if (f := _top(boxes, x, y)) is not None}
    painted = geometry.union([f.bbox for f in under])
    cell = painted and (max(painted[0], region[0]), max(painted[1], region[1]),
                        min(painted[2], region[2]), min(painted[3], region[3]))
    seen: Counter[Fill] = Counter()
    if cell and cell[0] < cell[2] and cell[1] < cell[3]:
        for x, y in _lattice(cell):
            if (f := _top(boxes, x, y)) is not None:
                seen[f] += 1
    return seen


def day_status(grid: CalendarGrid, fills: Sequence[Fill], key: Sequence[KeyEntry]
               ) -> tuple[dict[tuple[int, int], list[Status]], list[tuple[int, int, Rgb, BBox]]]:
    """Each day's statuses by (week, column) of the grid, in key order, and the colours and
    marks on days that no key entry names, as (week, column, colour, box).

    A colour marks the day when the reader sees it on at least MIN_COLOUR_SHARE of the day's
    coloured cell (`cell_colours`): at each sample point the box fill painted last that paints
    the point (`Fill.holds`), so a colour hidden under a later fill marks nothing. A mark (a
    drawn symbol, such as a star) marks the day whose region holds its centre: it sits beside
    the numeral, not on it. A day keeps every entry it matches (`_match`)."""
    near = [f for f in fills if geometry.overlap(f.bbox, grid.bbox) > 0]
    boxes = [f for f in near if f.shape == "box"]
    marks = [f for f in near if f.shape == "mark"]
    found: dict[tuple[int, int], list[Status]] = {}
    unmapped: list[tuple[int, int, Rgb, BBox]] = []
    for r, week in enumerate(grid.weeks):
        for c, line in enumerate(week):
            if line is None:
                continue
            seen = cell_colours(line, grid.regions[r][c], boxes)
            shares: Counter[Rgb] = Counter()
            for f, n in seen.items():
                shares[f.rgb] += n
            total = sum(shares.values())
            shown = [(f.rgb, f.bbox, "box") for f, _ in seen.most_common()
                     if shares[f.rgb] >= MIN_COLOUR_SHARE * total]
            shown += [(m.rgb, m.bbox, "mark") for m in marks if geometry.centre_in(m.bbox, grid.regions[r][c])]
            statuses: dict[KeyEntry, Status] = {}
            missed: dict[Rgb, BBox] = {}
            for rgb, box, shape in shown:
                entry = _match(rgb, shape, key)
                if entry is not None:
                    statuses.setdefault(entry, Status(entry, box))
                else:
                    missed.setdefault(rgb, box)
            if statuses:
                found[(r, c)] = sorted(statuses.values(), key=lambda s: key.index(s.entry))
            unmapped += [(r, c, rgb, box) for rgb, box in missed.items()]
    return found, unmapped


def _years(grids: Sequence[CalendarGrid]) -> dict[int, int]:
    """Each month the grids print, with its year; a month printed in two years has none."""
    seen: dict[int, set[int]] = {}
    for g in grids:
        seen.setdefault(g.month, set()).add(g.year)
    return {m: next(iter(ys)) for m, ys in seen.items() if len(ys) == 1}


def _dates(m: re.Match, years: dict[int, int]) -> tuple[date, ...] | None:
    """The dates an entry names, each month in the one year the grids print it; None when a
    month is not printed in one year, a date does not exist, or the range runs backwards."""
    first = MONTHS.index(m[1].upper()) + 1
    last = MONTHS.index(m[3].upper()) + 1 if m[3] else first
    if first not in years or last not in years:
        return None
    try:
        start = date(years[first], first, int(m[2]))
        end = date(years[last], last, int(m[4] or m[2]))
    except ValueError:
        return None
    if not start <= end <= start + timedelta(days=366):
        return None
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


def _entry(text: str, years: dict[int, int]) -> tuple[tuple[date, ...], str | None] | None:
    plain = re.sub(r"\s*-\s*", "-", squash(text.translate(DASHES)))
    m = ENTRY.fullmatch(plain)
    if not m or (dates := _dates(m, years)) is None:
        return None
    note = squash(text.split(":", 1)[1]) if m[5] is not None else None
    return (dates, note) if note != "" else None


def read_dated_list(lines: Sequence[Line], fills: Sequence[Fill], key: Sequence[KeyEntry],
                    grids: Sequence[CalendarGrid]) -> DatedList:
    """The groups of a dated list ("HOLIDAYS" over "July 1-5: Staff Holiday", ...).

    A group's heading is a line with a marker fill just left of it (within LABEL_REACH marker
    widths, on its row: `_beside`), with the lines wrapped under it. The group pairs with the key entry of
    its marker's colour and shape: the same printed colour, no comparison of words. Its lines
    start between the marker's left edge and the heading's right edge, below the heading, and
    run to the next heading there or to a gap of LIST_GAP sizes of the line above. Each line is an entry
    (`ENTRY`: "Month D[-[Month ]D][: text]"), the wrapped text of the entry above (indented, no
    month), a subtitle (before the first entry, no digit: it names no date), or unparsed.
    The lines of the grids and the key's labels are never read."""
    held = frozenset().union(*(g.lines for g in grids)) if grids else frozenset()
    labels = {b for e in key for b in e.label_boxes}
    free = [l for l in lines if id(l) not in held and l.bbox not in labels]
    heads: list[tuple[Line, Fill]] = []
    for l in free:
        markers = [f for f in fills if f.bbox[2] <= l.x0 + 1
                   and l.x0 - f.bbox[2] <= LABEL_REACH * (f.bbox[2] - f.bbox[0])
                   and _beside(l, f.bbox)
                   and max(f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]) <= MAX_SWATCH * l.height]
        if markers:
            heads.append((l, max(markers, key=lambda f: f.bbox[2])))
    starts = frozenset(id(l) for l, _ in heads)
    headings = [(_wrapped(l, free, starts - {id(l)}), f) for l, f in heads]
    taken = frozenset(id(l) for h, _ in headings for l in h)
    years = _years(grids)
    groups = []
    for heading, marker in headings:
        x_lo, x_hi = marker.bbox[0] - SAME_X, max(l.x1 for l in heading)
        last = heading[-1]
        below = last.top + last.height / 2
        end = min((h[0].top for h, _ in headings if h[0].top > heading[0].top
                   and x_lo <= h[0].x0 <= x_hi), default=float("inf"))
        body = sorted((l for l in free if id(l) not in taken and x_lo <= l.x0 <= x_hi
                       and below < l.top < end), key=lambda l: l.top)
        entries: list[tuple[list[Line], tuple[date, ...], str | None]] = []
        unparsed: list[tuple[str, BBox]] = []
        prev, current = last, None
        for l in body:
            if l.top - prev.bottom > LIST_GAP * prev.size:
                break
            prev = l
            read = _entry(l.text, years)
            if read is not None:
                current = ([l], *read)
                entries.append(current)
            elif not entries and not unparsed and not any(ch.isdigit() for ch in l.text):
                continue  # a subtitle ("District Closed")
            elif (current is not None and current[2] is not None and l.x0 > current[0][0].x0 + SAME_X
                  and not STARTS_WITH_MONTH.match(l.text.strip())):
                current[0].append(l)
                entries[-1] = current = (current[0], current[1],
                                         join_lines([current[2], l.text]))
            else:
                unparsed.append((l.text, l.bbox))
                current = None
        group_key = _match(marker.rgb, marker.shape, key)
        box = geometry.union([l.bbox for l in heading])
        if box is None:
            raise ValueError('calendar: expected box is not None')
        groups.append(DatedGroup(
            join_lines([l.text for l in heading]), box, group_key,
            tuple(DatedEntry(join_lines([l.text for l in ls]), tuple(l.bbox for l in ls), dates, note)
                  for ls, dates, note in entries),
            tuple(unparsed)))
    return DatedList(tuple(groups))


def annotate(grids: Sequence[CalendarGrid], tables: Sequence[TableData], lines: Sequence[Line],
             fills: Sequence[Fill]) -> None:
    """Write what the page prints about each day into its month table (`tables[i]` is
    `grids[i]`'s): `days[(row, column)]` holds the key labels of its colours and
    marks ("status") and the text of the list entries that name it ("notes"), each with the
    boxes its words are printed in. The doubts go to review as one fact per table or per page,
    never one per day: colours no key entry names (`calendar-colour-unmapped`, on the table),
    and, on the page's first month table, the list lines the grammar could not read
    (`calendar-list-unparsed`) and the disagreements between the list and the colours
    (`calendar-list-mismatch`). A group whose lines were all read is checked both ways: each
    entry must name a day of its colour, and each day of its colour must be named. A group
    with an unread line is checked one way only (an unread line may name any date). A fact's
    data are parallel lists, its boxes four numbers each, one box per item."""
    if not grids:
        return
    key = read_key(lines, fills, grids)
    by_date: dict[date, tuple[int, int, int]] = {}  # date → (grid, row, column), in grids that check
    marked: dict[date, list[Status]] = {}
    for gi, (g, t) in enumerate(zip(grids, tables)):
        found, unmapped = day_status(g, fills, key)
        good = g.check() is None
        for (r, c), statuses in found.items():
            t.days[(r + 1, c)] = {"status": [
                {"label": s.entry.label, "label_boxes": [geometry.rounded(b) for b in s.entry.label_boxes],
                 "swatch": geometry.rounded(s.entry.swatch), "evidence": geometry.rounded(s.evidence)} for s in statuses]}
        for r, week in enumerate(g.weeks):
            for c, line in enumerate(week):
                text = g.cell_text(r, c)
                if good and line is not None and text.isdigit():
                    day = date(g.year, g.month, int(text))
                    by_date[day] = (gi, r, c)
                    marked[day] = found.get((r, c), [])
        if unmapped:
            days = [f"{g.title} {g.cell_text(r, c)}" for r, c, _, _ in unmapped]
            colours = ["#%02x%02x%02x" % rgb for _, _, rgb, _ in unmapped]
            t.facts.append(TableFact("calendar-colour-unmapped",
                                   f"{g.title}: {len(days)} coloured or marked days that no entry of the "
                                   "colour key names: " + ", ".join(
                                       f"{d} ({c})" for d, c in list(zip(days, colours))[:5]),
                                   {"days": days, "colours": colours,
                                    "boxes": [v for *_, box in unmapped for v in geometry.rounded(box)]}))
    listed = read_dated_list(lines, fills, key, grids)
    for group in listed.groups:
        for e in group.entries:
            if e.note is None:
                continue
            box = geometry.union(list(e.boxes))
            if box is None:
                raise ValueError('calendar: expected box is not None')
            for day in e.dates:
                if day in by_date:
                    gi, r, c = by_date[day]
                    cell = tables[gi].days.setdefault((r + 1, c), {})
                    cell.setdefault("notes", []).append({"text": e.note, "box": geometry.rounded(box)})

    def quote(day: date) -> str:
        gi, r, c = by_date[day]
        return " — ".join([f"{grids[gi].title} {grids[gi].cell_text(r, c)}",
                           *(s.entry.label for s in marked[day])])

    # (entry or "", day or "", why, box): the entry's box, or the day's numeral's
    conflicts: list[tuple[str, str, str, BBox]] = []
    unread: list[tuple[str, BBox]] = []
    unvalidated: list[str] = []
    for group in listed.groups:
        if group.key is None:
            continue
        k = group.key
        unread += group.unparsed
        for e in group.entries:
            shown = [d for d in e.dates if d in by_date]
            if shown and not any(any(s.entry == k for s in marked[d]) for d in shown):
                box = geometry.union(list(e.boxes))
                if box is None:
                    raise ValueError('calendar: expected box is not None')
                conflicts.append((e.text, "", f"{e.text!r} under {group.heading} names no day "
                                              f"marked {k.label!r}", box))
        if not group.complete:
            unvalidated.append(group.heading)
            continue
        named = {d for e in group.entries for d in e.dates}
        for day, statuses in sorted(marked.items()):
            if day not in named and any(s.entry == k for s in statuses):
                gi, r, c = by_date[day]
                line = grids[gi].weeks[r][c]
                if line is None:
                    raise ValueError('calendar: expected line is not None')
                conflicts.append(("", quote(day), f"{quote(day)} is marked {k.label!r}, and no "
                                                  f"entry under {group.heading} names it", line.bbox))
    first = tables[0]
    if unread:
        first.facts.append(TableFact("calendar-list-unparsed",
                                   f"{len(unread)} lines of the dated list could not be read, so "
                                   f"{', '.join(unvalidated)} could not be checked against the "
                                   "calendar: " + "; ".join(repr(text) for text, _ in unread[:4]),
                                   {"lines": [text for text, _ in unread],
                                    "boxes": [v for _, box in unread for v in geometry.rounded(box)],
                                    "unvalidated": unvalidated}))
    if conflicts:
        first.facts.append(TableFact("calendar-list-mismatch",
                                   f"{len(conflicts)} disagreements between the dated list and the "
                                   "calendar: " + "; ".join(c[2] for c in conflicts[:3]),
                                   {"entries": [c[0] for c in conflicts], "days": [c[1] for c in conflicts],
                                    "conflicts": [c[2] for c in conflicts],
                                    "boxes": [v for c in conflicts for v in geometry.rounded(c[3])]}))
