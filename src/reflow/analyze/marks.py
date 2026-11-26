"""Inline marks: where bold and italic words come from (the layer itself is in the model:
Mark, Node.set_text, Node.join_inline and Node.check_inline).

`marks_for` reads a block's style runs (extract.lines.StyleRun) and raised runs (extract.lines.Line.sup)
and returns the `strong`, `em`, `sup` and accent (`red`, `green`) marks of its node. A
mark only ever says a word *is* bold, italic, raised or coloured, against a regular baseline: a
regular word is never marked, whatever the majority.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.model import ACCENTS, Mark, Node, mark_order

# (bold, italic, symbol, raised, accent) of one character
type Style = tuple[bool, bool, bool, bool, str]

# A colour is emphasis only as a short stretch of a block that is mostly in the text colour: at
# most ACCENT_RUN characters at a time, and no more than ACCENT_SHARE of the block's letters and
# digits. A heading or paragraph set wholly in a colour is that colour's style, not stress in it.
ACCENT_RUN = 40
ACCENT_SHARE = 0.5


def _fold(text: str, strict: bool) -> tuple[str, list[int]]:
    """The characters of `text` that carry the alignment, lower-cased, with each one's index in
    `text`: every character but spaces and soft hyphens when `strict`, else only letters and
    digits (punctuation is what the later text edits change)."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if strict and (ch.isspace() or ch == SOFT_HYPHEN):
            continue
        for low in ch.lower():
            if strict or low.isalnum():
                chars.append(low)
                origin.append(i)
    return "".join(chars), origin


def _styles(line: Line) -> list[Style]:
    """The style of each character of the line's text. Runs recorded for other text (the text
    was edited after) are not trusted: the line's own booleans stand for all of it. The raised
    runs always fit the text (Line.with_text drops them)."""
    n = len(line.text)
    raised = {i for start, end in line.sup for i in range(start, end)}
    if not line.runs or line.runs[-1].end != n:
        return [(line.bold, line.italic, False, i in raised, "") for i in range(n)]
    out: list[Style] = []
    for run in line.runs:
        out += [(run.bold, run.italic, run.symbol, i in raised, run.accent)
                for i in range(len(out), run.end)]
    return out + [(line.bold, line.italic, False, i in raised, "") for i in range(len(out), n)]


def _source(lines: Sequence[Line], strict: bool) -> tuple[str, list[Style]]:
    """The lines' folded characters, each with its style."""
    folded: list[str] = []
    styles: list[Style] = []
    for line in lines:
        text, origin = _fold(line.text, strict)
        by_char = _styles(line)
        folded.append(text)
        styles += [by_char[i] for i in origin]
    return "".join(folded), styles


def _runs_of(flags: Sequence[bool | None], text: str, kind: str) -> list[Mark]:
    """A mark over each maximal run of flagged characters; the spaces between two flagged
    characters stay inside the run. `None` is a space (it neither starts nor breaks a run)."""
    marks: list[Mark] = []
    start = end = None
    for i, flag in enumerate(flags):
        if flag is None:
            continue
        if flag:
            if start is None:
                start = i
            end = i + 1
        elif start is not None:
            marks.append(Mark(start, end, kind, text[start:end]))
            start = None
    if start is not None:
        marks.append(Mark(start, end, kind, text[start:end]))
    return marks


def marks_for(text: str, lines: Sequence[Line]) -> tuple[list[Mark], bool]:
    """The `strong`, `em` and `sup` marks for a node whose text came from `lines`, and whether
    the block is wholly italic.

    The node text is not the lines' text: lines are joined, soft hyphens removed, sometimes a
    list label cut. So the characters of the two are aligned instead of their offsets (as
    extract.links.offsets_of does), without spaces and soft hyphens; if the punctuation differs
    (a later edit), by letters and digits alone, and punctuation takes the style of the letter
    it touches. Each character of `text` takes the style of the one it came from. If the text is not
    found in the lines, there are no marks (a missing mark is a cosmetic loss, never wrong
    text) and the block is wholly italic by the line booleans.

    A wholly italic block is the typeface of a caption or a quotation, not stress inside it:
    it gets no `em`, and the caller keeps `class="i"`. A wholly bold block gets one `strong`
    over all its text. A run in a symbol font (a check box) is never emphasis. A `sup` covers
    only characters the lines raised themselves, never punctuation that took a letter's style
    (the period of "year.¹" is not raised)."""
    by_lines = all(line.italic for line in lines)
    for strict in (True, False):
        needle, origin = _fold(text, strict)
        if not needle:
            return [], by_lines
        for order in (list(lines), sorted(lines, key=lambda line: line.top)):
            haystack, source = _source(order, strict)
            if (at := haystack.find(needle)) >= 0:
                break
        else:
            continue
        break
    else:
        return [], by_lines
    style: list[Style | None] = [None] * len(text)
    for k, i in enumerate(origin):
        style[i] = source[at + k]
    raised = [None if ch.isspace() else bool(st and st[3]) for ch, st in zip(text, style)]
    if not strict:
        for i, ch in enumerate(text):
            if style[i] is None and _punctuation(ch):
                style[i] = (style[i - 1] if i else None) or (style[i + 1] if i + 1 < len(text) else None)
    marks: list[Mark] = []
    for kind, which in (("strong", 0), ("em", 1)):
        # Spaces, and punctuation touching no letter, neither start nor break a run.
        flags = [None if ch.isspace() or (st is None and _punctuation(ch))
                 else bool(st and not st[2] and st[which])
                 for ch, st in zip(text, style)]
        marks += _runs_of(flags, text, kind)
    marks += _runs_of(raised, text, "sup")
    marks += _accent_marks(text, style)
    letters = [st for ch, st in zip(text, style) if st is not None and ch.isalnum() and not st[2]]
    whole_italic = bool(letters) and all(st[1] for st in letters)
    if whole_italic:
        marks = [m for m in marks if m.kind != "em"]
    return sorted(marks, key=mark_order), whole_italic


def _accent_marks(text: str, style: Sequence[Style | None]) -> list[Mark]:
    """The accent marks of a block: each colour that is a short stretch of it (see ACCENT_RUN
    and ACCENT_SHARE). Spaces between two coloured characters stay inside the run; a symbol
    font's glyphs (a check box) are never coloured text."""
    letters = [st for ch, st in zip(text, style) if st is not None and ch.isalnum() and not st[2]]
    marks: list[Mark] = []
    for accent in ACCENTS:
        shown = sum(1 for st in letters if st[4] == accent)
        if not shown or shown > ACCENT_SHARE * len(letters):
            continue
        flags = [None if ch.isspace() or st is None else bool(not st[2] and st[4] == accent)
                 for ch, st in zip(text, style)]
        marks += [m for m in _runs_of(flags, text, accent) if m.end - m.start <= ACCENT_RUN]
    return marks


def _punctuation(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "P" or unicodedata.category(ch) == "Sc"


def drop_emphasis(node: Node) -> None:
    """Take the strong, em and accent marks off a node that became a heading: a heading's style
    is its level, so emphasis inside it would only repeat it. OCR marks stay."""
    node.marks = [m for m in node.marks if m.kind not in ("strong", "em", *ACCENTS)]
