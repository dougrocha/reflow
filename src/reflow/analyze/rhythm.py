"""The line spacing of a scanned page, and the OCR size jitter snapped to the page's size.

A double-spaced scan sets its lines a full font size apart, and OCR gives each line its own
size, off by a point or more from the next. The block rules read both as breaks. Only OCR pages
get a rhythm or a snapped size: a text-layer page keeps every break it has.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from reflow import geometry
from reflow.extract.lines import Line


# A line with this many words is body text: its size and spacing are the page's.
RHYTHM_WORDS = 3
# The page's pitch and size are measured only when this many such lines are on it...
RHYTHM_MIN_LINES = 8
# ...and this share of the samples lies within MODE_TOL of the mode. A title page of a dozen
# lines in as many sizes has neither.
RHYTHM_SHARE = 0.4
MODE_TOL = 0.2
# A baseline distance (× the upper line's size) that can be the leading: shorter is one line
# set over another, longer is a gap no page sets between its lines.
PITCH_MIN, PITCH_MAX = 0.9, 3.5
# An OCR line within this share of the page's modal size, or of its modal box height, is set in
# the page's size.
SNAP_TOL = 0.3


def ocr_page(lines: Sequence[Line]) -> bool:
    """Every non-rotated line with text on the page came from OCR (see layout._prepare)."""
    text = [l for l in lines if not l.rotated and l.text.strip()]
    return bool(text) and all(l.ocr for l in text)


def _wordy(line: Line) -> bool:
    return len(line.text.split()) >= RHYTHM_WORDS


def _mode(samples: Sequence[float]) -> int | None:
    """The modal rounded value of the samples, or None when fewer than RHYTHM_SHARE of them
    lie within MODE_TOL of it."""
    if not samples:
        return None
    mode = Counter(round(v) for v in samples).most_common(1)[0][0]
    near = sum(abs(v - mode) <= MODE_TOL * mode for v in samples)
    return mode if near >= RHYTHM_SHARE * len(samples) else None


def _same_column(a: Line, b: Line) -> bool:
    """The two lines overlap horizontally (blocks.shares_column, which imports this module)."""
    return geometry.horizontal_overlap(a.bbox, b.bbox) > 0


@dataclass(frozen=True)
class Rhythm:
    """The page's leading: the modal baseline-to-baseline distance of the lines of one column."""
    pitch: float

    @classmethod
    def of(cls, ordered: Sequence[Line]) -> Rhythm | None:
        """The rhythm of an OCR page, from its lines in reading order. Only consecutive lines of
        one column, both of RHYTHM_WORDS words, give a sample: a jump to the next column gives
        none. None on a text-layer page, or when the page has too few lines or no clear mode."""
        if not ocr_page(ordered):
            return None
        if sum(_wordy(l) for l in ordered) < RHYTHM_MIN_LINES:
            return None
        samples = [b.bottom - a.bottom for a, b in zip(ordered, ordered[1:])
                   if _wordy(a) and _wordy(b) and _same_column(a, b)
                   and PITCH_MIN * a.size <= b.bottom - a.bottom <= PITCH_MAX * a.size]
        pitch = _mode(samples)
        return cls(float(pitch)) if pitch is not None else None

    def is_leading(self, prev: Line, line: Line) -> bool:
        return (_same_column(prev, line)
                and abs((line.bottom - prev.bottom) - self.pitch) <= MODE_TOL * self.pitch)


def line_gap(prev: Line, line: Line, rhythm: Rhythm | None) -> float:
    """The white space between `prev` and the line under it, or 0 when the pair sits at the
    page's leading: on a double-spaced page that space is the spacing, not a break."""
    if rhythm is not None and rhythm.is_leading(prev, line):
        return 0.0
    return line.top - prev.bottom


def snap_ocr_sizes(lines: list[Line]) -> None:
    """Set a page's OCR lines in the page's size: the modal size of its OCR lines of
    RHYTHM_WORDS words, when it has RHYTHM_MIN_LINES of them and a clear mode (see _mode). A
    line takes it when its size or its box height is within SNAP_TOL of the page's mode (OCR
    sizes jitter by points, and an 18.7 pt size can come in an 11.3 pt box). A taller line keeps
    its size, and so does a bold one: an OCR layer that reports the weight reports the type
    ("TECHNICAL COMMITTEES" in bold over smaller names). Text-layer lines are never touched."""
    ocr = [l for l in lines if l.ocr and not l.rotated]
    body = [l for l in ocr if _wordy(l)]
    if len(body) < RHYTHM_MIN_LINES or (size := _mode([l.size for l in body])) is None:
        return
    height = Counter(round(l.height) for l in body).most_common(1)[0][0]
    for line in ocr:
        if not line.bold and (abs(line.size - size) <= SNAP_TOL * size
                              or abs(line.height - height) <= SNAP_TOL * height):
            line.size = float(size)
