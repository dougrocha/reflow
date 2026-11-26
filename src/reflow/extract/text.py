"""Characters → lines: where a line or a word starts, raised marks, style runs, and the lines
of a page (page_lines)."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection
from statistics import median

import pypdfium2 as pdfium

from reflow.extract.chars import Char, read_chars
from reflow.extract.fonts import GlyphNames, font_family
from reflow.extract.lines import SOFT_HYPHEN, Line, StyleRun
from reflow.extract.rtl import logical_order
from reflow.leaders import REPLACEMENT, normalize_text
from reflow.pdfobjects import private_use

# Characters further apart than this many font sizes start a new segment, so text
# side by side in columns or table cells isn't glued into one line.
SEGMENT_GAP = 1.5


# Characters on each side of a gap that are sampled for the run's letter spacing.
TRACK_WINDOW = 4


# A gap, in font sizes, between two runs on one line that is a word space (see _run_break_is_space),
# and between two characters of a row that pdfium broke (see lines_from_chars).
RUN_SPACE = 0.15


# On a line whose runs break inside words SPLIT_RUNS times or more, the gap that is a word space
# (see _settle_gap_spaces).
SPLIT_RUNS = 2


SPLIT_RUN_SPACE = 0.25


# A character that starts this many font sizes left of the line's first character, and whose top
# is this share of its height off the character before it, starts a new line (see
# _starts_new_line).
BACKSTEP = 1.0


BACKSTEP_RISE = 0.25


# A character overlapping the current line vertically by less than this fraction of
# its height starts a new line, even when pdfium didn't insert a line break.
MIN_LINE_OVERLAP = 0.3


# An OCR layer lists a scanned table's cells in the order it read them, so the text can jump
# back across the page, and it spells the cell rules as runs of underscores. A jump back of
# more than OCR_BACKSTEP font sizes starts another line, and so does a run of at least
# OCR_RULE_RUN underscores: both mark the end of a cell. Only a jump back onto the line's start
# (or to another baseline) counts, and only a gap of OCR_CELL_GAP font sizes after the rule, so
# OCR ordering noise and a form blank in a sentence ("Name: ____ on file") stay one line.
OCR_BACKSTEP = 3.0


OCR_RULE_RUN = 3


OCR_CELL_GAP = 1.0


# Glyphs drawn smaller than this (points, after the text matrix) are invisible: no viewer
# shows them. Generators leave such hidden copies behind (p17 repeats its publication numbers
# at 0.01 pt in white). Dropped only where the page has visible text, see _visible_chars. No
# sample PDF sets a glyph between 0.5 and 2 pt except as the whole page's only size.
INVISIBLE_SIZE = 1.0


# Text that may not show: invisible (render mode 3) or filled at most this dark (white, or
# transparent). Such text is dropped when its box renders as one flat colour (see _unseen).
LIGHT_INK = 0.1


# The scale of the render _unseen looks at (1 is 72 dpi), and the spread of each colour
# channel, 0 to 255, under which a box is flat: no glyph and nothing drawn under it shows there.
SEE_SCALE = 1.0


FLAT_SPREAD = 8


# An OCR layer's boxes can sit a point off the ink they were read from (naca-ocr): an invisible
# line's box is looked at with this margin (points) on every side.
LAYER_MARGIN = 1.0


# Glyphs that mark a note when they are set small and raised (see _ends_marker).
FOOTNOTE_MARKS = frozenset("0123456789*†‡")


SUP_SYMBOLS = frozenset("§¶")


# A superscript is set at most this share of the line's size and sits above the others'
# baseline by more than SUP_RAISE of that size.
SUP_SIZE = 0.8


SUP_RAISE = 0.2


# Font families (the name after the subset prefix, spaces and hyphens removed) whose letters
# are pictures: check boxes, bullets, arrows. A run in one is never emphasis.
SYMBOL_FONTS = ("wingdings", "webdings", "zapfdingbats", "symbol")


def _tracking(chars: list[Char | None], i: int) -> float:
    """The run's own letter spacing (tracking, the PDF Tc operator) near chars[i]: the median
    gap between neighbouring characters of the same text object on the same line, leaving out
    the gap being judged. Tracking is set per show operator, so gaps between separate text
    objects (a row of separately placed tick labels) never count. Untracked text is near zero."""
    here = chars[i]
    prev = chars[i - 1] if i else None
    if here is None or prev is None or prev.run is None or prev.run != here.run:
        return 0.0  # the gap being judged crosses text objects: no run to take spacing from
    gaps = []
    for k in range(max(1, i - TRACK_WINDOW), min(len(chars), i + TRACK_WINDOW + 1)):
        a, b = chars[k - 1], chars[k]
        if k == i or a is None or b is None or a.rotated or b.rotated:
            continue
        if a.run != here.run or b.run != here.run:
            continue
        if min(a.bottom, b.bottom) - max(a.top, b.top) <= 0:
            continue  # not on the same line
        gap = b.x0 - a.x1
        if gap >= -0.5 * max(a.size, b.size, 1.0):
            gaps.append(gap)
    return max(median(gaps), 0.0) if gaps else 0.0


def _run_break_is_space(prev: Char, c: Char) -> bool:
    """Whether two characters on one line, drawn by different text objects (runs), are
    separated by a word space. A run starts at its own position, so a space the author typed
    can fall between two runs of differing style (a bold or tracked label), and the gap is then
    the space alone. Within one run the gap is set by the font and tracking, and the gap
    between runs has no tracking, so a gap of RUN_SPACE font sizes or more is a space.
    Invisible OCR runs have approximate boxes that can leave such gaps inside a word;
    preserve their text layer's spacing instead. A smaller, vertically shifted run is a
    superscript or subscript attached to its neighbour, not another word. Infer word spaces
    only between letters, numbers or combining marks, not punctuation, leaders or operators."""
    if prev.run is None or c.run is None or prev.run == c.run or prev.rotated or c.rotated:
        return False
    if prev.invisible or c.invisible:
        return False
    size = max(c.size, prev.size, 1.0)
    if min(c.size, prev.size) <= 0.8 * size and abs(c.bottom - prev.bottom) > 0.15 * size:
        return False
    if any(unicodedata.category(ch)[0] not in "LMN" for ch in (prev.ch[-1], c.ch[0])):
        return False
    return c.x0 - prev.x1 > RUN_SPACE * size


def _settle_gap_spaces(line: list[Char]) -> None:
    """Withdraw the word spaces _run_break_is_space inferred (Char.gap_space) on a line set glyph
    by glyph, where pdfium, which places the glyphs by their advances, saw none:
    some run's boxes are even shares of its width (Char.spread), or runs break inside words at
    least SPLIT_RUNS times (one letter touching the next, from another text object). The gaps
    between runs there carry the letter spacing and the error of the boxes, so only a gap of
    SPLIT_RUN_SPACE font sizes is a space ("POL ICY HOL DER" and "Infor mation" are one word)."""
    def gap(a: Char, b: Char) -> float:
        return (b.x0 - a.x1) / max(a.size, b.size, 1.0)

    pairs = list(zip(line, line[1:]))
    if not any(b.gap_space for _, b in pairs):
        return
    spread = any(c.spread for c in line)
    splits = sum(1 for a, b in pairs
                 if a.run != b.run and not b.space_before and a.ch[-1:].isalnum()
                 and b.ch[:1].isalnum() and gap(a, b) <= RUN_SPACE)
    if not spread and splits < SPLIT_RUNS:
        return
    for a, b in pairs:
        if b.gap_space and gap(a, b) <= SPLIT_RUN_SPACE:
            b.space_before = b.gap_space = False


def _ocr_cell_break(cur: list[Char], c: Char) -> bool:
    """Whether OCR text `c` begins another cell, not the line `cur`: the text went back
    left by several font sizes, or the line has just drawn a rule of underscores and `c` is
    not part of it. Scanned tables are the only place both happen."""
    prev = cur[-1]
    if c.rotated or prev.rotated:
        return False
    size = max(c.size, prev.size, 1.0)
    if (c.x0 < prev.x1 - OCR_BACKSTEP * size
            and sum(ch.ch.isalnum() for ch in cur) >= OCR_RULE_RUN):
        # Back to the line's start, or onto another baseline: a new cell. A jump back that
        # lands inside the line on the same baseline is OCR ordering noise.
        shifted = abs((c.top + c.bottom) - (prev.top + prev.bottom)) > 0.6 * size
        if shifted or c.x0 <= min(ch.x0 for ch in cur) + size:
            return True  # a stray mark ahead of the text is not a cell
    if (c.ch.strip("_") and prev.ch == "_" and len(cur) >= OCR_RULE_RUN
            and c.x0 - prev.x1 > OCR_CELL_GAP * size):
        return all(ch.ch == "_" for ch in cur[-OCR_RULE_RUN:])
    return False


def _currency_slot(cur: list[Char], c: Char) -> int | None:
    """Where in the line `cur` a currency sign `c` belongs, when it was drawn after the figures
    it stands before. A spreadsheet export draws a cell's "$" as a text object of its own, after
    the figure of the cell on its left, so the stream reads "8,426,474 $" for "$ 8,426,474".
    The sign lies wholly left of the characters drawn just before it, on the same baseline: it
    goes before them, in the order it is printed in. Any other character keeps the stream's
    order, which the author may have meant (right-to-left text, overprinted marks)."""
    prev = cur[-1]
    if (unicodedata.category(c.ch[:1]) != "Sc" or c.rotated or prev.rotated or c.invisible
            or prev.invisible or c.run is None or prev.run is None or c.run == prev.run
            or c.x1 > prev.x0):
        return None
    at = len(cur)
    while at and cur[at - 1].x0 >= c.x1 - 0.5:
        at -= 1
    return at


# A currency sign set this many font sizes after a figure, by another text object, is the sign of
# the next cell, not a sign after this figure ("124,544 $ 106,865": the $ stands before 106,865).
SIGN_GAP = 0.4


def _is_next_cells_sign(prev: Char, c: Char, nxt: Char | None) -> bool:
    """Whether the currency sign `c` stands before the figure `nxt` that is drawn after it, not
    after the figure `prev`: spreadsheet exports draw each cell's "$" as an object of its own,
    in the cell's left edge, and it can come just after the figure of the cell to its left. A
    sign that ends the line ("3(a) $", the blank of a form) is no cell's sign."""
    return (unicodedata.category(c.ch[:1]) == "Sc" and prev.ch[-1:].isdigit()
            and nxt is not None and (nxt.ch[:1].isdigit() or nxt.ch[:1] in "(-–−" + SOFT_HYPHEN)
            and nxt.x0 > c.x1
            and not (c.rotated or prev.rotated or c.invisible or prev.invisible)
            and c.run is not None and prev.run is not None and c.run != prev.run
            and c.x0 - prev.x1 > SIGN_GAP * max(c.size, prev.size, 1.0))


def _right_to_left(ch: str) -> bool:
    """Whether the character reads right to left (Hebrew, Arabic): its line runs backwards, and its
    marks sit above and below it."""
    return unicodedata.bidirectional(ch[:1]) in ("R", "AL", "AN")


# A spreadsheet export draws a table row's figures as text objects in no left-to-right order: the
# second cell's before the first's, and the last digit of a figure after the next cell's. The
# figures go on lines of their own (see _jumps_back_in_a_figure), and a stray tail of digits is
# joined to the figure it abuts (see _join_figure_tails).
FIGURE_END = frozenset("0123456789.,%)")
FIGURE_START = frozenset("0123456789(-\u2013\u2212" + SOFT_HYPHEN)
FIGURE_TAIL = 3  # a figure's tail is at most this many characters
TAIL_GAP = 0.5   # points: a tail starts this close to the figure's end, or is no tail of it


def _ends_no_figure(cur: list[Char]) -> bool:
    """Whether the characters `cur` end with the "N/A" a table's cell holds in place of a figure."""
    return "".join(c.ch for c in cur[-3:]).upper() == "N/A" and (len(cur) == 3 or cur[-3].space_before)


def _jumps_back_in_a_figure(cur: list[Char], c: Char) -> bool:
    """Whether `c`, the start of a figure drawn by another text object, lies wholly left of the
    end of the figure, or the "N/A" in place of one, that the characters `cur` end: the objects
    were drawn out of order, so the text before and after the jump are cells of their own, not
    one line read right to left. A currency sign is placed by _currency_slot."""
    prev = cur[-1]
    return (c.run is not None and prev.run is not None and c.run != prev.run
            and (prev.ch[-1:] in FIGURE_END or _ends_no_figure(cur)) and c.ch[:1] in FIGURE_START
            and not (c.rotated or prev.rotated or c.invisible or prev.invisible)
            and c.x1 <= prev.x0)


def _is_tail_of(host: list[Char], tail: list[Char]) -> bool:
    """Whether the characters `tail` are the end of the figure that `host` ends: a few digits or
    marks on the same baseline, starting where the figure stops."""
    end, first = host[-1], tail[0]
    return (len(tail) <= FIGURE_TAIL and all(c.ch in FIGURE_END for c in tail)
            and end.ch[-1:] in FIGURE_END and not (end.rotated or first.rotated)
            and abs(first.x0 - end.x1) <= TAIL_GAP
            and min(end.bottom, first.bottom) - max(end.top, first.top)
            > 0.5 * max(first.bottom - first.top, 0.1))


def _starts_new_line(cur: list[Char], c: Char, tracking: float | Callable[[], float] = 0.0,
                     extent: tuple[float, float] | None = None) -> bool:
    """Whether `c` starts a new line after the characters `cur`. `tracking` is the run's
    letter spacing, or a function that reads it (only a gap wider than untracked text allows
    needs it). `extent` is the (top, bottom) of `cur`, when the caller keeps it as it goes."""
    prev = cur[-1]
    if prev.invisible and c.invisible and _ocr_cell_break(cur, c):
        return True
    if (c.run != prev.run and c.run is not None and prev.run is not None
            and c.size >= 2.5 * prev.size and c.x0 < prev.x0 - c.size):
        return True
    # Text that restarts left of the line's own start, and lower or higher than the character
    # before it, is not the line's continuation: pdfium lists the text objects of a ruled form's
    # boxes in the order they were drawn, and "Fee(s)" at the right edge of one box, set a few
    # points above the baseline of the next, is followed by the numbers "1 2 3" at the left edge
    # of another. Text restarted on the same baseline is left alone (fake-bold text, drawn twice,
    # a title drawn after its subtitle): OCR layers and RTL runs list their words that way.
    if (c.x0 < cur[0].x0 - BACKSTEP * max(c.size, prev.size, 1.0) and not c.invisible and not prev.invisible
            and abs(c.top - prev.top) >= BACKSTEP_RISE * max(c.bottom - c.top, 0.1)
            and not (_right_to_left(c.ch) or _right_to_left(prev.ch))):
        return True
    # Tracking widens every gap by Tc, and a word gap by about twice that, so the limit
    # grows with the run's spacing instead of staying at a fixed fraction of the font size.
    limit = SEGMENT_GAP * max(c.size, prev.size, 1.0)
    gap = c.x0 - prev.x1
    if gap > limit:  # tracking is never negative: a gap within `limit` is within any limit
        if callable(tracking):
            tracking = tracking()
        if gap > limit + 2 * tracking:
            return True
    if extent is None:
        extent = (min(ch.top for ch in cur), max(ch.bottom for ch in cur))
    top, bottom = extent
    overlap = min(bottom, c.bottom) - max(top, c.top)
    return overlap < MIN_LINE_OVERLAP * max(c.bottom - c.top, 0.1)


MATH_BEFORE_SUP = "=+-\u2212\u00d7\u00f7*/^)]}"  # a raised letter right after these is an exponent


def _raised_runs(chars: list[Char], starts: list[int], size: float) -> tuple[tuple[int, int], ...]:
    """Offsets in the line text of runs of footnote-marker glyphs set small and raised: at most
    SUP_SIZE of the line's size, with their bottom above the baseline of the line's other glyphs
    by more than SUP_RAISE of that size. A line made only of small glyphs has no baseline to
    compare with, so it has none."""
    normal = [c.bottom for c in chars if c.size > SUP_SIZE * size]
    if not normal:
        return ()
    baseline = median(normal)
    runs: list[tuple[int, int]] = []
    for i, c in enumerate(chars):
        if (c.size <= SUP_SIZE * size and c.bottom < baseline - SUP_RAISE * size
                and (c.ch in FOOTNOTE_MARKS or c.ch in SUP_SYMBOLS or c.ch.isalpha())):
            end = starts[i] + len(c.ch)
            if runs and runs[-1][1] == starts[i] and not c.space_before:
                runs[-1] = (runs[-1][0], end)
            else:
                runs.append((starts[i], end))
    return tuple(r for r in runs if not _glued_letters(chars, starts, r))


def _glued_letters(chars: list[Char], starts: list[int], run: tuple[int, int]) -> bool:
    """Whether a raised run of letters is not a note marker: an ordinal suffix (1st, 2nd, 3rd,
    4th) or a letter set against a digit or a math sign (an exponent: 10n, x=2a)."""
    first = next(i for i, st in enumerate(starts) if st == run[0])
    if not chars[first].ch.isalpha():
        return False
    if first == 0 or chars[first].space_before:
        return False
    prev = chars[first - 1].ch
    return prev.isdigit() or prev in MATH_BEFORE_SUP


def _symbol_font(font: str) -> bool:
    return font_family(font).startswith(SYMBOL_FONTS)


def _style_runs(chars: list[Char], starts: list[int], end: int) -> tuple[StyleRun, ...]:
    """The maximal runs of one (bold, italic, symbol-font, accent) style in a line's text. A
    space takes the style of the character before it. A line of one style has no runs, unless
    that style is a symbol font's or an accent."""
    runs: list[StyleRun] = []
    for c, start in zip(chars, starts):
        style = (c.bold, c.italic, _symbol_font(c.font), c.accent)
        if runs and style == runs[-1][2:]:
            continue
        if runs:
            runs[-1] = runs[-1]._replace(end=start)
        runs.append(StyleRun(start if runs else 0, end, *style))
    return tuple(runs) if len(runs) > 1 or runs[0].symbol or runs[0].accent else ()


LEGACY_FONT = re.compile(r"navajo", re.IGNORECASE)


# Fonts that draw pictures from the Private Use Area by design, by a part of the family name
# (font_family): the symbol fonts, and the icon fonts web pages print with. A private-use
# reading in one is a picture, not a lost letter.
ICON_FONTS = ("symbol", "wingdings", "webdings", "zapfdingbats", "fontawesome", "materialsymbols",
              "materialicons", "codicon", "icomoon", "glyphicons", "octicons", "ionicons",
              "dashicons")


def _icon_font(font: str) -> bool:
    family = font_family(font)
    return any(name in family for name in ICON_FONTS)


def _private_use_errors(chars: list[Char]) -> int:
    """The private-use characters of a line that read where letters should: in a word (between
    spaces) of two or more letters, digits or private-use characters, outside an icon font.
    A text font whose /ToUnicode reads into the Private Use Area maps its letters to nothing;
    a lone private-use glyph between two words is an icon or a bullet, whatever its font."""
    found = 0
    word: list[Char] = []

    def end() -> None:
        nonlocal found
        if sum(c.ch.isalnum() or private_use(c.ch) for c in word) >= 2:
            found += sum(1 for c in word if private_use(c.ch) and not c.unicode_error
                         and not _icon_font(c.font))
        word.clear()

    for c in chars:
        if c.space_before or c.ch.isspace():
            end()
        if not c.ch.isspace():
            word.append(c)
    end()
    return found


def _unicode_errors(text: str, chars: list[Char]) -> int:
    """How many characters of a line cannot be trusted. pdfium flags the glyphs it could not
    map (`unicode_error`), but it also hands back a bare U+FFFD for a lost vowel sign or mark
    without flagging it, so a U+FFFD that is not part of a dot leader counts too. So does a
    Private Use Area reading in a word, outside an icon font (`_private_use_errors`). Every
    character set in a legacy-encoded font (one named for Navajo: its letters sit on digits and
    brackets) counts: its text layer is wrong by construction."""
    flagged = sum(c.unicode_error for c in chars if c.ch != REPLACEMENT)
    lost = max(sum(c.unicode_error for c in chars if c.ch == REPLACEMENT),
               normalize_text(text)[2])
    legacy = sum(1 for c in chars if LEGACY_FONT.search(c.font) and not c.ch.isspace())
    return flagged + lost + _private_use_errors(chars) + legacy


def _make_line(page: int, chars: list[Char]) -> Line:
    parts, starts, at = [], [], 0
    for i, c in enumerate(chars):
        lead = " " if c.space_before and i else ""
        starts.append(at + len(lead))
        parts.append(lead + c.ch)
        at += len(lead) + len(c.ch)
    text = "".join(parts)
    weights = Counter()
    for c in chars:
        weights[c.size] += 1
    # Nearly all bold, so a run-in bold lead-in ("Note. The rest...") is not a bold line.
    bold = sum(c.bold for c in chars) / len(chars) >= 0.9
    italic = sum(c.italic for c in chars) / len(chars) >= 0.6
    return Line(
        runs=_style_runs(chars, starts, len(text)),
        page=page,
        bbox=(round(min(c.x0 for c in chars), 2), round(min(c.top for c in chars), 2),
              round(max(c.x1 for c in chars), 2), round(max(c.bottom for c in chars), 2)),
        text=text,
        size=weights.most_common(1)[0][0],
        sup=_raised_runs(chars, starts, weights.most_common(1)[0][0]),
        bold=bold,
        italic=italic,
        font=Counter(c.font for c in chars).most_common(1)[0][0],
        unicode_errors=_unicode_errors(text, chars),
        rotated=chars[0].rotated,
        turn=_turn(chars[0].angle) if chars[0].rotated else 0,
        angle=chars[0].angle,
        ink=round(median(c.ink for c in chars), 3),
        ocr=any(c.invisible for c in chars),
    )


def _turn(angle: float) -> int:
    """Reading direction of rotated text from its baseline angle: 1 upward (about 90°), -1
    downward (about 270°), 2 upside down (about 180°), 0 for any other angle."""
    if math.pi / 4 < angle < 3 * math.pi / 4:
        return 1
    if 5 * math.pi / 4 < angle < 7 * math.pi / 4:
        return -1
    if 3 * math.pi / 4 <= angle <= 5 * math.pi / 4:
        return 2
    return 0


def _starts_rotated_line(cur: list[Char], c: Char) -> bool:
    """Whether a rotated character starts a new row of rotated text. Rotated text runs along its
    baseline, so rows are side by side: a character starts a row when it lies outside the
    row's columns, or when it sits beyond the row's end along the baseline (another run)."""
    lo, hi = min(ch.x0 for ch in cur), max(ch.x1 for ch in cur)
    if min(hi, c.x1) - max(lo, c.x0) < MIN_LINE_OVERLAP * max(c.x1 - c.x0, 0.1):
        return True
    prev = cur[-1]
    return _baseline_gap(prev, c) > SEGMENT_GAP * max(c.size, prev.size, 1.0)


def _baseline_gap(prev: Char, c: Char) -> float:
    """The gap between two characters of a line along its baseline, in points: from the end of
    `prev` to the start of `c` in the reading direction, upright or rotated."""
    if not c.rotated:
        return c.x0 - prev.x1
    dx, dy = math.cos(c.angle), -math.sin(c.angle)  # reading direction in top-left coordinates
    pcx, pcy = (prev.x0 + prev.x1) / 2, (prev.top + prev.bottom) / 2
    ccx, ccy = (c.x0 + c.x1) / 2, (c.top + c.bottom) / 2
    extent = lambda ch: abs(dx) * (ch.x1 - ch.x0) + abs(dy) * (ch.bottom - ch.top)  # noqa: E731
    return (ccx - pcx) * dx + (ccy - pcy) * dy - (extent(prev) + extent(c)) / 2


def _ends_marker(prev: Char, c: Char) -> bool:
    """Whether `c` starts the text that a footnote marker `prev` precedes: a digit or symbol set
    small and raised, right before a letter set larger ("4Deeper": the 6 pt "4" of a note sits
    above the 8 pt line). Only the space between them is at stake; the marker stays where it is."""
    return (prev.ch in FOOTNOTE_MARKS and c.ch.isalpha() and not prev.rotated and not c.rotated
            and prev.size <= 0.8 * c.size and prev.bottom < c.bottom - 0.15 * c.size)


def _visible_chars(chars: list[Char | None], page: pdfium.PdfPage | None = None
                   ) -> list[Char | None]:
    """Drop the glyphs no reader can see: tiny ones (see INVISIBLE_SIZE), and, given the page,
    the text it renders as nothing (_unseen). Tiny text is removed only when the page also has
    visible text, so a page whose only text is tiny stays as it is. Text in render mode 3
    (invisible, as in a scan's OCR layer) over the scan's ink is kept: on a scanned page the
    OCR layer is the only text there is."""
    sized, unseen = _hiding(chars, page)
    if not unseen and not (sized and any(c is not None and c.size < INVISIBLE_SIZE for c in chars)):
        return chars
    return [c for c in chars if c is None or not _hidden(c, sized, unseen)]


def _hiding(chars: list[Char | None], page: pdfium.PdfPage | None) -> tuple[bool, set[int]]:
    """Whether the page has text of a visible size, and the ids of its unseen chars (_unseen)."""
    sized = any(c is not None and c.size >= INVISIBLE_SIZE for c in chars)
    return sized, _unseen(page, chars) if page is not None else set()


def _hidden(c: Char, sized: bool, unseen: set[int]) -> bool:
    return (sized and c.size < INVISIBLE_SIZE) or id(c) in unseen


def hidden_indices(page: pdfium.PdfPage) -> list[int]:
    """The textpage indices of the characters page_lines leaves out as hidden (_visible_chars),
    so that another reader of the page (pdfplumber's chars in a ruled table) can leave out the
    same glyphs."""
    chars = list(read_chars(page))
    sized, unseen = _hiding(chars, page)
    return [c.index for c in chars if c is not None and _hidden(c, sized, unseen)]


def _unseen(page: pdfium.PdfPage, chars: list[Char | None]) -> set[int]:
    """The ids of the chars a reader cannot see: invisible ones (render mode 3), and white or
    transparent ones (LIGHT_INK), whose word's box the rendered page fills with one flat colour
    (FLAT_SPREAD). An invisible word draws nothing, so its box is flat when nothing is drawn
    under it: a scan's OCR layer lies over the scan's ink and stays, and a line hidden on a blank
    spot goes. Each word is judged on its own, so a dot of ink under one word keeps only that
    word, and with a margin (LAYER_MARGIN), since an OCR layer's box can sit off its ink. A mark
    with under two letters or digits (a comma, a lone "I") is judged with its whole line: an OCR
    layer spaces its punctuation off as words, and their boxes miss the ink more often (naca-ocr). A white word draws its glyphs, so its box is flat when they
    fall on white; on a dark fill they show. A word off the page is unseen.

    A page that renders as nothing at all keeps its text, as a page of only tiny text does: an
    OCR layer whose image pdfium cannot draw, or that was kept without it, is all the page has.
    So does a page whose crop box reaches past its media box: pdfium renders only their
    overlap, and the boxes would not line up. The page is rendered only when it has such text."""
    groups: list[tuple[bool, int, list[Char]]] = []   # (invisible, line, the word's chars)
    lines: dict[int, list[Char]] = {}                  # each line's invisible chars
    current: list[Char] | None = None
    kind: tuple | None = None
    line = 0
    for c in chars:
        if c is None:
            line += 1
        if c is None or not (c.invisible or (c.run is not None and c.ink <= LIGHT_INK)):
            current, kind = None, None
            continue
        here = ("invisible",) if c.invisible else ("light", c.run)
        if current is None or c.space_before or here != kind:
            current, kind = [], here
            groups.append((c.invisible, line, current))
        current.append(c)
        if c.invisible:
            lines.setdefault(line, []).append(c)
    if not groups:
        return set()
    # pdfium renders the crop box's overlap with the media box (resolved, inherited or not).
    if any(abs(a - b) > 0.01 for a, b in zip(page.get_cropbox(), page.get_bbox())):
        return set()
    image = page.render(scale=SEE_SCALE).to_numpy()
    if _flat(image):
        return set()
    height, width = image.shape[:2]

    def seen(group: list[Char], margin: float) -> bool:
        """Whether the box of `group` shows anything."""
        x0, top = min(c.x0 for c in group) - margin, min(c.top for c in group) - margin
        x1, bottom = max(c.x1 for c in group) + margin, max(c.bottom for c in group) + margin
        if x1 <= 0 or bottom <= 0 or x0 * SEE_SCALE >= width or top * SEE_SCALE >= height:
            return False   # off the page
        left, right = max(math.ceil(x0 * SEE_SCALE), 0), min(math.floor(x1 * SEE_SCALE), width)
        up, down = max(math.ceil(top * SEE_SCALE), 0), min(math.floor(bottom * SEE_SCALE), height)
        if left >= right or up >= down:   # under a pixel inside: take the pixels it touches
            left, right = max(math.floor(x0 * SEE_SCALE), 0), min(math.ceil(x1 * SEE_SCALE), width)
            up, down = max(math.floor(top * SEE_SCALE), 0), min(math.ceil(bottom * SEE_SCALE), height)
        return not _flat(image[up:down, left:right])

    unseen: set[int] = set()
    for invisible, line, group in groups:
        shown = seen(group, LAYER_MARGIN if invisible else 0.0)
        if not shown and invisible and sum(c.ch.isalnum() for c in group) < 2:
            shown = seen(lines[line], LAYER_MARGIN)
        if not shown:
            unseen.update(id(c) for c in group)
    return unseen


def _flat(image) -> bool:
    """Whether a rendered patch is one colour: no channel spreads more than FLAT_SPREAD."""
    pixels = image.reshape(-1, image.shape[-1])
    return int((pixels.max(axis=0) - pixels.min(axis=0)).max()) <= FLAT_SPREAD


# An "f" at least this much wider than the same font's usual "f" is a ligature glyph (fi, fl)
# whose text mapping kept only the "f". Measured: 5.0pt against 3.6pt at 9pt.
WIDE_F = 1.25


# Samples of the usual width needed before a wider "f" counts as evidence.
WIDE_F_MIN_USUAL = 4


# An "f" at least this much wider than the advance its font declares for "f" is a ligature
# glyph, on a page with too few f's to learn the usual width from. Wider than WIDE_F: pdfium's
# box takes in a glyph's overhang, and an upright f's hook can overhang its advance by a
# quarter (fw9's HelveticaNeue Italic: 375 against 296); a fi or fl is two thirds wider or
# more (Helvetica 500 against 278, Times 556 against 333).
WIDE_F_DECLARED = 1.5


# Letters of a font, at one size, other than "f" that fix the scale its declared widths are
# drawn at (see _drawn_scale) before a declared width is measured against.
CALIBRATE_MIN = 8


def _wide_f(chars: list[Char | None],
            widths: Callable[[str], dict[str, float] | None] | None = None) -> set[int]:
    """ids of the "f" characters drawn wider than their font's usual "f" at that size. The
    usual width is learned from the page: the one that WIDE_F_MIN_USUAL or more of the font's
    f's share. A font with too few f's to learn from is measured against the width it declares
    for its "f" (`widths(font)`, per 1000 em by the character: GlyphNames.widths), drawn at the
    scale its other letters are (`_drawn_scale`): a page of two f's and two ligature glyphs
    read as "f" has no usual width to learn. An italic f's hook overhangs its advance, so
    italics are only ever measured against each other."""
    seen_f: dict[tuple[str, float], Counter] = {}
    for c in chars:
        if c is not None and c.ch == "f" and not c.rotated:
            seen_f.setdefault((c.font, round(c.size, 1)), Counter())[round(c.x1 - c.x0, 1)] += 1
    usual: dict[tuple[str, float], tuple[float, float]] = {}   # key -> (width, the ratio over it)
    calibrated: dict[tuple[str, float], tuple[float, dict[str, float]]] = {}   # (scale, declared)
    for key, seen in seen_f.items():
        width, count = seen.most_common(1)[0]
        if count >= WIDE_F_MIN_USUAL and count >= 0.5 * sum(seen.values()):
            usual[key] = width, WIDE_F
        elif widths is not None and (declared := widths(key[0])) and "f" in declared:
            scale = _drawn_scale(chars, key, declared)
            if scale is not None:
                usual[key] = declared["f"] * key[1] / 1000 * scale, WIDE_F_DECLARED
                calibrated[key] = scale, declared
    wide = set()
    for c in chars:
        if c is None or c.ch != "f" or (c.font, round(c.size, 1)) not in usual:
            continue
        key = (c.font, round(c.size, 1))
        width, ratio = usual[key]
        if c.x1 - c.x0 < ratio * width:
            continue
        if ratio == WIDE_F_DECLARED and (c.italic or not _run_at_scale(chars, c, *calibrated[key])):
            continue
        wide.add(id(c))
    return wide


def _ratios(chars: list[Char | None], declared: dict[str, float], size: float,
            keep: Callable[[Char], bool]) -> list[float]:
    """Drawn width over declared width, sorted, for the letters other than "f" that `keep`
    and the font declares."""
    return sorted((c.x1 - c.x0) / (declared[c.ch] * size / 1000) for c in chars
                  if c is not None and c.ch != "f" and c.ch in declared and not c.rotated
                  and not c.spread and keep(c))


def _drawn_scale(chars: list[Char | None], key: tuple[str, float],
                 declared: dict[str, float]) -> float | None:
    """How much wider the font's letters are drawn at this size than it declares them: the
    median, over CALIBRATE_MIN or more of its letters other than "f", of the drawn width over
    the declared one. Horizontal scaling (Tz) and a stretched text matrix are in a glyph's
    drawn width and not in its size, and a /Widths table can be off its glyphs by one factor:
    either would make every "f" look wide. None with too few letters to tell."""
    font, size = key
    ratios = _ratios(chars, declared, size,
                     lambda c: c.font == font and round(c.size, 1) == size)
    if len(ratios) < CALIBRATE_MIN:
        return None
    return ratios[len(ratios) // 2]


# A declared width is measured against only where the f's own text object draws its other
# letters within this share of the page's scale: a run set with its own Tz, or a text matrix
# that stretches a smaller size to the same drawn size, is not at the page's scale.
RUN_SCALE = 0.2


def _run_at_scale(chars: list[Char | None], f: Char, scale: float,
                  declared: dict[str, float]) -> bool:
    """Whether the text object that drew `f` draws its other letters at `scale`, the page's
    (`_drawn_scale`), by the median of their ratios. False for an object with no other letter
    the font declares (one glyph per show operator): nothing says what it is drawn at."""
    if f.run is None:
        return False
    ratios = _ratios(chars, declared, round(f.size, 1), lambda c: c.run == f.run)
    return bool(ratios) and abs(ratios[len(ratios) // 2] - scale) <= RUN_SCALE * scale


def _dropped_ligature_words(line: list[Char], wide: set[int]) -> tuple[str, ...]:
    """The lowercase words of `line` with a wide "f" not followed by the "i" or "f" an
    intact ligature would show. An "l" shows an intact fl only in the "f"'s own box: pdfium
    reads the glyph as two characters in one box, while a lost "i" leaves the next letter
    in a box of its own ("flled" for "filled")."""
    found: list[str] = []
    word: list[Char] = []

    def intact(i: int) -> bool:
        nxt = word[i + 1] if i + 1 < len(word) else None
        return nxt is not None and (nxt.ch in "if" or (
            nxt.ch == "l" and abs(nxt.x0 - word[i].x0) < 0.5 and abs(nxt.x1 - word[i].x1) < 0.5))

    def end() -> None:
        text = "".join(c.ch for c in word)
        if (any(id(c) in wide for c in word) and text.isalpha() and text.islower()
                and text not in found
                and not any(id(c) in wide and intact(i) for i, c in enumerate(word))):
            found.append(text)
        word.clear()

    for c in line:
        if c.space_before:
            end()
        word.append(c)
    end()
    return tuple(found)


def page_lines(page: pdfium.PdfPage, number: int, glyphs: GlyphNames | None = None,
               omit: Collection[int] = frozenset()) -> list[Line]:
    """The page's lines. `omit`: the textpage indices of the characters of the words of the
    page's own OCR layer that its grades leave out (unreadable_layer): they are not read, as
    reflow's own OCR never writes such a word into its layer."""
    chars = _visible_chars(list(read_chars(page, glyphs, number)), page)
    if omit:
        chars = [c for c in chars if c is None or c.index not in omit]
    logical_order(chars)
    widths = (lambda font: glyphs.widths(number, font)) if glyphs is not None else None
    return lines_from_chars(chars, number, widths)


def lines_from_chars(chars: list[Char | None], number: int,
                     widths: Callable[[str], dict[str, float] | None] | None = None
                     ) -> list[Line]:
    """The lines of a page's characters, in logical order, `None` marking where pdfium inserted
    a line break: the part of page_lines that reads no PDF. `widths` is what a font declares
    for its letters' advances, for the ligature check (`_wide_f`)."""
    made: list[list[Char]] = []  # the characters of each line, in order
    cur: list[Char] = []
    top = bottom = 0.0  # the vertical extent of `cur`, kept as characters join it
    broke = False  # pdfium inserted a line break since the last char
    moved = False  # the last character went before others of the line (see _currency_slot)

    def flush() -> None:
        nonlocal cur
        if cur:
            made.append(cur)
            cur = []

    wide = _wide_f(chars, widths)
    for i, c in enumerate(chars):
        if c is None:
            broke = True
            continue
        if cur and c.rotated != cur[0].rotated:
            flush()
        elif cur and c.rotated:
            if _starts_rotated_line(cur, c):
                flush()  # rotated text: a row is a column of its own, see _starts_rotated_line
        elif cur and _starts_new_line(cur, c, lambda i=i: _tracking(chars, i), (top, bottom)):
            flush()
        elif cur and _is_next_cells_sign(cur[-1], c, chars[i + 1] if i + 1 < len(chars) else None):
            flush()  # the sign of the next cell of a table row, see _is_next_cells_sign
        elif cur and _jumps_back_in_a_figure(cur, c):
            flush()  # a cell drawn before the one on its left, see _jumps_back_in_a_figure
        elif cur and broke and c.x0 < cur[-1].x1 - 0.5:
            flush()  # a real break: the text went back left
        if cur and broke and not c.space_before:
            # pdfium broke the line mid-row (after a superscript, or between glyphs each shown
            # by its own operator) and dropped the space it saw: the gap says whether one is there
            c.space_before = _baseline_gap(cur[-1], c) > RUN_SPACE * max(c.size, cur[-1].size, 1.0)
        if cur and _ends_marker(cur[-1], c):
            c.space_before = True
        if cur and not c.space_before and not c.inked_gap and _run_break_is_space(cur[-1], c):
            c.space_before = c.gap_space = True  # a word space between two runs of differing style
        broke = False
        if cur:
            top, bottom = min(top, c.top), max(bottom, c.bottom)
        else:
            top, bottom = c.top, c.bottom
        if moved and cur and abs(c.x0 - cur[-1].x1) <= TAIL_GAP:
            c.space_before = False  # it follows the figure by its place, not the sign it came after
        slot = _currency_slot(cur, c) if cur else None
        moved = slot is not None and slot < len(cur)
        cur.insert(len(cur) if slot is None else slot, c)
    flush()
    lines: list[Line] = []
    for chars_of in _join_figure_tails(made):
        _settle_gap_spaces(chars_of)
        line = _make_line(number, chars_of)
        if wide:
            line.ligature_words = _dropped_ligature_words(chars_of, wide)
        lines.append(line)
    return lines


TAIL_LOOKBACK = 8  # lines: the figure a tail ends was drawn at most this many lines before it


def _join_figure_tails(made: list[list[Char]]) -> list[list[Char]]:
    """The lines' characters with each stray tail of digits joined to the figure it continues
    (see _jumps_back_in_a_figure): the text object that ends a figure came after the next
    cell's objects, so a line of its own starts where the figure stops."""
    out: list[list[Char]] = []
    for chars_of in made:
        host = next((h for h in reversed(out[-TAIL_LOOKBACK:]) if _is_tail_of(h, chars_of)), None)
        if host is None:
            out.append(chars_of)
        else:
            chars_of[0].space_before = False
            host.extend(chars_of)
    return out
