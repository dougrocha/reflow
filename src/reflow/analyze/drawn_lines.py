"""Text lines cut where the boxes drawn on a form stand (analyze.drawn_boxes).

A form that draws its own check boxes and radio buttons prints the words of a choice around
them, and the text layer joins words across a box as it joins them across a gap. Two cuts give
each box the words that are its own:

- a line with a box inside it ("No [ ] Reason for induction ____") is cut at the box;
- a line that ends at a box, and has a question before the words of the first option ("Induction
  of Labor? Yes [ ] No [ ]"), is cut into the question and the option;

The positions of words in a line are estimated from the character count, as in
analyze.forms.split_captions, so a cut is made only where it falls on a space and near the box.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from reflow.extract.forms import Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent

CUT_SLACK = 12.0     # pt: a cut between words lies at most this far from the box it is cut for
ENDS_AT_BOX = 8.0    # pt: a line ends at most this far left of the box that its last words label
OPTION_WORDS = 3     # words: the longest option label cut off the end of a question
QUESTION = re.compile(r"^(?P<head>.*\w.*[?:])\s+(?P<tail>[^?:\s][^?:]*)$")


def _drawn(page: PageContent) -> list[Widget]:
    return [w for w in page.widgets if w.drawn]


def _on_row(box: Widget, line: Line) -> bool:
    return line.top - 3 <= (box.bbox[1] + box.bbox[3]) / 2 <= line.bottom + 3


def _part(line: Line, start: int, end: int) -> Line:
    """The text[start:end] of the line, placed by the character count."""
    per = (line.x1 - line.x0) / max(len(line.text), 1)
    return replace(line.cut(start, end, strip=True),
                   bbox=(line.x0 + start * per, line.top, line.x0 + end * per, line.bottom))


def _spaces(text: str) -> list[int]:
    return [m.start() for m in re.finditer(r"\s+", text)]


def _at_box(line: Line, box: Widget) -> int | None:
    """The offset of the space in `line` nearest where the box stands, when it is near enough
    and has words on both sides."""
    per = (line.x1 - line.x0) / max(len(line.text), 1)
    best = None
    for i in _spaces(line.text):
        if not line.text[:i].strip() or not line.text[i:].strip():
            continue
        off = abs(line.x0 + i * per - box.bbox[0])
        if off <= CUT_SLACK and (best is None or off < best[0]):
            best = (off, i)
    return None if best is None else best[1]


def _split_inside(line: Line, boxes: Sequence[Widget]) -> list[Line]:
    for box in sorted(boxes, key=lambda b: b.bbox[0]):
        if (_on_row(box, line) and line.x0 + 1 < box.bbox[0] and box.bbox[2] < line.x1 - 1
                and (at := _at_box(line, box)) is not None):
            head, tail = _part(line, 0, at), _part(line, at, len(line.text))
            return _split_inside(head, boxes) + _split_inside(tail, boxes)
    return [line]


def _split_question(line: Line, boxes: Sequence[Widget]) -> list[Line]:
    """A question run into the label of the box that ends the line."""
    ends = [b for b in boxes if _on_row(b, line) and 0 <= b.bbox[0] - line.x1 <= ENDS_AT_BOX]
    if not ends or not any(b.bbox[0] > line.x1 + 1 for b in boxes if b is not ends[0]
                           and _on_row(b, line)):
        return [line]
    found = QUESTION.match(line.text)
    if found is None or len(found["tail"].split()) > OPTION_WORDS:
        return [line]
    head_end = found.end("head")
    return [_part(line, 0, head_end), _part(line, found.start("tail"), len(line.text))]


def split_drawn_lines(page: PageContent, lines: Sequence[Line]) -> dict[int, list[Line]]:
    """The lines to cut around the page's drawn boxes. Returns {id(line): parts}."""
    boxes = _drawn(page)
    if not boxes:
        return {}
    cuts: dict[int, list[Line]] = {}
    for line in lines:
        if line.rotated or line.x1 <= line.x0:
            continue
        parts = [p for part in _split_inside(line, boxes) for p in _split_question(part, boxes)]
        if len(parts) > 1:
            cuts[id(line)] = parts
    return cuts
