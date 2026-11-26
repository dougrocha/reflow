"""A page's content: its lines, drawings, fills, icons and form widgets (extract_pages)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium

from reflow.extract.drawings import Drawing, Fill, page_drawings_and_fills
from reflow.extract.fonts import GlyphNames
from reflow.extract.forms import Widget
from reflow.extract.images import Icon, page_images_and_icons
from reflow.extract.lines import Line
from reflow.extract.ocrwords import PageWords, bind_words, unreadable_layer
from reflow.extract.scan import PageScan, scan_page
from reflow.extract.text import page_lines
from reflow.ingest.ocr_trust import read_sidecar
from reflow.model import BBox


@dataclass
class PageContent:
    number: int
    width: float
    height: float
    lines: list[Line]
    images: list[BBox]  # image objects on the page, top-left coordinates
    drawings: list[Drawing] = field(default_factory=list)
    icons: list[Icon] = field(default_factory=list)  # images ≤ ICON_MAX_SIDE, with keys
    widgets: list[Widget] = field(default_factory=list)  # fillable form fields (extract.forms)
    # The page's small drawn squares (extract.drawings.is_square): the check boxes of a form with
    # no AcroForm (analyze.drawn_boxes turns the ones that have labels into widgets).
    squares: list[BBox] = field(default_factory=list)
    # Its small drawn ellipses (extract.drawings.is_oval): the radio buttons of such a form.
    ovals: list[BBox] = field(default_factory=list)
    # What one look at the page's pixels found (extract.scan): its image objects' kinds, and its
    # pictures, pen ink and rules. Nothing is rendered for a page with little image.
    scan: PageScan = field(default_factory=PageScan)
    fills: list[Fill] = field(default_factory=list)  # coloured fills in paint order (Fill)
    # The page's OCR word grades bound to its text layer (extract.ocrwords); None when reflow did
    # not OCR the page, or its grades are not current.
    words: PageWords | None = None
    # What pdfplumber read from a page the table finder parsed anyway (pagecontent.PlumberPage):
    # engine._extract hands it to the conversion's PageReader and clears it.
    plumber: object | None = field(default=None, repr=False, compare=False)


def extract_pages(path: Path, first: int = 1, last: int | None = None) -> Iterator[PageContent]:
    """Yield each page's lines, image boxes, icons and drawings, in page order.

    `first` and `last` (1-based, inclusive) select a run of pages; every page is extracted on
    its own, with no state carried from one to the next, so a page's content is the same
    whichever run it is extracted in."""
    grades = read_sidecar(path).pages   # the OCR word grades, read once per call
    doc = pdfium.PdfDocument(path)
    glyphs = GlyphNames(path)
    try:
        stop = len(doc) if last is None else min(last, len(doc))
        for i in range(max(first, 1) - 1, stop):
            page = doc[i]
            try:
                width, height = page.get_size()
                images, icons = page_images_and_icons(page)
                grade = grades.get(i + 1)
                # None: a kept layer its grades no longer match, which bind_words calls misaligned
                lines = page_lines(page, i + 1, glyphs, unreadable_layer(grade, page) or ())
                words = bind_words(grade, page, lines) if grade is not None else None
                squares: list[BBox] = []
                ovals: list[BBox] = []
                drawings, fills = page_drawings_and_fills(page, squares=squares, ovals=ovals)
                rulings = [d.bbox for d in drawings if d.only_rules]
                scan = scan_page(page, lines, words, images, rulings)
                for line, stroke in zip(lines, scan.strokes):
                    line.stroke = stroke
                yield PageContent(i + 1, width, height, lines, images, drawings, icons,
                                  scan=scan, fills=fills, words=words, squares=squares, ovals=ovals)
            finally:
                page.close()
    finally:
        glyphs.close()
        doc.close()
