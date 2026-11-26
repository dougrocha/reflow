"""The reading of a TABLE node that the transcript and the Ask passages share."""

from __future__ import annotations

from reflow.model import Node, Role


def header_rows(rows: list[Node]) -> list[Node]:
    """The header band: every leading row whose cells are all column headers (a TH with no
    scope), so a span that starts in one header row and runs into the next stays in one band."""
    header = []
    for tr in rows:
        if not tr.children or not all(c.role == Role.TH and "scope" not in c.attrs
                                      for c in tr.children):
            break
        header.append(tr)
    return header
