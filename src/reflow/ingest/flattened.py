"""A form whose fields were flattened into its pages: the review fact `form-flattened`.

Flattening (qpdf --flatten-annotations, pdftk's flatten, pdf-lib's form.flatten, a "secure"
export) draws each field's appearance into the page and drops the AcroForm, so reflow has no
field to read and the reader gets a page of captions with nothing to fill in. The drawing alone
can't tell a flattened field from a ruled box or a table cell, but flattening leaves two traces
in the file that only a form leaves:

  - a widget annotation with no AcroForm to hold it: a field whose appearance the flattening
    did not draw (qpdf leaves fw9's empty text fields this way);
  - a form XObject that the page draws and that marks its content /Tx: the marked content in
    which a text field's appearance stream draws its text (ISO 32000-1, 12.7.3.3).

A page with MIN_TRACES of them gets the fact, with their boxes as its region (data
"region_boxes", top-left page points as the model's). One trace alone is not a form: a tool's
stamp, such as the date on the last page of a directory, can be a lone flattened field. A
document whose AcroForm still has fields is skipped whole, so the fact never fires on a page
whose fields reflow reads. The check never writes the file, and a file it can't parse gets no
fact.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pikepdf

from reflow.model import Document, Fact, Source
from reflow.pdfobjects import displayed

FACT = "form-flattened"
MIN_TRACES = 2     # traces a page needs: one alone is a stamp, not a form
TEXT_MARK = b"/Tx BMC"
DEPTH = 32         # the parent chain of an inherited page attribute, at most

log = logging.getLogger(__name__)

type Matrix = tuple[float, float, float, float, float, float]
type Box = tuple[float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _inherited(page: pikepdf.Dictionary, key: str):
    node, seen = page, 0
    while node is not None and seen < DEPTH:
        if key in node:
            return node[key]
        node, seen = node.get("/Parent"), seen + 1
    return None


def _has_fields(pdf: pikepdf.Pdf) -> bool:
    form = pdf.Root.get("/AcroForm")
    fields = form.get("/Fields") if isinstance(form, pikepdf.Dictionary) else None
    return isinstance(fields, pikepdf.Array) and len(fields) > 0


def _numbers(array, n: int) -> tuple[float, ...] | None:
    try:
        values = tuple(float(v) for v in array)
    except (TypeError, ValueError):
        return None
    return values if len(values) == n else None


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    """m then n: the matrix that maps through m first, as `cm` composes with the CTM."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def _map(m: Matrix, box: Box) -> Box:
    a, b, c, d, e, f = m
    xs, ys = [], []
    for x, y in ((box[0], box[1]), (box[0], box[3]), (box[2], box[1]), (box[2], box[3])):
        xs.append(a * x + c * y + e)
        ys.append(b * x + d * y + f)
    return min(xs), min(ys), max(xs), max(ys)


def _text_fields(xobjects, seen: dict[tuple[int, int], bool]) -> dict[str, tuple[Box, Matrix]]:
    """The page's form XObjects that mark content /Tx, by resource name: (BBox, Matrix)."""
    found: dict[str, tuple[Box, Matrix]] = {}
    if not isinstance(xobjects, pikepdf.Dictionary):
        return found
    for name, xobj in xobjects.items():
        if not isinstance(xobj, pikepdf.Stream) or xobj.get("/Subtype") != pikepdf.Name.Form:
            continue
        key = xobj.objgen   # an XObject the pages share is read once
        if key not in seen:
            try:
                seen[key] = TEXT_MARK in xobj.read_bytes()
            except pikepdf.PdfError:
                seen[key] = False
        bbox = _numbers(xobj.get("/BBox", ()), 4)
        if seen[key] and bbox is not None:
            matrix = _numbers(xobj.get("/Matrix", IDENTITY), 6) or IDENTITY
            found[str(name)] = (bbox, matrix)   # type: ignore[assignment]
    return found


def _placed(page: pikepdf.Page, fields: dict[str, tuple[Box, Matrix]]) -> list[Box]:
    """Where the page's own content draws those XObjects, in PDF page space."""
    boxes: list[Box] = []
    ctm, stack = IDENTITY, []
    for ins in pikepdf.parse_content_stream(page, "q Q cm Do"):
        op, operands = str(ins.operator), ins.operands
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else IDENTITY
        elif op == "cm":
            m = _numbers(operands, 6)
            if m is not None:
                ctm = _multiply(m, ctm)   # type: ignore[arg-type]
        elif op == "Do" and operands and str(operands[0]) in fields:
            bbox, matrix = fields[str(operands[0])]
            boxes.append(_map(_multiply(matrix, ctm), bbox))
    return boxes


def _top_left(page: pikepdf.Page, box: Box) -> Box | None:
    """A PDF-space box in the model's frame: top-left origin, relative to the crop box, turned
    as the page is displayed (pdfobjects.to_top_left, without a pdfium page)."""
    crop = _numbers(_inherited(page.obj, "/CropBox") or _inherited(page.obj, "/MediaBox") or (), 4)
    if crop is None:
        return None
    left, bottom = min(crop[0], crop[2]), min(crop[1], crop[3])
    right, top = max(crop[0], crop[2]), max(crop[1], crop[3])
    x0, y0, x1, y1 = box
    rotation = int(_inherited(page.obj, "/Rotate") or 0)
    turned = displayed((x0 - left, top - y1, x1 - left, top - y0), right - left, top - bottom,
                       rotation)
    return tuple(round(v, 2) for v in turned)   # type: ignore[return-value]


def page_traces(page: pikepdf.Page, seen: dict[tuple[int, int], bool]) -> list[Box]:
    """The boxes of the traces of flattened fields on one page of a document with no fields,
    top-left page points: its widget annotations and the /Tx XObjects it draws."""
    raw: list[Box] = []
    for annot in page.obj.get("/Annots", ()):
        if isinstance(annot, pikepdf.Dictionary) and annot.get("/Subtype") == pikepdf.Name.Widget:
            rect = _numbers(annot.get("/Rect", ()), 4)
            if rect is not None:
                raw.append((min(rect[0], rect[2]), min(rect[1], rect[3]),
                            max(rect[0], rect[2]), max(rect[1], rect[3])))
    resources = _inherited(page.obj, "/Resources")
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    fields = _text_fields(xobjects, seen)
    if fields:
        raw += _placed(page, fields)
    return [b for box in raw if (b := _top_left(page, box)) is not None]


def find(path: Path) -> dict[int, list[Box]]:
    """Page number → the boxes of its flattened fields, for each page with MIN_TRACES or more.
    Empty for a document whose AcroForm has fields, and for a file pikepdf can't read."""
    try:
        with pikepdf.open(path) as pdf:
            if _has_fields(pdf):
                return {}
            seen: dict[tuple[int, int], bool] = {}
            found = {}
            for number, page in enumerate(pdf.pages, 1):
                boxes = page_traces(page, seen)
                if len(boxes) >= MIN_TRACES:
                    found[number] = boxes
            return found
    except (pikepdf.PdfError, ValueError, TypeError) as exc:   # a damaged file only loses the fact
        log.info("flattened-form check skipped: %s", exc)
        return {}


def record(doc: Document, found: dict[int, list[Box]]) -> None:
    """A `form-flattened` page fact on each page `find` returned, its boxes as the region."""
    by_number = {p.number: p for p in doc.pages}
    for number, boxes in sorted(found.items()):
        page = by_number.get(number)
        if page is None:
            continue
        page.facts.append(Fact(
            FACT, Source.HEURISTIC,
            f"{len(boxes)} form fields were flattened into the page: they are drawn, and the "
            "PDF has no fillable fields",
            data={"region_boxes": [v for box in boxes for v in box]}))
