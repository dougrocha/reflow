"""Form widgets: the fillable AcroForm fields of each page, as plain picklable records.

Read in a pass of their own (never in the text or drawing extraction), so a document with no
form is extracted exactly as before. pdfium's form environment gives each widget's type,
fully qualified name, tooltip (/TU), value, flags, options and checked state, with the
inheritance from parent fields already applied; /MaxLen is read with pikepdf, because pdfium
only exposes it for the widget's own dictionary and generators put it on the parent.

Coordinates follow the model: top-left origin, relative to the crop box.
"""

from __future__ import annotations

import ctypes
import logging
import warnings
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow.pdfobjects import to_top_left
from reflow.model import BBox

# Field flags (/Ff), ISO 32000 tables 226, 228, 230 and 232.
FF_READONLY = 1 << 0
FF_REQUIRED = 1 << 1
FF_MULTILINE = 1 << 12
FF_PASSWORD = 1 << 13
FF_COMB = 1 << 24

KINDS = {
    pdfium_c.FPDF_FORMFIELD_TEXTFIELD: "text",
    pdfium_c.FPDF_FORMFIELD_COMBOBOX: "combo",
    pdfium_c.FPDF_FORMFIELD_LISTBOX: "list",
    pdfium_c.FPDF_FORMFIELD_CHECKBOX: "checkbox",
    pdfium_c.FPDF_FORMFIELD_RADIOBUTTON: "radio",
}  # push buttons and signature fields have nothing to type: they are left out


class DetectionSource(StrEnum):
    RULES = "rules"  # a candidate the rules were sure of
    ASSIST = "assist"  # one the model accepted
    TEXT = "text"  # a run of underscores
    DRAWN = "drawn"  # a drawn square


@dataclass(frozen=True)
class Detection:
    """How a widget that is not in the PDF came to be: read from the image of a scanned page
    (analyze.scan_fields), from a printed blank of a text layer (analyze.blanks), or from a square
    a born-digital form draws (analyze.drawn_boxes).
    """
    source: DetectionSource
    shape: str                          # analyze.scan_rules.Shape, "underscores" or "square"
    doubts: tuple[str, ...] = ()        # analyze.scan_rules.Doubt values, sorted
    model: str = ""                     # assist only
    confidence: float | None = None     # a decision model's, when it gave one


@dataclass
class Widget:
    page: int
    index: int  # the annotation's position in the page's /Annots: unique on the page
    kind: str  # "text", "combo", "list", "checkbox" or "radio"
    bbox: BBox
    name: str = ""  # fully qualified field name; not unique in practice, radios share it
    tooltip: str = ""  # /TU: the author's accessible name
    value: str = ""
    flags: int = 0
    options: list[str] = field(default_factory=list)  # combo and list entries
    selected: list[int] = field(default_factory=list)  # indexes into options
    checked: bool = False
    export: str = ""  # a checkbox's or radio's "on" value
    maxlen: int | None = None
    detected: Detection | None = None  # set on the widgets scan_fields makes; None for AcroForm ones
    on_blank: bool = False  # a text box that sits on a blank printed in underscores (analyze.blanks)

    @property
    def drawn(self) -> bool:
        """A square a born-digital form draws (analyze.drawn_boxes)."""
        return self.detected is not None and self.detected.source == DetectionSource.DRAWN
    @property
    def multiline(self) -> bool: return bool(self.flags & FF_MULTILINE)
    @property
    def readonly(self) -> bool: return bool(self.flags & FF_READONLY)
    @property
    def required(self) -> bool: return bool(self.flags & FF_REQUIRED)
    @property
    def comb(self) -> bool: return bool(self.flags & FF_COMB)
    @property
    def password(self) -> bool: return bool(self.flags & FF_PASSWORD)


def _text(fn, *args) -> str:
    """A UTF-16LE string from one of pdfium's two-call text getters."""
    n = fn(*args, None, 0)
    if n <= 2:
        return ""
    buf = (ctypes.c_ushort * n)()
    fn(*args, buf, n)
    return bytes(buf).decode("utf-16-le", "replace").rstrip("\0")


def _maxlens(path: Path, first: int, last: int) -> dict[tuple[int, int], int]:
    """(page, annotation index) → /MaxLen, looked up through the parent chain."""
    found: dict[tuple[int, int], int] = {}
    try:
        with pikepdf.open(path) as pdf:
            for number in range(first, min(last, len(pdf.pages)) + 1):
                for i, annot in enumerate(pdf.pages[number - 1].get("/Annots", [])):
                    node, seen = annot, 0
                    while node is not None and seen < 32:
                        if "/MaxLen" in node:
                            found[(number, i)] = int(node["/MaxLen"])
                            break
                        node = node.get("/Parent")
                        seen += 1
    except Exception:  # a damaged form dictionary only loses maxlength
        return {}
    return found


def read_widgets(path: Path, first: int = 1, last: int | None = None) -> dict[int, list[Widget]]:
    """Page number → that page's form widgets, in annotation order, for pages first..last.
    Empty for a document with no AcroForm."""
    doc = pdfium.PdfDocument(path)
    try:
        if doc.get_formtype() == pdfium_c.FORMTYPE_NONE:
            return {}
        # pypdfium2 logs "init_forms() called on XFA pdf" for XFA forms; their AcroForm
        # widgets are read all the same.
        log = logging.getLogger("pypdfium2._helpers.document")
        level, log.level = log.level, logging.ERROR
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                doc.init_forms()  # before any page is loaded
        finally:
            log.level = level
        env = doc.formenv
        stop = len(doc) if last is None else min(last, len(doc))
        out: dict[int, list[Widget]] = {}
        maxlens = None
        for number in range(max(first, 1), stop + 1):
            page = doc[number - 1]
            try:
                widgets = []
                for i in range(pdfium_c.FPDFPage_GetAnnotCount(page.raw)):
                    annot = pdfium_c.FPDFPage_GetAnnot(page.raw, i)
                    try:
                        if pdfium_c.FPDFAnnot_GetSubtype(annot) != pdfium_c.FPDF_ANNOT_WIDGET:
                            continue
                        kind = KINDS.get(pdfium_c.FPDFAnnot_GetFormFieldType(env, annot))
                        if kind is None:
                            continue
                        rect = pdfium_c.FS_RECTF()
                        pdfium_c.FPDFAnnot_GetRect(annot, rect)
                        bbox = to_top_left(page, (rect.left, rect.bottom, rect.right, rect.top))
                        w = Widget(number, i, kind, bbox,
                                   name=_text(pdfium_c.FPDFAnnot_GetFormFieldName, env, annot),
                                   tooltip=" ".join(_text(
                                       pdfium_c.FPDFAnnot_GetFormFieldAlternateName,
                                       env, annot).split()),
                                   flags=pdfium_c.FPDFAnnot_GetFormFieldFlags(env, annot))
                        if kind in ("checkbox", "radio"):
                            w.checked = bool(pdfium_c.FPDFAnnot_IsChecked(env, annot))
                            w.export = _text(pdfium_c.FPDFAnnot_GetFormFieldExportValue, env, annot)
                        else:
                            w.value = _text(pdfium_c.FPDFAnnot_GetFormFieldValue, env, annot)
                        if kind in ("combo", "list"):
                            count = max(pdfium_c.FPDFAnnot_GetOptionCount(env, annot), 0)
                            w.options = [_text(pdfium_c.FPDFAnnot_GetOptionLabel, env, annot, j)
                                         for j in range(count)]
                            w.selected = [j for j in range(count) if
                                          pdfium_c.FPDFAnnot_IsOptionSelected(env, annot, j)]
                        if kind == "text":
                            if maxlens is None:
                                maxlens = _maxlens(path, first, stop)
                            w.maxlen = maxlens.get((number, i))
                        widgets.append(w)
                    finally:
                        pdfium_c.FPDFPage_CloseAnnot(annot)
                if widgets:
                    out[number] = widgets
            finally:
                page.close()
        return out
    finally:
        doc.close()
