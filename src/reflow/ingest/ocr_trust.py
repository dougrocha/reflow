"""How far each word OCR read can be trusted, graded inside OCRmyPDF before the text layer exists.

Tesseract scores every word it reads (hOCR's `x_wconf`, 0-100), and OCRmyPDF throws that away
once the text layer is drawn. This module keeps it. Loaded as an OCRmyPDF plugin
(`--plugin <this file>`), it takes over the engine's `generate_hocr`: it lets Tesseract read the
page, grades each word (`grade_words`), writes the grades with their boxes to
`<--reflow-evidence>/<page>.json`, and with `--reflow-drop on` removes the words it cannot read
from the hOCR (`clean_hocr`), so they never enter the text layer. A dropped word keeps its box in
the evidence, so the conversion can report it on its page.

  READ    the reading is confident: text, headings and the Contents
  DOUBT   printed as read, marked doubtful, never navigation
  UNREAD  not printed; its box is a review item

It also measures each word's pixels for a line through it or under it, and the brackets at its
ends (`track_changes`): a page with struck and underlined words is a tracked-change page,
on which a struck word below READ_CONF is never printed; only its brackets stay in the layer.

`ingest.ocr.ocr_pages` checks the evidence and keeps it beside the working copy as the sidecar
`ocr-words.json` (`write_sidecar`), tied to that PDF by its hash and to this policy by
POLICY_VERSION; `extract.ocrwords` binds the grades to the text layer's lines.

Stdlib and Pillow only, and no `reflow` import: the system Python that runs /usr/bin/ocrmypdf
has Pillow and no numpy, and loads this file by path. The venv imports it as an ordinary module.
"""

from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import os
import re
import statistics
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops, ImageDraw

# Bump when the grading changes what a sidecar means: an older sidecar then reads as stale.
POLICY_VERSION = 7
SIDECAR_NAME = "ocr-words.json"

# The grading policy (grade_words), set from the grade report over fixtures captured through
# OCRmyPDF itself (tests/ocr/capture.py).
READ_CONF = 80        # a word at or above this confidence is read, whatever its colour
UNREAD_CONF = 40      # below this nothing is printed
SHAPE_CONF = 60       # below this a token that is neither all letters nor all digits is unread
PEN_UNREAD = 0.5      # a doubtful word whose dark pixels are this chromatic is pen: unread
SCRIPT_HEIGHT = 1.5   # a doubtful word this many times the page's read height is script: unread
STRAY_CHARS = 3       # a read word this short...
STRAY_LINE = 0.6      # ...in a line whose words are this share unread is a stray of the script
BOLD_STROKE = 1.5     # a word whose stroke weight is this many times the page's READ median is bold
SAME_STROKE = 1.15    # ...and of a neighbour's weight when within this factor of it

# Pen colour (pen_share): a pixel darker than DARK is ink; one whose RGB channels spread by at
# least CHROMA is chromatic. The same values as the page scan's (extract.scan), which imports
# pen_share from here.
DARK = 180
CHROMA = 40
# Stroke weight (stroke_weight): a pixel with more than STROKE_INK ink (0 paper, 1 black) is in a
# stroke. The page scan (extract.scan) measures its lines' weight with it too.
STROKE_INK = 0.25

# redo and force: reflow's own OCR of the page. keep: the page's own OCR layer, which reflow keeps
# and grades by a redo reading it throws away (grade_layer).
Mode = Literal["redo", "force", "keep"]


class Grade(StrEnum):
    READ = "read"
    DOUBT = "doubt"
    UNREAD = "unread"


@dataclass(frozen=True)
class RawWord:
    """One word of Tesseract's hOCR."""
    box: tuple[int, int, int, int]   # OCR image pixels, top-left origin
    text: str
    conf: int                        # x_wconf, 0..100
    line: int                        # index of its ocr_line in the page, in hOCR order
    id: str = ""                     # the hOCR element id, which clean_hocr removes by
    base: int | None = None          # its line's baseline under the word's centre, image pixels
    # A kept layer's word that Tesseract reads alike, letter for letter (grade_layer): two
    # readings agree, so its height is no sign of writing.
    agreed: bool = False


@dataclass(frozen=True)
class Word:
    box: tuple[int, int, int, int]   # OCR image pixels
    text: str                        # Tesseract's reading
    conf: int                        # x_wconf 0..100
    grade: Grade
    pen: float | None                # chromatic share of the word's dark pixels; None on a grey image
    tall: float                      # box height / page median READ height
    struck: bool = False             # a line runs through it (`struck`)
    underlined: bool = False         # a line runs under all of it (`underline`)
    layer_text: str = ""             # what clean_hocr wrote: the text, its brackets, or ""
    line: int = 0                    # its hOCR line, for the stray-letters rule
    # The characters [i, j) of `text` a line runs under when it is not all of them (the comma
    # before an insertion); None otherwise.
    under: tuple[int, int] | None = None
    closes: bool = False             # the measured "]" a struck passage ends at
    # A word of a page's own OCR layer that Tesseract did not read alike with confidence
    # (grade_layer): its doubt is that nothing confirms it, not that an OCR was unsure.
    unconfirmed: bool = False
    # A word of a page's own OCR layer that the one Tesseract word under it reads otherwise with
    # confidence (grade_layer): doubtful in its own right, but not a sign of a heading's type.
    contradicted: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["box"], d["grade"] = list(self.box), self.grade.value
        d["under"] = None if self.under is None else list(self.under)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Word:
        return cls(box=tuple(int(v) for v in d["box"]), text=str(d["text"]), conf=int(d["conf"]),
                   grade=Grade(d["grade"]), pen=None if d.get("pen") is None else float(d["pen"]),
                   tall=float(d["tall"]), struck=bool(d.get("struck", False)),
                   underlined=bool(d.get("underlined", False)),
                   layer_text=str(d.get("layer_text", "")), line=int(d.get("line", 0)),
                   under=None if d.get("under") is None else tuple(int(v) for v in d["under"]),
                   closes=bool(d.get("closes", False)),
                   unconfirmed=bool(d.get("unconfirmed", False)),
                   contradicted=bool(d.get("contradicted", False)))


@dataclass(frozen=True)
class PageGrade:
    page: int                        # 1-based page of the input PDF, from OCRmyPDF's file name
    mode: Mode
    image_px: tuple[int, int]        # the OCR image's size, which the boxes are in
    # keep: the words are the page's own layer's, in its order (grade_layer); else Tesseract's
    words: tuple[Word, ...]
    tracked: bool = False            # a tracked-change page (`track_changes`), from its words alone

    def to_dict(self) -> dict:
        return {"page": self.page, "mode": self.mode, "image_px": list(self.image_px),
                "words": [w.to_dict() for w in self.words], "tracked": self.tracked}

    @classmethod
    def from_dict(cls, d: dict) -> PageGrade:
        mode = d["mode"]
        if mode not in ("redo", "force", "keep"):
            raise ValueError(f"unknown OCR mode {mode!r}")
        width, height = (int(v) for v in d["image_px"])
        return cls(int(d["page"]), mode, (width, height),
                   tuple(Word.from_dict(w) for w in d["words"]), bool(d.get("tracked", False)))


@dataclass(frozen=True)
class SidecarRead:
    status: Literal["ok", "absent", "corrupt", "stale"]   # stale: PDF hash or POLICY_VERSION differs
    pages: dict[int, PageGrade]                           # empty unless status == "ok"


# -- hOCR ---------------------------------------------------------------------------------------

_BBOX = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_CONF = re.compile(r"x_wconf (\d+)")
_BASELINE = re.compile(r"baseline (-?[\d.]+) (-?[\d.]+)")
_LINE_CLASSES = {"ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat"}


class _HocrReader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.words: list[RawWord] = []
        self._stack: list[str | None] = []   # per open span: "line", "word" or None
        self._line = -1
        self._base: tuple[float, float, float] | None = None   # the line's (x0, y, slope)
        self._word: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "span":
            return
        a = dict(attrs)
        classes = set((a.get("class") or "").split())
        title = a.get("title") or ""
        if classes & _LINE_CLASSES:
            self._line += 1
            self._base = None
            if (box := _BBOX.search(title)) and (base := _BASELINE.search(title)):
                x0, _, _, y1 = (int(v) for v in box.groups())
                self._base = (x0, y1 + float(base.group(2)), float(base.group(1)))
            self._stack.append("line")
        elif "ocrx_word" in classes and (box := _BBOX.search(title)):
            conf = _CONF.search(title)
            self._word = {"box": tuple(int(v) for v in box.groups()), "id": a.get("id") or "",
                          "conf": int(conf.group(1)) if conf else 0, "text": []}
            self._stack.append("word")
        else:
            self._stack.append(None)

    def handle_endtag(self, tag):
        if tag != "span" or not self._stack:
            return
        if self._stack.pop() == "word" and self._word is not None:
            w = self._word
            text = "".join(w["text"]).strip()
            if text:
                base = None
                if self._base is not None:
                    x0, y, slope = self._base
                    base = round(y + slope * ((w["box"][0] + w["box"][2]) / 2 - x0))
                self.words.append(RawWord(w["box"], text, w["conf"], max(self._line, 0), w["id"],
                                          base))
            self._word = None

    def handle_data(self, data):
        if self._word is not None:
            self._word["text"].append(data)


def parse_hocr(hocr: str) -> list[RawWord]:
    """Every word of the page's hOCR with text, in reading order."""
    reader = _HocrReader()
    reader.feed(hocr)
    reader.close()
    return reader.words


_WORD_SPAN = re.compile(r"<span\b[^>]*\bclass=['\"]ocrx_word['\"][^>]*>.*?</span>", re.S)
_ID = re.compile(r"\bid=['\"]([^'\"]+)['\"]")


_LINE_TAG = re.compile(r"<span\b[^>]*\bclass=['\"](?:%s)['\"][^>]*>"
                       % "|".join(sorted(_LINE_CLASSES)))


def clean_hocr(hocr: str, words: Sequence[Word], ids: Sequence[str]) -> str:
    """The hOCR without its UNREAD words: each word element whose id is graded UNREAD is removed,
    whole, so the text layer never holds it, unless the word keeps a `layer_text` (the brackets
    of a struck word, `track_changes`): then that is its only text. `ids[i]` is the hOCR id of
    `words[i]` (RawWord.id).
    OCRmyPDF sizes a line's font from the height of its box, so a line that lost a word has the
    top of its box lowered to the top of the words it keeps (an upright line only; the bottom
    and the baseline, measured from it, stay). Every other element is kept as it was."""
    unread = {i: w.layer_text for w, i in zip(words, ids) if w.grade is Grade.UNREAD and i}

    def clean(segment: str) -> str:
        kept: list[int] = []
        dropped = False
        def drop(m: re.Match[str]) -> str:
            nonlocal dropped
            tag = m.group(0)[:m.group(0).index(">")]
            found = _ID.search(tag)
            if found and found.group(1) in unread:
                if keep := unread[found.group(1)]:
                    if box := _BBOX.search(tag):
                        kept.append(int(box.group(2)))
                    return f"{tag}>{html.escape(keep, quote=False)}</span>"
                dropped = True
                return ""
            if box := _BBOX.search(tag):
                kept.append(int(box.group(2)))
            return m.group(0)
        out = _WORD_SPAN.sub(drop, segment)
        line = _LINE_TAG.match(out)
        if not (dropped and kept and line) or "textangle" in line.group(0):
            return out
        box = _BBOX.search(line.group(0))
        if box is None or min(kept) <= int(box.group(2)):
            return out
        x0, _, x1, y1 = box.group(1), box.group(2), box.group(3), box.group(4)
        tag = line.group(0).replace(box.group(0), f"bbox {x0} {min(kept)} {x1} {y1}", 1)
        return tag + out[line.end():]

    cuts = [m.start() for m in _LINE_TAG.finditer(hocr)]
    edges = [0, *cuts, len(hocr)]
    return "".join(clean(hocr[a:b]) for a, b in zip(edges, edges[1:]))


# -- grading ------------------------------------------------------------------------------------

def _core(text: str) -> str:
    """The token without the punctuation and symbols around it ("ATTEST:" → "ATTEST")."""
    start, end = 0, len(text)
    while start < end and not text[start].isalnum():
        start += 1
    while end > start and not text[end - 1].isalnum():
        end -= 1
    return text[start:end]


_JOINERS = re.compile(r"(?<=[^\W\d_])['’-](?=[^\W\d_])")


_ADDRESS = re.compile(r"(?:https?://|www\.)[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
                      r"(?:/[\w./-]*)?", re.I)


def _plausible(text: str) -> bool:
    """Letters only or digits only, once the punctuation around it is set aside. An apostrophe
    or a hyphen between letters is part of a word ("owner's", "e-mail"). A web address, from
    "www." or its scheme on, is plausible too."""
    core = _JOINERS.sub("", _core(text))
    return bool(core) and (core.isalpha() or core.isdigit() or bool(_ADDRESS.fullmatch(core)))


def _letters(text: str) -> bool:
    """Letters only, once the punctuation around it is set aside (as `_plausible`)."""
    return _JOINERS.sub("", _core(text)).isalpha()


def _height(box: tuple[int, int, int, int]) -> int:
    return max(1, box[3] - box[1])


def colour_cast(words: Iterable[tuple[str, int, float | None]]) -> bool:
    """Whether the scan's colour tints the page's type as much as any ink: the median pen of the
    words (text, confidence, pen) that rule 1 of grade_words reads, longer than STRAY_CHARS."""
    typed = [pen for text, conf, pen in words if pen is not None and conf >= READ_CONF
             and len(_core(text) or text) > STRAY_CHARS]
    return bool(typed) and statistics.median(typed) >= PEN_UNREAD


def grade_words(raw: Sequence[RawWord], image: Image.Image | None) -> list[Word]:
    """The grades of one page's words, in order. The rules run in turn:

      1. conf >= READ_CONF: READ. Colour does not change it: printed colour read confidently
         stays text.
      2. conf < UNREAD_CONF, or conf < SHAPE_CONF and the token is neither letters nor digits
         ("6S512", "|87)88-HY"): UNREAD.
      3. otherwise DOUBT, but UNREAD when the word is pen-coloured (pen >= PEN_UNREAD) or script
         (tall >= SCRIPT_HEIGHT), unless a word of its line that rule 1 read, and that is longer
         than STRAY_CHARS, is coloured or as tall too: then the line is printed in colour or
         large type ("Carmel Area Wastewater District", a map's "CHAMBERLIN AVENUE").
      4. a READ word of at most STRAY_CHARS characters in a line whose words are at least
         STRAY_LINE UNREAD: UNREAD (stray confident letters of handwriting).
      5. a word rule 3 left UNREAD (conf >= UNREAD_CONF) is DOUBT when it is bold print
         beside bold print OCR read well (the "What's" of the heading "What's New"): a
         word next to it in its line, no further off than the taller of the two, is READ, all
         letters and of its colour (both pen >= PEN_UNREAD or neither), and both stroke weights
         (stroke_weight) are at least BOLD_STROKE times the page's median READ word and within
         SAME_STROKE of each other. A pen keeps one weight, so a weight that matches its
         neighbour's does not tell print from writing; a bold one beside confident letters
         does. Not figures: Tesseract reads handwritten ones confidently (a ledger's "000,").

    Rule 2's shape test lets a web address through ("www.irs.gov/FormW9"): it is print, never
    the letters or digits alone that the test asks of a word.

    `tall` is the word's height over the page's median READ height (1.0 when nothing is READ).
    No dictionary, no language list and no exception for capitals.

    Pen colour tells ink from black type, so it counts only on a page whose type is black: where
    the median pen of the words rule 1 reads (longer than STRAY_CHARS) is PEN_UNREAD or more, the
    scan's colour cast tints the type as much as any ink (naca's covers, on brown paper), and
    pen says nothing there, as on a grey scan. Each word still records its pen."""
    pens = pen_shares(image, [w.box for w in raw]) if image is not None else [None] * len(raw)
    cast = colour_cast((w.text, w.conf, pen) for w, pen in zip(raw, pens))
    read = [_height(w.box) for w in raw if w.conf >= READ_CONF]
    ref = statistics.median(read) if read else None
    talls = [round(_height(w.box) / ref, 3) if ref else 1.0 for w in raw]
    # Per line: whether a confidently read word (not a stray) is coloured, and whether one is tall.
    printed: dict[int, tuple[bool, bool]] = {}
    for w, pen, tall in zip(raw, pens, talls):
        if w.conf >= READ_CONF and len(_core(w.text) or w.text) > STRAY_CHARS:
            coloured, large = printed.get(w.line, (False, False))
            printed[w.line] = (coloured or (pen is not None and pen >= PEN_UNREAD),
                               large or tall >= SCRIPT_HEIGHT)
    words: list[Word] = []
    for w, pen, tall in zip(raw, pens, talls):
        coloured, large = printed.get(w.line, (False, False))
        if w.conf >= READ_CONF:
            grade = Grade.READ
        elif w.conf < UNREAD_CONF or (w.conf < SHAPE_CONF and not _plausible(w.text)):
            grade = Grade.UNREAD
        elif ((pen is not None and pen >= PEN_UNREAD and not coloured and not cast)
              or (tall >= SCRIPT_HEIGHT and not large and not w.agreed)):
            grade = Grade.UNREAD
        else:
            grade = Grade.DOUBT
        words.append(Word(w.box, w.text, w.conf, grade, None if pen is None else round(pen, 3),
                          tall, line=w.line))
    by_line: dict[int, list[int]] = {}
    for i, w in enumerate(words):
        by_line.setdefault(w.line, []).append(i)
    for members in by_line.values():
        unread = sum(words[i].grade is Grade.UNREAD for i in members)
        if unread < STRAY_LINE * len(members):
            continue
        for i in members:
            if words[i].grade is Grade.READ and len(_core(words[i].text) or words[i].text) <= STRAY_CHARS:
                words[i] = replace(words[i], grade=Grade.UNREAD)
    if image is not None:
        _keep_print(words, by_line, image.convert("L"))
    return [replace(w, layer_text="" if w.grade is Grade.UNREAD else w.text) for w in words]


def _keep_print(words: list[Word], by_line: dict[int, list[int]], grey: Image.Image) -> None:
    """Rule 5 of grade_words, in place."""
    strokes: dict[int, float | None] = {}

    def stroke(i: int) -> float | None:
        if i not in strokes:
            strokes[i] = stroke_weight(grey, words[i].box)
        return strokes[i]

    def coloured(w: Word) -> bool:
        return (w.pen or 0.0) >= PEN_UNREAD

    candidates = {i for i, w in enumerate(words) if w.grade is Grade.UNREAD
                  and UNREAD_CONF <= w.conf < READ_CONF and _plausible(w.text)}
    if not candidates:
        return
    read = [x for x in (stroke(i) for i, w in enumerate(words) if w.grade is Grade.READ) if x]
    if not read:
        return
    bold = BOLD_STROKE * statistics.median(read)
    for members in by_line.values():
        row = sorted(members, key=lambda i: words[i].box[0])
        for k, i in enumerate(row):
            w = words[i]
            if i not in candidates or (stroke(i) or 0.0) < bold:
                continue
            for j in row[max(0, k - 1):k] + row[k + 1:k + 2]:
                n = words[j]
                if (n.grade is Grade.READ and _letters(n.text) and coloured(n) == coloured(w)
                        and max(n.box[0] - w.box[2], w.box[0] - n.box[2])
                        <= max(_height(w.box), _height(n.box))
                        and (stroke(j) or 0.0) >= bold
                        and 1 / SAME_STROKE <= stroke(i) / stroke(j) <= SAME_STROKE):
                    words[i] = replace(w, grade=Grade.DOUBT)
                    break


def stroke_weight(grey: Image.Image, box: Sequence[float]) -> float | None:
    """The stroke weight of the ink in `box` (pixels of the grey image `grey`): the median weight
    of its horizontal runs of ink pixels, a run weighing its summed ink, so a sub-pixel stroke
    width. It separates bold from regular type where the share of dark pixels does not. None
    when the box holds no ink."""
    x0, top, x1, bottom = pixel_box(box, grey.size)
    width = x1 - x0
    if width <= 0 or bottom <= top:
        return None
    data = grey.crop((x0, top, x1, bottom)).tobytes()
    cut = int(255 * (1 - STROKE_INK))   # grey at or below this is ink
    flags = data.translate(bytes(1 if v <= cut else 0 for v in range(256)))
    runs = []
    for y in range(bottom - top):
        row = slice(y * width, (y + 1) * width)
        for run in re.finditer(b"\x01+", flags[row]):
            a, b = row.start + run.start(), row.start + run.end()
            runs.append((b - a) - sum(data[a:b]) / 255)
    return statistics.median(runs) if runs else None


LAYER_SHARE = 0.5     # a layer word and an OCR word are one when they share this much of the smaller
ALIKE_CHARS = 4       # see _alike: shorter words must agree exactly


def compact_nfkc(text: str) -> str:
    """Text compared without whitespace and in NFKC: Tesseract writes a ligature ("ﬂ") that the
    text layer reads back as its letters."""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _same_text(a: str, b: str) -> bool:
    return compact_nfkc(a) == compact_nfkc(b)


def _shared(a: Sequence[float], b: Sequence[float]) -> float:
    """The share of the smaller of two boxes that lies in both."""
    w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return w * h / smaller if w > 0 and h > 0 and smaller > 0 else 0.0


def grade_layer(layer: Sequence[tuple[str, tuple[int, int, int, int]]], read: Sequence[Word],
                image: Image.Image) -> list[Word]:
    """The grades of the words of a page's own OCR layer, which reflow keeps, in layer
    order. `layer` is its words with their boxes in pixels of `image`; `read` is Tesseract's
    reading of that image, which is not printed. The layer holds no confidence. Where Tesseract
    read the same text in its place (one word sharing LAYER_SHARE of the smaller box) with
    confidence (READ_CONF), the word takes Tesseract's. Any other word is READ_CONF - 1,
    doubtful, never unread by confidence alone: Tesseract is unsure of it, read it otherwise, or
    read nothing there, and its confidence says nothing of a reading it did not make (it reads
    naca's leader-dotted "Length" at 0 and its "Unit" at 16, the budget's "$215,711; and" at 0).
    Agreement alone proves nothing: the layer may be another Tesseract's ("*Key*", handwritten,
    is "AKey" to both). Such a word is `unconfirmed`: a doubtful one is not navigation by its
    grade but by the lexical gate, as the layer was before it had grades (text_quality.ocr_gate).
    But where the one Tesseract word under it reads otherwise with confidence, the two readings
    contradict each other and one is sure, so the word is doubtful in its own right, marked and
    reviewed, never navigation: naca's layer splits its bold head "APPARATUS AND PROCEDURE",
    which Tesseract reads at 92 and 91, as "APPARATU A D PROCEDUR E".
    grade_words then grades them like reflow's own OCR, so pen colour and height decide which
    doubtful words are writing; height not where Tesseract reads the word's letters alike with
    some confidence (SHAPE_CONF, `agreed`, `_alike`): two such readings are print set large (the
    budget's "County of Jefferson", at 78 and 73), and writing they agree on is still told by
    its pen ("AKey" is purple). A tall word kept so is not `unconfirmed` but doubtful: on a grey
    scan nothing tells a handwritten name read alike from print, so it is marked and reviewed.
    Each word takes the line of the Tesseract word it overlaps most;
    a word over none takes the line of the layer word beside it, before it or else after it,
    when the two share a line's height (`_same_line`), so the line's printed words still speak
    for it (a title's first word Tesseract missed), and starts a line of its own otherwise."""
    raw: list[RawWord] = []
    confirmed: list[bool] = []
    contradicted: list[bool] = []
    found: list[int | None] = []
    for text, box in layer:
        hits = [(share, w) for w in read if (share := _shared(box, w.box)) >= LAYER_SHARE]
        best = max(hits, key=lambda h: h[0])[1] if hits else None
        agree = len(hits) == 1 and _same_text(text, best.text)
        found.append(None if best is None else best.line)
        confirmed.append(agree and best.conf >= READ_CONF)
        contradicted.append(len(hits) == 1 and best.conf >= READ_CONF and not agree)
        alike = len(hits) == 1 and best.conf >= SHAPE_CONF and _alike(text, best.text)
        raw.append(RawWord(box, text, best.conf if confirmed[-1] else READ_CONF - 1, 0,
                           agreed=alike))
    lines = max_read = max((w.line for w in read), default=0)
    for i in range(len(raw)):          # rightward: a word over none takes its left neighbour's
        if found[i] is None and i and found[i - 1] is not None \
                and _same_line(raw[i - 1].box, raw[i].box):
            found[i] = found[i - 1]
    for i in reversed(range(len(raw))):   # then its right neighbour's: a line's first word
        if found[i] is None and i + 1 < len(raw) and found[i + 1] is not None \
                and _same_line(raw[i].box, raw[i + 1].box):
            found[i] = found[i + 1]
    for i, line in enumerate(found):    # the rest start lines, a run of them on one line one
        if line is None:
            if i and found[i - 1] > max_read and _same_line(raw[i - 1].box, raw[i].box):
                found[i] = found[i - 1]
            else:
                lines += 1
                found[i] = lines
        raw[i] = replace(raw[i], line=found[i])
    # A word a sure reading contradicts, or a tall one kept by agreement alone, is doubtful in its
    # own right: marked and reviewed.
    return [w if sure or (r.agreed and w.tall >= SCRIPT_HEIGHT)
            else replace(w, contradicted=True) if against else replace(w, unconfirmed=True)
            for w, r, sure, against in zip(grade_words(raw, image), raw, confirmed, contradicted)]


def _alike(a: str, b: str) -> bool:
    """Two readings of one word that agree on its letters and digits, but for one of them when
    both have at least ALIKE_CHARS ("LEVY?" and "LEVY2", "body)}" and "body)B")."""
    # Symbols go before NFKC, which spells some as letters ("Expenses\u2122" is "ExpensesTM").
    x, y = (unicodedata.normalize("NFKC", "".join(c for c in t if c.isalnum())) for t in (a, b))
    if not x or not y:
        return False
    if x == y:
        return True
    if min(len(x), len(y)) < ALIKE_CHARS or abs(len(x) - len(y)) > 1:
        return False
    if len(x) > len(y):
        x, y = y, x
    i = next((k for k in range(len(x)) if x[k] != y[k]), len(x))
    return x[i + (len(x) == len(y)):] == y[i + 1:]


def _same_line(a: Sequence[float], b: Sequence[float]) -> bool:
    """Whether box `b` follows `a` on its line: to its right, overlapping half the shorter height."""
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return b[0] >= a[0] and overlap >= 0.5 * min(a[3] - a[1], b[3] - b[1])


def _masks(image: Image.Image) -> tuple[Image.Image, Image.Image] | None:
    """(dark, chromatic) masks of the image, 255 where set; None when no pixel is chromatic."""
    rgb = image.convert("RGB")
    r, g, b = rgb.split()
    spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b),
                                 ImageChops.darker(ImageChops.darker(r, g), b))
    chroma = spread.point(lambda v: 255 if v >= CHROMA else 0)
    if chroma.getbbox() is None:
        return None
    dark = rgb.convert("L").point(lambda v: 255 if v < DARK else 0)
    return dark, chroma


def pixel_box(box: Sequence[float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """The box in whole pixels, clipped to an image of `size`."""
    import math
    return (max(0, min(size[0], math.floor(box[0]))), max(0, min(size[1], math.floor(box[1]))),
            max(0, min(size[0], math.ceil(box[2]))), max(0, min(size[1], math.ceil(box[3]))))


def pen_shares(image: Image.Image, boxes: Sequence[Sequence[float]]) -> list[float | None]:
    """`pen_share` of each box, reading the image's colour once."""
    masks = _masks(image)
    if masks is None:
        return [None] * len(boxes)
    dark, chroma = masks
    out: list[float | None] = []
    for box in boxes:
        crop = pixel_box(box, dark.size)
        d = dark.crop(crop)
        total = d.histogram()[255]
        if not total:
            out.append(0.0)
            continue
        both = ImageChops.multiply(d, chroma.crop(crop)).histogram()[255]
        out.append(both / total)
    return out


def pen_share(image: Image.Image, box: Sequence[float]) -> float | None:
    """The chromatic share of the dark pixels in `box` (pixels of `image`): blue or purple ink
    against black type. None when the image has no chromatic pixel at all (a grey scan says
    nothing); 0.0 when the box holds no dark pixel."""
    return pen_shares(image, [box])[0]


# -- tracked changes ----------------------------------------------------------------------------
#
# A bill with tracked changes strikes deleted words through the middle and underlines inserted
# ones. The probes read the page image, in shares of a word's height so they hold at any OCR
# resolution; a strike or an underline must stop at the words it marks (a rule through or under
# a table row, a line or a form field runs on past them).

STRIKE_SPAN = 0.55    # a strike runs on, unbroken, under this share of the word's width at least
STRIKE_LONG = 0.8     # ...and this many read heights (a lone "e" or "H" has its own bar)
STRIKE_RATIO = 2.5    # ...in rows this many times as dark as the word's median row
STRIKE_THICK = 0.25   # ...at most this share of the word's height thick
STRIKE_BAND = (0.2, 0.8)   # ...inside this band of the word's height
UNDER_SPAN = 0.9      # an underline under this share of a word underlines all of it
UNDER_BAND = 0.35     # an underline lies under the baseline by at most this share of the height
UNDER_THICK = 0.25    # ...and is at most this thick
UNDER_PART = 0.5      # a line under part of a word is at least this share of a letter long
UNDER_GAP = 0.15      # an underline lies below the baseline by at least this share of the height
REACH = 1.0           # a mark ends within this many word heights of its line's words
LETTERED = 0.3       # ...or ends under letters: this share of the columns at its end hold ink
BRACKET_TALL = 0.8    # a bracket's stroke is dark over this share of the word's height


def ink_mask(image: Image.Image) -> Image.Image:
    """The image's dark pixels (DARK), 255 where set."""
    return image.convert("L").point(lambda v: 255 if v < DARK else 0)


def _cover(ink: Image.Image, box: tuple[int, int, int, int]) -> list[float]:
    """The share of dark pixels in each row of `box`."""
    x0, y0, x1, y1 = box
    return [v / 255 for v in ink.crop(box).resize((1, y1 - y0), Image.BOX).tobytes()]


def _columns(ink: Image.Image, y0: int, y1: int) -> list[bool]:
    """Across the whole image, whether each column is dark in most of the rows [y0, y1)."""
    strip = ink.crop((0, y0, ink.width, max(y1, y0 + 1))).resize((ink.width, 1), Image.BOX)
    return [v >= 128 for v in strip.tobytes()]


def _run(dark: list[bool], at: int, gap: int) -> tuple[int, int] | None:
    """The run of dark columns through column `at`, breaks of at most `gap` columns bridged."""
    if not 0 <= at < len(dark) or not dark[at]:
        return None
    a = b = at
    while True:
        nxt = next((x for x in range(b + 1, min(len(dark), b + gap + 2)) if dark[x]), None)
        if nxt is None:
            break
        b = nxt
    while True:
        prv = next((x for x in range(a - 1, max(-1, a - gap - 2), -1) if dark[x]), None)
        if prv is None:
            break
        a = prv
    return a, b + 1


def _longest(dark: list[bool], x0: int, x1: int, gap: int) -> tuple[int, int] | None:
    """The longest run of dark columns that overlaps [x0, x1)."""
    best, x = None, x0
    while x < x1:
        if dark[x] and (run := _run(dark, x, gap)) is not None:
            if best is None or run[1] - run[0] > best[1] - best[0]:
                best = run
            x = run[1]
        x += 1
    return best


def _confined(a: int, b: int, row: Sequence[tuple[int, int, int, int]], slack: float) -> bool:
    """Whether a mark [a, b) stays within the words of its line, give or take `slack`. Tesseract
    splits a struck passage into odd boxes, so the line's extent is what is measured."""
    return min(box[0] for box in row) - slack <= a and b <= max(box[2] for box in row) + slack


def _ends(ink: Image.Image, a: int, b: int, top: int, h: int,
          row: Sequence[tuple[int, int, int, int]], slack: float) -> bool:
    """Whether each end of a band [a, b) whose top is row `top` lies within the words of its
    line (`row`, give or take `slack`) or under letters: in the half word height inside the
    end, LETTERED of the columns hold ink above it. Each end on its own: Tesseract can lose the
    first letters of a struck line (legislation p24's "additional"), and a struck passage
    can end at a full stop, with nothing above it. A rule through a table row ends in blank
    space or at a thin border."""
    def lettered(x0: int, x1: int) -> bool:
        x0, x1 = max(0, x0), min(ink.width, x1)
        if x1 - x0 < 2:
            return False
        above = ink.crop((x0, max(0, top - round(0.35 * h)), x1, top))
        above = above.resize((x1 - x0, 1), Image.BOX)
        return sum(v > 0 for v in above.tobytes()) >= LETTERED * (x1 - x0)
    half = round(0.5 * h)
    return ((min(box[0] for box in row) - slack <= a or lettered(a, a + half))
            and (b <= max(box[2] for box in row) + slack or lettered(b - half, b)))


def struck(ink: Image.Image, box: tuple[int, int, int, int],
           row: Sequence[tuple[int, int, int, int]] = (), read: float = 0.0) -> bool:
    """Whether a line runs through the word (`_strike`)."""
    return _strike(ink, box, row, read) is not None


def _strike(ink: Image.Image, box: tuple[int, int, int, int],
            row: Sequence[tuple[int, int, int, int]] = (), read: float = 0.0
            ) -> tuple[int, int, int, int] | None:
    """The strike through the word, or None: a band in the middle of its height, at most
    STRIKE_THICK of it thick, at least STRIKE_RATIO times as dark as its median row, unbroken
    under STRIKE_SPAN of its width between any brackets at its ends and STRIKE_LONG read
    heights (`read`, the page's median READ height), with letters above and below it, and
    each end within the words of its line (`row`, their boxes) or under letters (`_ends`). Returns the
    strike's box: its run, which may reach on through the next struck words, and its rows."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < 3 or h < 5:
        return None
    cover = _cover(ink, box)
    median = statistics.median(cover)
    lo, hi = round(STRIKE_BAND[0] * h), round(STRIKE_BAND[1] * h)
    rows = [y for y in range(lo, hi) if cover[y] >= STRIKE_RATIO * median > 0]
    bands: list[list[int]] = []
    for y in rows:
        if bands and y == bands[-1][-1] + 1:
            bands[-1].append(y)
        else:
            bands.append([y])
    for band in bands:
        a, b = band[0], band[-1] + 1
        if b - a > STRIKE_THICK * h or not (median > 0 and max(cover[:a], default=0) >= median
                                            and max(cover[b:], default=0) >= median):
            continue
        dark = _columns(ink, y0 + a, y0 + b)
        run = _longest(dark, x0, x1, 1)
        if run is None:
            continue
        inside = min(run[1], x1) - max(run[0], x0)
        left, right = bracket_ends(ink, box)   # a struck "[or]" is struck between its brackets
        w = (right[0] if right else x1) - (left[2] if left else x0)
        # Tesseract can lose words of a struck passage, so the line's words may not reach
        # the ends of its strike; letters at both ends do.
        if (inside >= STRIKE_SPAN * w and inside >= STRIKE_LONG * read
                and _ends(ink, run[0], run[1], y0 + a, h, [box, *row], REACH * h)):
            return run[0], y0 + a, run[1], y0 + b
    return None


def underline(ink: Image.Image, box: tuple[int, int, int, int],
              row: Sequence[tuple[int, int, int, int]] = (), base: int | None = None,
              read: float = 0.0, chars: int = 1) -> tuple[int, int] | None:
    """The columns [a, b) of the word a line runs under, or None: a thin dark band under its
    baseline (`base`, its line's; else the box bottom, which Tesseract sometimes draws round
    the underline), within UNDER_BAND of the word's height, that stays within the words of its
    line (`row`): a rule under a whole line or a form field runs on. It covers UNDER_SPAN of
    the word, or reaches one end of it and is at least UNDER_PART of a letter (of `chars`)
    long: the comma before an inserted phrase. `read` is the page's median READ height."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < 2 or h < 5:
        return None
    pitch = w / max(1, chars)
    size = max(h, read)   # a short word ("a") is no measure of how far below it a line lies
    # Below the feet of the letters, whose serifs are as long as a letter.
    top = (base + max(2, round(UNDER_GAP * size)) if base is not None and base > y0
           else round(y1 - 0.12 * h))
    bottom = min(ink.height, round((base if base is not None else y1) + UNDER_BAND * size))
    gap = max(1, round(0.1 * size))
    # Shorter than this is a bracket's serif or a descender's foot.
    need = max(0.5 * read, UNDER_PART * pitch)

    def under(y0: int, y1: int) -> tuple[int, int] | None:
        run = _longest(_columns(ink, y0, y1), x0, x1, gap)
        return run if run is not None and min(run[1], x1) - max(run[0], x0) >= need else None

    found = None
    rows = [y for y in range(top, bottom) if under(y, y + 1)]
    bands: list[list[int]] = []
    for y in rows:
        if bands and y == bands[-1][-1] + 1:
            bands[-1].append(y)
        else:
            bands.append([y])
    for band in bands:
        if len(band) > UNDER_THICK * size or (run := under(band[0], band[-1] + 1)) is None:
            continue
        lo, hi = max(run[0], x0), min(run[1], x1)
        # A line under part of the word reaches one of its ends (the comma of "court,").
        if ((hi - lo >= UNDER_SPAN * w or lo <= x0 + gap or hi >= x1 - gap)
                and _confined(run[0], run[1], [box, *row], REACH * h)
                and (found is None or hi - lo > found[1] - found[0])):
            found = (lo, hi)
    return found


def _bracket(cell: Image.Image) -> tuple[int, int] | None:
    """The columns [a, b) of a "[" at the left of `cell` (a word's end, ink 255): a stroke dark
    over BRACKET_TALL of the height, serifs at its top and bottom pointing right, and nothing
    beside it between them."""
    e, h = cell.size
    cols = list(cell.resize((e, 1), Image.BOX).tobytes())
    s0 = next((x for x in range(e) if cols[x] >= 255 * BRACKET_TALL), None)
    if s0 is None:
        return None
    # The stroke of a scanned bracket wobbles: its edges are the columns half dark around it.
    s1 = s0
    while s1 + 1 < e and cols[s1 + 1] >= 255 * BRACKET_TALL / 2:
        s1 += 1
    while s0 and cols[s0 - 1] >= 255 * BRACKET_TALL / 2:
        s0 -= 1
    # The stroke's own top and bottom: a word's box can reach below a bracket (a descender).
    stem = cell.crop((s0, 0, s1 + 1, h)).getbbox()
    if stem is None:
        return None
    top, bottom = stem[1], stem[3]
    t = max(2, round(0.12 * (bottom - top)))

    def serif(y0: int, y1: int) -> int:
        dark = [v > 0 for v in cell.crop((0, y0, e, y1)).resize((e, 1), Image.BOX).tobytes()]
        x = s1
        while x + 1 < e and dark[x + 1]:
            x += 1
        return x
    reach = min(serif(top, top + t), serif(bottom - t, bottom))
    if reach - s1 < 2:
        return None
    mid = (top + round(0.25 * (bottom - top)), top + round(0.75 * (bottom - top)))
    if cell.crop((s1 + 1, mid[0], reach + 1, mid[1])).getbbox() is not None:
        return None
    if s0 and cell.crop((0, mid[0], s0, mid[1])).getbbox() is not None:
        return None
    return s0, reach + 1


def _bare(ink: Image.Image, box: tuple[int, int, int, int],
          bracket: tuple[int, int, int, int], strike: tuple[int, int, int, int]) -> bool:
    """Whether the word in `box` holds no letters left of its measured `bracket`: no ink in the
    upper middle of its height but the `strike` that runs into it ("law]" keeps "law"). The
    bracket's own arms run left along its top and bottom, and the full stop of a struck
    sentence (legislation p24's "legislation.]") sits on the baseline, below its letters."""
    if bracket[0] - 1 <= box[0]:
        return True
    h = box[3] - box[1]
    top = box[1] + round(0.2 * h)
    left = ink.crop((box[0], top, bracket[0] - 1, box[1] + round(0.55 * h)))
    ImageDraw.Draw(left).rectangle((0, strike[1] - top - 1, left.width, strike[3] - top + 1), fill=0)
    return left.getbbox() is None


def bracket_ends(ink: Image.Image, box: tuple[int, int, int, int]
                 ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int, int, int] | None]:
    """The boxes of a "[" at the word's left end and a "]" at its right, each None when the
    pixels hold none: measured, so a misread "(e¥)" or "fe¥|" gives the same answer."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < 2 or h < 5:
        return None, None
    edge = min(w, round(0.6 * h))
    pad = round(0.3 * h)   # Tesseract's box can stop short of a bracket
    a, b = max(0, x0 - pad), min(ink.width, x1 + pad)
    left = _bracket(ink.crop((a, y0, x0 + edge, y1)))
    right = _bracket(ink.crop((x1 - edge, y0, b, y1)).transpose(Image.Transpose.FLIP_LEFT_RIGHT))
    return ((a + left[0], y0, a + left[1], y1) if left else None,
            (b - right[1], y0, b - right[0], y1) if right else None)


def _under_part(word: Word, run: tuple[int, int]) -> tuple[int, int] | None:
    """The characters of a word a line runs under part of: what it reaches from one end, cut
    where the word's letters meet its punctuation (an inserted comma after "court", or "court"
    inserted before a comma). A word with no punctuation at that end has no such cut: None."""
    text, (x0, _, x1, _) = word.text, word.box
    core = _core(text)
    if not core:
        return None
    start = text.index(core)
    end = start + len(core)
    if run[1] >= x1 - 1 and run[0] > x0 and end < len(text):
        return end, len(text)
    if run[0] <= x0 + 1 and run[1] < x1 and end < len(text):
        return 0, end
    if run[0] <= x0 + 1 and run[1] < x1 and start > 0:
        return 0, start
    if run[1] >= x1 - 1 and run[0] > x0 and start > 0:
        return start, len(text)
    return None


def track_changes(words: Sequence[Word], raw: Sequence[RawWord], image: Image.Image
                  ) -> tuple[list[Word], bool]:
    """The words with `struck`, `underlined` and `under` measured, and whether the page is a
    tracked-change page: one struck word with brackets at both ends, or two struck words, and
    one underline, all on this page (no count is pooled across pages, so an excerpt decides as
    the whole document does). On a tracked page a struck word below READ_CONF is never
    printed, whatever its grade: it is UNREAD, and its text layer holds only the brackets
    measured at its ends ("[]", "[", "]" or nothing). So is every word of a strike whose words
    are mostly below READ_CONF, and the "]" a strike ends at, struck with it and kept out of the
    layer. Elsewhere the flags stay and nothing changes."""
    ink = ink_mask(image)
    reads = [_height(w.box) for w in words if w.grade is Grade.READ]
    read = statistics.median(reads) if reads else 0.0
    rows: dict[int, list[tuple[int, int, int, int]]] = {}
    for w in words:
        rows.setdefault(w.line, []).append(w.box)
    out: list[Word] = []
    brackets: list[tuple[bool, bool]] = []
    strikes = [_strike(ink, w.box, rows[w.line], read) for w in words]
    by_line: dict[int, list[tuple[int, int, int, int]]] = {}
    for w, strike in zip(words, strikes):
        if strike is not None:
            by_line.setdefault(w.line, []).append(strike)
    runs: list[tuple[int, int, int, int] | None] = []   # the strike through each word
    for w, r, strike in zip(words, raw, strikes):
        row = rows[w.line]
        # A sliver Tesseract cut from a struck passage ("]", "|") is too short to measure; it is
        # struck when it lies inside the strike of a word of its line.
        hit = strike if strike is not None else next(
            ((a, b, c, d) for a, b, c, d in by_line.get(w.line, ())
             if a - 2 <= w.box[0] and w.box[2] <= c + 2 and w.box[1] <= b and d <= w.box[3]),
            None)
        runs.append(hit)
        hit = hit is not None
        run = (underline(ink, w.box, row, r.base, read, len(w.text))
               if w.grade is not Grade.UNREAD else None)
        whole = run is not None and run[1] - run[0] >= UNDER_SPAN * (w.box[2] - w.box[0])
        under = _under_part(w, run) if run is not None and not whole else None
        ends = bracket_ends(ink, w.box) if hit else (None, None)
        brackets.append((ends[0] is not None, ends[1] is not None))
        out.append(replace(w, struck=hit, underlined=whole, under=under))
    struck_words = [i for i, w in enumerate(out) if w.struck]
    tracked = (any(all(brackets[i]) for i in struck_words) or len(struck_words) >= 2) and any(
        w.underlined or w.under for w in out)
    if tracked:
        # The words one strike runs through (their strikes overlap on their line) are one
        # deletion: when OCR could not read most of them, the few it read (legislation p24's
        # "1,") are no reading of it.
        group: dict[int, int] = {}
        last: tuple[int, int, int] | None = None   # line, group, and how far its strikes run
        for i in sorted(struck_words, key=lambda i: (out[i].line, runs[i][0])):
            if last is not None and last[0] == out[i].line and runs[i][0] <= last[2]:
                group[i] = last[1]
                last = (last[0], last[1], max(last[2], runs[i][2]))
            else:
                group[i] = i
                last = (out[i].line, i, runs[i][2])
        lost: dict[int, list[bool]] = {}
        for i in struck_words:
            lost.setdefault(group[i], []).append(out[i].conf < READ_CONF)
        unread = {g for g, below in lost.items() if sum(below) * 2 > len(below)}
        for i in struck_words:
            if out[i].conf < READ_CONF or group[i] in unread:
                left, right = brackets[i]
                out[i] = replace(out[i], grade=Grade.UNREAD,
                                 layer_text=("[" if left else "") + ("]" if right else ""))
        # The lone "]" that closes a struck passage, where a strike of its line ends, is measured,
        # not read (legislation p24 reads it "R" and "|"): it belongs to the deletion, as the
        # brackets of a struck "[$36]" do, and it closes the passage (`closes`). Alone on its line
        # it keeps no text: the layer would set a lone "]" above its box, where no line binds it.
        for i, w in enumerate(out):
            h = _height(w.box)
            right = None if w.struck else bracket_ends(ink, w.box)[1]
            ends = next((s for s in by_line.get(w.line, ()) if right is not None
                         and w.box[0] - REACH * h <= s[2] <= (w.box[0] + w.box[2]) / 2
                         and s[1] < w.box[3] and s[3] > w.box[1]), None)
            if ends is not None and _bare(ink, w.box, right, ends):
                out[i] = replace(w, struck=True, grade=Grade.UNREAD, layer_text="", closes=True)
    return out, tracked


# -- the sidecar --------------------------------------------------------------------------------

def sidecar_path(working: Path) -> Path:
    """Where the word grades of a working copy live: beside it."""
    return working.with_name(SIDECAR_NAME)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _write_json(path: Path, value) -> None:
    """Write atomically: a temp file beside `path`, then a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_sidecar(working: Path, grades: Sequence[PageGrade]) -> None:
    """Keep `grades` beside the working copy, with the hash of the PDF they belong to."""
    _write_json(sidecar_path(working), {
        "pdf_sha256": file_hash(working), "policy": POLICY_VERSION,
        "pages": [g.to_dict() for g in sorted(grades, key=lambda g: g.page)]})


def read_sidecar(working: Path) -> SidecarRead:
    """The grades kept beside `working`. Never raises on bad input: no file is "absent", one that
    does not parse is "corrupt", and one written for another PDF or another POLICY_VERSION is
    "stale"; in each case there are no pages."""
    path = sidecar_path(working)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return SidecarRead("absent", {})
    except (OSError, ValueError):
        return SidecarRead("corrupt", {})
    try:
        if data["policy"] != POLICY_VERSION or data["pdf_sha256"] != file_hash(working):
            return SidecarRead("stale", {})
        pages = [PageGrade.from_dict(p) for p in data["pages"]]
    except OSError:
        return SidecarRead("absent", {})
    except (KeyError, TypeError, ValueError):
        return SidecarRead("corrupt", {})
    return SidecarRead("ok", {g.page: g for g in pages})


def read_evidence(path: Path) -> PageGrade:
    """One page's evidence file as the plugin wrote it; raises on anything malformed."""
    return PageGrade.from_dict(json.loads(path.read_text(encoding="utf-8")))


# -- the OCRmyPDF plugin ------------------------------------------------------------------------

_PAGE_FILE = re.compile(r"^(\d+)_")


def page_of(output_hocr: str | os.PathLike) -> int:
    """The 1-based page OCRmyPDF is reading, from the name it gives the page's files
    ("000003_ocr_hocr.hocr")."""
    m = _PAGE_FILE.match(Path(output_hocr).name)
    if m is None:
        raise ValueError(f"no page number in {Path(output_hocr).name!r}")
    return int(m.group(1))


def grade_page(input_file: Path, output_hocr: Path, mode: Mode, evidence: Path | None,
               drop: bool) -> PageGrade:
    """Grade the hOCR Tesseract wrote for the page image `input_file`, write the evidence file
    and, with `drop`, rewrite the hOCR without its UNREAD words."""
    hocr = output_hocr.read_text(encoding="utf-8")
    raw = parse_hocr(hocr)
    with Image.open(input_file) as image:
        image.load()
        size = image.size
        words, tracked = track_changes(grade_words(raw, image), raw, image)
    grade = PageGrade(page_of(output_hocr), mode, size, tuple(words), tracked)
    if evidence is not None:
        _write_json(Path(evidence) / f"{grade.page}.json", grade.to_dict())
    if drop:
        output_hocr.write_text(clean_hocr(hocr, words, [w.id for w in raw]), encoding="utf-8")
    return grade


def _option(options, name: str):
    value = getattr(options, name, None)
    if value is None:
        value = (getattr(options, "extra_attrs", None) or {}).get(name)
    return value


def _mode(options) -> Mode:
    return "redo" if getattr(options, "redo_ocr", False) else "force"


def in_page_order(pages: list) -> list:
    """`pages` sorted by their `pageno`: the order OCRmyPDF's fpdf2 renderer must draw them in
    for two runs to write the same bytes. OCRmyPDF collects the pages for the renderer as each
    page's OCR finishes (`task_finished`, in `as_completed` order), so with --jobs above 1 the
    list is in whatever order the workers finished, which load decides. The renderer draws all
    pages with one shared font, and gives each glyph its ID the first time it draws it, so the
    render order sets every page's glyph IDs, its text layer's bytes, and the font's /W and
    /CIDToGIDMap. Each page still lands on its own page of the output (it is grafted by
    `pageno`), so the order changes nothing but the bytes."""
    return sorted(pages, key=lambda page: page.pageno)


def _graft_in_page_order() -> None:
    """Make OCRmyPDF's grafter render the fpdf2 text layers in page order (`in_page_order`),
    whatever order the pages finished in. A no-op on an OCRmyPDF without this renderer."""
    try:
        from ocrmypdf._graft import OcrGrafter
    except ImportError:
        return
    finalize = getattr(OcrGrafter, "finalize", None)
    if finalize is None or getattr(finalize, "_reflow_page_order", False):
        return

    def ordered_finalize(self, *args, **kwargs):
        for name in ("fpdf2_hocr_pages", "fpdf2_parsed_pages"):
            pages = getattr(self, name, None)
            if isinstance(pages, list):
                pages[:] = in_page_order(pages)
        return finalize(self, *args, **kwargs)

    ordered_finalize._reflow_page_order = True
    OcrGrafter.finalize = ordered_finalize


if importlib.util.find_spec("ocrmypdf") is not None:   # loaded by OCRmyPDF, not by the venv
    from ocrmypdf import hookimpl
    from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine

    @hookimpl
    def add_options(parser):
        group = parser.add_argument_group("reflow", "Word grades for reflow")
        group.add_argument("--reflow-evidence", metavar="DIR", default=None,
                           help="write each page's word grades to DIR/<page>.json")
        group.add_argument("--reflow-drop", choices=("off", "on"), default="off",
                           help="leave unreadable words out of the text layer")

    class GradingEngine(TesseractOcrEngine):
        @staticmethod
        def generate_hocr(input_file, output_hocr, output_text, options):
            TesseractOcrEngine.generate_hocr(input_file, output_hocr, output_text, options)
            evidence = _option(options, "reflow_evidence")
            grade_page(Path(input_file), Path(output_hocr), _mode(options),
                       Path(evidence) if evidence else None,
                       _option(options, "reflow_drop") == "on")

    @hookimpl
    def get_ocr_engine(options):
        return GradingEngine()

    _graft_in_page_order()
