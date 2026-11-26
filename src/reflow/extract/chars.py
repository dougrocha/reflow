"""Characters from pdfium's text page: each glyph's text, box, font, size, ink and angle.

Uses pypdfium2 (about 6x faster than pdfplumber on samples/p17.pdf)."""

from __future__ import annotations

import ctypes
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import NamedTuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow import geometry
from reflow.extract.fonts import LIGATURE_CODEPOINTS, FontCache, GlyphNames, font_family
from reflow.extract.lines import SOFT_HYPHEN
from reflow.extract.redraws import Redraws
from reflow.model import BBox
from reflow.pdfobjects import displayed, displayed_angle, fill_colour, iter_objects

# The widest stretch, in font sizes, with no glyph ink between two letters that a space pdfium
# placed back inside the word may span and still not be a word gap (see _ink_gap). Ink to ink,
# letters of a word are under 0.2 apart and a word space is 0.5 or more.
PHANTOM_SPACE_INK = 0.25


@dataclass
class Char:
    ch: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float
    bold: bool
    italic: bool
    font: str
    space_before: bool
    unicode_error: bool
    rotated: bool
    run: int | None  # the text object (one show operator) this character belongs to
    angle: float = 0.0  # the baseline's direction in [0, 2π), counter-clockwise from the x axis
    ink: float = 1.0  # the glyphs' darkness on white (see _ink)
    accent: str = ""  # the named accent the fill colour is, or "" (see _accent)
    invisible: bool = False  # render mode 3, usually an OCR text layer
    index: int = -1  # the textpage index of the character pdfium gave
    # pdfium put this letter at the same origin as the letter before it in its run: it spread
    # the run's width evenly over its letters, so the box is a share, not where the glyph is.
    spread: bool = False
    gap_space: bool = False  # space_before inferred from the gap between runs (_run_break_is_space)
    # The gap before this letter holds the ink of a glyph pdfium gave no character (see
    # phantom_space in read_chars): its width says nothing about a word space.
    inked_gap: bool = False


def join_surrogates(high: int, low: int) -> int | None:
    """The code point of a UTF-16 surrogate pair, or None if the two don't form one."""
    if 0xD800 <= high <= 0xDBFF and 0xDC00 <= low <= 0xDFFF:
        return 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
    return None


def same_box(a: BBox, b: BBox) -> bool:
    return all(abs(x - y) < 0.05 for x, y in zip(a, b))


def _even_share(prev: BBox, box: BBox) -> bool:
    """Whether `box` starts where `prev` ends and is as wide: what pdfium gives the letters of
    a run whose glyphs it could not place one by one (see Char.spread)."""
    return abs(box[0] - prev[2]) < 0.01 and abs((box[2] - box[0]) - (prev[2] - prev[0])) < 0.01


class _Run(NamedTuple):
    """What a text object (one show operator) gives all of its characters: font, style, the
    baseline's angle as displayed, the rendered size and the ink."""
    font: str
    bold: bool
    italic: bool
    angle: float
    rotated: bool
    size: float
    ink: float
    invisible: bool
    accent: str = ""


ZAPF = {"3": "\u2713", "4": "\u2714", "7": "\u2717", "8": "\u2718", "l": "\u25cf",
        "n": "\u25a0", "o": "\u274f", "q": "\u2751", "u": "\u25c6"}


# Check boxes and marks drawn from a symbol font's letters: the text layer says "P" or "o", the page shows a
# box. Keyed by font family (the name after the subset prefix, spaces and hyphens removed).
SYMBOL_BOXES = {
    "wingdings2": {"P": "\u2611", "R": "\u2612", "T": "\u2612", "Q": "\u2610", "S": "\u2610"},
    "wingdings": {"o": "\u2610", "q": "\u2610", "x": "\u2612", "\u00fe": "\u2611", "\u00fd": "\u2612",
                  "\u00a8": "\u2610", "\u00fb": "\u2612", "\u00fc": "\u2611"},
    # Dingbats with the letter of their code in the text layer: the checkup checklist's age
    # columns are rows of "l", which the page shows as "●".
    "zapfdingbats": ZAPF,
    "zapfdingbatsitc": ZAPF,
}


def symbol_box(font: str, ch: str) -> str | None:
    """The box or mark a symbol font draws for the letter `ch` in its code, or None."""
    return SYMBOL_BOXES.get(font_family(font), {}).get(ch)


def read_chars(page: pdfium.PdfPage, glyphs: GlyphNames | None = None,
           number: int = 0) -> Iterator[Char | None]:
    """Yield characters in pdfium's order; None marks a line break. `glyphs` names the
    ligature glyphs of characters pdfium cannot map (see GlyphNames).

    Font, style, angle, size and ink are read once per text object, not once per character:
    a page of body text is a few hundred objects and some 5,000 characters, and each read is
    a call into pdfium. Characters pdfium generated itself have no object and are read one by
    one. Boxes go through one FS_RECTF for the whole page."""
    left, bottom, right, top_edge = page.get_cropbox()
    rotation = page.get_rotation()
    width, height = right - left, top_edge - bottom
    fonts = FontCache()
    matrix = pdfium_c.FS_MATRIX()
    rect = pdfium_c.FS_RECTF()
    runs: dict[int, _Run] = {}  # text object address → what its characters share
    page_textpage = page.get_textpage()
    textpage = page_textpage.raw  # the handle: no wrapper property lookup on every call
    get_unicode = pdfium_c.FPDFText_GetUnicode
    get_text_object = pdfium_c.FPDFText_GetTextObject
    has_map_error = pdfium_c.FPDFText_HasUnicodeMapError
    is_hyphen = pdfium_c.FPDFText_IsHyphen
    get_box = pdfium_c.FPDFText_GetLooseCharBox
    get_origin = pdfium_c.FPDFText_GetCharOrigin
    addressof = ctypes.addressof
    ox, oy, px, py = (ctypes.c_double() for _ in range(4))

    upright = rotation % 360 == 0  # displayed() returns the box as it is

    def _same_origin(a: int, b: int) -> bool:
        if not (get_origin(textpage, a, ctypes.byref(px), ctypes.byref(py))
                and get_origin(textpage, b, ctypes.byref(ox), ctypes.byref(oy))):
            return False
        return abs(ox.value - px.value) < 0.01 and abs(oy.value - py.value) < 0.01

    def box_of(i: int) -> BBox:
        if not get_box(textpage, i, rect):
            raise pdfium.PdfiumError("Failed to get charbox.")
        box = (rect.left - left, top_edge - rect.top, rect.right - left, top_edge - rect.bottom)
        return box if upright else displayed(box, width, height, rotation)

    def read_run(i: int, obj) -> _Run:
        font, bold, italic = fonts.lookup(textpage, i, matrix, obj)
        angle = displayed_angle(_char_angle(textpage, i, matrix), rotation)
        return _Run(font, bold, italic, angle, min(angle, 2 * math.pi - angle) > 0.05,
                    font_size(textpage, i, matrix), _ink(obj) if obj else 1.0,
                    bool(obj) and pdfium_c.FPDFTextObj_GetTextRenderMode(obj) == 3,
                    _accent(obj) if obj else "")

    inks: list[tuple[int, BBox]] | None = None  # (address, box) of each drawn text object

    def ink_boxes() -> list[tuple[int, BBox]]:
        nonlocal inks
        if inks is None:
            inks = []
            for obj, (x0, y0, x1, y1) in iter_objects(page, {pdfium_c.FPDF_PAGEOBJ_TEXT}):
                if x1 - x0 > 0 and pdfium_c.FPDFTextObj_GetTextRenderMode(obj.raw) != 3:
                    inks.append((addressof(obj.raw.contents), displayed(
                        (x0 - left, top_edge - y1, x1 - left, top_edge - y0), width, height,
                        rotation)))
        return inks

    def textless(address: int) -> bool:
        """Whether the text object at `address` gave pdfium no characters of its own."""
        obj = ctypes.cast(address, pdfium_c.FPDF_PAGEOBJECT)
        return pdfium_c.FPDFTextObj_GetText(obj, textpage, None, 0) <= 2  # the UTF-16 terminator

    def phantom_space(at: int, box: BBox, run: int | None) -> bool:
        """Whether the space pdfium generated at index `at`, before the letter in `box` (of the
        text object `run`), is not a word gap. A run with an /ActualText replacement ("TREA"
        … "T" read as "treat") hands its text to the first of its objects and spreads it over
        that object's width, so a glyph the run draws later, after a jump of the text position,
        has no character of its own. pdfium then sees a gap after the spread letters and
        generates a space, which it places back at the run's start. When such a textless glyph
        is drawn in the gap and no stretch of the gap without ink is a word space wide, the
        space is not one ("Treat ment")."""
        if (not upright or prev_box is None or not prev_ch.isalpha()
                or get_text_object(textpage, at) or not get_box(textpage, at, rect)):
            return False
        if rect.left - left >= prev_box[2] - 0.5:
            return False  # placed after the letter before it, where a word gap would be
        others = [(a, ink) for a, ink in ink_boxes() if a not in (prev_run, run)]
        gap = _ink_gap(prev_box, box, others)
        if gap is None or gap[0] >= PHANTOM_SPACE_INK * max(prev_size, 1.0):
            return False
        return any(textless(a) for a in gap[1])

    try:
        space = False
        space_at = -1  # the pdfium index of the space that set `space`
        prev_size = 0.0  # the size of the last character written
        count = page_textpage.count_chars()
        pair_low = False  # the previous char was a high surrogate joined with this one
        prev_ch, prev_box = "", None  # the last character written, and its box
        prev_i, prev_run = -1, None  # the pdfium index and run of that character
        lig_box = None  # the box of the ligature glyph the last character was drawn in
        redraws = Redraws()
        for i in range(count):
            if pair_low:
                pair_low = False
                continue
            code = get_unicode(textpage, i)
            # pdfium gives characters outside the BMP as two UTF-16 surrogates, one per
            # index. Join a pair into its real character; drop a lone half, which can't be
            # written as UTF-8.
            if 0xD800 <= code <= 0xDBFF and i + 1 < count:
                joined = join_surrogates(code, get_unicode(textpage, i + 1))
                if joined is not None:
                    code, pair_low = joined, True
            if 0xD800 <= code <= 0xDFFF:
                continue
            ch = chr(code)
            if ch in "\r\n":
                yield None
                space = False
                prev_ch, prev_box, lig_box = "", None, None
                continue
            obj = get_text_object(textpage, i)
            unicode_error = bool(has_map_error(textpage, i))
            # The letters of a ligature: a Unicode ligature code point, or a glyph with no
            # mapping whose font program names a ligature. Checked before the spaces: such a
            # glyph comes out of pdfium as a control character (U+001F reads as a space).
            letters = LIGATURE_CODEPOINTS.get(ch)
            if letters is None and unicode_error and glyphs is not None:
                letters = glyphs.letters(number, obj, code)
            if letters is None and (ch.isspace() or ch == "\x00"):
                # pdfium reports the gap a ligature glyph leaves after its advance as a
                # space drawn over that glyph. Inside a ligature it is not a word gap.
                if lig_box is not None and geometry.centre_strictly_in(box_of(i), lig_box):
                    continue
                space, space_at = True, i
                continue
            if is_hyphen(textpage, i):
                # pdfium reports a line-end hyphen as \x02. Keep it as a soft hyphen so
                # the layout step can rejoin the word ("learn-" + "ing").
                ch = SOFT_HYPHEN
            box = box_of(i)
            # pdfium draws an "fi" pair as two characters in one box (the ligature glyph's
            # advance); the second letter continues the first's glyph.
            continues = (letters is None and prev_ch == "f" and ch in "fil"
                         and prev_box is not None and same_box(box, prev_box))
            run = addressof(obj.contents) if obj else None  # the object's address
            info = runs.get(run) if run is not None else None
            if info is None:
                info = read_run(i, obj)
                if run is not None:
                    runs[run] = info
            if redraws.seen(ch, box, info.size, run):
                space = False  # the gap before a copy is the copy's, not a word gap
                continue
            if letters is None and (box_glyph := symbol_box(info.font, ch)):
                ch = box_glyph
            inked_gap = (space and letters is None and ch.isalpha() and not info.rotated
                         and not info.invisible and phantom_space(space_at, box, run))
            if inked_gap:
                space = False
            spread = (run is not None and run == prev_run and prev_box is not None
                      and ch.isalpha() and prev_ch.isalpha()
                      and _even_share(prev_box, box) and _same_origin(prev_i, i))
            for piece in (letters if letters is not None else ch):
                yield Char(
                    ch=piece, x0=box[0], top=box[1], x1=box[2], bottom=box[3],
                    size=info.size, bold=info.bold, italic=info.italic, font=info.font,
                    space_before=space, unicode_error=unicode_error and letters is None,
                    rotated=info.rotated, run=run, angle=info.angle, ink=info.ink, accent=info.accent,
                    invisible=info.invisible, spread=spread, inked_gap=inked_gap, index=i,
                )
                inked_gap = False
                space = False
                prev_ch = piece
            prev_box, prev_i, prev_run, prev_size = box, i, run, info.size
            lig_box = box if letters is not None or continues else None
    finally:
        page_textpage.close()


def _ink_gap(a: BBox, b: BBox, inks: Sequence[tuple[int, BBox]]
             ) -> tuple[float, list[int]] | None:
    """The widest stretch between the letters in boxes `a` and `b` (left to right) that no glyph
    ink on their line covers, and the text objects drawn between them: of `inks`, (address, box)
    pairs, those whose middle is within the letters' height. None when none is drawn there."""
    top, bottom = min(a[1], b[1]), max(a[3], b[3])
    lo, hi = a[2], b[0]
    covers = sorted((x0, x1, k) for k, (x0, t, x1, btm) in inks
                    if top <= (t + btm) / 2 <= bottom and x1 > lo and x0 < hi)
    between = [k for x0, x1, k in covers if lo < (x0 + x1) / 2 < hi]
    if not between:
        return None
    widest, at = 0.0, lo
    for x0, x1, _ in covers:
        widest = max(widest, x0 - at)
        at = max(at, x1)
    return max(widest, hi - at), between


def _ink(obj) -> float:
    """How dark a text object's glyphs look on a white page: the fill colour's lightness scaled by
    its opacity. Black is 1, white 0, and a transparent watermark (black at 15% opacity) is 0.15.
    A text object with no fill colour of its own is black."""
    colour = fill_colour(obj)
    if colour is None:
        return 1.0
    r, g, b, a = colour
    lightness = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return round(a / 255 * (1 - lightness), 3)


# A fill colour is an accent only when it is clearly a colour: saturated (the gap between its
# brightest and dimmest channel over the brightest) and bright enough not to be a near-black navy,
# and of a hue that reads as red or green. Everything else is no accent: black, greys, pastels,
# the dark blues and browns that set body text, and every blue, which a page prints as its links
# and its brand colour far more often than as stress (the sample documents' blue is all of
# those). The renderer shows an accent as one of
# a few theme-safe colours, never as the PDF's own value (see transcript.css, .ink-*).
ACCENT_SATURATION = 0.6
ACCENT_BRIGHTNESS = 0.5
ACCENT_HUES = (("red", -25.0, 20.0), ("green", 85.0, 165.0))


def accent_of(r: int, g: int, b: int) -> str:
    """The accent ("red" or "green") a fill colour shows as, or "" for none."""
    top, low = max(r, g, b), min(r, g, b)
    if top / 255 < ACCENT_BRIGHTNESS or (top - low) / top < ACCENT_SATURATION:
        return ""
    span = top - low
    if top == r:
        hue = 60 * (((g - b) / span) % 6)
    elif top == g:
        hue = 60 * ((b - r) / span + 2)
    else:
        hue = 60 * ((r - g) / span + 4)
    hue = hue - 360 if hue > 335 else hue
    return next((name for name, a, z in ACCENT_HUES if a <= hue <= z), "")


def _accent(obj) -> str:
    """The accent of a text object's fill colour; "" for an invisible (OCR) text layer."""
    if pdfium_c.FPDFTextObj_GetTextRenderMode(obj) == 3:
        return ""
    colour = fill_colour(obj)
    if colour is None or colour[3] < 255:
        return ""
    return accent_of(*colour[:3])


def _char_angle(textpage, i: int, matrix) -> float:
    """Rotation of a character's baseline in [0, 2π). pdfium's FPDFText_GetCharAngle
    folds shear into the angle, so oblique text drawn with a skewed matrix (fake italic)
    reads as rotated; take the angle of the matrix's x axis instead."""
    if pdfium_c.FPDFText_GetMatrix(textpage, i, ctypes.byref(matrix)):
        return math.atan2(matrix.b, matrix.a) % (2 * math.pi)
    return pdfium_c.FPDFText_GetCharAngle(textpage, i) % (2 * math.pi)


def font_size(textpage, i: int, matrix) -> float:
    """Font size as rendered. pdfium's FPDFText_GetFontSize ignores the text matrix, so
    PDFs that set `1 Tf` and scale with `Tm` (common in generated reports) all read as
    size 1. Scale by the matrix: size × sqrt(|det|)."""
    size = pdfium_c.FPDFText_GetFontSize(textpage, i)
    if pdfium_c.FPDFText_GetMatrix(textpage, i, ctypes.byref(matrix)):
        size *= abs(matrix.a * matrix.d - matrix.b * matrix.c) ** 0.5
    return round(size, 1)
