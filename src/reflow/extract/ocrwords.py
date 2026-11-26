"""OCR word grades bound to the text layer they produced.

The grading plugin (ingest.ocr_trust) leaves one `PageGrade` per page reflow OCR'd, in OCR image
pixels. `bind_words` maps those boxes onto the displayed page and checks them against the working
copy's own invisible text: the text layer was written from these very words, so every word that
was kept must coincide with a text-layer word of the same text (`same_place`). A sidecar that
is shifted, turned or bound to another page fails here, on evidence, rather than on its size:

  graded      every kept word matched; each OCR line carries its words (`Line.grades`)
  partial     some did not: the lines they overlap stay ungraded (`grades` None)
  misaligned  under MIN_MATCHED of them did: every line of the page stays ungraded

Either of the last two is an `ocr-grades-unmatched` warning with the boxes of the unmatched
words, and an ungraded line falls back to the lexical OCR gate (text_quality).
"""

from __future__ import annotations

import statistics
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from reflow import geometry
from reflow.pdfobjects import layer_word_chars, layer_words, to_top_left
from reflow.ingest.ocr_trust import Grade, PageGrade, SidecarRead, compact_nfkc
from reflow.model import BBox

if TYPE_CHECKING:
    import pypdfium2 as pdfium

    from reflow.extract.lines import Line

MATCH_IOU = 0.5      # a kept word and its text-layer word overlap at least this much across
MIN_MATCHED = 0.9    # a page whose kept words match less than this share is misaligned
REMNANT = 0.5        # a line that lost this share of its words keeps only a remnant

PageStatus = Literal["graded", "partial", "misaligned"]


@dataclass(frozen=True)
class GradedWord:
    bbox: BBox                  # top-left points of the displayed page (Line.bbox space)
    text: str                   # Tesseract's reading
    layer_text: str             # what the text layer holds for it: the text, or "" (dropped)
    conf: int
    grade: Grade
    pen: float | None
    tall: float
    struck: bool = False        # Word.struck: a line runs through it
    underlined: bool = False    # Word.underlined: a line runs under all of it
    line: int | None = None     # PageContent.lines index; None: dropped, or matched no line
    ocr_line: int = 0           # Tesseract's line on the page (Word.line)
    remnant: bool = False       # kept on a line whose words OCR mostly could not read (REMNANT)
    under: tuple[int, int] | None = None   # Word.under: the characters of `text` underlined
    closes: bool = False        # Word.closes: the measured "]" a struck passage ends at
    unconfirmed: bool = False   # Word.unconfirmed: a kept layer's word no reading confirms
    contradicted: bool = False  # Word.contradicted: a kept layer's word a sure reading contradicts


@dataclass(frozen=True)
class PageWords:
    status: PageStatus
    words: tuple[GradedWord, ...]          # every graded word, dropped ones included
    read_height: float | None              # median READ height, points
    unmatched: tuple[BBox, ...] = ()       # kept words that matched no text-layer word
    tracked: bool = False                  # PageGrade.tracked
    matched: frozenset[int] = field(default=frozenset(), repr=False)   # indexes into words

    @property
    def omitted(self) -> tuple[GradedWord, ...]:
        """The words whose reading is not printed: graded UNREAD (a struck word's brackets may
        stay in the layer, `layer_text`)."""
        return tuple(w for w in self.words if w.grade is Grade.UNREAD)


def same_place(word: BBox, layer: BBox) -> bool:
    """Whether a graded word and a text-layer word are one word on the page: their horizontal
    extents overlap by MATCH_IOU (intersection over union), and their vertical extents at all.
    The layer's boxes are the font's, set from the line's height (OCRmyPDF sizes each line's
    font to fit it, and which part of the line a short word's box covers differs between
    OCRmyPDF versions: 17.14 sets a handwritten line's boxes 14 pt lower than 17.13 did), so a
    short word's box is shorter than its layer box, or sits above or below its middle, and a
    full IoU or a half overlap would refuse it. A shifted or turned sidecar still fails the
    horizontal test, or the vertical one once it is off by a box's height, and the page is
    misaligned when under MIN_MATCHED of its words match."""
    inter = min(word[2], layer[2]) - max(word[0], layer[0])
    union = max(word[2], layer[2]) - min(word[0], layer[0])
    across = min(word[3], layer[3]) - max(word[1], layer[1])
    return union > 0 and inter / union >= MATCH_IOU and across > 0


def invisible_boxes(page: pdfium.PdfPage) -> list[BBox]:
    """The boxes of the page's invisible-text words (someone else's OCR layer, or reflow's)."""
    return [box for _, box in layer_words(page)]


def _centre_line(box: BBox, text: str, lines: Sequence[Line]) -> int | None:
    """The OCR line holding the word's centre, the one whose text holds the word first (OCR
    lines overlap on a busy page); the one it overlaps most when none holds its centre."""
    best, area, around = None, 0.0, None
    for i, line in enumerate(lines):
        if not line.ocr:
            continue
        if geometry.centre_in(box, line.bbox):
            if text in compact_nfkc(line.text):
                return i
            around = i if around is None else around
            continue
        shared = geometry.overlap(line.bbox, box)
        if shared > area:
            best, area = i, shared
    return around if around is not None else best


def bind_words(grade: PageGrade, page: pdfium.PdfPage,
               lines: Sequence[Line] | None) -> PageWords:
    """The page's graded words in page points, validated against its text layer. With `lines`
    (the page's extracted lines), each kept word is assigned the OCR line holding it, and each
    OCR line's `grades` is set in place: its words, () when it holds none, None when the page is
    misaligned or the line overlaps an unmatched word. A kept layer's grades (mode "keep") whose
    words no longer read as the layer's do (`unreadable_layer` None) are misaligned. With None (an engine that reads no lines),
    only the words and the status are returned."""
    _, _, width, height = to_top_left(page, page.get_cropbox())
    sx, sy = width / grade.image_px[0], height / grade.image_px[1]
    placed = [(w, (round(w.box[0] * sx, 2), round(w.box[1] * sy, 2),
                   round(w.box[2] * sx, 2), round(w.box[3] * sy, 2))) for w in grade.words]
    layer: dict[str, list[BBox]] = {}
    for text, box in layer_words(page):
        layer.setdefault(compact_nfkc(text), []).append(box)
    matched: set[int] = set()
    unmatched: list[BBox] = []
    kept = 0
    for i, (w, box) in enumerate(placed):
        if not w.layer_text:
            continue
        kept += 1
        if any(same_place(box, other) for other in layer.get(compact_nfkc(w.layer_text or w.text), ())):
            matched.add(i)
        else:
            unmatched.append(box)
    status: PageStatus = ("misaligned" if (kept and len(matched) < MIN_MATCHED * kept)
                          or unreadable_layer(grade, page) is None
                          else "partial" if unmatched else "graded")
    # A line whose words with a letter or digit were at least REMNANT dropped: what it kept is
    # what was left of a scrawl or a struck-out passage (text_quality.ocr_gate).
    alnum: dict[int, list[bool]] = {}
    for w in grade.words:
        if any(ch.isalnum() for ch in w.text):
            alnum.setdefault(w.line, []).append(w.grade is Grade.UNREAD)
    remnant = {line for line, dropped in alnum.items() if sum(dropped) >= REMNANT * len(dropped)}
    words = []
    for i, (w, box) in enumerate(placed):
        at = (_centre_line(box, compact_nfkc(w.layer_text or w.text), lines)
              if lines is not None and i in matched else None)
        words.append(GradedWord(
            bbox=box, text=w.text, layer_text=w.layer_text, conf=w.conf, grade=w.grade, pen=w.pen,
            tall=w.tall, struck=w.struck, underlined=w.underlined, line=at, ocr_line=w.line,
            remnant=w.grade is not Grade.UNREAD and w.line in remnant, under=w.under,
            closes=w.closes, unconfirmed=w.unconfirmed, contradicted=w.contradicted))
    reads = [b[3] - b[1] for w, b in placed if w.grade is Grade.READ]
    out = PageWords(status, tuple(words), round(statistics.median(reads), 2) if reads else None,
                    tuple(unmatched), grade.tracked, frozenset(matched))
    if lines is not None:
        for i, line in enumerate(lines):
            if not line.ocr:
                continue
            if status == "misaligned" or any(geometry.intersects(line.bbox, b) for b in unmatched):
                line.grades = None
            else:
                line.grades = tuple(w for w in words if w.line == i)
    return out


def unreadable_layer(grade: PageGrade | None, page: pdfium.PdfPage) -> frozenset[int] | None:
    """The textpage indices of the characters of the words of the page's own OCR layer that its
    grades leave out (a "keep" grade, ingest.ocr_trust.grade_layer): extraction does not
    read them, so the kept layer prints what reflow's own OCR would. The grades are of
    `layer_words` in order, so a word is its characters, never a box that a neighbour's letters
    may lie in. Empty for any other page. None for a layer that no longer reads as its grades
    do: they are not its grades, nothing is left out, and bind_words calls the page misaligned."""
    if grade is None or grade.mode != "keep":
        return frozenset()
    layer = layer_word_chars(page)
    if [text for text, _, _ in layer] != [w.text for w in grade.words]:
        return None
    return frozenset(i for (_, _, chars), w in zip(layer, grade.words) if w.grade is Grade.UNREAD
                     for i in chars)


def missing_grades(read: SidecarRead, expected: Collection[int]) -> dict[int, str]:
    """page → why the grades of a page reflow OCR'd with a grading engine are not usable:
    "absent", "corrupt" or "stale" from the sidecar, or "no-page" when it is current but lacks
    the page. A page not in `expected` is deliberately ungraded and is never named."""
    if read.status != "ok":
        return {n: read.status for n in sorted(expected)}
    return {n: "no-page" for n in sorted(expected) if n not in read.pages}
