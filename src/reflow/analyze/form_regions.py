"""Where a page's text was not read, and what a targeted reread of it should be asked.

Whole-page transcription can drop a form's small field labels, and OCR can miss a band of them
outright: on a scan of the ADA dental claim form, Tesseract reads fields 1 to 23 and the rows
of the services grid, but not one of the labels 24 to 31 in the grid's header. This module
finds such gaps from the page's own evidence and routes each one to a region worth reading
again. It asks no model and changes nothing in the document: analyze.form_repair asks, and the
proposals it makes wait for review.

Eligibility. Only a page whose text came from OCR is looked at: OCR ran on it in this
conversion (`ocr-page`), part of its image went unread (`ocr-band-missing`), or it holds a
kept OCR layer (a node whose source is OCR). Born-digital text is the PDF's own and is never
second-guessed, so a born-digital page is skipped as `native-text` and costs nothing.

Evidence. The page is read as scan_rules reads it (`survey`): its solid rules, the glyph-sized
pieces of ink, and the checkbox squares, comb boxes and answer circles its rules found. The text
the PDF holds is pdfium's text layer, OCR's included. A glyph whose middle no text box covers is
unread ink. Unread glyphs that line up, word by word and line by line, are one gap; a gap with
too few glyphs, or none set in a line, is a speck, a tick or a logo, and is dropped.

A region. A gap that rules cross (two vertical rules or more) is a row of a ruled form: its
region runs from the rule over it to the rule under it, and out to the verticals that close
it, so no cell is cut; its task is FIELDS. Any other gap is TEXT, padded by half a line. Either
way the region grows over a line of read text it would cut, so a label is never halved. Each
region records why it was chosen (`reason`), the text already read inside it (`known`), the
field numbers missing from the page's sequence that fall inside it (`expected`), the columns
its rules make, and the controls the rules found inside it.

Bounds. A region taller than MAX_SHARE of the page is not read (`too-large`): a band that
size is a whole form, which a model reads worse than its parts (it collapses the form into a
table). At most MAX_PER_PAGE regions per page and `max_regions` per document, the gaps with
the most unread glyphs first; the rest are `budget`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from reflow import geometry
from reflow.analyze import scan_rules
from reflow.model import FACT_OCR_PAGE, BBox, Document, PageInfo, Role, Source

if TYPE_CHECKING:
    from PIL.Image import Image

MIN_GLYPH = 1.8        # pt: a shorter piece of ink is a speck or a dot, not a letter
MAX_GLYPH = 14.0       # pt: a taller one is a mark, a logo or a picture's piece
COVER_PAD = 1.0        # pt: a glyph this close to a text box is that text's
WORD_GAP = 1.6         # glyph heights: letters of one line are at most this far apart
LINE_GAP = 1.2         # line heights: lines of one gap are at most this far apart
MIN_GLYPHS = 8         # a gap with fewer unread glyphs is a speck, a tick or a stray mark
MIN_LINE = 4           # glyphs: a gap needs one line at least this long
RULE_REACH = 14.0      # pt: a FIELDS region reaches out to a rule this far above and below
MIN_COLUMNS = 2        # vertical rules crossing a gap that make it a ruled row
PICTURE_PAD = 8.0      # pt: ink this close to a picture is the picture's (a logo's lettering)
MAX_PICTURE = 0.5      # of the page area: a bigger picture is the scan itself, not a picture on it
PAD = 2.0              # pt: around a region, inside the page
MAX_SHARE = 0.35       # of the page height: a taller region is a whole form, not read
MAX_PER_PAGE = 6
DEFAULT_MAX_REGIONS = 24
# A numbered form field's label: "24. Procedure Date", "29a. Diag. Pointer".
FIELD_ID = re.compile(r"(?<![\w.])(\d{1,3})([a-z]?)\.(?=\s|$)")
MIN_SEQUENCE = 5       # numbered fields on a page before their gaps say anything


class Task(StrEnum):
    FIELDS = "fields"   # a ruled row of a form: field numbers, labels and the widgets beside them
    TEXT = "text"       # lines of text OCR did not read


class Origin(StrEnum):
    OCR = "ocr"               # OCR read the page in this conversion
    OCR_LAYER = "ocr-layer"   # the PDF came with an OCR layer, which was kept


@dataclass(frozen=True)
class Control:
    """A place to fill in that the rules found inside a region (scan_rules.Shape)."""
    shape: str
    bbox: BBox
    cells: int = 1


@dataclass(frozen=True)
class Region:
    page: int
    bbox: BBox                      # points, top-left origin
    task: Task
    origin: Origin
    reason: str                     # why it was chosen, in words
    unread: int                     # unread glyphs inside it
    known: tuple[str, ...] = ()     # text already read inside it, top to bottom
    expected: tuple[str, ...] = ()  # field numbers missing from the page's sequence, inside it
    columns: int = 0                # cells its vertical rules make; 0 when none cross it
    inked: int = 0                  # of those cells, the ones that hold ink, read or not
    controls: tuple[Control, ...] = ()

    def crop_box(self, page_size: tuple[float, float]) -> BBox:
        return self.bbox


@dataclass(frozen=True)
class Skip:
    page: int
    reason: str           # native-text, too-large, budget
    bbox: BBox | None = None


@dataclass(frozen=True)
class Routing:
    regions: tuple[Region, ...]
    skipped: tuple[Skip, ...]


@dataclass(frozen=True)
class Text:
    """One run of text the PDF holds (pdfium's text rect), points, top-left origin."""
    bbox: BBox
    text: str


# -- eligibility ------------------------------------------------------------------------------


def origin(info: PageInfo, ocr_nodes: bool) -> Origin | None:
    """Where the page's text came from, or None for a born-digital page's own text."""
    if any(f.code in (FACT_OCR_PAGE, "ocr-band-missing") for f in info.facts):
        return Origin.OCR
    if ocr_nodes:
        return Origin.OCR_LAYER
    return None


# -- geometry ---------------------------------------------------------------------------------


def _covered(glyph: BBox, texts: Sequence[Text]) -> bool:
    cx, cy = (glyph[0] + glyph[2]) / 2, (glyph[1] + glyph[3]) / 2
    return any(t.bbox[0] - COVER_PAD <= cx <= t.bbox[2] + COVER_PAD and
               t.bbox[1] - COVER_PAD <= cy <= t.bbox[3] + COVER_PAD for t in texts)


def _groups(boxes: list[BBox], near) -> list[list[BBox]]:
    """Connected groups of boxes under `near` (union-find)."""
    parent = list(range(len(boxes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    for n, i in enumerate(order):
        for j in order[n + 1:]:
            if near(boxes[i], boxes[j]):
                parent[find(j)] = find(i)
            elif boxes[j][0] - boxes[i][2] > 200:   # sorted by x0: none further can be near
                break
    out: dict[int, list[BBox]] = {}
    for i, b in enumerate(boxes):
        out.setdefault(find(i), []).append(b)
    return list(out.values())


def _same_line(a: BBox, b: BBox) -> bool:
    h = max(a[3] - a[1], b[3] - b[1])
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    gap = max(a[0], b[0]) - min(a[2], b[2])
    return overlap >= 0.4 * min(a[3] - a[1], b[3] - b[1]) and gap <= WORD_GAP * h


def _same_gap(a: BBox, b: BBox) -> bool:
    h = max(a[3] - a[1], b[3] - b[1])
    vgap = max(a[1], b[1]) - min(a[3], b[3])
    hgap = max(a[0], b[0]) - min(a[2], b[2])
    return vgap <= LINE_GAP * h and hgap <= 2 * h


@dataclass(frozen=True)
class _Gap:
    bbox: BBox
    glyphs: int
    line_height: float


def _band(line: BBox, rules: Sequence[BBox]) -> tuple[float | None, float | None]:
    """The nearest horizontal rules over and under the line that reach over its extent: which
    ruled row it is in. Lines of different rows are never one gap, nor joined through ink
    outside the rules."""
    near = [r for r in rules if r[0] <= line[2] and r[2] >= line[0]]
    over = max((_mid(r, 1) for r in near if _mid(r, 1) <= line[1] + 0.5), default=None)
    under = min((_mid(r, 1) for r in near if _mid(r, 1) >= line[3] - 0.5), default=None)
    return over, under


def gaps(glyphs: Sequence[BBox], texts: Sequence[Text], pictures: Sequence[BBox] = (),
         rules: Sequence[BBox] = ()) -> list[_Gap]:
    """The unread ink: glyphs no text and no picture covers, grouped into lines and the lines
    into gaps; lines a horizontal rule parts are never one gap."""
    masks = [Text((p[0] - PICTURE_PAD, p[1] - PICTURE_PAD, p[2] + PICTURE_PAD,
                   p[3] + PICTURE_PAD), "") for p in pictures]
    unread = [g for g in glyphs if MIN_GLYPH <= g[3] - g[1] <= MAX_GLYPH
              and g[2] - g[0] <= MAX_GLYPH and not _covered(g, texts)
              and not _covered(g, masks)]
    rows = {g: _band(g, rules) for g in unread}
    lines = [g for g in _groups(unread, lambda a, b: rows[a] == rows[b] and _same_line(a, b))
             if len(g) >= 2]
    boxes = {geometry.union(line): line for line in lines}
    out = []
    bands = {b: _band(b, rules) for b in boxes}
    for group in _groups(list(boxes), lambda a, b: bands[a] == bands[b] and _same_gap(a, b)):
        count = sum(len(boxes[b]) for b in group)
        if count < MIN_GLYPHS or max(len(boxes[b]) for b in group) < MIN_LINE:
            continue
        heights = sorted(b[3] - b[1] for b in group)
        out.append(_Gap(geometry.union(group), count, heights[len(heights) // 2]))
    return out


def _mid(rule: BBox, axis: int) -> float:
    return (rule[axis] + rule[axis + 2]) / 2


def _cell(gap: _Gap, survey: scan_rules.Survey) -> BBox | None:
    """The ruled row the gap sits in: between the nearest rules over and under it that span
    it, as far as both rules run. None unless a vertical rule closes the gap's cell on each
    side: then the gap is not in a ruled form."""
    x0, y0, x1, y1 = gap.bbox
    spans = [h for h in survey.horizontal if h[0] <= x0 + 2 and h[2] >= x1 - 2]
    over = max((h for h in spans if y0 - RULE_REACH <= _mid(h, 1) <= y0 + 1),
               key=lambda h: _mid(h, 1), default=None)
    under = min((h for h in spans if y1 - 1 <= _mid(h, 1) <= y1 + RULE_REACH),
                key=lambda h: _mid(h, 1), default=None)
    if over is None or under is None:
        return None
    top, bottom = _mid(over, 1), _mid(under, 1)
    walls = [_mid(v, 0) for v in survey.vertical if v[1] <= top + 1.5 and v[3] >= bottom - 1.5]
    left = max((x for x in walls if x <= x0 + 1), default=None)
    right = min((x for x in walls if x >= x1 - 1), default=None)
    if left is None or right is None:
        return None
    # The row runs as far as both its rules do: the cells beside the gap are its context.
    return (max(over[0], under[0]), top, min(over[2], under[2]), bottom)


def _cells(box: BBox, survey: scan_rules.Survey) -> list[BBox]:
    """The cells the vertical rules running through the box's whole height make of it."""
    inner = sorted({round(_mid(v, 0), 1) for v in survey.vertical
                    if v[1] <= box[1] + 1.5 and v[3] >= box[3] - 1.5
                    and box[0] + 1 < _mid(v, 0) < box[2] - 1})
    edges = [box[0], *inner, box[2]]
    return [(a, box[1], b, box[3]) for a, b in zip(edges, edges[1:], strict=False)]


def _inked(cells: Sequence[BBox], glyphs: Sequence[BBox]) -> int:
    """How many of the cells hold a glyph, read or not."""
    letters = [g for g in glyphs if MIN_GLYPH <= g[3] - g[1] <= MAX_GLYPH]
    return sum(any(geometry.centre_in(g, c) for g in letters) for c in cells)


def _whole_lines(box: BBox, texts: Sequence[Text]) -> BBox:
    """Grow the box over each line of read text it would cut."""
    x0, y0, x1, y1 = box
    for t in texts:
        a = t.bbox
        if a[0] < x1 and a[2] > x0 and a[1] < y1 and a[3] > y0:
            x0, y0, x1, y1 = min(x0, a[0]), min(y0, a[1]), max(x1, a[2]), max(y1, a[3])
    return (x0, y0, x1, y1)


def text_lines(texts: Sequence[Text]) -> tuple[str, ...]:
    """The texts as lines of words, top to bottom, each left to right."""
    lines: list[list[Text]] = []
    for t in sorted(texts, key=lambda t: (t.bbox[1], t.bbox[0])):
        for line in lines:
            a = line[0].bbox
            if min(a[3], t.bbox[3]) - max(a[1], t.bbox[1]) >= 0.5 * min(a[3] - a[1], t.bbox[3] - t.bbox[1]):
                line.append(t)
                break
        else:
            lines.append([t])
    return tuple(" ".join(w.text.strip() for w in sorted(line, key=lambda t: t.bbox[0]))
                 for line in lines)


# -- the page's numbered fields ---------------------------------------------------------------


def field_ids(texts: Sequence[Text]) -> list[tuple[str, BBox]]:
    out = []
    for t in texts:
        for m in FIELD_ID.finditer(t.text):
            out.append((m.group(1) + m.group(2), t.bbox))
    return out


def missing_fields(found: Sequence[tuple[str, BBox]]) -> list[tuple[str, BBox]]:
    """The whole numbers missing from the page's run of numbered fields, each with the span
    between the nearest fields read before and after it (where it should be). Sub-fields (29a)
    can't be missed this way: nothing says a field has them."""
    numbers: dict[int, BBox] = {}
    for fid, box in found:
        if fid.isdigit():
            numbers.setdefault(int(fid), box)
    if len(numbers) < MIN_SEQUENCE:
        return []
    keys = sorted(numbers)
    out = []
    for lo, hi in zip(keys, keys[1:], strict=False):
        if hi - lo <= 1 or hi - lo > 20:
            continue
        span = (0.0, min(numbers[lo][1], numbers[hi][1]), 1e9, max(numbers[lo][3], numbers[hi][3]))
        out += [(str(n), span) for n in range(lo + 1, hi)]
    return out


# -- routing ----------------------------------------------------------------------------------


def route_page(number: int, size: tuple[float, float], source: Origin,
               survey: scan_rules.Survey, texts: Sequence[Text], pictures: Sequence[BBox] = ()
               ) -> tuple[list[Region], list[Skip]]:
    """The regions of one page worth reading again, and those it won't read. `pictures` are
    the boxes of the page's figures and artifacts: their ink is not text to read. Pure."""
    width, height = size
    missing = missing_fields(field_ids(texts))
    regions: list[Region] = []
    skips: list[Skip] = []
    rows: dict[tuple[int, int], list[tuple[_Gap, BBox]]] = {}
    plain: list[tuple[BBox, _Gap]] = []
    for gap in gaps(survey.glyphs, texts, pictures, survey.horizontal):
        if (cell := _cell(gap, survey)) is not None:
            rows.setdefault((round(cell[1]), round(cell[3])), []).append((gap, cell))
        else:
            plain.append((gap.bbox, gap))
    found: list[tuple[BBox, Task, int, tuple[int, int], str]] = []
    for row in rows.values():
        box = geometry.union(cell for _, cell in row)
        count = sum(g.glyphs for g, _ in row)
        cells = _cells(box, survey)
        columns = len(cells)
        if columns < MIN_COLUMNS:
            plain += [(g.bbox, g) for g, _ in row]
            continue
        found.append((box, Task.FIELDS, count, (columns, _inked(cells, survey.glyphs)),
                      f"{count} unread glyphs in {len(row)} of the {columns} cells of a ruled "
                      "row, which the text layer does not cover"))
    for bbox, gap in plain:
        pad = gap.line_height / 2
        found.append(((bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad), Task.TEXT,
                      gap.glyphs, (0, 0),
                      f"{gap.glyphs} unread glyphs in lines of text the text layer does not cover"))
    for box, task, count, (columns, inked), why in sorted(found, key=lambda f: -f[2]):
        box = _whole_lines(box, texts)
        box = (max(0.0, box[0] - PAD), max(0.0, box[1] - PAD),
               min(width, box[2] + PAD), min(height, box[3] + PAD))
        if any(geometry.centre_in(box, r.bbox) or geometry.centre_in(r.bbox, box) for r in regions):
            continue
        if box[3] - box[1] > MAX_SHARE * height:
            skips.append(Skip(number, "too-large", box))
            continue
        expected = tuple(fid for fid, span in missing
                         if span[1] - 1 <= (box[1] + box[3]) / 2 <= span[3] + 1)
        if expected:
            why += f"; field numbers {expected[0]}–{expected[-1]} are missing from the page"
        known = text_lines([t for t in texts if geometry.centre_in(t.bbox, box) and t.text.strip()])
        controls = tuple(Control(c.shape.value, c.bbox, c.cells) for c in survey.candidates
                         if geometry.centre_in(c.bbox, box))
        regions.append(Region(number, box, task, source, why, count, known, expected,
                              columns, inked, controls))
    for extra in regions[MAX_PER_PAGE:]:
        skips.append(Skip(number, "budget", extra.bbox))
    kept = sorted(regions[:MAX_PER_PAGE], key=lambda r: (r.bbox[1], r.bbox[0]))
    return kept, skips


def page_texts(page) -> list[Text]:
    """pdfium's text rects of a page, top-left origin. Caller holds the pdfium lock."""
    height = page.get_height()
    textpage = page.get_textpage()
    try:
        out = []
        for i in range(textpage.count_rects()):
            left, bottom, right, top = textpage.get_rect(i)
            out.append(Text((left, height - top, right, height - bottom),
                            textpage.get_text_bounded(left, bottom, right, top)))
        return out
    finally:
        textpage.close()


@dataclass(frozen=True)
class PageRead:
    """What routing reads of one page: its size in points, its text, its image at
    scan_rules.SCALE in grayscale."""
    size: tuple[float, float]
    texts: list[Text]
    image: Image


def read_page(pdf: Path, number: int) -> PageRead:
    """Page `number` of `pdf`, read for routing. Caller holds the pdfium lock."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf)
    try:
        page = document[number - 1]
        try:
            return PageRead((page.get_width(), page.get_height()), page_texts(page),
                            page.render(scale=scan_rules.SCALE, grayscale=True).to_pil()
                            .convert("L"))
        finally:
            page.close()
    finally:
        document.close()


def route(doc: Document, read: Callable[[int], PageRead],
          max_regions: int = DEFAULT_MAX_REGIONS) -> Routing:
    """Every region of the document worth reading again, in page order, and what was skipped.
    `read` is asked only for the pages whose text came from OCR."""
    ocr_pages = {n.page for n in doc.root.walk() if n.source == Source.OCR and n.page}
    sizes = {p.number: p.width * p.height for p in doc.pages}
    pictures: dict[int, list[BBox]] = {}
    for n in doc.root.walk():
        # An unread figure is text OCR doubted (figures.figure_node): its ink is what is sought.
        if n.role in (Role.FIGURE, Role.ARTIFACT) and n.page and n.bbox and \
                not n.attrs.get("unread") and \
                (n.bbox[2] - n.bbox[0]) * (n.bbox[3] - n.bbox[1]) < MAX_PICTURE * sizes[n.page]:
            pictures.setdefault(n.page, []).append(n.bbox)
    regions: list[Region] = []
    skipped: list[Skip] = []
    for info in doc.pages:
        source = origin(info, info.number in ocr_pages)
        if source is None:
            skipped.append(Skip(info.number, "native-text"))
            continue
        page = read(info.number)
        found, skips = route_page(info.number, page.size, source,
                                  scan_rules.survey(page.image, [], info.number), page.texts,
                                  pictures.get(info.number, ()))
        skipped += skips
        regions += found
    # The document's budget goes to the gaps with the most unread ink, wherever they are.
    ranked = sorted(regions, key=lambda r: -r.unread)
    skipped += [Skip(r.page, "budget", r.bbox) for r in ranked[max_regions:]]
    regions = sorted(ranked[:max_regions], key=lambda r: (r.page, r.bbox[1], r.bbox[0]))
    return Routing(tuple(regions), tuple(skipped))


__all__ = ["DEFAULT_MAX_REGIONS", "Control", "Origin", "Region", "Routing", "Skip", "Task",
           "PageRead", "Text", "field_ids", "gaps", "missing_fields", "origin", "page_texts",
           "read_page", "route", "route_page"]
