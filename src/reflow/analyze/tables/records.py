"""A record printed over and over in a ruled grid: one table.

A form prints its dependents, or its children, as the same block of labels over blank cells,
once for each. Read cell by cell, that is the block's labels repeated; it is one table, a column
for each label and a row for each repeat. The rows of the grid around the record keep their own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from reflow import geometry
from reflow.extract.tables.cells import TableData, looks_numeric

MAX_LABEL_WORDS = 6   # a label of a record's field is this long at most
MAX_RECORD_BANDS = 6  # a record is this many rows of the grid (labels over blanks) at most


def _is_blank(row: Sequence[str | None]) -> bool:
    return not any(c and c.strip() for c in row)


def _is_label_band(row: Sequence[str | None]) -> bool:
    """A row of short labels and nothing else: no figure, no sentence."""
    texts = [c for c in row if c and c.strip()]
    return (bool(texts) and all(len(c.split()) <= MAX_LABEL_WORDS and any(ch.isalpha() for ch in c)
                                and not looks_numeric(c) for c in texts))


def _find_record(rows: Sequence[Sequence[str | None]]) -> tuple[int, int, int] | None:
    """The first record printed over and over: (start, bands, repeats). A record is `bands`
    consecutive rows, labels over blank cells, and the run holds the same record at least twice
    in a row."""
    signature = [tuple(c or "" for c in row) for row in rows]
    for start in range(len(rows)):
        if not _is_label_band(rows[start]):
            continue
        for bands in range(2, MAX_RECORD_BANDS + 1):
            group = signature[start:start + bands]
            if len(group) < bands:
                break
            kinds = [_is_blank(r) for r in rows[start:start + bands]]
            if all(kinds) or not any(kinds) or not all(
                    blank or _is_label_band(r) for blank, r in zip(kinds, rows[start:start + bands])):
                continue
            repeats = 1
            while signature[start + repeats * bands:start + (repeats + 1) * bands] == group:
                repeats += 1
            if repeats >= 2:
                return start, bands, repeats
    return None


def _part(table: TableData, first: int, last: int) -> TableData:
    """The rows `first` to `last` of the table, with the box of their cells."""
    grid = table.grid[first:last]
    box = geometry.union(b for row in grid for b in row if b is not None) or table.bbox
    return replace(table, rows=table.rows[first:last], grid=grid, header_rows=1,
                   bbox=tuple(round(v, 2) for v in box), cells=[],
                   why=f"{table.why}; rows {first + 1} to {last}")


def _record_table(table: TableData, start: int, bands: int, repeats: int) -> TableData:
    """The record's table: a header row of its labels, left to right and top to bottom, and a
    blank row for each repeat. It has no cell grid: its header holds cells of several rows of the
    grid, so no row of it has a place on the page that a line could be found in."""
    end = start + bands * repeats
    labels = [text for i in range(start, start + bands) for text in table.rows[i]
              if text and text.strip()]
    box = geometry.union(b for row in table.grid[start:end] for b in row if b is not None) or table.bbox
    return replace(table, rows=[labels] + [[""] * len(labels) for _ in range(repeats)],
                   grid=[], header_rows=1, bbox=tuple(round(v, 2) for v in box), cells=[],
                   why=f"{table.why}; a record of {len(labels)} labels printed {repeats} times, "
                       f"rows {start + 1} to {end}")


def split_records(table: TableData) -> list[TableData]:
    """The table as its parts: a record printed over and over in its rows is a table of its
    own (see the module), and the rows before and after it are tables of the grid's kind. A
    table with no such record is returned whole."""
    if table.method != "ruled" or not table.grid or len(table.grid) != len(table.rows):
        return [table]
    found = _find_record(table.rows)
    if found is None:
        return [table]
    start, bands, repeats = found
    end = start + bands * repeats
    parts: list[TableData] = []
    if start:
        parts += split_records(_part(table, 0, start))
    parts.append(_record_table(table, start, bands, repeats))
    if end < len(table.rows):
        parts += split_records(_part(table, end, len(table.rows)))
    return parts
