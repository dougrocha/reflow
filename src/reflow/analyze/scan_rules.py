"""The places to fill in on a scanned form page, found by rules from the page image and its OCR
lines: underlines, blank ruled cells, comb digit boxes and checkbox squares.

Pure: a grayscale image and the page's lines in, candidates out. No model, no Run, no pdfium
(except `detect`, the worker entry, which renders the page). PIL only: the
rules stay pure Python although numpy is a dependency. Coordinates are points, top-left origin, like Line.bbox and Widget.bbox.

A candidate is certain exactly when it has no doubts. Doubts are the only route to "asked":
the scan-field assist (analyze.scan_fields) asks the model about candidates with doubts, and
the rules alone keep only the certain ones.

How the page is read: dark pixels are found row by row as runs (`re` over the row's bytes),
and runs stacked in adjacent rows merge into thin rules; the transposed image gives the
vertical ones. Every shape is built from rules, never from OCR glyph boxes (OCR reads a
checkbox as "O" or "]"), and a bold glyph's stem never counts: a box needs a top and a bottom
rule that span both of its sides.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from PIL import ImageChops, ImageFilter
from PIL.Image import Resampling, Transpose

from reflow import geometry
from reflow.analyze.constants import ABOVE_GAP, LEFT_GAP, OPTION_MARKER, RIGHT_GAP
from reflow.model import BBox

if TYPE_CHECKING:
    from PIL.Image import Image

    from reflow.extract.lines import Line

SCALE = 150 / 72   # px per point the page is read at: 150 dpi, a scan's usual resolution

DARK = 160              # gray level below which a pixel is ink
FAINT = 235             # mean gray of a pixel column below which a pale comb divider runs there
MIN_RULE = 4.0          # pt: the shortest run kept as a rule or a box side
MAX_THICK = 3.0         # pt: a thicker run is a fill (a black bar), not a rule
MIN_DASH = 2.5          # pt: the shortest dash of a dashed rule
MAX_DASH = 10.0         # pt: a longer run is a rule of its own, not one dash of a row
DASH_GAP = 13.0         # pt: dashes of one rule are at most this far apart (a scan loses a dash or two)
MIN_DASHES = 4          # a row of fewer dashes is a hyphen or a dash in text, not a rule
DASH_FILL = 0.4         # the share of a dashed rule's length that is ink
MIN_DOT = 1.0           # pt: the narrowest speck of a dotted rule
MAX_DOT = 3.5           # pt: a wider run is not a dot
DOT_GAP = 9.0           # pt: dots of one rule are at most this far apart
MIN_DOTS = 8            # a row of fewer dots is an ellipsis, not a rule
DOT_FILL = 0.1          # the share of a dotted rule's length that is ink
DOT_AREA = 4.0          # pt: the radius of the paper a speck is compared with
FAINT_DELTA = 14        # gray levels under the local mean at which a pixel is a speck of a dotted rule
CIRCLE_D = (4.5, 8.5)   # pt: the outer diameter of an answer circle
SMALL_CIRCLE = 5.4      # pt: a ring this small is drawn with a thin line, or it is a letter
TINY_CIRCLE = 4.9       # pt: a ring this small, in a line of words, is a bullet or a degree sign
DISC = 0.85             # ink share of a circle's middle above which it is a solid dot, not a ring
RING_FAINT = 0.15       # a ring's pixel is ink when this share of the way from the paper to the darkest ink
RING_POINTS = 24        # points of a circle's circumference that are looked at
RING = 0.95             # share of a ring's circumference that is ink
CIRCLE_CLEAR = 0.1      # ink share of the 1.5 pt each side of a circle: a letter in a word has neighbours
CIRCLE_ROOM = 2.0       # pt: both sides of a circle are paper this far (ring edge to the next ink)
CIRCLE_NEAR = 12.0      # pt: in a line of words, a circle has ink this near it on one side: its label or its fellows
WORD_GAP = 1.0          # ring diameters: ink this near it on both sides, in running text: a word, not a circle
BULLET_SIZE = 0.8       # a ring under this share of its label's capital height is a glyph set in the label's type
BULLET_GAP = 0.67       # capital heights: a ring this close to its label is a glyph and a word space from it
TALL_LETTER = 1.7       # times the median letter: a taller piece of ink in a label is letters run together
BULLET_REACH = 1.5      # capital heights: a label further from a ring than this is not the words it is set in
MAX_LETTER = 24.0       # pt: the largest piece of ink read as a letter of a label
LABEL_REACH = 60.0      # pt: the label's letters are looked for this far from a ring
LETTER_MIN = 0.5        # ring diameters: a smaller piece of ink beside a ring is a leader dot or a speck, not a letter
LABEL_LETTERS = 5       # the nearest this many pieces of ink on a side of a ring are read as the label's letters
LEADER_REACH = 160.0    # pt: a label may be this far left of its box when leader dots run between
LEADER_NUMBER = 60.0    # pt: a dotted rule this close to a page number on its row ends in it: a leader
LEADER_END = 10.0       # pt: a dotted rule this close to a box or to text ends in it: a leader
DASH_PAD = 3.0          # pt: a dashed wall runs this far past its end dashes: a corner may fall in a gap
DASH_CLEAR = 0.1        # ink share of the 3 pt beside a dashed rule's dashes: a letter's bar has a stem
TOL = 1.5               # pt: rules meet at a corner within this
SQUARE_SIDE = (6.0, 16.0)  # pt: a checkbox's side; glyph counters stay under it
COMB_WIDTH = (6.0, 30.0)   # pt: a comb box's width
MAX_BOX = 32.0          # pt: the tallest box a square or a comb is made of
BLANK = 0.04            # ink share of an area above which it is not clean
FILLED = 0.15           # ink share above which an area is filled in, not a place to write
SOLID = 0.6             # ink share above which a small box is a solid mark, not a checkbox
CAPTION_DROP = 12.0     # pt: a cell's caption starts at most this far under its top rule
MAX_CELL = 150.0        # pt: the tallest ruled band or box read as one cell
MIN_STRIP = 9.0         # pt: the lowest blank box that can be written in
MIN_CELL_STRIP = 12.0   # pt: the lowest blank strip under a caption that can be written in
MULTILINE = 28.0        # pt: a blank at least this tall takes two handwritten lines
WRITE_ABOVE = 11.0      # pt: the space above an underline that is written in
MIN_UNDERLINE = 18.0    # pt: the shortest underline, and the narrowest cell
PREFIX = 12.0           # pt: the most of a box a caption printed into it may take ("$")
LONG_RULE = 0.6         # share of the page width: a rule this long may be a separator
MAX_RULE = 0.85         # share of the page width: a rule this long is one
MIN_CERTAIN = 2         # a page with fewer certain candidates is not taken for a form
LEADER = re.compile(r"[.…·_\-–\s]+")
# A page number: digits, a roman numeral, or either with a section prefix ("A-1"), a range
# ("3-4") or a "p." / "page" before it.
PAGE_NUMBER = re.compile(r"(?:pp?\.?\s*|page\s+)?(?:[a-z]{1,2}\s?[-–.]\s?)?\d{1,4}(?:\s?[-–]\s?\d{1,4})?"
                         r"|[ivxlcdm]{1,6}", re.I)
CONTENTS_TITLE = re.compile(r"(?:table\s+of\s+)?contents", re.I)
# What OCR reads the leader dots before a square as: dots, dashes, underscores and bullets.
LEADER_TOKEN = re.compile(r"[.…·•_\-–]+")
# What OCR reads an empty checkbox square as: a token made only of these.
BOX_GLYPHS = re.compile(r"[\[\]|()Oo0D£¢©®]{1,2}")
ROUND_NOISE = "OoDQ0"  # what OCR makes of a ring
MARKER = "□"       # matches constants.OPTION_MARKER, so the planner splits the row's labels
if not (OPTION_MARKER.fullmatch(MARKER)):
    raise RuntimeError('scan_rules: expected OPTION_MARKER.fullmatch(MARKER)')


class Shape(StrEnum):
    UNDERLINE = "underline"   # a free horizontal rule with blank space above it
    CELL = "cell"             # the blank strip of a ruled cell under (or beside) its caption
    COMB = "comb"             # a run of equal adjacent boxes for single characters
    SQUARE = "square"         # a small closed empty square: a checkbox
    CIRCLE = "circle"         # a small empty ring: an answer circle to mark, a checkbox


class Doubt(StrEnum):
    """Why a candidate is not certain. Each is a pure predicate over the page geometry."""
    NO_CAPTION = "no-caption"       # no OCR line near enough to label it
    GRID_CELL = "grid-cell"         # inside a ruled grid: a table, not a form
    LONG_RULE = "long-rule"         # an underline long enough to be a separator
    INK_INSIDE = "ink-inside"       # the area is not clean: a mark, a value, a stray glyph
    TEXT_INSIDE = "text-inside"     # an OCR line overlaps the area
    SHORT_COMB = "short-comb"       # a run of only two boxes
    BULLET = "bullet"               # a ring set in the type of the line beside it: a list bullet
    FEW_FIELDS = "few-fields"       # the page has too few certain candidates to be a form


# What each doubt means, in the words the model is shown. One entry per Doubt.
DOUBT_TEXT: dict[Doubt, str] = {
    Doubt.NO_CAPTION: "no printed caption sits next to it",
    Doubt.GRID_CELL: "it is a cell of a ruled table",
    Doubt.LONG_RULE: "the line is long enough to be a separator rule",
    Doubt.INK_INSIDE: "there is some ink inside it",
    Doubt.TEXT_INSIDE: "printed text overlaps it",
    Doubt.SHORT_COMB: "it is a run of only two boxes",
    Doubt.BULLET: "it is as small and as close to the words beside it as a list bullet",
    Doubt.FEW_FIELDS: "the page has few other places to fill in",
}
if not (set(DOUBT_TEXT) == set(Doubt)):
    raise RuntimeError('every Doubt needs its DOUBT_TEXT')


@dataclass(frozen=True)
class Candidate:
    page: int
    shape: Shape
    bbox: BBox                              # the area to write in, as an AcroForm widget's rect
    cells: int = 1                          # COMB: boxes in the run (the field's maxlength)
    multiline: bool = False                 # CELL tall enough for more than one line
    doubts: frozenset[Doubt] = frozenset()

    @property
    def kind(self) -> Literal["text", "checkbox"]:
        return "checkbox" if self.shape in (Shape.SQUARE, Shape.CIRCLE) else "text"

    @property
    def certain(self) -> bool:
        return not self.doubts


# -- raster primitives ------------------------------------------------------------------------

class _Rule(NamedTuple):
    """A thin straight run of ink, in points: from `lo` to `hi` along it, `a` to `b` across."""
    lo: float
    hi: float
    a: float
    b: float

    @property
    def at(self) -> float:
        return (self.a + self.b) / 2

    def spans(self, lo: float, hi: float) -> bool:
        return self.lo <= lo + TOL and self.hi >= hi - TOL


def _rules(bw: Image, shortest: float = MIN_RULE, merge: bool = True) -> list[_Rule]:
    """Runs of ink at least `shortest` long in each row, merged with the runs of about the same
    extent in the rows under them; the merged stacks no thicker than MAX_THICK. `merge` False
    skips the second merge, of stacks that touch: the marks of a dotted or dashed rule are
    stacks of their own, and there are thousands of them on a page."""
    width, height = bw.size
    data = bw.tobytes()
    run = re.compile(rb"\x00{%d,}" % round(shortest * SCALE))
    slack = 3  # px: a rule's rows may start or end a pixel or two apart
    done: list[list[int]] = []
    open_: list[list[int]] = []   # x0, y0, x1, y1, last row's x0, last row's x1
    for y in range(height):
        base = y * width
        grown: list[list[int]] = []
        for m in run.finditer(data, base, base + width):
            x0, x1 = m.start() - base, m.end() - base
            for s in open_:
                if abs(s[4] - x0) <= slack and abs(s[5] - x1) <= slack:
                    s[0], s[2], s[3], s[4], s[5] = min(s[0], x0), max(s[2], x1), y, x0, x1
                    open_.remove(s)
                    grown.append(s)
                    break
            else:
                grown.append([x0, y, x1, y, x0, x1])
        done.extend(open_)
        open_ = grown
    done.extend(open_)
    # Stacks that touch are one rule when they cover nearly the same extent (a scan's rows
    # start and end a pixel apart), or when one is a sliver of a row lying along the other (a
    # rule's row broken in pieces). A black bar under a rule is neither, so the rule stays thin.
    thick = MAX_THICK * SCALE
    done.sort(key=lambda s: s[1])
    if not merge:
        return [_Rule(x0 / SCALE, x1 / SCALE, y0 / SCALE, (y1 + 1) / SCALE)
                for x0, y0, x1, y1, _, _ in done if (y1 + 1 - y0) / SCALE <= MAX_THICK]
    merged: list[list[int]] = []
    near: list[list[int]] = []   # the merged stacks that reach the current row
    for s in done:
        near = [m for m in near if m[3] >= s[1] - 1]
        for m in near:
            overlap = min(m[2], s[2]) - max(m[0], s[0])
            if overlap >= 0.8 * max(m[2] - m[0], s[2] - s[0]) or (
                    overlap >= 0.8 * min(m[2] - m[0], s[2] - s[0]) and s[3] - s[1] <= 1
                    and max(m[3], s[3]) - min(m[1], s[1]) + 1 <= thick):
                m[0], m[2], m[3] = min(m[0], s[0]), max(m[2], s[2]), max(m[3], s[3])
                break
        else:
            merged.append(s)
            near.append(s)
    return [_Rule(x0 / SCALE, x1 / SCALE, y0 / SCALE, (y1 + 1) / SCALE)
            for x0, y0, x1, y1, _, _ in merged if (y1 + 1 - y0) / SCALE <= MAX_THICK]


class _Chain(NamedTuple):
    """How a row of marks reads as one rule: the shortest and longest mark, the farthest apart two
    marks of the rule are, the fewest marks, the share of the rule's length they ink."""
    shortest: float
    longest: float
    gap: float
    count: int
    fill: float


DASHES = _Chain(MIN_DASH, MAX_DASH, DASH_GAP, MIN_DASHES, DASH_FILL)
DOTS = _Chain(MIN_DOT, MAX_DOT, DOT_GAP, MIN_DOTS, DOT_FILL)


def _chains(marks: list[_Rule], how: _Chain, clear: Image) -> list[_Rule]:
    """Each row of collinear marks that reads as one rule, as that rule. `clear` is the ink
    image the bands beside the marks are checked on."""
    chains: list[list[_Rule]] = []
    open_: dict[int, list[list[_Rule]]] = {}   # by the whole point its rows lie at
    for d in sorted((r for r in marks if r.hi - r.lo <= how.longest), key=lambda r: r.lo):
        row = int(d.at)
        for key in (row - 1, row, row + 1):
            if key in open_:
                open_[key] = [c for c in open_[key] if d.lo - c[-1].hi <= how.gap]
        near = [(abs(d.at - c[-1].at), key, c) for key in (row - 1, row, row + 1)
                for c in open_.get(key, ())
                if d.lo >= c[-1].hi - TOL and abs(d.at - c[-1].at) <= 0.8]
        if near:
            _, key, c = min(near, key=lambda n: n[0])
            open_[key].remove(c)
            c.append(d)
            open_.setdefault(row, []).append(c)   # a skewed rule drifts: file it where it is
        else:
            chains.append([d])
            open_.setdefault(row, []).append(chains[-1])
    return [_Rule(c[0].lo, c[-1].hi, min(r.a for r in c), max(r.b for r in c))
            for c in chains if len(c) >= how.count
            and sum(r.hi - r.lo for r in c) >= how.fill * (c[-1].hi - c[0].lo)
            and _evenly(c) and _clear(clear, c)]


def _evenly(marks: list[_Rule]) -> bool:
    """Most of the marks are one pitch apart, or a whole number of pitches (a scan loses some)."""
    pitches = sorted(b.lo - a.lo for a, b in pairwise(marks))
    pitch = pitches[len(pitches) // 2]
    if pitch <= 0:   # marks that start at one x (a speck over a longer one) have no pitch
        return False
    steps = [(p / pitch, p) for p in pitches]
    return sum(abs(n - round(n)) <= 0.25 for n, _ in steps) >= 0.7 * len(steps)


def _dashed(bw: Image, solid: list[_Rule], short: list[_Rule], pad: float = 0.0
            ) -> tuple[list[_Rule], list[_Rule]]:
    """The horizontal rules, with each run of collinear dashes (MIN_DASHES or more, at most DASH_GAP apart,
    at least DASH_FILL ink) joined into one rule. A scan of a dashed rule drops dashes and
    renders the rest a hair under MIN_RULE, so the dashes are read on their own shorter minimum;
    dashes that join are replaced by the joined rule, and no rule of the solid pass is lost.
    The joined rules run `pad` past their end dashes. Returns the rules and the joined ones
    among them."""
    joined = _chains(short, DASHES, bw)
    if not joined:
        return solid, []
    joined = [r._replace(lo=r.lo - pad, hi=r.hi + pad) for r in joined]
    # A solid rule the chain already covers (its dashes long enough for the first pass) goes.
    covered = lambda r: any(abs(r.at - j.at) <= 1.0 and r.lo >= j.lo - TOL and r.hi <= j.hi + TOL  # noqa: E731
                            for j in joined)
    return [r for r in solid if not covered(r)] + joined, joined


def _dotted(gray: Image, bw: Image) -> list[_Rule]:
    """The dotted rules: rows of specks that are only a little darker than the paper around
    them, too pale for DARK on a scan of grey paper. A speck is a pixel FAINT_DELTA under the
    local mean; the rows are chained like dashes, with the paper's grain and the page's text
    kept out by the evenness of the pitch and the clear bands beside the specks."""
    near = gray.filter(ImageFilter.BoxBlur(round(DOT_AREA * SCALE)))
    specks = ImageChops.subtract(near, gray).point(lambda v: 0 if v >= FAINT_DELTA else 255)
    return _chains(_rules(specks, MIN_DOT, merge=False), DOTS, specks)


def _clear(bw: Image, dashes: list[_Rule]) -> bool:
    """Nothing hangs from the dashes or sits on them: the bands just above and just under their
    middles are each nearly clean. The bars of "T" and "E", the arches of "m" and the baseline
    of a line of text have a stem or a body on one side."""
    for sign in (-1, 1):
        ink = area = 0
        for r in dashes:
            a, b = (r.a - 3.0, r.a - 0.5) if sign < 0 else (r.b + 0.5, r.b + 3.0)
            x0, y0, x1, y1 = (round(v * SCALE) for v in (r.lo + 0.5, a, r.hi - 0.5, b))
            if x1 > x0 and y1 > y0:
                ink += bw.crop((x0, y0, x1, y1)).histogram()[0]
                area += (x1 - x0) * (y1 - y0)
        if area and ink / area > DASH_CLEAR:
            return False
    return True


def _px(box: BBox) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = (round(v * SCALE) for v in box)
    return x0, y0, x1, y1


def _inset(box: BBox, d: float) -> BBox:
    return box[0] + d, box[1] + d, box[2] - d, box[3] - d


class _Box(NamedTuple):
    """A closed rectangle of rules: two horizontals and the verticals between them."""
    bbox: BBox   # the sides' centre lines
    top: _Rule
    bottom: _Rule
    ink: float   # inside, clear of the sides


_Claims = dict[_Rule, list[tuple[float, float]]]   # the stretches of rules boxes and bands use


class _Page:
    """One page's rules and lines, and the queries the shape rules make of them."""

    def __init__(self, image: Image, lines: Sequence[Line]):
        self.gray = image
        self.bw = image.point(lambda v: 0 if v < DARK else 255)
        self.width = image.width / SCALE
        solid, self.dashed = _dashed(self.bw, _rules(self.bw), _rules(self.bw, MIN_DASH))
        self.dotted = [d for d in _dotted(image, self.bw) if not any(
            abs(d.at - r.at) <= 1.5 and min(d.hi, r.hi) - max(d.lo, r.lo) >= 0.5 * (d.hi - d.lo)
            for r in solid)]
        # A dotted rule is a line to write on, never a wall: boxes and cells are built from the
        # solid ones only.
        self.h = sorted(solid, key=lambda r: r.at)
        self.h_at = [r.at for r in self.h]
        flipped = self.bw.transpose(Transpose.TRANSPOSE)
        solid_v = _rules(flipped)
        self.v = sorted(solid_v, key=lambda r: r.at)
        self.v_at = [r.at for r in self.v]
        # Dashed verticals join too. They are walls only for the underlines' test of a frame or
        # a table (a box is made of solid rules), so a dashed table's borders are no underlines.
        self.walls = sorted(_dashed(flipped, solid_v, _rules(flipped, MIN_DASH), DASH_PAD)[0],
                            key=lambda r: r.at)
        self.walls_at = [r.at for r in self.walls]
        self.glyphs = _blobs(self.bw, round(MAX_LETTER * SCALE))
        circle = round(CIRCLE_D[1] * SCALE)
        self.blobs = [b for b in self.glyphs if b.x1 - b.x0 <= circle and b.y1 - b.y0 <= circle]
        self.lines = [l for l in lines if not l.rotated and any(c.isalnum() for c in l.text)]
        self.leaders = [l for l in lines if not l.rotated and LEADER.fullmatch(l.text)]

    def ink(self, box: BBox) -> float:
        x0, y0, x1, y1 = _px(box)
        if x1 <= x0 or y1 <= y0:
            return 0.0
        return self.bw.crop((x0, y0, x1, y1)).histogram()[0] / ((x1 - x0) * (y1 - y0))

    def sides(self, x0: float, x1: float, top: float, bottom: float, dashed: bool = False
              ) -> list[_Rule]:
        """The vertical rules between x0 and x1 that run from top to bottom, left to right;
        with `dashed`, the joined dashed ones as well."""
        rules, at = (self.walls, self.walls_at) if dashed else (self.v, self.v_at)
        i = bisect.bisect_left(at, x0 - TOL)
        j = bisect.bisect_right(at, x1 + TOL)
        found: list[_Rule] = []
        for r in rules[i:j]:
            if r.spans(top, bottom) and (not found or r.at - found[-1].at > TOL):
                found.append(r)
        return found

    def verticals(self, x0: float, x1: float, top: float, bottom: float, dashed: bool = False
                  ) -> list[float]:
        return [r.at for r in self.sides(x0, x1, top, bottom, dashed)]

    def horizontals(self, top: float, bottom: float) -> list[_Rule]:
        return self.h[bisect.bisect_left(self.h_at, top):bisect.bisect_right(self.h_at, bottom)]

    def rule_over(self, line: Line, x: float | None = None) -> _Rule | None:
        """The rule a caption starting at `x` hangs from: the nearest one at most CAPTION_DROP
        above the line that runs on under its first words. Read against the line's middle,
        since OCR can make a line's box taller than its text."""
        x = line.x0 if x is None else x
        under = min(line.x1, x + 24)
        middle = (line.top + line.bottom) / 2
        return max((r for r in self.horizontals(line.top - CAPTION_DROP, middle - 2)
                    if r.spans(x, under)), key=lambda r: r.at, default=None)

    def boxes(self) -> list[_Box]:
        """Every closed rectangle no taller than MAX_CELL that no rule crosses."""
        found: dict[tuple[int, ...], _Box] = {}
        hs = [r for r in self.h if r.hi - r.lo >= SQUARE_SIDE[0]]
        ats = [r.at for r in hs]
        for i, top in enumerate(hs):
            for j in range(i + 1, len(hs)):
                height = ats[j] - ats[i]
                if height > MAX_CELL:
                    break
                bottom = hs[j]
                lo, hi = max(top.lo, bottom.lo), min(top.hi, bottom.hi)
                # Past a comb's height a box is a cell, at least as wide as an underline.
                if height < SQUARE_SIDE[0] or hi - lo < (
                        SQUARE_SIDE[0] if height <= MAX_BOX else MIN_UNDERLINE):
                    continue
                for x0, x1 in pairwise(self.verticals(lo, hi, top.at, bottom.at)):
                    bbox = (x0, top.at, x1, bottom.at)
                    key = _px(bbox)
                    if x1 - x0 < SQUARE_SIDE[0] or key in found or any(
                            r.spans(x0 + TOL, x1 - TOL)
                            for r in self.horizontals(top.b + TOL / 2, bottom.a - TOL / 2)):
                        continue
                    inside = _inset(bbox, min(1.5, (x1 - x0) / 4, height / 4))
                    found[key] = _Box(bbox, top, bottom, self.ink(inside))
        return list(found.values())

    def dividers(self, box: BBox) -> list[float]:
        """Pale vertical lines splitting a blank box at an even pitch into three or more: a
        comb drawn in light gray, its dividers too pale to be rules."""
        x0, y0, x1, y1 = _px(_inset(box, 1.0))
        if x1 - x0 < 8 or y1 - y0 < 4:
            return []
        means = self.gray.crop((x0, y0, x1, y1)).resize((x1 - x0, 1), Resampling.BOX).tobytes()
        runs = [m.span() for m in re.finditer(rb"[\x00-%c]+" % (FAINT - 1), means)]
        cuts = [(x0 + (a + b - 1) / 2) / SCALE for a, b in runs]
        if len(cuts) < 2:
            return []
        pitches = [b - a for a, b in pairwise([box[0], *cuts, box[2]])]
        pitch = sum(pitches) / len(pitches)
        if not COMB_WIDTH[0] <= pitch <= COMB_WIDTH[1] or \
                any(abs(p - pitch) > 0.2 * pitch for p in pitches):
            return []
        return cuts


# -- shapes -----------------------------------------------------------------------------------

def _on_row(line: Line, box: BBox) -> bool:
    """The line's middle is level with the box, as forms._Planner._row reads it."""
    cy = (line.top + line.bottom) / 2
    return box[1] - 3 <= cy <= box[3] + 3


def _caption_above(line: Line, box: BBox) -> bool:
    return -3 <= box[1] - line.bottom <= ABOVE_GAP and line.x0 < box[2] and line.x1 > box[0] - 6


def _caption_left(line: Line, box: BBox, reach: float = LEFT_GAP) -> bool:
    return _on_row(line, box) and line.x1 <= box[0] + 2 and box[0] - line.x1 <= reach


def _width(box: _Box) -> float:
    return box.bbox[2] - box.bbox[0]


def _span(run: list[_Box]) -> BBox:
    return run[0].bbox[0], run[0].bbox[1], run[-1].bbox[2], run[0].bbox[3]


def _neighbours(box: _Box, boxes: Sequence[_Box]) -> list[_Box]:
    """The boxes of about the same width right above or below: the next rows of a column."""
    return [o for o in boxes if (o.bottom is box.top or o.top is box.bottom)
            and o.bbox[0] < box.bbox[2] - TOL and o.bbox[2] > box.bbox[0] + TOL
            and abs(_width(o) - _width(box)) <= 0.3 * _width(box)]


def _worded_token(word: str) -> bool:
    """A word: two or more letters or digits, one of them not what OCR makes of a ring ("O0O0DO00")."""
    return (sum(c.isalnum() for c in word) >= 2 and not BOX_GLYPHS.fullmatch(word)
            and any(c.isalnum() and c not in ROUND_NOISE for c in word))


def _worded(line: Line) -> bool:
    """The line has words in it: not only the glyphs OCR reads a square or a leader as, nor the
    run of round-glyph noise it reads a row of circles as."""
    return any(_worded_token(w) for w in line.text.split())


def _leader_between(page: _Page, line: Line, box: BBox) -> bool:
    """Leader dots run on the box's row from the line to the box: a dotted or dashed rule, or an
    OCR line of dots, that starts after the line and ends at the box."""
    runs = [(r.lo, r.hi) for r in (*page.dotted, *page.dashed) if box[1] <= r.at <= box[3] + 2]
    runs += [(l.x0, l.x1) for l in page.leaders if _on_row(l, box)]
    return any(lo >= line.x1 - 6 and box[0] - LEADER_END <= hi <= box[0] + 2 for lo, hi in runs)


def _labelled_left(page: _Page, box: BBox) -> bool:
    """A line of words ends before the box on its row, close or with leader dots between, or runs
    on into the box (OCR reads the square as a glyph at the end of its own label)."""
    for l in page.lines:
        if not _on_row(l, box) or l.x0 >= box[0] - 3 or not _worded(l):
            continue
        if l.x1 <= box[0] + 2:
            gap = box[0] - l.x1
            if gap <= LEFT_GAP or gap <= LEADER_REACH and _leader_between(page, l, box):
                return True
        elif l.x1 <= box[2] + 4:
            return True
    return False


def _captions(page: _Page, box: BBox) -> bool:
    """Something printed on the box's row names it: a line starting just right of it, a line
    just left of it (any, even a stray glyph), or the label further left."""
    return any(_on_row(l, box) and (l.x1 > box[2] + 2 and l.x0 <= box[2] + RIGHT_GAP
                                    or l.x1 <= box[0] + 2 and box[0] - l.x1 <= LEFT_GAP)
               for l in page.lines) or _labelled_left(page, box)


def _with_neighbours(box: _Box, boxes: Sequence[_Box]) -> list[_Box]:
    """The box and the boxes of its row it runs on into: squares in a run of a few (grades 1 to
    8) take the label of the run, printed left of its first."""
    row = sorted((b for b in boxes if abs(b.bbox[1] - box.bbox[1]) <= 2 * TOL
                  and abs(b.bbox[3] - box.bbox[3]) <= 2 * TOL), key=lambda b: b.bbox[0])
    at = row.index(box)
    first = last = at
    while first > 0 and row[first].bbox[0] - row[first - 1].bbox[2] <= 1.5 * _width(box) \
            and abs(_width(row[first - 1]) - _width(box)) <= 0.3 * _width(box):
        first -= 1
    while last + 1 < len(row) and row[last + 1].bbox[0] - row[last].bbox[2] <= 1.5 * _width(box) \
            and abs(_width(row[last + 1]) - _width(box)) <= 0.3 * _width(box):
        last += 1
    return row[first:last + 1]


def _squares_and_combs(page: _Page, boxes: list[_Box], number: int
                       ) -> tuple[list[Candidate], set[_Box]]:
    """Checkboxes, and the runs of adjacent equal boxes of a comb; also the boxes they took."""
    rows: dict[tuple[int, int], list[_Box]] = {}
    for box in boxes:
        if box.bbox[3] - box.bbox[1] <= MAX_BOX:
            rows.setdefault((id(box.top), id(box.bottom)), []).append(box)
    out: list[Candidate] = []
    used: set[_Box] = set()
    combs: list[tuple[list[_Box], set[Doubt]]] = []
    for row in rows.values():
        row.sort(key=lambda b: b.bbox[0])
        runs: list[list[_Box]] = []
        for box in row:
            width = _width(box)
            # A ticked box is still a checkbox; a solid one is a bullet.
            if box.ink > SOLID or not COMB_WIDTH[0] <= width <= COMB_WIDTH[1]:
                runs.append([])
            elif runs and runs[-1] and runs[-1][-1].bbox[2] == box.bbox[0] \
                    and abs(width - _width(runs[-1][-1])) <= 0.3 * width \
                    and box.ink <= BLANK and runs[-1][-1].ink <= BLANK:
                runs[-1].append(box)
            else:
                runs.append([box])
        for run in filter(None, runs):
            grid = any(_neighbours(box, boxes) for box in run)
            if len(run) >= 2:
                combs.append((run, {Doubt.GRID_CELL} if grid else set()))
                used.update(run)
                continue
            box = run[0]
            w, h = _width(box), box.bbox[3] - box.bbox[1]
            if not (SQUARE_SIDE[0] <= min(w, h) and max(w, h) <= SQUARE_SIDE[1]
                    and max(w, h) <= 1.34 * min(w, h)):
                continue
            used.add(box)
            doubts = set()
            if grid:
                doubts.add(Doubt.GRID_CELL)
            if box.ink > BLANK:
                doubts.add(Doubt.INK_INSIDE)
            if not any(_captions(page, b.bbox) for b in _with_neighbours(box, boxes)):
                doubts.add(Doubt.NO_CAPTION)
            out.append(Candidate(number, Shape.SQUARE, box.bbox, doubts=frozenset(doubts)))
    spans = [_span(run) for run, _ in combs]
    for (run, doubts), bbox in zip(combs, spans, strict=True):
        # The runs of one number (SSN 3-2-4) sit on one row a box or two apart, under one
        # caption.
        number_ = [bbox]
        for box in number_:
            number_ += [s for s in spans if s not in number_ and abs(s[1] - box[1]) <= TOL
                        and max(s[0], box[0]) - min(s[2], box[2]) <= 2 * _width(run[0])]
        if len(run) == 2 and len(number_) == 1:
            doubts.add(Doubt.SHORT_COMB)
        whole = (min(s[0] for s in number_), bbox[1], max(s[2] for s in number_), bbox[3])
        if not any(_caption_above(l, whole) or _caption_left(l, whole) for l in page.lines):
            doubts.add(Doubt.NO_CAPTION)
        out.append(Candidate(number, Shape.COMB, bbox, cells=len(run), doubts=frozenset(doubts)))
    return out, used


def _on_ring(page: _Page, cx: float, cy: float, r: float) -> bool:
    """RING of the circle's circumference is ink: RING_POINTS points on it, each with a pixel
    along the radius (within a pixel of it) a quarter of the way or more from the paper to the ring's
    darkest ink. Read on the gray image, since a thin ring's pixels fall under the ink threshold in
    places; a letter "C" has an opening of plain paper."""
    x0, y0, x1, y1 = (round(v * SCALE) for v in (cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2))
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, page.gray.width), min(y1, page.gray.height)
    if x1 <= x0 or y1 <= y0:
        return False
    levels = sorted(page.gray.crop((x0, y0, x1, y1)).tobytes())
    paper, ink = levels[len(levels) * 4 // 5], levels[0]
    if paper - ink < 60:
        return False
    cut = paper - RING_FAINT * (paper - ink)
    hits = 0
    for k in range(RING_POINTS):
        a = math.tau * k / RING_POINTS
        for dr in (-1, 0, 1):
            x = round((cx + r * math.cos(a)) * SCALE + dr * math.cos(a))
            y = round((cy + r * math.sin(a)) * SCALE + dr * math.sin(a))
            if x0 <= x < x1 and y0 <= y < y1 and page.gray.getpixel((x, y)) <= cut:
                hits += 1
                break
    return hits >= RING * RING_POINTS


class _Blob(NamedTuple):
    """A connected piece of ink, in pixels: its bounding box and how many pixels it holds."""
    x0: int
    y0: int
    x1: int
    y1: int
    ink: int


def _blobs(bw: Image, widest: int) -> list[_Blob]:
    """The connected pieces of ink (8-connected, found run by run) whose box is at most `widest`
    pixels each way. Bigger pieces (a page's text frame, a table) are left out whole."""
    width, height = bw.size
    data = bw.tobytes()
    run = re.compile(rb"\x00+")
    parent: list[int] = []
    boxes: list[list[int]] = []   # x0, y0, x1, y1, ink of each run's piece, by run id

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        i, j = find(i), find(j)
        if i != j:
            parent[j] = i
            a, b = boxes[i], boxes[j]
            a[0], a[1], a[2], a[3], a[4] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), \
                max(a[3], b[3]), a[4] + b[4]

    before: list[tuple[int, int, int]] = []   # the previous row's runs: x0, x1, id
    for y in range(height):
        base = y * width
        row = []
        j = 0
        for m in run.finditer(data, base, base + width):
            x0, x1 = m.start() - base, m.end() - base
            me = len(parent)
            parent.append(me)
            boxes.append([x0, y, x1, y + 1, x1 - x0])
            while j < len(before) and before[j][1] < x0:
                j += 1
            k = j
            while k < len(before) and before[k][0] <= x1:
                union(me, before[k][2])
                k += 1
            row.append((x0, x1, me))
        before = row
    return [_Blob(*b) for i, b in enumerate(boxes) if parent[i] == i and b[2] - b[0] <= widest
            and b[3] - b[1] <= widest and b[4] > 0]


def _paper_beside(page: _Page, b: _Blob, side: int, reach: float) -> float:
    """The paper between a piece of ink and the next ink on its left (`side` < 0) or right, in
    points, at most `reach`; over the piece's own height."""
    n = round(reach * SCALE)
    x0, x1 = (max(b.x0 - n, 0), b.x0) if side < 0 else (b.x1, min(b.x1 + n, page.bw.width))
    if x1 <= x0:
        return reach
    near = ImageChops.invert(page.bw.crop((x0, b.y0, x1, b.y1))).getbbox()
    if near is None:
        return reach
    return ((b.x0 - (x0 + near[2])) if side < 0 else (x0 + near[0] - b.x1)) / SCALE


def _in_running_text(page: _Page, bbox: BBox) -> bool:
    """The ring sits within the extent of an OCR line of words: running text, where a lone round
    glyph is a letter, a number or a bullet. A ring among OCR noise ("O0O0DO00") is a row of
    answer circles."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return any(l.top - 1 <= cy <= l.bottom + 1 and l.x0 - 2 <= cx <= l.x1 + 2 and _worded(l)
               for l in page.lines)


def _bullet_like(page: _Page, b: _Blob, bbox: BBox, left: float, right: float) -> bool:
    """The ring is a glyph set in the type of the words around it (a list bullet, a lone "o" or
    "O", a small hollow bullet between words), not a circle drawn beside a label. A glyph is
    smaller than the tallest letters beside it (a drawn circle is as tall as they are or taller);
    at the start or the end of a line (`left` or `right` is open paper) it is also only a word
    space from the nearest of them, where a drawn circle stands further off. The letters are the
    pieces of ink on a side of the ring on its row; fewer than three there are no label to
    compare with."""
    cy = (bbox[1] + bbox[3]) / 2
    d = bbox[2] - bbox[0]
    reach = LABEL_REACH * SCALE
    for side, open_side in ((1, left), (-1, right)):
        near = sorted((o for o in page.glyphs if o is not b
                       and 0 <= (o.x0 - b.x1 if side > 0 else b.x0 - o.x1) <= reach
                       and abs((o.y0 + o.y1) / 2 / SCALE - cy) <= d and (o.y1 - o.y0) >= LETTER_MIN * d * SCALE),
                      key=lambda o: o.x0 - b.x1 if side > 0 else b.x0 - o.x1)
        letters = near[:LABEL_LETTERS]
        if len(letters) < 3:
            continue
        heights = sorted((o.y1 - o.y0) / SCALE for o in letters)
        capital = max(h for h in heights if h <= TALL_LETTER * heights[len(heights) // 2])
        gap = (min(o.x0 for o in letters) - b.x1 if side > 0 else b.x0 - max(o.x1 for o in letters)) / SCALE
        if gap <= BULLET_REACH * capital and (
                d < BULLET_SIZE * capital or open_side >= CIRCLE_NEAR and gap < BULLET_GAP * capital):
            return True
    return False


def _circles(page: _Page, number: int) -> list[Candidate]:
    """Answer circles: a small piece of ink about as wide as tall and one diameter across, a
    ring with its middle clear of ink (a tick may be in it), and room either side. A letter in
    a word has neighbours close by, a lone "o", "O" or "0" between two words is a word gap from
    them, and a bullet or degree sign is smaller than any printed circle: none is a circle. A solid
    disc is a bullet; a ring that is open is a "C"."""
    out: list[Candidate] = []
    lo, hi = (round(v * SCALE) for v in CIRCLE_D)
    for b in page.blobs:
        w, h = b.x1 - b.x0, b.y1 - b.y0
        # A small piece must be a thin ring: a bold "o" of the same size is mostly ink.
        thin = 0.45 if min(w, h) < SMALL_CIRCLE * SCALE else 0.75
        if not (lo <= w <= hi and lo <= h <= hi) or abs(w - h) > 0.2 * max(w, h) \
                or not 0.12 <= b.ink / (w * h) <= thin:
            continue
        cx, cy = (b.x0 + b.x1) / 2 / SCALE, (b.y0 + b.y1) / 2 / SCALE
        r = (w + h) / 4 / SCALE
        bbox = (cx - r, cy - r, cx + r, cy + r)
        if not _on_ring(page, cx, cy, r - 0.5):
            continue
        inner = page.ink((cx - 0.5 * r, cy - 0.5 * r, cx + 0.5 * r, cy + 0.5 * r))
        sides = (page.ink((bbox[0] - 1.5, bbox[1] + 1, bbox[0] - 0.3, bbox[3] - 1)),
                 page.ink((bbox[2] + 0.3, bbox[1] + 1, bbox[2] + 1.5, bbox[3] - 1)))
        if inner > DISC or max(sides) > CIRCLE_CLEAR:
            continue
        # An answer circle stands clear of its label or its fellows. A round letter has a neighbour
        # close by, inside a word, or a word gap away on both sides between two words; inside a
        # line of words, a ring under TINY_CIRCLE across is a bullet or a degree sign, whatever
        # room it has, and a ring with nothing near it is a chart's marker or numeral.
        d = bbox[2] - bbox[0]
        left, right = (_paper_beside(page, b, side, max(WORD_GAP * d, CIRCLE_NEAR)) for side in (-1, 1))
        if min(left, right) < CIRCLE_ROOM or _in_running_text(page, bbox) and (
                max(left, right) < WORD_GAP * d or min(left, right) >= CIRCLE_NEAR or d < TINY_CIRCLE):
            continue
        doubts = set()
        if _in_running_text(page, bbox) and _bullet_like(page, b, bbox, left, right):
            doubts.add(Doubt.BULLET)
        if inner > BLANK:
            doubts.add(Doubt.INK_INSIDE)
        if not (_captions(page, bbox) or any(_caption_above(l, bbox) for l in page.lines)):
            doubts.add(Doubt.NO_CAPTION)
        out.append(Candidate(number, Shape.CIRCLE, bbox, doubts=frozenset(doubts)))
    return out


def _blank_boxes(page: _Page, boxes: list[_Box], used: set[_Box], number: int
                 ) -> list[Candidate]:
    """Closed boxes with nothing printed inside, captioned from outside: above, or on the left
    (often in a cell of their own). A pale comb inside makes one a COMB. A box with a note
    printed along its bottom ("Check here if...") offers the tall blank above the note."""
    out: list[Candidate] = []
    for box in boxes:
        x0, y0, x1, y1 = box.bbox
        if box in used or x1 - x0 < max(MIN_UNDERLINE, y1 - y0) or y1 - y0 < MIN_STRIP \
                or box.ink > FILLED:
            continue
        # OCR reads a pale comb's dividers as text ("[ITTTITTT1]"): a line counts as printed
        # only when there is ink under it.
        inner = _inset(box.bbox, 1.5)
        printed = [l for l in page.lines if geometry.centre_in(l.bbox, box.bbox)
                   and page.ink(geometry.intersection(l.bbox, inner)) > BLANK]
        area, caption = _after_caption(box.bbox, [l for l in page.lines if l not in printed],
                                       PREFIX)
        ink = box.ink if caption is None else page.ink(_inset(area, 1.5))
        if printed:
            area = (area[0], area[1], x1, min(l.top for l in printed) - 1)
            if area[3] - y0 < MULTILINE:
                continue
            ink = page.ink(_inset(area, 1.5))
        doubts = set()
        if ink > BLANK:
            doubts.add(Doubt.INK_INSIDE)
        if caption is None and not any(_caption_above(l, area) or _caption_left(l, area)
                                       for l in page.lines):
            doubts.add(Doubt.NO_CAPTION)
        if _in_table(box, boxes, page):
            doubts.add(Doubt.GRID_CELL)
        if not printed and (cuts := page.dividers(area)):
            out.append(Candidate(number, Shape.COMB, area, cells=len(cuts) + 1,
                                 doubts=frozenset(doubts)))
        else:
            out.append(Candidate(number, Shape.CELL, area, multiline=area[3] - area[1] >= MULTILINE,
                                 doubts=frozenset(doubts)))
    return out


def _in_table(box: _Box, boxes: list[_Box], page: _Page) -> bool:
    """Most of the box's column holds printed text: an empty cell of a table, not a column of
    blanks to fill in (with at most a heading over it)."""
    column, seen = [box], {box}
    for b in column:
        for o in _neighbours(b, boxes):
            if o not in seen:
                seen.add(o)
                column.append(o)
    texted = sum(any(geometry.centre_in(l.bbox, o.bbox) for l in page.lines) for o in column[1:])
    return texted > 1 and texted >= len(column) - 1 - texted


def _cells(page: _Page, boxes: list[_Box], claims: _Claims, number: int) -> list[Candidate]:
    """Blank strips of ruled bands, each under the caption printed at the top of its band.
    Each claims its stretch of its band's top and bottom rules from the underlines."""
    seen: set[tuple[int, ...]] = set()
    out: list[Candidate] = []
    for line in page.lines:
        # OCR may read the captions of two cells side by side as one line: a divider that
        # crosses the line starts another caption.
        middle = (line.top + line.bottom) / 2
        splits = page.verticals(line.x0 + 2, line.x1 - 2, middle - 6, middle + 6)
        for x in [line.x0, *(s + 1 for s in splits)]:
            top = page.rule_over(line, x)
            if top is None:
                continue
            under = min(line.x1, x + 24)
            bottom = next((r for r in page.horizontals(line.bottom - 1, top.at + MAX_CELL)
                           if r.spans(x, under)), None)
            if bottom is None:
                continue
            band = (max(top.lo, bottom.lo), min(top.hi, bottom.hi))
            # The cell's sides need only reach a strip's height under the caption: a cell can
            # end where its dividers do, on a pale fill's edge that is no rule.
            sides = page.sides(*band, top.at, line.bottom + MIN_STRIP)
            left = max((s for s in sides if s.at <= x + 1), key=lambda s: s.at, default=None)
            right = min((s for s in sides if s.at > x + 1), key=lambda s: s.at, default=None)
            x0 = left.at if left else band[0]
            x1 = right.at if right else band[1]
            floor = min([bottom.at] + [s.hi for s in (left, right) if s and s.hi < bottom.at - TOL])
            cell = (x0, top.at, x1, floor)
            if _px(cell) in seen or x1 - x0 < MIN_UNDERLINE:
                continue
            seen.add(_px(cell))
            inside = [l for l in page.lines if geometry.centre_in(l.bbox, cell)]
            # The strip is under the caption's own column of text, the words of its first row
            # that follow on from `x`; text further right in the same cell (the "Check here"
            # note beside the I-9's additional information) ends the strip there.
            reach = max(line.x1, x)
            for l in sorted(inside, key=lambda l: l.x0):
                if _on_row(l, line.bbox) and x <= l.x0 <= reach + LEFT_GAP:
                    reach = max(reach, l.x1)
            column = [l for l in [line, *inside] if l.x0 < reach and l.x1 > x]
            strip_top = max(l.bottom for l in column)
            x1 = min([x1] + [l.x0 - 2 for l in inside if l.x0 >= reach
                             and l.bottom > strip_top + 1])
            strip = (x0, strip_top, x1, floor)
            if floor - strip_top < MIN_CELL_STRIP \
                    or x1 - x0 < max(MIN_UNDERLINE, 2 * (floor - strip_top)) or any(
                    b.bbox[0] >= x0 - TOL and b.bbox[2] <= x1 + TOL
                    and b.bbox[1] >= strip_top - TOL and b.bbox[3] <= floor + TOL for b in boxes):
                continue
            ink = page.ink((x0 + 2, strip_top + 1, x1 - 2, floor - 1.5))
            if ink > FILLED:
                continue
            bbox = (x0 + 1.5, strip_top, x1 - 1, floor)
            doubts = {Doubt.INK_INSIDE} if ink > BLANK else set()
            multiline = floor - strip_top >= MULTILINE
            if _slack(page, strip, cell, top):
                doubts.add(Doubt.GRID_CELL)
            for rule in (top, bottom):
                claims.setdefault(rule, []).append((x0, x1))
            out.append(Candidate(number, Shape.CELL, bbox, multiline=multiline,
                                 doubts=frozenset(doubts)))
    return out


def _slack(page: _Page, strip: BBox, cell: BBox, top: _Rule) -> bool:
    """Text in another cell of the strip's ruled band runs on level with the strip, down into
    its last MIN_STRIP: the strip is the slack under a short entry of a table row, not a place
    to write. Text that starts a cell of its own under a rule of its own (fw9's "6 City"
    beside the requester's box), or whose paragraph runs on past the band (a heading or a
    note beside several rows), does not count."""
    def runs_on(l: Line) -> bool:
        return any(o is not l and l.top < o.top <= l.bottom + 3 and abs(o.x0 - l.x0) <= 3
                   and o.bottom > strip[3] + 1 for o in page.lines)

    return any(l.top < strip[3] - 1 and max(strip[1], strip[3] - MIN_STRIP) + 1 < l.bottom
               <= strip[3] + 1
               and top.lo <= l.x0 <= top.hi and not cell[0] <= (l.x0 + l.x1) / 2 <= cell[2]
               and ((own := page.rule_over(l)) is None or own.at <= top.at + TOL)
               and not runs_on(l)
               for l in page.lines)


def _leads_to(page: _Page, rule: _Rule, hi: float, boxes: Sequence[_Box]) -> bool:
    """A dotted rule that runs up to a small box (a checkbox or a digit box), to the start of a
    line of text on its row, or to a page number a little further off (a table of contents'
    right-aligned numbers) is a leader ("Kindergarten ........ [ ]"), not a place to write."""
    if rule not in page.dotted:
        return False
    return any(b.bbox[0] - 2 <= hi + LEADER_END and b.bbox[0] >= hi - 2
               and b.bbox[1] - 2 <= rule.at <= b.bbox[3] + 2 and _width(b) <= COMB_WIDTH[1]
               and b.bbox[3] - b.bbox[1] <= MAX_BOX for b in boxes) or any(
        l.x0 >= hi - 2 and l.x0 - hi <= (LEADER_NUMBER if PAGE_NUMBER.fullmatch(l.text.strip())
                                          else LEADER_END)
        and l.top - 1 <= rule.at <= l.bottom + 1
        for l in page.lines) or _unread_number(page, rule, hi)


def _unread_number(page: _Page, rule: _Rule, hi: float) -> bool:
    """Under a recognized contents heading, unread ink after a leader may be a page number.
    Without that context it may be a printed unit after a dotted entry field."""
    row = (rule.at - 10, rule.at + 3)
    return (any(l.bottom < rule.at and CONTENTS_TITLE.fullmatch(l.text.strip()) for l in page.lines)
            and any(_worded(l) and rule.lo - 12 <= l.x1 <= rule.lo + 6 and l.top - 1 <= rule.at <= l.bottom + 1
                for l in page.lines)
            and not any(hi - 2 <= l.x0 <= hi + LEADER_NUMBER and l.top - 1 <= rule.at <= l.bottom + 1
                        for l in page.lines)
            and any(hi <= g.x0 / SCALE <= hi + LEADER_NUMBER and row[0] <= (g.y0 + g.y1) / 2 / SCALE <= row[1]
                    and 3 <= (g.y1 - g.y0) / SCALE <= 14 for g in page.glyphs))


def _underlines(page: _Page, claims: _Claims, number: int, boxes: Sequence[_Box] = ()
                ) -> list[Candidate]:
    """The free stretches of rules with room to write above them: no box or band claims
    them, no frame closes both ends, nothing hangs under them (a divider starting at the
    rule makes it the top of a table, a black bar the edge of a label)."""
    out: list[Candidate] = []
    for rule in [*page.h, *page.dotted]:
        for lo, hi in _unclaimed(rule, claims.get(rule, [])):
            length = hi - lo
            if length < MIN_UNDERLINE or length > MAX_RULE * page.width \
                    or _leads_to(page, rule, hi, boxes):
                continue
            if page.verticals(lo - TOL, lo + TOL, rule.a - 2, rule.a, dashed=True) and \
                    page.verticals(hi - TOL, hi + TOL, rule.a - 2, rule.a, dashed=True):
                continue
            if page.ink((lo, rule.b, hi, rule.b + 2)) > 0.5 or any(
                    rule.a - TOL <= r.lo <= rule.b + TOL and r.hi - rule.b >= MIN_STRIP
                    for r in page.sides(lo, hi, rule.b + 1, rule.b + 1, dashed=True)):
                continue
            bbox, caption = _after_caption((lo, rule.at - WRITE_ABOVE, hi, rule.at), page.lines,
                                           length / 2)
            if bbox[2] - bbox[0] < MIN_UNDERLINE:
                continue
            ink = page.ink((bbox[0] + 1, rule.a - WRITE_ABOVE, bbox[2] - 1, rule.a - 1))
            if ink > FILLED:
                continue
            doubts = set()
            if ink > BLANK:
                doubts.add(Doubt.INK_INSIDE)
            if caption is not None and _crossed(page, rule, lo, hi) and any(
                    0 <= caption.top - l.bottom <= 3 and min(l.x1, caption.x1)
                    - max(l.x0, caption.x0) >= (caption.x1 - caption.x0) / 2 for l in page.lines):
                doubts.add(Doubt.GRID_CELL)   # the slack after a paragraph in a table's cell
            if length > LONG_RULE * page.width:
                doubts.add(Doubt.LONG_RULE)
            # On a rule of dashes or dots, an OCR line counts as printed only with ink under it,
            # above the rule: the pages of such rules are noisy scans whose OCR reads whole rows
            # of blank paper as text.
            joined = rule in page.dashed or rule in page.dotted
            if any(l.top < rule.a - 2 and l.bottom > bbox[1] + 2 and l.x0 < bbox[2]
                   and l.x1 > bbox[0] and not LEADER.fullmatch(l.text) and l is not caption
                   and (not joined or page.ink(geometry.intersection(l.bbox, (*bbox[:3], rule.a - 1))) > BLANK)
                   for l in page.lines):
                doubts.add(Doubt.TEXT_INSIDE)
            if caption is None and not any(
                    _caption_left(l, bbox, 2 * LEFT_GAP) or _caption_above(l, bbox)
                    or 0 <= l.top - rule.at <= ABOVE_GAP and l.x0 >= lo - 6 and l.x0 < hi
                    for l in page.lines):
                doubts.add(Doubt.NO_CAPTION)
            out.append(Candidate(number, Shape.UNDERLINE, bbox, doubts=frozenset(doubts)))
    return out


def _crossed(page: _Page, rule: _Rule, lo: float, hi: float) -> bool:
    """A divider runs through the stretch of the rule, on from the row above to the row below."""
    return bool(page.sides(lo, hi, rule.a - MIN_STRIP, rule.b + MIN_STRIP))


def _unclaimed(rule: _Rule, claimed: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The stretches of the rule no box or band claims, at least MIN_UNDERLINE long."""
    pieces, at = [], rule.lo
    for lo, hi in sorted(claimed):
        if lo - at >= MIN_UNDERLINE:
            pieces.append((at, lo))
        at = max(at, hi)
    if rule.hi - at >= MIN_UNDERLINE:
        pieces.append((at, rule.hi))
    return pieces


def _after_caption(area: BBox, lines: Sequence[Line], reach: float
                   ) -> tuple[BBox, Line | None]:
    """The area to write in after a caption printed into its left end, at most `reach` into
    it: a label over the start of its rule, the "$" before an amount. The area starts after
    the caption, on its row."""
    caption = max((l for l in lines if area[0] < l.x1 <= area[0] + reach
                   and area[1] - 2 <= (l.top + l.bottom) / 2 <= area[3] + 2
                   and l.x0 <= area[0] + 6 and not LEADER.fullmatch(l.text)),
                  key=lambda l: l.x1, default=None)
    if caption is None:
        return area, None
    return (caption.x1 + 1, max(area[1], caption.top - 1), area[2], area[3]), caption


_RANK = {Shape.SQUARE: 0, Shape.CIRCLE: 0, Shape.COMB: 1, Shape.CELL: 2, Shape.UNDERLINE: 3}


def _inside(a: BBox, b: BBox, share: float = 0.8) -> bool:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w > 0 and h > 0 and w * h >= share * (a[2] - a[0]) * (a[3] - a[1])


def propose(image: Image, lines: Sequence[Line], page: int) -> list[Candidate]:
    """Every candidate on one page, in (top, x0) order. `image` is the page in mode "L" at
    SCALE px per point."""
    return _propose(_Page(image, lines), page)


@dataclass(frozen=True)
class Survey:
    """What the rules read on one page, for the region pass (analyze.form_regions): the solid
    rules, each piece of ink no bigger than a letter, and the candidates. Boxes in points."""
    horizontal: tuple[BBox, ...]
    vertical: tuple[BBox, ...]
    glyphs: tuple[BBox, ...]
    candidates: tuple[Candidate, ...]


def survey(image: Image, lines: Sequence[Line], page: int) -> Survey:
    """`propose`, and the rules and glyphs it read the page by. `image` as for `propose`."""
    scan = _Page(image, lines)
    return Survey(tuple((r.lo, r.a, r.hi, r.b) for r in scan.h),
                  tuple((r.a, r.lo, r.b, r.hi) for r in scan.v),
                  tuple((g.x0 / SCALE, g.y0 / SCALE, g.x1 / SCALE, g.y1 / SCALE)
                        for g in scan.glyphs),
                  tuple(_propose(scan, page)))


def _propose(scan: _Page, page: int) -> list[Candidate]:
    boxes = scan.boxes()
    found, used = _squares_and_combs(scan, boxes, page)
    found += _circles(scan, page)
    found += _blank_boxes(scan, boxes, used, page)
    claims: _Claims = {}
    for box in boxes:
        for rule in (box.top, box.bottom):
            claims.setdefault(rule, []).append((box.bbox[0], box.bbox[2]))
    found += _cells(scan, boxes, claims, page)
    found += _underlines(scan, claims, page, boxes)
    # One place, one candidate: the surest, then the most specific shape.
    kept: list[Candidate] = []
    for c in sorted(found, key=lambda c: (len(c.doubts), _RANK[c.shape])):
        if all(geometry.iou(c.bbox, k.bbox) < 0.5 for k in kept):
            kept.append(c)
    # A ruled cell that holds an underline is the same blank, drawn larger: the underline is
    # the tighter one.
    kept = [c for c in kept if c.shape is not Shape.CELL or not any(
        k.shape is Shape.UNDERLINE and _inside(k.bbox, c.bbox) for k in kept)]
    if sum(c.certain for c in kept) < MIN_CERTAIN:
        kept = [replace(c, doubts=c.doubts | {Doubt.FEW_FIELDS}) for c in kept]
    return sorted(kept, key=lambda c: (c.bbox[1], c.bbox[0]))


# -- OCR repair -------------------------------------------------------------------------------

def scrub(lines: Sequence[Line], candidates: Sequence[Candidate]) -> list[Line]:
    """The page's lines with OCR noise that a kept candidate explains taken out: the glyphs
    OCR reads a checkbox square as ("O", "]", "[") and leader dots before an underline, so the
    form rules can bind the captions. Lines that touch no candidate come back unchanged.

    A row's square glyphs become MARKER, which OPTION_MARKER splits the row's option
    labels at, when there are as many of them as squares on the row; a line made only of
    leader dots (or the dash between a comb's runs) just left of a candidate is dropped, as is
    a scrap of dots and box glyphs ("- o") right before a square."""
    squares = [c.bbox for c in candidates if c.shape in (Shape.SQUARE, Shape.CIRCLE)]
    circles = [c.bbox for c in candidates if c.shape is Shape.CIRCLE]
    out: list[Line] = []
    for line in lines:
        words = line.text.split()
        if LEADER.fullmatch(line.text) and any(_caption_left(line, c.bbox, 2 * LEFT_GAP)
                                               for c in candidates):
            continue
        if all(LEADER.fullmatch(w) or BOX_GLYPHS.fullmatch(w) for w in words) and any(
                _caption_left(line, b, RIGHT_GAP) for b in squares):
            continue
        on_line = [b for b in squares if _on_row(line, b)
                   and line.x0 - 2 <= (b[0] + b[2]) / 2 <= line.x1]
        if on_line:
            junk = [i for i, w in enumerate(words) if BOX_GLYPHS.fullmatch(w)]
            # A round letter or digit with words on both sides ("uno o dos", "Type O negative")
            # is part of the sentence, not an answer circle's glyph.
            sentence = any(b in circles for b in on_line) and any(_between_words(words, i) for i in junk)
            if len(junk) == len(on_line) and not sentence:
                for i in junk:
                    words[i] = MARKER
                # The leader dots between a label and its square, read as "...", "_" or "•"
                # (the planner takes "•" for an option marker too).
                words = [w for w in words if not LEADER_TOKEN.fullmatch(w)]
                line = line.retext(" ".join(words))
        out.append(line)
    return out


def _between_words(words: list[str], i: int) -> bool:
    """The token is a lone letter or digit with a plain word right before it and one right after
    ("uno o dos"); a table row of figures and separators is not a sentence."""
    return (0 < i < len(words) - 1 and words[i].isalnum()
            and all(words[j].isalpha() and _worded_token(words[j]) for j in (i - 1, i + 1)))


def detect(pdf: Path, number: int, lines: Sequence[Line]) -> list[Candidate]:
    """Render page `number` of `pdf` at SCALE in grayscale and propose. Picklable, for the
    pool; the caller holds the pdfium lock when it runs in-process."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    try:
        page = doc[number - 1]
        try:
            image = page.render(scale=SCALE, grayscale=True).to_pil().convert("L")
        finally:
            page.close()
    finally:
        doc.close()
    return propose(image, lines, number)
