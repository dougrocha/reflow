"""Put each page of the working copy in the frame its text is read in, so every reader agrees.

Two page settings change only how a page sits, yet the readers take them differently:

- A MediaBox off the origin. pdfium reads a page relative to its crop box, and pdfplumber relative
  to the media box's top but the user space's left, so the same word has two positions and the
  layout, tables and tag text that join the two readers drift apart. `to_origin` moves the boxes
  to the origin and the content, the annotations and the destinations with them: the page renders
  the same, in the coordinates every reader shares.
- A /Rotate that turns upright text sideways or upside down (a page saved turned). reflow reads a
  page as it is displayed, so every line is rotated text, and the page loses its reading order,
  its tables and its links. `turned_upright` finds the pages whose text is upright before the
  turn, and their /Rotate is cleared. A page that /Rotate turns upright (a landscape table drawn
  sideways on a portrait sheet) keeps it.

Both run in `normalize`, before any reader opens the working copy, so its pages, its picture and
everything read from it share one frame. The original is published as it was.
"""

from __future__ import annotations

import ctypes
import io
import math
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pikepdf import Array, Name

BOXES = ("/MediaBox", "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox")
# A char is upright when its baseline is within this angle of horizontal (radians, about 6°).
UPRIGHT = 0.1
# A turned page is read unturned when at least this share of its chars, and MIN_CHARS of them,
# stand upright before the turn.
UPRIGHT_SHARE = 0.5
MIN_CHARS = 20


def _inherited(node: pikepdf.Dictionary, key: str) -> pikepdf.Object | None:
    while node is not None:
        if key in node:
            return node[key]
        node = node.get(Name.Parent)
    return None


def _rotation(page: pikepdf.Page) -> int:
    value = _inherited(page.obj, "/Rotate")
    try:
        return int(value) % 360 if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _upright_share(page: pdfium.PdfPage) -> tuple[int, int]:
    """(chars upright in user space, chars that are not whitespace) on one page."""
    textpage = page.get_textpage()
    matrix = pdfium_c.FS_MATRIX()
    upright = total = 0
    try:
        for i in range(textpage.count_chars()):
            code = pdfium_c.FPDFText_GetUnicode(textpage.raw, i)
            if code == 0 or chr(code).isspace():
                continue
            if pdfium_c.FPDFText_GetMatrix(textpage.raw, i, ctypes.byref(matrix)):
                angle = math.atan2(matrix.b, matrix.a)
            else:
                angle = pdfium_c.FPDFText_GetCharAngle(textpage.raw, i)
            total += 1
            angle %= 2 * math.pi
            upright += min(angle, 2 * math.pi - angle) <= UPRIGHT
    finally:
        textpage.close()
    return upright, total


def turned_upright(pdf: pikepdf.Pdf, lock: AbstractContextManager | None = None) -> list[int]:
    """The 0-based pages that /Rotate turns although their text stands upright without it.
    pdfium reads them, holding `lock` (pdfium's)."""
    turned = [i for i, page in enumerate(pdf.pages) if _rotation(page)]
    if not turned:
        return []
    buffer = io.BytesIO()
    pdf.save(buffer)
    found = []
    with lock or nullcontext():
        doc = pdfium.PdfDocument(buffer.getvalue())
        try:
            for i in turned:
                page = doc[i]
                try:
                    upright, total = _upright_share(page)
                finally:
                    page.close()
                if upright >= MIN_CHARS and upright >= UPRIGHT_SHARE * total:
                    found.append(i)
        finally:
            doc.close()
    return found


def _dest_array(value) -> pikepdf.Array | None:
    """The explicit destination a /Dest, a /D or a named destination's value holds, if any."""
    if isinstance(value, pikepdf.Dictionary):   # a named destination's {/D [...]}
        value = value.get("/D")
    return value if isinstance(value, pikepdf.Array) else None


def _destinations(pdf: pikepdf.Pdf) -> Iterator[pikepdf.Array]:
    """Every explicit destination array in the file, once: the named ones, the outline's and the
    annotations' (a /Dest, or a GoTo action's /D)."""
    actions: set[tuple[int, int]] = set()   # a GoTo action two links share moves once

    def of(holder) -> pikepdf.Array | None:
        dest = holder.get("/Dest")
        action = holder.get("/A")
        if isinstance(action, pikepdf.Dictionary) and action.get("/S") == Name.GoTo:
            if action.is_indirect:
                if action.objgen in actions:
                    return None
                actions.add(action.objgen)
            dest = action.get("/D")
        return _dest_array(dest)

    found: list[pikepdf.Array | None] = []
    root = pdf.Root
    names = root.get("/Names")
    if isinstance(names, pikepdf.Dictionary) and isinstance(names.get("/Dests"), pikepdf.Dictionary):
        found += [_dest_array(v) for _, v in pikepdf.NameTree(names.Dests).items()]
    old = root.get("/Dests")
    if isinstance(old, pikepdf.Dictionary):
        found += [_dest_array(old[k]) for k in old.keys()]
    outlines = root.get("/Outlines")
    stack = [outlines.get("/First")] if isinstance(outlines, pikepdf.Dictionary) else []
    holders: dict[tuple[int, int], pikepdf.Dictionary] = {}
    while stack:
        item = stack.pop()
        if isinstance(item, pikepdf.Dictionary) and item.objgen not in holders:
            holders[item.objgen] = item
            stack += [item.get("/Next"), item.get("/First")]
    for page in pdf.pages:
        for annot in page.obj.get("/Annots", []):
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            if annot.is_indirect:
                holders.setdefault(annot.objgen, annot)
            else:
                found.append(of(annot))
    found += [of(h) for h in holders.values()]
    seen = set()
    for dest in found:
        if dest is None:
            continue
        if dest.is_indirect:
            if dest.objgen in seen:
                continue
            seen.add(dest.objgen)
        yield dest


def _shift_destination(dest: pikepdf.Array, dx: float, dy: float) -> None:
    """Move an explicit destination's coordinates by (dx, dy); null ones stay null."""
    kind = dest[1] if len(dest) > 1 else None
    axes = {Name.XYZ: "xy", Name.FitH: "y", Name.FitBH: "y", Name.FitV: "x", Name.FitBV: "x",
            Name.FitR: "xyxy"}.get(kind, "")
    for k, axis in enumerate(axes, start=2):
        if k >= len(dest) or dest[k] is None:
            continue   # null: the viewer keeps its current value
        try:
            dest[k] = float(dest[k]) + (dx if axis == "x" else dy)
        except (TypeError, ValueError):
            pass


def to_origin(pdf: pikepdf.Pdf) -> int:
    """Move every page whose media box is off the origin to it, with its other boxes, its
    content, its annotations and the destinations that point at it. Returns the pages moved."""
    shifts: dict[tuple[int, int], tuple[float, float]] = {}
    seen: set[tuple[int, int]] = set()   # an annotation two pages list moves once
    for page in pdf.pages:
        media = _inherited(page.obj, "/MediaBox")
        if media is None:
            continue
        dx, dy = -float(media[0]), -float(media[1])
        if dx == 0 and dy == 0:
            continue
        for key in BOXES:
            box = _inherited(page.obj, key) if key in ("/MediaBox", "/CropBox") else page.obj.get(key)
            if box is None:
                continue
            x0, y0, x1, y1 = (float(v) for v in box)
            page.obj[key] = Array([x0 + dx, y0 + dy, x1 + dx, y1 + dy])
        page.contents_add(pdf.make_stream(f"q 1 0 0 1 {dx:g} {dy:g} cm\n".encode()), prepend=True)
        page.contents_add(pdf.make_stream(b"\nQ\n"))
        for annot in page.obj.get("/Annots", []):
            if not isinstance(annot, pikepdf.Dictionary) or annot.is_indirect and annot.objgen in seen:
                continue
            if annot.is_indirect:
                seen.add(annot.objgen)
            if "/Rect" in annot:
                x0, y0, x1, y1 = (float(v) for v in annot.Rect)
                annot.Rect = Array([x0 + dx, y0 + dy, x1 + dx, y1 + dy])
            if "/QuadPoints" in annot:
                annot.QuadPoints = Array([float(v) + (dx if k % 2 == 0 else dy)
                                          for k, v in enumerate(annot.QuadPoints)])
        shifts[page.obj.objgen] = (dx, dy)
    if shifts:
        for dest in _destinations(pdf):
            target = dest[0] if len(dest) else None
            if isinstance(target, pikepdf.Dictionary) and target.objgen in shifts:
                _shift_destination(dest, *shifts[target.objgen])
    return len(shifts)


def upright_frame(pdf: pikepdf.Pdf, lock: AbstractContextManager | None = None) -> None:
    """Put every page of `pdf` in the frame its text is read in (see the module note). `lock`
    is pdfium's, held while a turned page's text is read."""
    for i in turned_upright(pdf, lock):
        pdf.pages[i].obj.Rotate = 0
    to_origin(pdf)
