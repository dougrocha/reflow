"""One census of a PDF's fonts, read by the audit's SRC-TXT-002 (reflow.checks.source) and by the
text-trust judge (reflow.ingest.text_trust), so the two never disagree about which fonts are
flagged and neither parses the other's messages.

`remapped` is the judge's alone: a simple font whose /ToUnicode reads its letters as other
letters than its own encoding names them. A wrong /ToUnicode that still reads as letters ("Gpsn"
for "Form") is invisible on the page and to every reader of the text layer; the glyph names the
font's encoding gives are a second opinion that costs no OCR."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import BytesIO

import pikepdf

_FONT_FILES = ("/FontFile", "/FontFile2", "/FontFile3")
_SYMBOLIC = 1 << 2
_NONSYMBOLIC = 1 << 5


@dataclass(frozen=True)
class FontCensus:
    """Font base names (as /BaseFont gives them, subset prefix kept) -> the pages using them."""
    not_embedded: dict[str, list[int]] = field(default_factory=dict)
    no_unicode: dict[str, list[int]] = field(default_factory=dict)   # Type3 or symbolic, no /ToUnicode
    remapped: dict[str, list[int]] = field(default_factory=dict)     # /ToUnicode against the encoding

    def no_unicode_on(self, page: int) -> frozenset[str]:
        """The names, without a subset prefix, of the flagged fonts without /ToUnicode on `page`."""
        return frozenset(name.rpartition("+")[2] for name, pages in self.no_unicode.items()
                         if page in pages)

    def remapped_on(self, page: int) -> frozenset[str]:
        """The names, without a subset prefix, of the `remapped` fonts on `page`."""
        return frozenset(name.rpartition("+")[2] for name, pages in self.remapped.items()
                         if page in pages)


def fonts(resources) -> Iterator[tuple[str, pikepdf.Dictionary]]:
    if resources is None:
        return
    found = resources.get("/Font")
    if not isinstance(found, pikepdf.Dictionary):
        return
    for name, font in found.items():
        if isinstance(font, pikepdf.Dictionary):
            yield name, font


def form_fonts(resources, seen: set[tuple[int, int]] | None = None
               ) -> Iterator[tuple[str, pikepdf.Dictionary]]:
    """The fonts of the Form XObjects `resources` draws, and of the forms they draw, each form
    once (a form that draws itself, or one drawn twice, is walked once)."""
    seen = set() if seen is None else seen
    if resources is None:
        return
    xobjects = resources.get("/XObject")
    if not isinstance(xobjects, pikepdf.Dictionary):
        return
    for _, xobject in xobjects.items():
        if not isinstance(xobject, pikepdf.Stream) or xobject.get("/Subtype") != pikepdf.Name.Form:
            continue
        key = xobject.objgen if xobject.is_indirect else (id(xobject), -1)
        if key in seen:
            continue
        seen.add(key)
        inner = xobject.get("/Resources")
        yield from fonts(inner)
        yield from form_fonts(inner, seen)


def _descriptor(font: pikepdf.Dictionary):
    if font.get("/Subtype") == "/Type0":
        kids = font.get("/DescendantFonts")
        if isinstance(kids, pikepdf.Array) and len(kids):
            font = kids[0]
    return font.get("/FontDescriptor")


def font_census(pdf: pikepdf.Pdf) -> FontCensus:
    """The fonts of each page's resources that are not embedded (Type3 aside), and the Type3 or
    symbolic ones without /ToUnicode."""
    not_embedded: dict[str, list[int]] = {}
    no_unicode: dict[str, list[int]] = {}
    remapped: dict[str, list[int]] = {}
    judged: dict[tuple[int, int], bool] = {}
    for number, page in enumerate(pdf.pages, start=1):
        resources = page.obj.get("/Resources")
        # `remapped` reads the fonts the page's forms draw too: pdfium extracts their text with
        # the page's. The audit's two lists keep to the page's own fonts, as they always have.
        for _, font in (*fonts(resources), *form_fonts(resources)):
            key = font.objgen if font.is_indirect else (id(font), number)
            if key not in judged:
                judged[key] = _remapped(font)
            if judged[key]:
                pages = remapped.setdefault(str(font.get("/BaseFont", "/font")).lstrip("/"), [])
                if number not in pages:
                    pages.append(number)
        for _, font in fonts(resources):
            subtype = str(font.get("/Subtype", ""))
            base = str(font.get("/BaseFont", subtype or "font")).lstrip("/")
            descriptor = _descriptor(font)
            flags = int(descriptor.get("/Flags", 0)) if descriptor is not None else 0
            type3 = subtype == "/Type3"
            if not type3 and (descriptor is None
                              or not any(k in descriptor for k in _FONT_FILES)):
                not_embedded.setdefault(base, []).append(number)
            symbolic = bool(flags & _SYMBOLIC) and not flags & _NONSYMBOLIC
            if (type3 or symbolic) and "/ToUnicode" not in font:
                no_unicode.setdefault(base, []).append(number)
    return FontCensus(not_embedded, no_unicode, remapped)


# Letters a /ToUnicode must read otherwise than the encoding, and the share of the letters both
# name. Measured on every font of the page-check documents: a clean font disagrees on at most 17
# letters (arxiv's SFTT1000) and at most 38% of them (Univers-Condensed of az-cobra-application);
# a shifted /ToUnicode (tests/robustness, tounicode-shifted) moves all 52 ASCII letters, 41% to
# 92% of the letters, as its accented letters stay.
REMAPPED_LETTERS = 26
REMAPPED_SHARE = 0.4


def remapped_counts(font: pikepdf.Dictionary) -> tuple[int, int]:
    """(letters both the /ToUnicode and the encoding give, letters they give differently)."""
    try:
        encoded = _encoding(font)
        if not encoded or not isinstance(font.get("/ToUnicode"), pikepdf.Stream):
            return 0, 0
        from pdfminer.cmapdb import CMapParser, FileUnicodeMap
        cmap = FileUnicodeMap()
        CMapParser(cmap, BytesIO(font.ToUnicode.read_bytes())).run()
        told = {int(k): v for k, v in cmap.cid2unichr.items()}
    except Exception:  # noqa: BLE001 - a font this can't read is no evidence
        return 0, 0
    both = other = 0
    for code, text in told.items():
        named = encoded.get(code)
        if not named or len(text) != 1 or len(named) != 1 or not (text.isalpha() and named.isalpha()):
            continue
        both += 1
        if unicodedata.normalize("NFKC", text) != unicodedata.normalize("NFKC", named):
            other += 1
    return both, other


def _remapped(font: pikepdf.Dictionary) -> bool:
    """A simple font (Type1, TrueType) with an encoding it names (or a standard-14 font) whose
    /ToUnicode gives another letter than the encoding's glyph name for REMAPPED_LETTERS codes and
    REMAPPED_SHARE of the codes where both give a letter. Any font this can't read says nothing."""
    both, other = remapped_counts(font)
    return other >= REMAPPED_LETTERS and other >= REMAPPED_SHARE * both


_STANDARD_14 = {"Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic", "Helvetica",
                "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique", "Courier",
                "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"}


def _encoding(font: pikepdf.Dictionary) -> dict[int, str] | None:
    """Code -> the text the glyph name the encoding gives it stands for, for a Type1 or TrueType
    font with an /Encoding it names (a name, or a dictionary with /BaseEncoding or /Differences),
    or a standard-14 text font without one. None for any other font: a symbolic font's built-in
    encoding is in its program, which this does not read."""
    from pdfminer.encodingdb import EncodingDB, name2unicode
    from pdfminer.psparser import LIT
    if font.get("/Subtype") not in (pikepdf.Name.Type1, pikepdf.Name.TrueType, pikepdf.Name.MMType1):
        return None
    enc = font.get("/Encoding")
    base = str(font.get("/BaseFont", "/"))[1:].rpartition("+")[2]
    diff = None
    if isinstance(enc, pikepdf.Name):
        name = str(enc)[1:]
    elif isinstance(enc, pikepdf.Dictionary):
        name = str(enc.get("/BaseEncoding", pikepdf.Name.StandardEncoding))[1:]
        diff = [int(x) if isinstance(x, int) else LIT(str(x)[1:]) for x in enc.get("/Differences", [])]
    elif enc is None and base in _STANDARD_14:
        name = "StandardEncoding"
    else:
        return None
    if name not in ("StandardEncoding", "WinAnsiEncoding", "MacRomanEncoding", "PDFDocEncoding"):
        return None
    out = dict(EncodingDB.get_encoding(name))
    code = 0
    for item in diff or ():   # a glyph name with no Unicode value says nothing about its code
        if isinstance(item, int):
            code = item
            continue
        try:
            out[code] = name2unicode(item.name)
        except Exception:  # noqa: BLE001 - pdfminer raises KeyError or PDFKeyError
            out.pop(code, None)
        code += 1
    return out
