"""Checks on the page-vision model's heading and row-header answers before a dispute is shown.

The model is confidently wrong in a few repeated ways: it calls continuation
lines, form identifiers and lines set like the body text headings, and the first column of any
grid row headers, even when that column holds dates or numbers. Each check names one of those
ways from the page's own evidence. A check rejects a dispute; it never makes one, and it changes
nothing in the document.

Only the checks in ON filter the disputes. A check is on only where it rejects none of the
headings and row headers the page specs require: tests/pages/vision_gates.py scores each one,
and the counts are in docs/research/vision-gates.md. RUNS_ON and NO_WITNESS are off: they reject
required headings (an address before a directory name ends with no stop; a section title on a
scan is set like the body text). DATA_COLUMN rejects no required row header, but the pass asks
no row-header question yet, so it has no dispute to filter.

There is no check against a heading that ends in a lower-case word: it would reject real
headings such as `Requests for Information`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from reflow.model import HEADING_ROLES, Node, Role


class Gate(StrEnum):
    CONTINUATION = "continuation"    # starts in lower case, or ends in a comma or semicolon
    RUNS_ON = "runs-on"              # the paragraph before it stops mid-sentence
    FORM_ID = "form-id"              # a form's identifier and revision
    NO_WITNESS = "no-witness"        # set like the body text, and no tag calls it a heading
    DATA_COLUMN = "data-column"      # a column of numbers, dates or weekdays: never row headers


# The checks that filter disputes: those with no false rejection on the specs' required headings
# (docs/research/vision-gates.md).
ON: frozenset[Gate] = frozenset({Gate.CONTINUATION, Gate.FORM_ID})

# A paragraph of at least this many words that ends with no stop is a sentence the next block
# goes on with. A shorter one is a label ("Effective 07/02/2025") and says nothing.
PROSE_WORDS = 6
SENTENCE_END = tuple(".:;!?)]\"'”’…")
# A form's code (BOE-305-AH) beside a revision mark (REV. 12, (05-24)): the form's identifier.
FORM_CODE = re.compile(r"\b[A-Z]{2,}-\d{2,}(?:-[A-Z0-9]+)*\b")
REVISION = re.compile(r"\bREV(?:ISED)?\b\.?|\bRev\.|\(\d{1,2}[-/]\d{2,4}\)")
HEADINGS = frozenset(r.value for r in HEADING_ROLES)


def _continues(text: str) -> bool:
    """It starts in lower case, or ends in a comma or semicolon: a piece of a sentence."""
    t = text.strip()
    return t[:1].islower() or t.endswith((",", ";"))


def _runs_on(before: Node | None) -> bool:
    """The paragraph before it is prose that stops mid-sentence, so this block may finish it."""
    if before is None or before.role is not Role.P:
        return False
    prev = (before.text or "").strip()
    return len(prev.split()) >= PROSE_WORDS and not prev.endswith(SENTENCE_END)


def _form_id(text: str) -> bool:
    return bool(FORM_CODE.search(text) and REVISION.search(text))


def witness(node: Node) -> str | None:
    """What sets the block apart as a heading, besides the model: its type (bold over all of its
    text, larger than the body text, or a heading's ink on a scan, recorded by layout as
    `set_apart`) or the PDF's tag tree. None when nothing does."""
    text = node.text or ""
    start, end = len(text) - len(text.lstrip()), len(text.rstrip())
    if any(m.kind == "strong" and m.start <= start and m.end >= end for m in node.marks):
        return "bold"
    if set_apart := node.attrs.get("set_apart"):
        return str(set_apart)
    for f in node.facts:
        if f.code == "role-disagreement" and f.data.get("theirs") in HEADINGS:
            return "tag"
        if f.code == "role-agreement" and node.role.value in HEADINGS:
            return "tag"
    return None


def heading_gates(node: Node, before: Node | None) -> tuple[Gate, ...]:
    """Every check that rejects making `node` a heading. `before` is the nearest block with text
    before it in the page's reading order, or None."""
    text = node.text or ""
    found = []
    if _continues(text):
        found.append(Gate.CONTINUATION)
    if _runs_on(before):
        found.append(Gate.RUNS_ON)
    if _form_id(text):
        found.append(Gate.FORM_ID)
    if witness(node) is None:
        found.append(Gate.NO_WITNESS)
    return tuple(found)


NUMBER = re.compile(r"^[$€£(+\-−]*\s*\d[\d,.\s]*%?\)?\s*[$€£]?$")
DATE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
WEEKDAY = re.compile(r"^(?:sun|mon|tue|tues|wed|thu|thur|thurs|fri|sat)(?:day|nesday|rsday|urday"
                     r"|sday)?\.?$", re.I)
DASHES = frozenset({"-", "–", "—", "−"})


def data_column(cells: Sequence[str]) -> bool:
    """A column of numbers, dates or weekday names (a dash for none): data cells, never row
    headers. False when it has nothing but blanks and dashes."""
    seen = [c.strip() for c in cells if c.strip() and c.strip() not in DASHES]
    return bool(seen) and all(NUMBER.match(c) or DATE.match(c) or WEEKDAY.match(c) for c in seen)


__all__ = ["ON", "Gate", "data_column", "heading_gates", "witness"]
