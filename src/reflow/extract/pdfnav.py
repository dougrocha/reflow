"""Where a PDF's navigation points: the page a destination names and how far down it sits, in
the model's displayed top-left points. Link annotations (reflow.extract.links) and bookmarks
(reflow.extract.tags) both resolve their destinations here. pikepdf only; no page is parsed."""

from __future__ import annotations

import pikepdf
from pikepdf import Name

from reflow.model import BBox
from reflow.pdfobjects import displayed

# A page's crop box (left, bottom, right, top) and /Rotate, as displayed() reads them.
type Frame = tuple[float, float, float, float, int]


def inherited(node: pikepdf.Dictionary, key: Name) -> pikepdf.Object | None:
    """The page's own entry or the nearest one up the /Pages tree (CropBox, Rotate, ...)."""
    while node is not None:
        if key in node:
            return node[key]
        node = node.get(Name.Parent)
    return None


def frame_of(page: pikepdf.Dictionary) -> Frame:
    box = inherited(page, Name.CropBox)
    if box is None:
        box = inherited(page, Name.MediaBox)
    left, bottom, right, top = (float(v) for v in box)
    rotate = inherited(page, Name.Rotate)
    return (left, bottom, right, top, int(rotate) if rotate is not None else 0)


def page_numbers(doc: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    """The 1-based number of each page, by the page object's id."""
    return {page.objgen: number for number, page in enumerate(doc.pages, 1)}


def displayed_width(frame: Frame) -> float:
    """The page's width as displayed: a page turned a quarter shows its crop box's height."""
    left, bottom, right, top, rotation = frame
    return top - bottom if rotation % 180 == 90 else right - left


def rect_to_model(frame: Frame, rect: pikepdf.Array) -> BBox:
    """A PDF user-space rectangle as the model's top-left box, turned as the page is shown."""
    left, bottom, right, top_edge, rotation = frame
    x0, y0, x1, y1 = (float(v) for v in rect)
    box = (min(x0, x1) - left, top_edge - max(y0, y1), max(x0, x1) - left, top_edge - min(y0, y1))
    turned = displayed(box, right - left, top_edge - bottom, rotation)
    return tuple(round(v, 2) for v in turned)


def displayed_y(frame: Frame, x: float, y: float) -> float:
    """The displayed height of a user-space point (x, y) on a page with this frame."""
    left, bottom, right, top_edge, rotation = frame
    point = displayed((x - left, top_edge - y, x - left, top_edge - y),
                      right - left, top_edge - bottom, rotation)
    return round(point[1], 2)


def _number(arr: pikepdf.Array, i: int) -> float | None:
    if i >= len(arr):
        return None
    try:
        return float(arr[i])
    except (TypeError, ValueError, pikepdf.PdfError):
        return None


class Destinations:
    """The destinations of one open PDF: named ones (the /Names /Dests name tree, then the old
    /Dests dictionary) and the page each page object is."""

    def __init__(self, doc: pikepdf.Pdf):
        self._doc = doc
        self.page_of = page_numbers(doc)
        names = doc.Root.get("/Names")
        tree = names.get("/Dests") if isinstance(names, pikepdf.Dictionary) else None
        self._tree = pikepdf.NameTree(tree) if tree is not None else None
        legacy = doc.Root.get("/Dests")
        self._legacy = legacy if isinstance(legacy, pikepdf.Dictionary) else None

    def frame(self, page: int) -> Frame:
        return frame_of(self._doc.pages[page - 1].obj)

    def lookup(self, name) -> pikepdf.Object | None:
        key = str(name) if isinstance(name, pikepdf.String) else str(name)[1:]
        if self._tree is not None:
            try:
                return self._tree[key]
            except (KeyError, ValueError, TypeError, pikepdf.PdfError):
                pass
        if self._legacy is not None:
            return self._legacy.get("/" + key)
        return None

    def array(self, dest) -> pikepdf.Array | None:
        """The explicit destination array behind a destination: a name, a string, a dictionary
        with /D, or the array itself."""
        for _ in range(4):
            if isinstance(dest, pikepdf.Array):
                return dest
            if isinstance(dest, pikepdf.Dictionary):
                dest = dest.get("/D")
            elif isinstance(dest, (pikepdf.Name, pikepdf.String)):
                dest = self.lookup(dest)
            else:
                return None
        return None

    def resolve(self, dest) -> tuple[int, float | None] | None:
        """The 1-based page a destination names, and its displayed top when it sets one."""
        arr = self.array(dest)
        if arr is None or len(arr) < 2:
            return None
        number = self.page_of.get(getattr(arr[0], "objgen", None))
        if number is None:
            return None
        frame = self.frame(number)
        style = str(arr[1])
        if style == "/XYZ":
            left, top = _number(arr, 2), _number(arr, 3)
        elif style in ("/FitH", "/FitBH"):
            left, top = None, _number(arr, 2)
        elif style == "/FitR":
            left, top = _number(arr, 2), _number(arr, 5)
        else:
            return number, None
        if top is None:
            return number, None
        return number, displayed_y(frame, left if left is not None else frame[0], top)
