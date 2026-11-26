"""Row headers and header ids for data tables.

A post-pass over TABLE nodes, whichever engine made them. It reads the cells themselves: a column is numeric when at least NUMERIC_SHARE of its data cells are
numbers (table_evidence.is_number); when column 0 is text and a numeric column follows,
the column-0 data cells become TH scope="row". Spanned header bands (a TH with colspan
over a second header row) get ids and the data cells get `headers` listing them, so a
screen reader announces both levels.

Writes attrs only: cell.role = TH, cell.attrs["scope"] = "row"; cell.attrs["id"] on header
cells of multi-row bands; cell.attrs["headers"] = [ids]. The renderer reads them.
"""

from __future__ import annotations

from reflow.analyze.table_evidence import is_number, tokens
from reflow.model import Node, Role

NUMERIC_SHARE = 0.6
MIN_DATA_ROWS = 2
TEXT_SHARE = 0.6
_DASHES = frozenset({"-", "–", "—"})


def _rows(table: Node) -> list[Node]:
    return [child for child in table.children if child.role == Role.TR]


def _kind(cell: Node) -> str:
    """"empty", "numeric" (every token a number or a dash placeholder) or "text"."""
    toks = tokens(cell.text or "")
    if not toks:
        return "empty"
    if all(is_number(tok) or tok in _DASHES for tok in toks):
        return "numeric"
    return "text"


def _header_rows(table: Node, rows: list[Node]) -> set[int]:
    """The leading attrs["header_rows"] rows, plus any row whose cells are all TH."""
    count = int(table.attrs.get("header_rows", 0))
    header = set(range(min(count, len(rows))))
    header |= {i for i, tr in enumerate(rows)
               if tr.children and all(cell.role == Role.TH for cell in tr.children)}
    return header


def _place(rows: list[Node]) -> tuple[dict[tuple[int, int], Node], list[tuple[int, int, Node]]]:
    """Lay the cells on the grid, honouring colspan and rowspan. Returns the cell covering
    each (row, column) position, and each cell's (row, start column, cell)."""
    owner: dict[tuple[int, int], Node] = {}
    placed: list[tuple[int, int, Node]] = []
    for r, tr in enumerate(rows):
        col = 0
        for cell in tr.children:
            while (r, col) in owner:
                col += 1
            colspan = int(cell.attrs.get("colspan", 1))
            rowspan = int(cell.attrs.get("rowspan", 1))
            placed.append((r, col, cell))
            for dr in range(rowspan):
                for dc in range(colspan):
                    owner[(r + dr, col + dc)] = cell
            col += colspan
    return owner, placed


def mark_row_headers(table: Node) -> bool:
    """Promote column 0 to row headers when the table is a label-plus-numbers table.
    Returns True when it changed anything. Docling tables that already carry scope="row"
    are left alone."""
    rows = _rows(table)
    if any(cell.attrs.get("scope") == "row" for tr in rows for cell in tr.children):
        return False
    header = _header_rows(table, rows)
    data = [r for r in range(len(rows)) if r not in header]
    if len(data) < MIN_DATA_ROWS:
        return False

    owner, _ = _place(rows)
    header_cells = {id(cell) for r in header for cell in rows[r].children}

    def body_cell(r: int, c: int) -> Node | None:
        cell = owner.get((r, c))
        return None if cell is None or id(cell) in header_cells else cell

    def kinds_in(c: int) -> list[str]:
        """Kinds of the body cells in column c, empty cells left out."""
        found = []
        for r in data:
            cell = body_cell(r, c)
            if cell is not None and _kind(cell) != "empty":
                found.append(_kind(cell))
        return found

    width = max((c for _, c in owner), default=-1) + 1
    text_in_column_0 = sum(1 for r in data
                           if body_cell(r, 0) is not None and _kind(body_cell(r, 0)) == "text")
    if text_in_column_0 < TEXT_SHARE * len(data):
        return False
    numeric_follows = False
    for c in range(1, width):
        kinds = kinds_in(c)
        if kinds and sum(k == "numeric" for k in kinds) / len(kinds) >= NUMERIC_SHARE:
            numeric_follows = True
    if not numeric_follows:
        return False

    changed = False
    for r in data:
        cell = body_cell(r, 0)
        if cell is None or _kind(cell) == "empty":
            continue
        cell.role = Role.TH
        cell.attrs["scope"] = "row"
        changed = True
    return changed


def link_header_bands(table: Node) -> bool:
    """For a header band of two or more rows with spanning cells: give each header cell an
    id "<table id>-h<r>-<c>" and each data cell `headers` naming the cells above it."""
    rows = _rows(table)
    header = _header_rows(table, rows)
    if len(header) < 2:
        return False
    owner, placed = _place(rows)
    header_ids = {id(cell) for r, _, cell in placed if r in header}
    if not any(int(cell.attrs.get("colspan", 1)) > 1
               for r, _, cell in placed if r in header):
        return False

    anchor = table.attrs.get("anchor", table.id)
    for r, c, cell in placed:
        if r in header:
            cell.attrs["id"] = f"{anchor}-h{r}-{c}"

    changed = True
    for r, c, cell in placed:
        if r in header:
            continue
        headers: list[str] = []
        for hr in sorted(header):
            above = owner.get((hr, c))
            if above is not None and id(above) in header_ids and "id" in above.attrs:
                if above.attrs["id"] not in headers:
                    headers.append(above.attrs["id"])
        if headers:
            cell.attrs["headers"] = headers
    return changed


def apply(root: Node) -> dict[str, int]:
    """Run both over every TABLE under `root`; returns counts {"row_headers": n, "bands": n}.
    Bands first: mark_row_headers changes roles, which the header-row rule reads."""
    counts = {"row_headers": 0, "bands": 0}
    tables = [node for node in root.walk() if node.role == Role.TABLE]
    for table in tables:
        if link_header_bands(table):
            counts["bands"] += 1
        if mark_row_headers(table):
            counts["row_headers"] += 1
    return counts
