"""Walk a page's objects, including those nested in form XObjects, in page coordinates.

pdfium reports the bounds of an object inside a form XObject in the form's own space;
the form object's matrix maps that space onto the page. Ignoring it puts embedded
charts and images in the wrong place (usually the bottom-left corner).
"""

from __future__ import annotations

import ctypes
import math
from collections.abc import Iterator

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

INVISIBLE = 3   # PDF text render mode 3: an OCR text layer
SOFT_BREAK = "\x02"   # pdfium's mark of a word hyphenated over a line end


def private_use(ch: str) -> bool:
    """Whether `ch` is a Private Use Area code point (U+E000-F8FF, or planes 15 and 16): a
    reading no font gives a meaning, which a /ToUnicode of a symbol font or a broken one
    hands out in place of text."""
    o = ord(ch)
    return 0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0x10FFFD

# (x0, bottom, x1, top) in PDF page space: bottom-left origin, not yet cropped.
type RawBox = tuple[float, float, float, float]


def walk_objects(page: pdfium.PdfPage, types: set[int], max_depth: int = 8,
                 ) -> Iterator[tuple[int, RawBox]]:
    """Yield (object type, page-space bounds) for every object of `types`."""
    for obj, box in iter_objects(page, types, max_depth):
        yield obj.type, box


def iter_objects(page: pdfium.PdfPage, types: set[int], max_depth: int = 8,
                 ) -> Iterator[tuple[pdfium.PdfObject, RawBox]]:
    """Yield (object, page-space bounds) for every object of `types`."""
    for obj, box, _ in iter_placed(page, types, max_depth):
        yield obj, box


def iter_placed(page: pdfium.PdfPage, types: set[int], max_depth: int = 8,
                ) -> Iterator[tuple[pdfium.PdfObject, RawBox, pdfium.PdfMatrix]]:
    """Like iter_objects, and also the matrix that maps the object's own coordinates (its path
    points, say) onto the page: its own matrix, then the forms' matrices."""

    def walk(form, matrix: pdfium.PdfMatrix | None, depth: int):
        for obj in page.get_objects(form=form, max_depth=0):
            if obj.type == pdfium_c.FPDF_PAGEOBJ_FORM:
                if depth < max_depth:
                    m = obj.get_matrix()
                    yield from walk(obj, m if matrix is None else m.multiply(matrix),
                                    depth + 1)
                continue
            if obj.type not in types:
                continue
            x0, y0, x1, y1 = obj.get_bounds()
            if matrix is not None:
                xs, ys = [], []
                for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
                    px, py = matrix.on_point(x, y)
                    xs.append(px)
                    ys.append(py)
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            own = obj.get_matrix()
            yield obj, (x0, y0, x1, y1), own if matrix is None else own.multiply(matrix)

    yield from walk(None, None, 0)


def displayed(box: tuple[float, float, float, float], width: float, height: float,
              rotation: int) -> tuple[float, float, float, float]:
    """A box (x0, top, x1, bottom) in the crop box's top-left frame, turned to the page as it is
    displayed. /Rotate turns the page clockwise by `rotation` degrees; `width` and `height` are
    the crop box's own, before the turn. A page with /Rotate 90 (a landscape table on a portrait
    sheet) has its text upright on screen, so its boxes must be read in the turned frame."""
    x0, top, x1, bottom = box
    match rotation % 360:
        case 90:
            return (height - bottom, x0, height - top, x1)
        case 180:
            return (width - x1, height - bottom, width - x0, height - top)
        case 270:
            return (top, width - x1, bottom, width - x0)
        case _:
            return box


def displayed_angle(angle: float, rotation: int) -> float:
    """A baseline angle (radians, counter-clockwise) turned to the displayed page."""
    return (angle - math.radians(rotation)) % (2 * math.pi)


def to_top_left(page: pdfium.PdfPage, box: RawBox) -> tuple[float, float, float, float]:
    """Convert to the model's coordinates: top-left origin, relative to the crop box, as the
    page is displayed (see displayed)."""
    left, bottom, right, top_edge = page.get_cropbox()
    x0, y0, x1, y1 = box
    turned = displayed((x0 - left, top_edge - y1, x1 - left, top_edge - y0),
                       right - left, top_edge - bottom, page.get_rotation())
    return tuple(round(v, 2) for v in turned)


def layer_words(page: pdfium.PdfPage) -> list[tuple[str, tuple[float, float, float, float]]]:
    """The words of the page's invisible text (render mode 3), each with the union of its
    characters' loose boxes in top-left displayed points (layer_word_chars)."""
    return [(text, box) for text, box, _ in layer_word_chars(page)]


def layer_word_chars(page: pdfium.PdfPage
                     ) -> list[tuple[str, tuple[float, float, float, float], tuple[int, ...]]]:
    """`layer_words`, each with the textpage indices of its characters. A word ends at
    whitespace, at a break pdfium inserted, and at the end of a line it is hyphenated over
    (pdfium's "\x02"): each part is a word of its own line, so its box never spans two lines."""
    textpage = page.get_textpage()
    words: list[tuple[str, tuple[float, float, float, float], tuple[int, ...]]] = []
    chars: list[str] = []
    indices: list[int] = []
    box: list[float] | None = None

    def flush() -> None:
        nonlocal box
        if chars and box is not None:
            words.append(("".join(chars), to_top_left(page, tuple(box)), tuple(indices)))
        chars.clear()
        indices.clear()
        box = None

    try:
        for i in range(textpage.count_chars()):
            obj = pdfium_c.FPDFText_GetTextObject(textpage.raw, i)
            ch = chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, i))
            if ch == SOFT_BREAK:
                flush()
                continue
            if not obj or ch.isspace() or pdfium_c.FPDFTextObj_GetTextRenderMode(obj) != INVISIBLE:
                flush()
                continue
            x0, y0, x1, y1 = textpage.get_charbox(i, loose=True)
            box = [x0, y0, x1, y1] if box is None else [min(box[0], x0), min(box[1], y0),
                                                        max(box[2], x1), max(box[3], y1)]
            chars.append(ch)
            indices.append(i)
        flush()
    finally:
        textpage.close()
    return words


def fill_colour(obj) -> tuple[int, int, int, int] | None:
    """The (r, g, b, a) of a raw pdfium page object's fill colour, each 0..255; None when it
    has none of its own."""
    r, g, b, a = (ctypes.c_uint() for _ in range(4))
    if not pdfium_c.FPDFPageObj_GetFillColor(obj, r, g, b, a):
        return None
    return r.value, g.value, b.value, a.value
