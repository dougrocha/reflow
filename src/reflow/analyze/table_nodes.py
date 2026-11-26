"""Tables as nodes: a table's rows and cells (TABLE/TR/TH/TD) with the form section bars and
the merged cells they carry."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from reflow import geometry
from reflow.analyze.constants import CONF_TABLE, PUA_BULLETS
from reflow.extract.lines import Line
from reflow.extract.tables.cells import Bar, TableData, TableMethod
from reflow.model import Node, Role, Source


CONF_ALIGNED_TABLE = 0.5  # structure from text alignment alone: a guess at every column


BAR_SHARE = 0.4  # a section bar spans at least this share of its form table's width
BAR_ROW_MAX = 2  # bars side by side in one row: a form's two columns


def mark_section_bars(tables: list[TableData], fields: dict[tuple[int, int, int], list[Node]],
                      lines: Sequence[Line]) -> None:
    """Mark the section bars of the form tables: a wide cell over more than one column of
    the grid, with no control, whose text is
    one or two lines that start in bold ("HEADER INFORMATION", "OTHER COVERAGE (Mark applicable
    box ...)") titles the fields under it, in a table whose other cells mostly start in the
    regular weight (its field labels). A row of more than BAR_ROW_MAX bold cells is a row of
    column headers. `fields` are the controls by (table, row, column); `lines` the page's lines,
    without the labels the controls took. The cell's attrs get "bar": the box and the text of
    its lines (table_node). The lines' reader keeps the case the cells' reader can lose
    ("INFORmATION")."""
    for k, t in enumerate(tables):
        if not t.grid:
            continue
        width = t.bbox[2] - t.bbox[0]
        cells = [(i, j, box, sorted((l for l in lines if geometry.inside(geometry.core(l.bbox), box)),
                                    key=lambda l: l.top))
                 for i, row in enumerate(t.grid) for j, box in enumerate(row)
                 if box is not None and (t.rows[i][j] or "").strip()]
        bold = [c for c in cells if c[3] and c[3][0].edge_style("head")[0]]
        if 2 * len(bold) >= len(cells):
            continue  # a table set in bold: no bar stands out
        per_row = Counter(i for i, *_ in bold)
        for i, j, box, held in bold:
            if per_row[i] > BAR_ROW_MAX:
                continue  # a row of column headers: a form sets two bars side by side at most
            if (k, i, j) in fields or box[2] - box[0] < BAR_SHARE * width:
                continue
            if j + 1 >= len(t.grid[i]) or t.grid[i][j + 1] is not None:
                continue  # one column of the grid: a column's header, not a bar over the fields
            text = _letters(t.rows[i][j])
            n = next((n for n in (1, 2) if _letters(" ".join(l.text for l in held[:n])) == text), 0)
            if n:  # the bar wraps onto a second line at most
                t.bars[(i, j)] = Bar(geometry.union(l.bbox for l in held[:n]),
                                     " ".join(l.text.strip() for l in held[:n]))
        _split_bars(t, k, fields, lines, width)


def _split_bars(t: TableData, k: int, fields: dict, lines: Sequence[Line], width: float) -> None:
    """A bar the table's grid cut into several cells of its row ("HEADER INFOR", "mAT", "ION"):
    one bold line that the row's text cells spell out together. The first cell takes the bar,
    the others "bar_part" (their text is the bar's)."""
    for i, row in enumerate(t.grid):
        parts = [(j, box) for j, box in enumerate(row)
                 if box is not None and (t.rows[i][j] or "").strip()]
        if len(parts) < 2 or any((k, i, j) in fields or t.has_cell_notes(i, j) for j, _ in parts):
            continue
        span = geometry.union(box for _, box in parts)
        held = [l for l in lines if geometry.inside(geometry.core(l.bbox), span)]
        whole = geometry.union(box for box in row if box is not None)  # with the row's empty cells
        if (len(held) == 1 and held[0].edge_style("head")[0] and whole[2] - whole[0] >= BAR_SHARE * width
                and _letters(held[0].text) == _letters(" ".join(t.rows[i][j] for j, _ in parts))):
            t.bars[(i, parts[0][0])] = Bar(held[0].bbox, held[0].text.strip())
            t.bar_parts.update((i, j) for j, _ in parts[1:])


def _letters(text: str | None) -> str:
    """The letters and digits of a text, casefolded: a cell's text and its line's may differ in
    case and spacing when they come from two readers of the text layer."""
    return "".join(c for c in (text or "").casefold() if c.isalnum())


def _span_owners(t: TableData) -> dict[tuple[int, int], tuple[int, int]]:
    """For each merged (None) place of a ruled grid, the cell whose box covers it: the middle
    of its column (the narrowest box in that column) and of its row (the shortest box in that
    row) lies in the box. A place no box covers has no owner."""
    if not t.grid:
        return {}
    xs: dict[int, tuple[float, float]] = {}
    ys: dict[int, tuple[float, float]] = {}
    for i, boxes in enumerate(t.grid):
        for j, b in enumerate(boxes):
            if b is None:
                continue
            if j not in xs or b[2] - b[0] < xs[j][0]:
                xs[j] = (b[2] - b[0], (b[0] + b[2]) / 2)
            if i not in ys or b[3] - b[1] < ys[i][0]:
                ys[i] = (b[3] - b[1], (b[1] + b[3]) / 2)
    owners: dict[tuple[int, int], tuple[int, int]] = {}
    for i, row in enumerate(t.rows):
        for j, cell in enumerate(row):
            if cell is not None or i not in ys or j not in xs:
                continue
            x, y = xs[j][1], ys[i][1]
            owners[(i, j)] = next(
                ((k, l) for k in range(i, -1, -1) for l in range(j, -1, -1)
                 if l < len(t.grid[k]) and (b := t.grid[k][l]) is not None
                 and b[0] <= x <= b[2] and b[1] <= y <= b[3]), None)
    return {at: owner for at, owner in owners.items() if owner is not None}


def table_node(t: TableData, fields: dict[tuple[int, int], list[Node]] | None = None) -> Node:
    """Table → TABLE/TR/TH/TD nodes. The leading rows (t.header_rows) are header rows (TH),
    a guess hence the confidence. A None cell is merged into the cell on its left (colspan).
    The table's attrs record how it was found: ruled lines or text alignment, its size and
    its header rows. `fields` are form controls by (row, column), placed in those cells."""
    columns = max((len(r) for r in t.rows), default=0)
    why = t.why or f"{t.method} table"
    owners = _span_owners(t)
    # A cell that reaches down from the header rows into the body is a row label beside its
    # fields, not a column header: the header rows end above its row (and HTML lets no rowspan
    # cross from the head into the body).
    heads = t.header_rows
    while crossing := [k for (i, _), (k, _) in owners.items() if k < heads <= i]:
        heads = min(crossing)
    table = Node(Role.TABLE, page=t.page, bbox=t.bbox,
                 confidence=CONF_TABLE if t.method in (TableMethod.RULED, TableMethod.CALENDAR) else CONF_ALIGNED_TABLE,
                 source=Source.HEURISTIC,
                 attrs={"method": t.method.value, "rows": len(t.rows), "columns": columns,
                        "header_rows": heads, "why": [why]})
    if t.caption:
        table.children.append(Node(Role.CAPTION, page=t.page, bbox=t.caption_bbox, text=t.caption,
                                   confidence=CONF_TABLE,
                                   attrs={"why": [f"{t.method} table, its printed title"]}))
    for code, detail, data in t.facts:
        table.fact(code, Source.HEURISTIC, detail, **data)
    made: dict[tuple[int, int], Node] = {}
    for i, row in enumerate(t.rows):
        header = i < heads
        tr = Node(Role.TR, page=t.page, text=f"row {i}")
        for j, cell in enumerate(row):
            if cell is None and (owner := owners.get((i, j))) in made and owner[0] < i:
                # A cell of an earlier row reaches down over this one (a row label beside the
                # rows of its fields): it spans these rows, and the row has no cell there.
                above = made[owner]
                above.attrs["rowspan"] = max(above.attrs.get("rowspan", 1), i - owner[0] + 1)
                continue
            if cell is None and tr.children:
                prev = tr.children[-1]
                prev.attrs["colspan"] = prev.attrs.get("colspan", 1) + 1
                continue
            where = f"{'header' if header else 'data'} cell, row {i + 1}, column {j + 1}"
            # A table that knows its cells' text (t.boxes) makes traceable cells: each has the
            # box of its printed text, and its id comes from that box.
            td = Node(Role.TH if header else Role.TD, page=t.page,
                      bbox=t.boxes[i][j] if t.boxes else None,
                      text=(cell or "").translate(PUA_BULLETS), confidence=CONF_TABLE,
                      attrs={"why": [f"{t.method} table, {where}"]})
            if t.boxes and t.grid[i][j]:
                td.attrs["cell"] = list(t.grid[i][j])
            if (day := t.days.get((i, j))) is not None:
                td.attrs["day"] = day
            td.children = (fields or {}).pop((i, j), [])
            if (i, j) in t.bar_parts:
                td.set_text("")  # the bar's text, in the bar's cell of the row
            if (bar := t.bars.get((i, j))) is not None:
                # A form's section bar: its text is the heading of the fields under it, in its cell.
                box, text = bar
                td.children.insert(0, Node(Role.H2, page=t.page, bbox=tuple(box), text=text,
                                           confidence=CONF_TABLE, attrs={"why": [
                                               "heading: a bold section bar of the form table"]}))
                td.set_text("")
            tr.children.append(td)
            made[(i, j)] = td
        table.children.append(tr)
    # A control whose cell is not a cell of the rows (a merged one) goes in its row's first cell.
    for (i, _), left in (fields or {}).items():
        if i < len(table.children) and table.children[i].children:
            table.children[i].children[0].children.extend(left)
    return table
