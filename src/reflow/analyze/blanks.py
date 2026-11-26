"""Paper blanks on pages that have a text layer: a printed fill-in line becomes a text field.

A form printed with no AcroForm draws its blanks as runs of underscores ("Mother's Name:______
DOB:______"). The text layer holds the caption and the underscores, and the PDF names no field.
This pass turns each such run into a synthetic text `Widget` on the page (`page.widgets`), after
the extraction and before layout, as analyze.scan_fields does for a scan. The forms planner
(analyze.forms) then binds the caption to the field, groups the fields and builds the FIELD
nodes. The model reads no image and nothing is asked: the text layer says all there is to know.

A run is a fill-in line when
- it is three or more underscores, or three or more short runs set apart by spaces
  ("A __ __ __ __", the boxes of an ID), and not an identifier or word that has underscores
  inside it (`snake___case`);
- its line is upright, is not OCR text (a scan's text layer is analyze.scan_fields' business)
  and is not inside a table (a table's rules and cells are the table's);
- no widget lies on it but one text box (a check box, a comb or two boxes on a run leave it
  alone); that box, the PDF's own, is that blank's field: no new field is made, the run is cut
  all the same so that the caption labels the box;
- its line is a form line, not a paragraph (MAX_SENTENCE_WORDS words besides the runs), the text
  after it is a caption or a unit, not the rest of a sentence (`_captioning`), and it
  is not a rule across the page (a line of nothing but underscores, WIDE of the page wide,
  that no caption leads), nor a short dash alone (MIN_LONE wide) in a column of a listing.

The line is cut at each run: the pieces with words are the captions that label the fields, and
the underscores the fields replace leave the text. Every printed word stays. Blank-only lines
that follow a captioned blank, under it and aligned with its right end ("What happened?
______" and two lines of nothing under it), are the rest of that blank: one multiline field.
Where a line is cut, the positions are estimated from the characters (`_x`), as
forms.split_blank_lines does.

What the field is not: it has no PDF name, so the viewer's "Download filled PDF" leaves it
out, and what is typed in it stays in the browser (the form note says so). Each carries
`Detection("text", "underscores")`; the HTML marks it `data-field-source="blank"`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from reflow import geometry
from reflow.extract.forms import FF_MULTILINE, Detection, DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData
from reflow.model import BBox

# Three or more underscores, or three or more groups of them set apart by spaces.
RUN = re.compile(r"(?<!_)(?:_+[ \t]+){2,}_+(?!_)|_{3,}")
SHORT_RUN = 6               # a run this short inside a word is an identifier, not a blank
OPEN_QUOTES = ('"', "'", "\u201c", "\u2018")
CLOSE_QUOTES = ('"', "'", "\u201d", "\u2019")
MAX_TAIL = 4                # words after a run, up to the next one, that a caption or a unit may have
MAX_SENTENCE_WORDS = 30     # a line of more words than this (besides the runs) is a paragraph
WIDE = 0.8                  # a lone run this share of the page wide is a rule, not a blank
MAX_HEIGHT = 2.5            # × the font size: a taller line is a scrap of garbled glyphs, not a form line
MIN_LONE = 60.0             # a line of nothing but blank is a place to write at least this wide (points)
CONTINUES = 3.0             # × line height: a blank-only line this near under a blank continues it
RIGHT_EDGE = 12.0           # points: the lines of one answer area end this near each other
DETECTION = Detection(DetectionSource.TEXT, "underscores")


def _x(line: Line, at: int) -> float:
    """Where the character at `at` starts, from the line's width shared by the characters in
    proportion to the width of a typical glyph (an underscore is wider than a lowercase i)."""
    weights = [_weight(c) for c in line.text]
    total = sum(weights) or 1.0
    return line.x0 + (line.x1 - line.x0) * sum(weights[:at]) / total


def _weight(c: str) -> float:
    if c == "_":
        return 0.56
    if c.isspace():
        return 0.28
    if c in "il.,:;'|!()":
        return 0.26
    if c in "mwMW":
        return 0.82
    return 0.68 if c.isupper() else 0.55 if c.isdigit() else 0.5


def _runs(line: Line) -> list[re.Match[str]]:
    """The runs of the line that are blanks (not underscores inside a word)."""
    text = line.text
    found = []
    for m in RUN.finditer(text):
        inside = (m.start() > 0 and text[m.start() - 1].isalnum()
                  and m.end() < len(text) and text[m.end()].isalnum())
        quoted = (text[:m.start()].rstrip().endswith(OPEN_QUOTES)
                  and text[m.end():].lstrip().startswith(CLOSE_QUOTES))
        if not (inside and len(m.group()) < SHORT_RUN) and not quoted:   # "Enter "____" when ..."
            found.append(m)
    return found


def _captioning(line: Line, runs: Sequence[re.Match[str]]) -> list[re.Match[str]]:
    """The runs whose text after them is a caption or nothing: a few words ("grams"), or words
    that end in a colon or a question mark, up to the next run. A blank in the middle of a
    sentence ("I ____ being first duly sworn on oath ...") is a sentence's gap, not a form
    line, and stays text."""
    kept = []
    for i, m in enumerate(runs):
        tail = line.text[m.end():runs[i + 1].start() if i + 1 < len(runs) else len(line.text)]
        head = line.text[runs[i - 1].end() if i else 0:m.start()].rstrip()
        # A sentence that runs on through the blank ("I ____ agree."): the words before it end
        # in a word, not a caption's colon, and the words after it go on in lower case or end
        # the sentence. A unit after a caption ("Weight: ____ grams") is not one.
        words = tail.split()
        if (head and head[-1].isalnum() and words and not tail.rstrip().endswith((":", "?"))
                and (words[0][0].islower() or tail.rstrip().endswith((".", "!", ";", ",")))):
            continue
        if len(words) <= MAX_TAIL or tail.rstrip().endswith((":", "?")):
            kept.append(m)
    return kept


def _words(line: Line, runs: Sequence[re.Match[str]]) -> int:
    text = line.text
    for m in reversed(runs):
        text = text[:m.start()] + " " + text[m.end():]
    return len(text.split())


def _owner(box: BBox, line: Line, widgets: Sequence[Widget]) -> Widget | None | bool:
    """What is on the blank: None when no widget is, the one text box that sits on it (its
    centre within the run, the box the PDF made for that blank), or True when a widget lies on it
    that is not a single such box (a check box, a comb, two boxes), which leaves it alone."""
    on_row = [w for w in widgets if w.bbox[1] - 3 <= (line.top + line.bottom) / 2 <= w.bbox[3] + 3
              and min(w.bbox[2], box[2]) > max(w.bbox[0], box[0])]
    if not on_row:
        return None
    mine = [w for w in on_row if w.kind in ("text", "combo") and not w.comb
            and box[0] - 2 <= (w.bbox[0] + w.bbox[2]) / 2 <= box[2] + 2]
    return mine[0] if len(mine) == 1 == len(on_row) else True


def _inside(line: Line, tables: Sequence[TableData]) -> bool:
    return any(t.bbox and geometry.centre_in(line.bbox, t.bbox) for t in tables)


def _eligible(line: Line, tables: Sequence[TableData]) -> bool:
    """An upright, readable form line: not OCR, not junk glyphs (U+FFFD, a line far taller than
    its type), not in a table, and not a placeholder in a quotation ("“... between ____ and ____")."""
    text = line.text
    return (not line.ocr and not line.rotated and line.x1 > line.x0 and "_" in text
            and not line.unicode_errors and "\ufffd" not in text
            and line.bottom - line.top <= MAX_HEIGHT * max(line.size, 6.0)
            and text.count("\u201c") <= text.count("\u201d") and text.count('"') % 2 == 0
            and not _inside(line, tables))


def _widget(page: PageContent, index: int, box: BBox, multiline: bool = False) -> Widget:
    return Widget(page=page.number, index=index, kind="text", bbox=box,
                  flags=FF_MULTILINE if multiline else 0, detected=DETECTION)


def _caption(line: Line, start: int, end: int, left: float, right: float) -> Line | None:
    """The piece of the line in text[start:end], or None when it has no word to label with."""
    piece = line.cut(start, end, strip=True)
    if not any(c.isalnum() for c in piece.text):
        return None
    return replace(piece, bbox=(left, line.top, right, line.bottom))


def _cut(page: PageContent, line: Line, runs: Sequence[tuple[re.Match[str], Widget | None]],
         first: int) -> tuple[list[Line], list[Widget]]:
    """The pieces of `line` between its runs, and a new field on each run no widget is on."""
    pieces: list[Line] = []
    fields: list[Widget] = []
    at, left = 0, line.x0
    for m, owner in runs:
        start, end = _x(line, m.start()), _x(line, m.end())
        if isinstance(owner, Widget):
            start = min(start, owner.bbox[0])   # the caption ends where the PDF's box begins
        if piece := _caption(line, at, m.start(), left, start):
            pieces.append(piece)
        if isinstance(owner, Widget):
            owner.on_blank = True
        if owner is None:
            fields.append(_widget(page, first + len(fields), (start, line.top, end, line.bottom)))
        at, left = m.end(), end
    if piece := _caption(line, at, len(line.text), left, line.x1):
        pieces.append(piece)
    return pieces, fields


def _blank_only(line: Line, runs: Sequence[tuple[re.Match[str], Widget | None]]) -> bool:
    return len(runs) == 1 and not any(c.isalnum() for c in line.text)


def detect(page: PageContent, tables: Sequence[TableData]) -> int:
    """Give the page a text field for each blank it prints in underscores. Mutates
    `page.widgets` and `page.lines`; returns the number of fields made."""
    candidates = [(l, _runs(l)) for l in page.lines if _eligible(l, tables)]
    candidates = [(l, _captioning(l, r)) for l, r in candidates if r and _words(l, r) <= MAX_SENTENCE_WORDS]
    candidates = [(l, r) for l, r in candidates if r]
    if not candidates:
        return 0
    existing = list(page.widgets)
    first = max((w.index for w in existing), default=-1) + 1
    cuts: dict[int, list[Line]] = {}
    fields: list[Widget] = []
    previous: tuple[Line, Widget] | None = None   # the last blank-ended line and its last field
    rules: dict[int, tuple[Line, Widget]] = {}    # lone wide blank lines that may be rules
    for line, runs in sorted(candidates, key=lambda c: (c[0].top, c[0].x0)):
        found = [(m, _owner((_x(line, m.start()), line.top, _x(line, m.end()), line.bottom),
                            line, existing)) for m in runs]
        blanks = [(m, owner) for m, owner in found if owner is not True]
        if not blanks:
            previous = None
            continue
        if _blank_only(line, blanks) and blanks[0][1] is None:
            if (previous is not None
                    and -2.0 <= line.top - previous[0].bottom <= CONTINUES * (line.bottom - line.top)
                    and abs(line.x1 - previous[1].bbox[2]) <= RIGHT_EDGE
                    and not any(previous[0].bottom < o.top and o.bottom < line.top
                                and o.x0 < line.x1 and o.x1 > line.x0 for o in page.lines)):
                # The rest of the answer area above: one field over all of its lines.
                top = previous[1]
                top.bbox = (top.bbox[0], top.bbox[1], max(top.bbox[2], line.x1), line.bottom)
                top.flags |= FF_MULTILINE
                cuts[id(line)] = []
                rules.pop(id(top), None)
                previous = (line, top)
                continue
        if _blank_only(line, blanks) and line.x1 - line.x0 < MIN_LONE:
            previous = None
            continue   # a short dash in a column of a table of listings, not a line to write on
        pieces, made = _cut(page, line, blanks, first + len(fields))
        cuts[id(line)] = pieces
        fields.extend(made)
        if made and _blank_only(line, blanks) and line.x1 - line.x0 >= WIDE * page.width:
            rules[id(made[0])] = (line, made[0])   # a rule across the page, unless more lines join it
        last = blanks[-1]
        ends = last[1] is None and last[0].end() == len(line.text.rstrip())
        previous = (line, made[-1]) if ends else None
    for line, widget in rules.values():   # a lone line across the page is a rule, not a blank
        fields.remove(widget)
        del cuts[id(line)]
    if not cuts:
        return 0
    page.widgets = existing + fields
    page.lines = [piece for l in page.lines for piece in cuts.get(id(l), [l])]
    return len(fields)


def run_pass(pages: Sequence[PageContent], tables: dict[int, list[TableData]]) -> int:
    """The engine's pass over the extracted pages (analyze.engine._extract). Returns the number
    of fields made, in the document."""
    return sum(detect(page, tables.get(page.number, [])) for page in pages)


__all__ = ["DETECTION", "RUN", "detect", "run_pass"]
