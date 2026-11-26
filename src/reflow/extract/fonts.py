"""Fonts as extraction reads them: the weight and slant a font program draws, the glyph names
that recover ligatures and dropped text, and the fonts a page's resources hold."""

from __future__ import annotations

import ctypes
import hashlib
import math
import re
import struct
from functools import lru_cache
from pathlib import Path

import pikepdf
import pypdfium2.raw as pdfium_c

ITALIC_FLAG = 1 << 6  # PDF font descriptor flag


FAKE_BOLD_MODES = (2, 6)  # text render modes: fill+stroke, fill+stroke+clip


# Glyphs leaning right by this many degrees (after removing rotation) are drawn italic.
FAKE_ITALIC_MIN = 5.0


FAKE_ITALIC_MAX = 35.0


BOLD_NAME = re.compile(r"bold|black|heavy|semibold|demi|-medi|\.b$|,b$", re.I)


ITALIC_NAME = re.compile(r"italic|oblique|-ital|-it$", re.I)


def program_style(data: bytes) -> tuple[int | None, bool]:
    """(weight class, italic) from an embedded TrueType program's OS/2 and head tables.

    Generators often embed a subset of the real Arial Bold or Arial Italic under the same
    name and a regular-looking descriptor, so the font program is the only place the drawn
    weight and slant are recorded. CFF and Type 1 programs carry no such tables: (None, False).
    """
    if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
        return None, False
    try:
        count = struct.unpack_from(">H", data, 4)[0]
        tables = {}
        for k in range(count):
            tag, _, offset, _ = struct.unpack_from(">4sIII", data, 12 + 16 * k)
            tables[tag] = offset
        weight, italic = None, False
        if b"OS/2" in tables:
            os2 = tables[b"OS/2"]
            weight = struct.unpack_from(">H", data, os2 + 4)[0]
            italic = bool(struct.unpack_from(">H", data, os2 + 62)[0] & 1)  # fsSelection
        if b"head" in tables:
            italic = italic or bool(struct.unpack_from(">H", data, tables[b"head"] + 44)[0] & 2)
        return weight, italic
    except struct.error:  # truncated or damaged program: no evidence either way
        return None, False


def drawn_lean(matrix) -> float:
    """How far the glyphs lean right, in degrees, after removing any rotation. The text
    matrix maps the glyph's up direction to (c, d); upright text gives 0 and a sheared
    (fake italic) matrix gives a positive angle, while text set in an italic face keeps an
    upright matrix and is judged by its font program or name instead."""
    n = math.hypot(matrix.a, matrix.b)
    if n == 0:
        return 0.0
    along = (matrix.c * matrix.a + matrix.d * matrix.b) / n
    across = (matrix.a * matrix.d - matrix.b * matrix.c) / n
    return math.degrees(math.atan2(along, across))


class FontCache:
    """Style per font object. FPDFText_GetFontInfo and FPDFText_GetFontWeight are per-char
    FFI calls, and the base name is shared by different font objects ("Arial" can be a
    regular subset and a bold one), so the cache is keyed by the font handle."""

    def __init__(self) -> None:
        self.buf = ctypes.create_string_buffer(256)
        self.flags = ctypes.c_int()
        self.styles: dict[object, tuple[str, bool, bool]] = {}

    def _static_style(self, name: str, font, flags: int) -> tuple[bool, bool]:
        """(bold, italic) from the font itself, not from any one character's drawing."""
        program = _font_program(font)
        weight, italic = program_style(program) if program else (None, False)
        if weight is None:
            weight = pdfium_c.FPDFFont_GetWeight(font) if font else 0
        bold = weight >= 600 or bool(BOLD_NAME.search(name))
        if not italic:
            # The descriptor's italic flag is trusted only with a sane ItalicAngle: generators
            # set flag 64 on upright text with angles like -120, which is not a slant.
            angle = ctypes.c_int()
            if font and pdfium_c.FPDFFont_GetItalicAngle(font, ctypes.byref(angle)):
                italic = bool(flags & ITALIC_FLAG) and -30 <= angle.value <= -3
            else:
                italic = bool(flags & ITALIC_FLAG)  # no font handle: nothing better to go on
        return bold, italic or bool(ITALIC_NAME.search(name))

    def lookup(self, textpage, i: int, matrix, obj=None) -> tuple[str, bool, bool]:
        """(name, bold, italic) for the character at index i, whose text object is `obj`
        (looked up when not given). Everything here belongs to the text object, not to the
        character: `read_chars` calls it once per object."""
        pdfium_c.FPDFText_GetFontInfo(textpage, i, self.buf, len(self.buf),
                                      ctypes.byref(self.flags))
        if obj is None:
            obj = pdfium_c.FPDFText_GetTextObject(textpage, i)
        font = pdfium_c.FPDFTextObj_GetFont(obj) if obj else None
        key = ctypes.cast(font, ctypes.c_void_p).value if font else self.buf.value
        if key not in self.styles:
            name = self.buf.value.decode("utf-8", "replace")
            bold, italic = self._static_style(name, font, self.flags.value)
            self.styles[key] = (name, bold, italic)
        name, bold, italic = self.styles[key]
        if not bold:
            # "Fake bold": a regular font drawn with fill + stroke (text render modes 2
            # and 6), common in generated directories and reports.
            bold = bool(obj) and pdfium_c.FPDFTextObj_GetTextRenderMode(obj) in FAKE_BOLD_MODES
        if not italic and pdfium_c.FPDFText_GetMatrix(textpage, i, ctypes.byref(matrix)):
            # Fake italic: a regular face drawn with a sheared text matrix.
            italic = FAKE_ITALIC_MIN <= drawn_lean(matrix) <= FAKE_ITALIC_MAX
        return name, bold, italic


def _font_program(font) -> bytes | None:
    """The embedded font file, or None when the font is not embedded."""
    if not pdfium_c.FPDFFont_GetIsEmbedded(font):
        return None
    size = ctypes.c_ulong()
    if not pdfium_c.FPDFFont_GetFontData(font, None, 0, ctypes.byref(size)) or not size.value:
        return None
    buf = (ctypes.c_ubyte * size.value)()
    pdfium_c.FPDFFont_GetFontData(font, buf, size.value, ctypes.byref(size))
    return bytes(buf)


# The letters of each Unicode ligature code point (U+FB00-FB06). The long-s-t ligatures
# are written as "st", as the text reads.
LIGATURE_CODEPOINTS = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "ﬅ": "st", "ﬆ": "st",
}


# Glyph names (PostScript and Adobe Glyph List style) of the ligatures a font may draw.
LIGATURE_GLYPHS = {
    "ff": "ff", "f_f": "ff", "fi": "fi", "f_i": "fi", "fl": "fl", "f_l": "fl",
    "ffi": "ffi", "f_f_i": "ffi", "ffl": "ffl", "f_f_l": "ffl", "st": "st", "s_t": "st",
}


def ligature_letters(glyph: str) -> str | None:
    """The letters a ligature glyph draws ("f_i.liga" → "fi"), or None for any other glyph."""
    return LIGATURE_GLYPHS.get(glyph.split(".", 1)[0])


class GlyphNames:
    """Glyph names for characters pdfium cannot map to Unicode, and the widths each font
    declares for its letters.

    pdfium reports such a character with its raw code in the font, and a simple font's
    /Encoding /Differences names the glyph for each code. pdfium drops the subset tag from
    a font's name, so the font dictionary is matched by its embedded font program instead.
    A glyph is named only when every matching dictionary on the page agrees on it.
    The PDF is opened on first use: for such a character, or for a font's widths.

    `widths` reads the same dictionaries, by name: the ligature check (extract.text._wide_f)
    measures a page with too few f's to learn the font's usual "f" from against them.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._pdf: pikepdf.Pdf | None = None
        self._pages: dict[int, tuple[dict[str, list[dict[int, str]]],
                                     dict[str, list[dict[str, float] | None]]]] = {}

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None

    def _page_fonts(self, number: int) -> tuple[dict[str, list[dict[int, str]]],
                                               dict[str, list[dict[str, float] | None]]]:
        """(program hash → each font dictionary's Differences, name key → each dictionary's
        declared widths) for the fonts on one page, including the fonts of the form XObjects
        it draws (a form has its own resources)."""
        if number not in self._pages:
            fonts: dict[str, list[dict[int, str]]] = {}
            widths: dict[str, list[dict[str, float] | None]] = {}
            try:
                if self._pdf is None:
                    self._pdf = pikepdf.open(self._path)
                _collect_fonts(self._pdf.pages[number - 1].obj.get("/Resources", {}), fonts,
                               widths, 0)
            except (pikepdf.PdfError, AttributeError, IndexError, KeyError, TypeError, ValueError):
                fonts, widths = {}, {}  # a PDF pikepdf can't read gives no glyph names
            self._pages[number] = (fonts, widths)
        return self._pages[number]

    def letters(self, number: int, obj, code: int) -> str | None:
        """The ligature letters the text object's glyph at `code` draws, or None."""
        font = pdfium_c.FPDFTextObj_GetFont(obj) if obj else None
        if not font or not 0 <= code <= 255:
            return None
        program = _font_program(font)
        if program:
            key = hashlib.sha1(program).hexdigest()
        else:  # not embedded: the base name is the font
            buf = ctypes.create_string_buffer(256)
            pdfium_c.FPDFFont_GetBaseFontName(font, buf, len(buf))
            key = _name_key(buf.value.decode("latin-1"))
        names = {d.get(code) for d in self._page_fonts(number)[0].get(key, [])}
        glyph = names.pop() if len(names) == 1 else None
        return ligature_letters(glyph) if glyph else None

    def widths(self, number: int, font: str) -> dict[str, float] | None:
        """The advances the page's font named `font` (as pdfium names it) declares for its
        letters and digits, per 1000 em, when every dictionary of that name on the page
        declares the same; None otherwise, and for a font that declares none
        (`_declared_widths`)."""
        found = self._page_fonts(number)[1].get(_name_key(font), [])
        return found[0] if found and found[0] and all(w == found[0] for w in found) else None


@lru_cache(maxsize=None)
def font_family(font: str) -> str:
    """A font's name without its subset prefix, lower-cased, keeping only letters and digits:
    "ABCDEF+ZapfDingbats-ITC" is "zapfdingbatsitc"."""
    return re.sub(r"[^a-z0-9]", "", font.rpartition("+")[2].lower())


def _name_key(base_font: str) -> str:
    """Key for a font with no embedded program: its base name, without any subset tag."""
    return "name:" + base_font.split("+", 1)[-1]


MAX_FORM_DEPTH = 8  # nesting of form XObjects followed when collecting fonts


def _collect_fonts(resources, fonts: dict[str, list[dict[int, str]]],
                   widths: dict[str, list[dict[str, float] | None]], depth: int) -> None:
    """Add the fonts in a resources dictionary, and in the forms it draws, to `fonts` (by
    program hash, or name) and their declared widths to `widths` (by name)."""
    for font in resources.get("/Font", {}).values():
        name = _name_key(str(font.get("/BaseFont", ""))[1:])
        fonts.setdefault(_program_hash(font) or name, []).append(_differences(font.get("/Encoding")))
        widths.setdefault(name, []).append(_declared_widths(font))
    if depth >= MAX_FORM_DEPTH:
        return
    for xobject in resources.get("/XObject", {}).values():
        if xobject.get("/Subtype") == "/Form" and "/Resources" in xobject:
            _collect_fonts(xobject["/Resources"], fonts, widths, depth + 1)


def _declared_widths(font) -> dict[str, float] | None:
    """The advances a simple font (Type1, TrueType) declares for the letters and digits its
    encoding names, per 1000 em, by the character: its /Widths entry at each code
    (reflow.fontcensus._encoding: a named standard encoding, with /Differences, or a
    standard-14 font's own), or the standard 14 fonts' metrics for one without /Widths. None
    for any other font: a Type0 font's /W is by CID, and a symbolic font's built-in encoding is
    in its program."""
    try:
        from pdfminer.fontmetrics import FONT_METRICS

        from reflow.fontcensus import _encoding

        encoded = {c: t for c, t in (_encoding(font) or {}).items()
                   if len(t) == 1 and t.isalnum()}
        declared = font.get("/Widths")
        if isinstance(declared, pikepdf.Array):
            first = int(font.get("/FirstChar", 0))
            out = {t: float(declared[c - first]) for c, t in sorted(encoded.items())
                   if 0 <= c - first < len(declared) and float(declared[c - first]) > 0}
        else:
            base = str(font.get("/BaseFont", "/"))[1:].rpartition("+")[2]
            metrics = FONT_METRICS.get(base, (None, {}))[1]
            out = {t: float(metrics[t]) for t in set(encoded.values()) if t in metrics}
        return out or None
    except Exception:  # noqa: BLE001 - a font this can't read declares nothing
        return None


def _program_hash(font) -> str | None:
    """SHA-1 of a font dictionary's embedded program, as pdfium's _font_program gives it."""
    desc = font.get("/FontDescriptor")
    if desc is None:
        return None
    for key in ("/FontFile", "/FontFile2", "/FontFile3"):
        if key in desc:
            return hashlib.sha1(desc[key].read_bytes()).hexdigest()
    return None


def _differences(encoding) -> dict[int, str]:
    """Code → glyph name from an /Encoding dictionary's /Differences array."""
    names: dict[int, str] = {}
    if not isinstance(encoding, pikepdf.Dictionary) or "/Differences" not in encoding:
        return names
    code = 0
    for item in encoding["/Differences"]:
        if isinstance(item, pikepdf.Name):
            names[code] = str(item)[1:]
            code += 1
        else:
            code = int(item)
    return names
