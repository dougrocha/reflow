"""Glyphs drawn twice in place, read once.

Some producers fake bold by drawing a line again at a tiny offset, and some leave a second copy
of a text layer over the first. Every reader then reads each word twice: pdfium drops a copy
only when it follows the object it repeats closely, so on a dense page most copies get through,
and pdfminer drops none. A glyph is a redraw when a glyph of the same character, from another
text object, was drawn before it in (nearly) the same box. Within one text object a repeated box
is a ligature's letters ("ff" drawn as one glyph), not a redraw.

This module imports nothing, so the stage cache keys of the readers that use it stay small.
"""

from __future__ import annotations

from collections import defaultdict

# How far, in font sizes, each edge of a redraw's box may lie from the first glyph's. A fake
# bold shifts the copy by a few hundredths of the size; the narrowest glyphs ("i", ".") sit a
# fifth of the size or more apart when drawn side by side.
REDRAW_SHIFT = 0.1
CELL = 4.0  # points: the side of a bucket, and the widest shift a redraw may have

type Box = tuple[float, float, float, float]


class Redraws:
    """The glyphs of one page seen so far, by character and bucket of the box's corner."""

    def __init__(self) -> None:
        self._seen: defaultdict[tuple[str, int, int], list[tuple[Box, object]]] = defaultdict(list)

    def seen(self, ch: str, box: Box, size: float, run: object = None) -> bool:
        """Whether `ch` in `box` repeats a glyph seen before, drawn by another text object
        (`run`; None is an object of its own). A glyph that is not a redraw is recorded."""
        shift = min(max(REDRAW_SHIFT * size, 0.05), CELL)
        col, row = int(box[0] // CELL), int(box[1] // CELL)
        for c in (col - 1, col, col + 1):
            for r in (row - 1, row, row + 1):
                for other, by in self._seen.get((ch, c, r), ()):
                    if (by is None or by != run) and all(
                            abs(a - b) <= shift for a, b in zip(box, other)):
                        return True
        self._seen[(ch, col, row)].append((box, run))
        return False
