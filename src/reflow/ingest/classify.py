"""Per-page classification: born-digital, scanned, mixed, or empty.

Uses pypdfium2 because it is fast enough to run on every page of a 1,000-page document.
The thresholds are deliberately simple; tune them against real fixtures.
"""

from __future__ import annotations

from collections.abc import Iterable

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow.model import BBox, PageInfo, PageKind
from reflow.pdfobjects import to_top_left, walk_objects

# A page with images over at least this fraction of its area and no text is a scan: a logo
# or a photo with words drawn in it (a back cover) is read, not skipped as an empty page.
SCANNED_MIN_COVERAGE = 0.2
# A page that is almost entirely image but also has text is probably a scan with an
# OCR text layer of unknown quality.
OCR_LAYER_MIN_COVERAGE = 0.9


def boxes_coverage(boxes: Iterable[BBox], width: float, height: float) -> float:
    """Fraction of a `width` x `height` page under `boxes` (top-left points; overlaps
    double-count; capped at 1)."""
    area = 0.0
    for x0, top, x1, bottom in boxes:
        w, h = min(x1, width) - max(x0, 0), min(bottom, height) - max(top, 0)
        if w > 0 and h > 0:
            area += w * h
    return min(area / (width * height), 1.0) if width and height else 0.0


def image_coverage(page: pdfium.PdfPage, width: float, height: float) -> float:
    """Fraction of the page covered by image objects."""
    return boxes_coverage((to_top_left(page, box)
                           for _, box in walk_objects(page, {pdfium_c.FPDF_PAGEOBJ_IMAGE})),
                          width, height)


def classify(char_count: int, coverage: float) -> PageKind:
    if char_count == 0:
        return PageKind.SCANNED if coverage >= SCANNED_MIN_COVERAGE else PageKind.EMPTY
    if coverage >= OCR_LAYER_MIN_COVERAGE:
        return PageKind.MIXED
    return PageKind.BORN_DIGITAL


def page_info(page: pdfium.PdfPage, number: int) -> PageInfo:
    width, height = page.get_size()
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_range()
    finally:
        textpage.close()
    chars = sum(not c.isspace() for c in text)
    coverage = image_coverage(page, width, height)
    return PageInfo(
        number=number,
        width=round(width, 2),
        height=round(height, 2),
        rotation=page.get_rotation(),
        kind=classify(chars, coverage),
        char_count=chars,
        image_coverage=round(coverage, 3),
    )
