"""A printed form drawn as one ruled grid is read as a form, not as a table.

Some printed forms (an ADA dental claim, a CMS-1500 claim) rule their whole page into boxes:
each box holds a numbered caption in its top-left corner ("12. Policyholder/Subscriber Name")
and an empty area below it to write in. The ruled-table finder takes the page's rules for one
grid, and the page comes out as a table wider than the page, its cells in the order the rules
cut them, its captions run together. Such a grid is not data: no column means anything, and the
rows do not repeat. It is a form layout, and it reads as one: section headings, then each
caption with its choices, in the form's printed numbering.

`split` takes the page's ruled tables and finds the grids that are form layouts:
- a repeated band stays a table: rows that share one set of column edges (the record of services
  of a claim, its lines 1 to 10 under the headers 24 to 31) are a real table, with their header
  rows above them, and are cut out of the grid as a table of their own;
- the rest is a form when most of its text is item captions (a number and a full stop, "1.",
  "29a."), its caption boxes have their caption at the top and room below it, and its rows do not
  repeat. Anything else, such as a table with a numbered first column, is left as it was.

`build` then reads the region's text lines (not the table's cell strings: the cells of a form's
grid are cut by every rule on the page, and their text comes out in pieces and doubled):
- a line that starts with an item number starts an item; a bold line that starts no item is a
  section bar, a heading; the lines of a box that follow either, with no gap, are its text;
- the items are put in the printed numbering. A bar goes before the item that follows it, and
  an unnumbered item after the item above it, so the sections stay whole;
- the controls the form planner (analyze.forms) found in the region, a fieldset for each group
  of choices, go in the same order, each by the number its legend or its label starts with.
No line is dropped: every line of the region is in the items, or was taken as a control's label.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from reflow import geometry
from reflow.analyze.constants import CONF_PARAGRAPH, CONF_TABLE, HANG
from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.extract.tables.cells import TableData, TableMethod
from reflow.model import BBox, Node, Role, Source
from reflow.text_quality import squash

# An item caption: a number of up to three digits and an optional letter, then a full stop
# ("1.", "12.", "29a."), and not a decimal ("5.2").
ITEM = re.compile(r"^(\d{1,3})([a-z]?)\.(?!\d)")

MIN_ROWS = 6            # rows of boxes a form grid has at least
MIN_ITEMS = 8           # numbered captions a form grid holds at least
UNIQUE_SHARE = 0.8      # of the captions carry a number that no other caption carries
MIN_ITEM_SHARE = 0.25  # of its boxes that hold text, at least
CAPTION_SHARE = 0.6     # of the captions are at the top of a box with room below them
CAPTION_DEPTH = 0.45    # a caption starts in the top this share of its box
ROOM = 1.5              # and the box is at least this many lines high
REPEAT_SHARE = 0.7      # rows of a form grid that repeat another row's columns: at most this share
MIN_BAND_ROWS = 3       # rows of one set of columns that make a table of them
MIN_BAND_COLUMNS = 4    # and the columns they have
EDGE_TOL = 1.5          # pt: column edges this close are one
HEIGHT_TOL = 0.2        # the rows of a band are this alike in height
MAX_HEADER_ROWS = 2     # rows over a band that may be its headers
HEADER_SHARE = 0.6      # of the band's columns have text in a header row
HANG_BACK = 4.0         # and at most this far left
ROW = 4.0               # pt: boxes whose tops are this close are on one row
BAR_INDENT = 6.0        # pt: a bar's second line starts under the first
GAP = 1.0               # lines of one box are at most this many line heights apart
BOLD_SHARE = 0.5        # a form whose lines are mostly bold has no bars
CONF_ITEM = CONF_PARAGRAPH
CONF_BAR = CONF_TABLE  # a section bar of a form table


REGION_SLACK = 1.5  # pt: a line whose core lies this near a box is in it


@dataclass
class Region:
    """A part of a form grid: the rows of the grid above or below its table, with the page's
    text lines inside them."""
    page: int
    bbox: BBox
    cells: list[BBox]
    lines: list[Line] = field(default_factory=list)


@dataclass
class FormGrid:
    """A region read as a form: its nodes in reading order, and where they sit on the page."""
    bbox: BBox
    nodes: list[Node]


def in_box(line: Line, box: BBox) -> bool:
    return geometry.inside(geometry.core(line.bbox), box, slack=REGION_SLACK)


def in_region(line: Line, region: Region) -> bool:
    return in_box(line, region.bbox)


def in_any_region(line: Line, regions: Sequence[Region]) -> bool:
    return any(in_region(line, r) for r in regions)


def _edges(row: Sequence[BBox | None]) -> list[tuple[float, float]]:
    return [(b[0], b[2]) for b in row if b is not None]


def _same_edges(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    return len(a) == len(b) and all(abs(p[0] - q[0]) <= EDGE_TOL and abs(p[1] - q[1]) <= EDGE_TOL
                                    for p, q in zip(a, b))


def _band(row: Sequence[BBox | None]) -> tuple[float, float] | None:
    have = [b for b in row if b is not None]
    return (min(b[1] for b in have), max(b[3] for b in have)) if have else None


def _alternating(edges: list[list[tuple[float, float]]], bands: list[tuple[float, float] | None]
                 ) -> tuple[int, int] | None:
    """The longest run of rows that repeat in pairs: every row has the columns and the height of
    the row two below it, and at least MIN_BAND_ROWS pairs. A record of services whose lines each
    have a merged subrow (the shaded supplemental line of a claim's box 24) alternates a tall row
    of three cells with a short one of two."""
    n = len(edges)

    def alike(a: int, b: int) -> bool:
        if not (bands[a] and bands[b]) or not _same_edges(edges[a], edges[b]):
            return False
        height = bands[a][1] - bands[a][0]
        return abs((bands[b][1] - bands[b][0]) - height) <= HEIGHT_TOL * height

    best: tuple[int, int] | None = None
    i = 0
    while i < n - 2:
        k = i
        while k + 2 < n and alike(k, k + 2):
            k += 1
        if k > i and k + 2 - i >= 2 * MIN_BAND_ROWS and (best is None or k + 1 - i > best[1] - best[0]):
            best = (i, k + 1)
        i = k + 1 if k > i else i + 1
    return best


def _find_band(t: TableData) -> tuple[int, int, int] | None:
    """The rows of the grid's repeated band: (first header row, first body row, last row), or
    None. The body is the longest run of rows with one set of columns and one height, and at
    least MIN_BAND_COLUMNS of them; the header rows over it have its columns, or columns made of
    its columns, and text. Failing that, rows that repeat in pairs (see `_alternating`) are the
    body of a table whose one header row over them has MIN_BAND_COLUMNS cells with text, across
    the body's width, and may cut it finer than the body's own columns."""
    n = len(t.grid)
    edges = [_edges(row) for row in t.grid]
    bands = [_band(row) for row in t.grid]
    best: tuple[int, int] | None = None
    i = 0
    while i < n:
        j = i
        if len(edges[i]) >= MIN_BAND_COLUMNS and bands[i]:
            height = bands[i][1] - bands[i][0]
            while (j + 1 < n and bands[j + 1] and _same_edges(edges[i], edges[j + 1])
                   and abs((bands[j + 1][1] - bands[j + 1][0]) - height) <= HEIGHT_TOL * height):
                j += 1
        if j - i + 1 >= MIN_BAND_ROWS and (best is None or j - i > best[1] - best[0]):
            best = (i, j)
        i = j + 1
    if best is None:
        return _find_alternating_band(t, edges, bands)
    first, last = best
    columns = {round(x, 1) for lo, hi in edges[first] for x in (lo, hi)}
    head = first
    while head > 0 and first - head < MAX_HEADER_ROWS:
        above = t.grid[head - 1]
        texts = [(t.rows[head - 1][j] or "").strip() for j, b in enumerate(above) if b is not None]
        if (len([x for x in texts if x]) >= max(3, HEADER_SHARE * len(edges[first])) and all(
                any(abs(x - c) <= EDGE_TOL for c in columns) for lo, hi in _edges(above) for x in (lo, hi))):
            head -= 1
        else:
            break
    return head, first, last


def _find_alternating_band(t: TableData, edges: list[list[tuple[float, float]]],
                           bands: list[tuple[float, float] | None]) -> tuple[int, int, int] | None:
    run = _alternating(edges, bands)
    if run is None or run[0] == 0:
        return None
    first, last = run
    head = first - 1
    texts = [(t.rows[head][j] or "").strip() for j, b in enumerate(t.grid[head]) if b is not None]
    across = geometry.union(b for i in range(first, last + 1) for b in t.grid[i] if b is not None)
    top = _span(range(head, head + 1), t)
    if (len(edges[head]) >= MIN_BAND_COLUMNS and len([x for x in texts if x]) >= HEADER_SHARE * len(edges[head])
            and across and top and abs(top[0] - across[0]) <= EDGE_TOL and abs(top[2] - across[2]) <= EDGE_TOL):
        return head, first, last
    return None


def _span(rows: range, t: TableData) -> BBox | None:
    return geometry.union(b for i in rows for b in t.grid[i] if b is not None)


def _outer(cells: Sequence[BBox]) -> list[BBox]:
    """The boxes that no other box holds. A grid's rules cut a small box out of a bigger one
    (the boxes of a row of check boxes, drawn inside the cell that captions them): the cell is
    the form's box, and what is drawn inside it is part of it."""
    out: list[BBox] = []
    for c in sorted(set(cells), key=lambda b: -geometry.area(b)):
        if not any(geometry.inside(c, o, slack=EDGE_TOL) for o in out):
            out.append(c)
    return out


def _owner(line: Line, cells: Sequence[BBox]) -> BBox | None:
    """The smallest box that holds the middle of the line."""
    cx, cy = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
    inside = [c for c in cells if c[0] <= cx <= c[2] and c[1] <= cy <= c[3]]
    return min(inside, key=geometry.area, default=None)


def is_start(line: Line) -> bool:
    return ITEM.match(line.text.strip()) is not None


def _is_form(regions: list[Region], rows: list[list[BBox | None]]) -> bool:
    """Whether the text and the boxes of the regions are a form's: numbered captions that are most
    of the text, set at the top of boxes with room below them, in rows that do not repeat."""
    lines = [l for r in regions for l in r.lines]
    cells = [c for r in regions for c in r.cells]
    starts = [l for l in lines if is_start(l)]
    boxes = {o for l in lines if (o := _owner(l, cells)) is not None}
    if len(starts) < MIN_ITEMS or len(starts) < MIN_ITEM_SHARE * len(boxes):
        return False
    # A form numbers each item once. A table that numbers its rows in each column (the lists of
    # acceptable documents of the I-9: 1. 2. 3. under List A, again under List B) repeats them.
    numbers = Counter(ITEM.match(l.text.strip()).group(0) for l in starts)
    if sum(1 for n in numbers.values() if n == 1) < UNIQUE_SHARE * len(starts):
        return False
    deep = 0
    for line in starts:
        box = _owner(line, cells)
        if box is not None and line.top - box[1] <= CAPTION_DEPTH * (box[3] - box[1]) \
                and box[3] - box[1] >= ROOM * line.height:
            deep += 1
    if deep < CAPTION_SHARE * len(starts):
        return False
    shapes = [(_edges(row), _band(row)) for row in rows if any(b is not None for b in row)]
    repeated = sum(1 for k, (e, band) in enumerate(shapes)
                   if len(e) >= 2 and band and any(
                       _same_edges(e, other) and abs((band[1] - band[0]) - (ob[1] - ob[0])) <= HEIGHT_TOL * (band[1] - band[0])
                       for m, (other, ob) in enumerate(shapes) if m != k and ob))
    return repeated <= REPEAT_SHARE * len(shapes)


def _compact(rows: list[list[str | None]], grid: list[list[BBox | None]]
             ) -> tuple[list[list[str | None]], list[list[BBox | None]]]:
    """The rows with the columns dropped where every row has a merged place (None): a cell that
    spanned them spans one less, and no column is left that no row of this part starts."""
    width = max((len(r) for r in rows), default=0)
    keep = [j for j in range(width) if any(j < len(r) and r[j] is not None for r in rows)]
    return ([[r[j] if j < len(r) else None for j in keep] for r in rows],
            [[g[j] if j < len(g) else None for j in keep] for g in grid])


def split(tables: Sequence[TableData], lines: Sequence[Line]) -> tuple[list[TableData], list[Region]]:
    """The tables, with each ruled grid that is a form layout taken out: its repeated band stays
    a table, and the rest comes back as regions of lines. The tables that are not form grids come
    back as they were, in order."""
    kept: list[TableData] = []
    regions: list[Region] = []
    for t in tables:
        parts = _parts(t, lines)
        if parts is None:
            kept.append(t)
            continue
        table, found = parts
        if table is not None:
            kept.append(table)
        regions += found
    return kept, regions


def _parts(t: TableData, lines: Sequence[Line]) -> tuple[TableData | None, list[Region]] | None:
    if t.method != "ruled" or len(t.grid) < MIN_ROWS or len(t.rows) != len(t.grid):
        return None
    band = _find_band(t)
    n = len(t.grid)
    outside = [range(0, band[0]), range(band[2] + 1, n)] if band else [range(n)]
    regions: list[Region] = []
    for rows in outside:
        if not len(rows):
            continue
        box = _span(rows, t)
        if box is None:
            continue
        cells = _outer([b for i in rows for b in t.grid[i] if b is not None])
        regions.append(Region(t.page, box, cells,
                              [l for l in lines if in_box(l, box)]))
    if not regions or not _is_form(regions, [t.grid[i] for rows in outside for i in rows]):
        return None
    if band is None:
        return None, regions
    head, first, last = band
    rows, grid = _compact([t.rows[i] for i in range(head, last + 1)],
                          [t.grid[i] for i in range(head, last + 1)])
    table = TableData(t.page, tuple(round(v, 2) for v in _span(range(head, last + 1), t)), rows,
                      method=TableMethod.RULED, header_rows=first - head, grid=grid,
                      why=f"ruled lines: {len(rows)} rows × {max(len(r) for r in rows)} columns, "
                          f"{first - head} header row{'' if first - head == 1 else 's'}; the repeated rows "
                          f"of a form grid, which is read as a form (analyze.form_grid)")
    return table, regions


def wall_cuts(regions: Sequence[Region], lines: Sequence[Line]) -> dict[int, list[Line]]:
    """The lines that run across a wall of the form's boxes, cut at the wall: the text layer lists
    the words of two boxes side by side as one line ("D ____ 32. Total Fee"). Where each part
    starts is read from the line's width and its character count, and moved to the nearest space,
    as analyze.forms.split_captions does. Returns {id(line): parts}."""
    cuts: dict[int, list[Line]] = {}
    for region in regions:
        for line in lines:
            if (line.rotated or line.x1 - line.x0 < 4 or len(line.text) < 3
                    or not in_region(line, region)):
                continue
            cy = (line.top + line.bottom) / 2
            walls = sorted({round(e, 1) for c in region.cells if c[1] <= cy <= c[3]
                            for e in (c[0], c[2]) if line.x0 + 4 < e < line.x1 - 4})
            per_char = (line.x1 - line.x0) / len(line.text)
            starts = [0]
            for wall in walls:
                if _owner_at(region.cells, wall - 1.5, cy) is _owner_at(region.cells, wall + 1.5, cy):
                    continue
                guess = round((wall - line.x0) / per_char)
                near = [m.end() for m in re.finditer(r"\s+", line.text)
                        if abs(m.end() - guess) <= 4 and m.end() > starts[-1] + 1]
                near = [k for k in near if ITEM.match(line.text[k:])]   # only caption runs into caption
                if near:
                    starts.append(min(near, key=lambda k: abs(k - guess)))
            if len(starts) > 1:
                ends = starts[1:] + [len(line.text)]
                cuts[id(line)] = [replace(line.cut(a, b, strip=True),
                                          bbox=(line.x0 + a * per_char, line.top, line.x0 + b * per_char, line.bottom))
                                  for a, b in zip(starts, ends)]
    return cuts


def _owner_at(cells: Sequence[BBox], x: float, y: float) -> BBox | None:
    inside = [c for c in cells if c[0] <= x <= c[2] and c[1] <= y <= c[3]]
    return min(inside, key=geometry.area, default=None)


# -- reading a region -------------------------------------------------------------------------

@dataclass
class _Item:
    lines: list[Line]
    bar: bool = False
    node: Node | None = None   # a control the planner found, in place of lines

    @property
    def box(self) -> BBox:
        if self.node is not None:
            return self.node.bbox
        return geometry.union(l.bbox for l in self.lines)

    @property
    def text(self) -> str:
        if self.node is not None:
            return (self.node.text or "").strip()
        return " ".join(squash(l.text.replace(SOFT_HYPHEN, "")) for l in self.lines)

    @property
    def key(self) -> tuple[int, int] | None:
        text = self.text
        if self.node is not None and not ITEM.match(text) and self.node.children:
            text = (self.node.children[0].text or "").strip()   # a group with no legend: its first label
        m = ITEM.match(text)
        return (int(m[1]), ord(m[2]) - 96 if m[2] else 0) if m else None


def _bold_share(lines: Sequence[Line]) -> float:
    return sum(1 for l in lines if l.edge_style("head")[0]) / max(len(lines), 1)


def _bar(line: Line, bold: float) -> bool:
    """A bold line that starts no item, in a form that is not mostly bold, and that is no sentence
    (it does not end with a full stop: "Si la respuesta es Sí, complete ..."): a section bar."""
    text = line.text.strip()
    return (not is_start(line) and bold <= BOLD_SHARE and len(text) >= 4 and not text.endswith(".")
            and line.edge_style("head")[0])


def bars(region: Region) -> list[Line]:
    """The region's section bars: the lines that are headings, which no control takes as its label."""
    bold = _bold_share(region.lines)
    return [l for l in region.lines if _bar(l, bold)]


def as_table(region: Region) -> TableData:
    """The region's boxes as a grid, one box to a row, with the text of each box in it: what the
    form planner takes for a table, to know which box each control and each line is in
    (method FORM_GRID: its labels are in the control's own box)."""
    held: dict[BBox, list[Line]] = {c: [] for c in region.cells}
    for line in region.lines:
        if (box := _owner(line, region.cells)) is not None:
            held[box].append(line)
    rows = [[" ".join(l.text.replace(SOFT_HYPHEN, "").strip() for l in sorted(ls, key=lambda l: (round(l.top), l.x0)))]
            for ls in held.values()]
    return TableData(region.page, region.bbox, rows, method=TableMethod.FORM_GRID, header_rows=0,
                     grid=[[c] for c in held], why="the boxes of a form grid, for its controls")


def _items(region: Region, taken: set[int]) -> list[_Item]:
    """The region's lines (not the ones taken as labels) cut into items: a box at a time, a new
    item at each numbered line, and at each section bar and the line after one."""
    lines = [l for l in region.lines if id(l) not in taken]
    bold = _bold_share(region.lines)
    by_box: dict[BBox | None, list[Line]] = {}
    for line in lines:
        by_box.setdefault(_owner(line, region.cells), []).append(line)
    items: list[_Item] = []
    for box_lines in by_box.values():
        box_lines.sort(key=lambda l: (round(l.top), l.x0))
        here: list[_Item] = []
        for line in box_lines:
            bar = _bar(line, bold)
            owner = None if is_start(line) or bar else _continues(line, here)
            if owner is None:
                owner = _Item([line], bar=bar)
                here.append(owner)
                items.append(owner)
            else:
                owner.lines.append(line)
    return items


def _continues(line: Line, items: Sequence[_Item]) -> _Item | None:
    """The item of its box that an unnumbered line goes on: the item whose last line is just above
    it and starts about where it does (a wrapped caption, its second line a little indented), or
    the item whose last line is on its row, left of it. A section bar goes on only under its own
    left edge."""
    best: tuple[float, float] | None = None
    found: _Item | None = None
    for item in items:
        last = item.lines[-1]
        height = max(line.height, 1.0)
        drop = line.top - last.bottom
        if item.bar:
            fits = -0.5 * height <= drop <= GAP * height and abs(line.x0 - last.x0) <= BAR_INDENT
        else:
            stacked = (-0.5 * height <= drop <= GAP * height
                       and last.x0 - HANG_BACK <= line.x0 <= last.x0 + HANG)
            beside = abs(line.top - last.top) <= 0.5 * height and line.x0 >= last.x1 - 1
            fits = stacked or beside
        if fits:
            rank = (abs(drop) if not abs(line.top - last.top) <= 0.5 * height else 1e6, abs(line.x0 - last.x0))
            if best is None or rank < best:
                best, found = rank, item
    if found is None:
        # Text in the box under a caption but not set on it (centred, or a signature line): the
        # box's own caption's, the nearest one above it.
        above = [i for i in items if not i.bar and i.key is not None and i.lines[0].top <= line.top + 2]
        found = max(above, key=lambda i: i.lines[0].top, default=None)
    return found


def _below(item: _Item, others: Sequence[_Item]) -> _Item | None:
    """The nearest numbered item at or under this one that shares some of its width."""
    box = item.box
    found = [o for o in others if o is not item and o.key is not None
             and o.box[1] >= box[1] - 2 and geometry.horizontal_overlap(o.box, box) > 0]
    return min(found, key=lambda o: (o.box[1] - box[1], o.box[0]), default=None)


def _above(item: _Item, others: Sequence[_Item]) -> _Item | None:
    """The nearest numbered item above this one that shares some of its width, else the nearest
    on its row to its left."""
    box = item.box
    found = [o for o in others if o is not item and o.key is not None and o.box[1] <= box[1] + 2
             and geometry.horizontal_overlap(o.box, box) > 0]
    if not found:
        found = [o for o in others if o is not item and o.key is not None
                 and abs(o.box[1] - box[1]) <= ROW and o.box[2] <= box[0] + 2]
    return max(found, key=lambda o: (o.box[1], o.box[0]), default=None)


def _order(items: list[_Item]) -> list[_Item]:
    """The items in the form's printed numbering. A bar goes before the numbered item under it
    and any other unnumbered item after the numbered item above it, so a section stays whole; an
    unnumbered item with no numbered item near stays where it is."""
    ranked: list[tuple[tuple[float, float, int, float], _Item]] = []
    position = sorted(items, key=lambda i: (round(i.box[1]), i.box[0]))
    for rank, item in enumerate(position):
        key = item.key
        if key is not None:
            order: tuple[float, float] = (float(key[0]), float(key[1]))
        elif item.bar:
            nxt = _below(item, items)
            order = (float(nxt.key[0]), float(nxt.key[1]) - 0.5) if nxt and nxt.key else (1e9, 0.0)
        elif (prev := _above(item, items)) is not None and prev.key is not None:
            order = (float(prev.key[0]), float(prev.key[1]) + 0.5)
        else:
            order = (1e9, 0.0)
        ranked.append(((*order, rank, 0.0), item))
    return [i for _, i in sorted(ranked, key=lambda r: r[0])]


def build(region: Region, taken: set[int], controls: Sequence[Node]) -> FormGrid:
    """The region as nodes, in order: a heading for each section bar, a paragraph for each
    caption, and the controls the planner placed in the region (`controls`: fields and
    fieldsets), each with its caption's number. `taken` are the ids of the lines the controls
    took as their labels."""
    items = _items(region, taken)
    items += [_Item([], node=n) for n in controls]
    nodes: list[Node] = []
    for item in _order(items):
        if item.node is not None:
            nodes.append(item.node)
            continue
        text = item.text
        role, conf = (Role.H2, CONF_BAR) if item.bar else (Role.P, CONF_ITEM)
        nodes.append(Node(role, page=region.page, bbox=item.box, text=text, confidence=conf,
                          source=Source.HEURISTIC,
                          attrs={"why": ["heading: a bold section bar of a form grid" if item.bar
                                         else "form grid: one box of the form, its caption"]}))
    return FormGrid(region.bbox, nodes)
