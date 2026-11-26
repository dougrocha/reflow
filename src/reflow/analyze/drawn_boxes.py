"""Check boxes drawn on a form that has no AcroForm.

A printed form from a word processor or a design tool often draws its check boxes as small
stroked squares and leaves the PDF with no fields: the page has a text layer, and no widget names
a box. Without a widget the planner (analyze.forms) has no control to bind to "M" and "F", so the
choices stay plain words in the text. This pass reads the squares the extraction kept
(`PageContent.squares`, extract.drawings.is_square) and makes a `Widget` of each one that is
a check box, so the planner binds its label, groups the choices under their question, and builds
the controls exactly as it does for a PDF's own form.

A square is a check box when:
- it is empty (no printed text inside it), and no other square of its size touches it: a run of
  touching squares is a comb for single characters, or the cells of a grid, not a choice;
- a line of words sits beside it on its row: right of it (its label), or left of it (a question
  or label that ends at the box, as in "Dental? [ ] Medical? [ ]");
- the page has at least MIN_BOXES such squares: one lone square is a bullet or a mark;
- it lies on the page: a printer's slug in the margin has squares too.

A document with any AcroForm widget is skipped whole: its form is the PDF's own, and the boxes
it draws are those widgets' frames. The widgets made here carry a `Detection` (source "drawn",
shape "square") so the review shows they were found on the page, not read from the PDF.
"""

from __future__ import annotations

from collections.abc import Sequence

from reflow import geometry
from reflow.extract.forms import Detection, DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import BBox

RIGHT_REACH = 28.0   # pt: the label starts at most this far right of the box
LEFT_REACH = 28.0    # pt: or ends at most this far left of it
TOUCH = 1.5          # pt: squares this close, of one size, are a comb or a grid
SAME_SIZE = 1.0      # pt: squares whose sides differ by less are one size
MIN_BOXES = 2        # check boxes a page needs: a lone square is a bullet
ON_ROW = 0.5         # of the box's height: a label must overlap the box's row this much


def _on_page(box: BBox, page: PageContent) -> bool:
    """A printer's slug in the margin, or off the page, has squares too."""
    return box[0] >= 0 and box[1] >= 0 and box[2] <= page.width and box[3] <= page.height


def _same_size(a: BBox, b: BBox) -> bool:
    return (abs((a[2] - a[0]) - (b[2] - b[0])) <= SAME_SIZE
            and abs((a[3] - a[1]) - (b[3] - b[1])) <= SAME_SIZE)


def _touching(a: BBox, b: BBox) -> bool:
    return (a[0] - TOUCH <= b[2] and b[0] - TOUCH <= a[2]
            and a[1] - TOUCH <= b[3] and b[1] - TOUCH <= a[3])


def _distinct(squares: Sequence[BBox]) -> list[BBox]:
    """The squares with the doubles dropped: a box stroked twice, or a fill drawn under its
    outline, is one box."""
    out: list[BBox] = []
    for s in squares:
        if not any(geometry.iou(s, o) > 0.8 for o in out):
            out.append(s)
    return out


def _on_row(line: Line, box: BBox) -> bool:
    shared = min(line.bottom, box[3]) - max(line.top, box[1])
    return shared >= ON_ROW * (box[3] - box[1])


def _labelled(box: BBox, lines: Sequence[Line]) -> bool:
    for line in lines:
        if line.rotated or not any(c.isalpha() for c in line.text) or not _on_row(line, box):
            continue   # a figure beside a square is a chart's mark, not a choice
        if box[2] - 1.0 <= line.x0 <= box[2] + RIGHT_REACH or box[0] - LEFT_REACH <= line.x1 <= box[0] + 1.0:
            return True
        if line.x0 + 1.0 < box[0] and box[2] < line.x1 - 1.0:   # in the line: "No [ ] Reason for ..."
            return True
    return False


def _empty(box: BBox, lines: Sequence[Line]) -> bool:
    """Whether no text line has its middle inside the box."""
    for line in lines:
        cx, cy = (line.x0 + line.x1) / 2, (line.top + line.bottom) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            return False
    return True


def _choices(shapes: Sequence[BBox], page: PageContent, comb: bool = True) -> list[BBox]:
    """The boxes among `shapes` that are choices: see the module's rules. `comb`: a run of
    touching shapes of one size is a comb or a grid (squares); ellipses stacked in a list touch."""
    squares = _distinct([s for s in shapes if _on_page(s, page)])
    lines = [l for l in page.lines if not l.rotated]
    boxes = [s for i, s in enumerate(squares)
             if not (comb and any(j != i and _same_size(s, o) and _touching(s, o)
                                  for j, o in enumerate(squares)))
             and _empty(s, lines) and _labelled(s, lines)]
    if len(boxes) < MIN_BOXES:
        return []
    return sorted(boxes, key=lambda b: (round(b[1]), b[0]))


def check_boxes(page: PageContent) -> list[BBox]:
    """The boxes of the page's check boxes, in reading order (top to bottom, left to right)."""
    return _choices(page.squares, page)


def radio_buttons(page: PageContent) -> list[BBox]:
    """The boxes of the page's radio buttons (drawn ellipses), in reading order."""
    return _choices(page.ovals, page, comb=False)


def inject(pages: Sequence[PageContent]) -> int:
    """Give the pages of a document with no form of its own a widget for each drawn check box.
    Returns how many were made."""
    if any(w.detected is None for p in pages for w in p.widgets):   # the PDF's own form
        return 0
    made = 0
    for page in pages:
        found = [("checkbox", "square", box) for box in check_boxes(page)] \
            + [("radio", "oval", box) for box in radio_buttons(page)]
        found.sort(key=lambda f: (round(f[2][1]), f[2][0]))
        for index, (kind, shape, box) in enumerate(found, 1):
            page.widgets.append(Widget(page=page.number, index=index, kind=kind, bbox=box,
                                       detected=Detection(DetectionSource.DRAWN, shape)))
            made += 1
    return made
