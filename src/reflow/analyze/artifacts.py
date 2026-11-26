"""Page artifacts: running headers and footers, page numbers, margin stamps
and watermarks. These lines leave the reading order before it is built.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection
from statistics import median
from typing import NamedTuple

from reflow.extract.drawings import MIN_RULED_RULES
from reflow import geometry
from reflow.analyze.constants import BODY_SIZE_SLACK, HEADING_SCALE
from reflow.analyze.rhythm import ocr_page
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.patterns import CODE_START, NUMBER
from reflow.text_quality import squash


# Running headers/footers live in the outer MARGIN fraction of the page height; on a scan, of
# the height of its text (a legal-size scan sets its running head a quarter of the way down).
MARGIN = 0.1


# A margin line (digits ignored) is running text when it repeats on this share of all pages,
# or on this share of the pages between its first and last appearance (chapter headers).
REPEAT_SHARE = 0.25
CHAPTER_SHARE = 0.5
MIN_REPEATS = 3


# ...and sits at the same height: this share of its appearances lie within POSITION_TOLERANCE
# points of the median top. A section title or table row repeats without that.
POSITION_SHARE = 0.9
POSITION_TOLERANCE = 2.0
# A scan's registration moves its lines from page to page: about half a scanned line.
SCAN_POSITION_TOLERANCE = 5.0


# A line this close (in font sizes) to a body line in its column is text of that column, not
# furniture: a column heading, a section banner or a table row sits right against its body.
TIGHT_GAP = 0.5


# A running head or foot is one or two rows (lines side by side on one row count once); a
# longer stack of tight margin lines is a table header or a list, which is text.
MAX_HEADER_LINES = 2


# A page number follows the page index: another page within PAGE_CHAIN pages prints the same
# offset (printed value - page index).
PAGE_CHAIN = 2
PAGE_TOP_TOLERANCE = 1.0  # × the line's height: a folio sits at the same height from page to page
MAX_DIGIT_SHARE = 0.3  # see _margin_key
PAGE_NUMBER = re.compile(r"^(page\s*)?([0-9]+|[ivxlcdm]+)(\s*(of|/)\s*[0-9]+)?$", re.I)
# "Page 12 of 142 · Fileid …": the folio leads a longer running line.
PAGE_OF_PREFIX = re.compile(r"^page\s+([0-9]+)\s+of\s+[0-9]+\b", re.I)


# Why a line with no letter or digit is dropped; a table's placeholder dash is one (layout keeps it visible).
NO_TEXT_WHY = "rule or icon, no letters or digits"

Band = tuple[float, float]


def _margin_band(page: PageContent, *, by_text: bool = True) -> Band:
    """Where a page's top margin ends and its bottom margin starts: MARGIN of the page box in,
    or, with `by_text`, on a scan (see rhythm.ocr_page) MARGIN of its text's extent in, from the
    top of its highest line to the bottom of its lowest. The printed page numbers that label the
    pages (printed_numbers) always read the page box."""
    if not by_text:
        return page.height * MARGIN, page.height * (1 - MARGIN)
    top, bottom = 0.0, page.height
    if ocr_page(page.lines):
        text = [l for l in page.lines if not l.rotated and l.text.strip()]
        top, bottom = min(l.top for l in text), max(l.bottom for l in text)
    return top + MARGIN * (bottom - top), bottom - MARGIN * (bottom - top)


def _margin_key(line: Line, band: Band) -> str | None:
    """Key for comparing margin lines across pages (`band` is the page's _margin_band). Digits
    are ignored only in mostly alphabetic lines ("Page 12 of 142 · Fileid ..."); otherwise every
    address of the same shape ("4150 N 108th Ave, Ste 142") at the top of a column would look
    like one running header."""
    if line.top > band[0] and line.bottom < band[1]:
        return None
    text = squash(line.text.lower())
    digits = sum(c.isdigit() for c in text)
    letters = sum(c.isalpha() for c in text)
    if digits <= MAX_DIGIT_SHARE * (digits + letters):
        text = re.sub(r"\d+", "#", text)
    return text


def _rotated_key(text: str) -> str:
    """Key for comparing rotated lines across pages: lower case, digits ignored ("Page 3" and
    "Page 4" are one stamp)."""
    return re.sub(r"\d+", "#", squash(text.lower()))


class Folio(NamedTuple):
    """A printed page number: its value, and whether it is a roman numeral (front matter)."""
    value: int
    roman: bool


def _numeral(text: str) -> int:
    """The value of a printed page number: "12", or a roman numeral (PAGE_NUMBER lets only
    the letters i, v, x, l, c, d, m through)."""
    if text.isdigit():
        return int(text)
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total, prev = 0, 0
    for ch in reversed(text.lower()):
        v = values[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total


def _folio(text: str) -> Folio | None:
    """The page number a margin line prints alone ("12", "Page 12", "12 of 100", "iv")."""
    if m := PAGE_NUMBER.match(text.strip()):
        return Folio(_numeral(m.group(2)), not m.group(2).isdigit())
    return None


def _is_table_text(line: Line) -> bool:
    """Margin text that is a table's, not a running head's: a figure (a value, a dash) or a
    row that starts with a row code ("4000 Budget authority, gross"). "Chapter 13 How To…" and
    "REPORT NO. 914 …" are running heads."""
    text = squash(line.text)
    return bool(NUMBER.match(text)) or bool(CODE_START.match(text))


def _shares_row(line: Line, page: PageContent, furniture: Collection[int], band: Band) -> bool:
    """Whether other text sits on the same row as the line, in any column: body text, or margin
    text that is a table's (see _is_table_text: a value or a row code, as in a table row in the
    bottom band, "4000 Budget authority … 7"). Running headers and footers (`furniture`), other
    margin text (a page's own running head, which varies from page to page), rules, icons and
    rotated stamps don't count."""
    top, bottom = line.top, line.bottom   # read once: this runs against every line of the page
    return any(o is not line and o.bbox[1] < bottom and top < o.bbox[3]
               and not o.rotated and any(ch.isalnum() for ch in o.text)
               and id(o) not in furniture
               and (_margin_key(o, band) is None or _is_table_text(o))
               for o in page.lines)


def _page_number_ids(pages: list[PageContent], furniture: Collection[int] = ()) -> set[int]:
    """ids of the lines that print their own page's number. A page-number-shaped margin line
    alone on its row counts only if its value follows the page index: another page within
    PAGE_CHAIN pages prints the same offset (value - page) at the same height and edge
    distance. A table cell "1" or a stray publication number "501" on one page has no such
    partner. A number on a row with other text (a table row in the margin, "4000 Budget
    authority, gross ... 7") is content, never a folio. `furniture` holds the ids of the
    running headers and footers, which do not count as other text on the row."""
    # (page, offset, edge, line, bare): a folio-shaped margin line alone on its row. bare means
    # the line is nothing but the number, so only a bare one can be dropped as a page number.
    # edge is its distance from the nearer outer side, so facing pages' mirrored folios match.
    found: list[tuple[int, int, float, Line, bool]] = []
    for page in pages:
        band = _margin_band(page)
        for line in page.lines:
            if _margin_key(line, band) is None or _shares_row(line, page, furniture, band):
                continue
            edge = min(line.x0, page.width - line.x1)
            text = line.text.strip()
            if folio := _folio(text):
                found.append((page.number, folio.value - page.number, edge, line, True))
            elif m := PAGE_OF_PREFIX.match(text):
                found.append((page.number, int(m.group(1)) - page.number, edge, line, False))
    by_offset: dict[int, list[tuple[int, float, Line]]] = {}  # offset → (page, edge, line)
    for number, offset, edge, line, _ in found:
        by_offset.setdefault(offset, []).append((number, edge, line))

    def tracks(number: int, offset: int, edge: float, line: Line) -> bool:
        # The partner sits where the folio sits: same height and same edge distance, to a line.
        tol = PAGE_TOP_TOLERANCE * line.height
        return any(other != number and abs(other - number) <= PAGE_CHAIN
                   and abs(p_line.top - line.top) <= tol and abs(p_edge - edge) <= tol
                   for other, p_edge, p_line in by_offset[offset])

    return {id(line) for number, offset, edge, line, bare in found
            if bare and tracks(number, offset, edge, line)}


def _repeats_across(occurrences: list[tuple[PageContent, Line]], page_count: int) -> bool:
    """Whether a text appears on enough pages to be furniture: REPEAT_SHARE of all pages, or
    CHAPTER_SHARE of the pages from its first to its last appearance. Position is not checked (a
    side tab sits at a different height on each page)."""
    nums = sorted({page.number for page, _ in occurrences})
    span = nums[-1] - nums[0] + 1
    if len(nums) < MIN_REPEATS:
        return False
    return len(nums) >= REPEAT_SHARE * page_count or len(nums) >= CHAPTER_SHARE * span


def _repeats_in_place(occurrences: list[tuple[PageContent, Line]], page_count: int) -> bool:
    """Whether a margin line repeats as page furniture: its text (digits ignored) appears on
    REPEAT_SHARE of all pages or on CHAPTER_SHARE of the pages from its first to its last
    appearance, and in each half of the page (a header and a footer may both repeat) it sits
    at the same height on POSITION_SHARE of its appearances. A count alone is not enough: a
    section title or a table row can repeat many times. A sideways copy (the footer of a
    landscape page, read as displayed) is compared only with the other sideways copies. The
    same height is SCAN_POSITION_TOLERANCE when every appearance was read by OCR, on a scan or
    in a band of a page image beside the page's own text, POSITION_TOLERANCE otherwise."""
    nums = sorted({page.number for page, _ in occurrences})
    span = nums[-1] - nums[0] + 1
    if len(nums) < MIN_REPEATS:
        return False
    if len(nums) < REPEAT_SHARE * page_count and len(nums) < CHAPTER_SHARE * span:
        return False
    groups: dict[tuple[bool, bool], list[tuple[bool, float]]] = {}
    for page, line in occurrences:
        groups.setdefault((line.rotated, line.top < page.height / 2), []).append(
            (line.ocr, line.top))
    for group in groups.values():
        if not group:
            continue
        mode = median(top for _, top in group)
        tol = SCAN_POSITION_TOLERANCE if all(ocr for ocr, _ in group) else POSITION_TOLERANCE
        in_place = sum(abs(top - mode) <= tol for _, top in group)
        if in_place < POSITION_SHARE * len(group):
            return False
    return True


def _is_text_block(page: PageContent, line: Line, repeated: Collection[int] = ()) -> bool:
    """Whether a margin line is part of text rather than furniture: a body line sits right
    against it in its column, or the stack of margin lines it sits in is longer than
    MAX_HEADER_LINES (a table's header or a list, not a running head or foot). A taller stack
    whose every line is in `repeated` (the bold running lines at body size or larger, past their
    first appearance) is a running head: a title block of three rows printed on every page,
    not the rows of a table."""
    stack, frontier = {id(line)}, [line]
    band = _margin_band(page)
    while frontier:
        current = frontier.pop()
        x0, top, x1, bottom = current.bbox
        reach = TIGHT_GAP * current.size
        for other in page.lines:
            ox0, otop, ox1, obottom = other.bbox   # not tight: another column, or not within
            # TIGHT_GAP of the font size above or below
            if (ox0 >= x1 or ox1 <= x0 or not max(otop - bottom, top - obottom) <= reach
                    or id(other) in stack):
                continue
            if _margin_key(other, band) is None:
                return True  # body text right against the stack
            stack.add(id(other))
            frontier.append(other)
    return _rows(page, stack) > MAX_HEADER_LINES and not stack.issubset(repeated)


def _rows(page: PageContent, stack: set[int]) -> int:
    """How many rows the stacked lines make: lines whose vertical extents overlap by at least
    half the smaller height are one row (a date split into a label and a value is one row)."""
    members = sorted((l.bbox for l in page.lines if id(l) in stack), key=lambda b: b[1])
    rows: list[tuple[float, float]] = []
    for _, top, _, bottom in members:
        if rows:
            r_top, r_bottom = rows[-1]
            overlap = min(bottom, r_bottom) - max(top, r_top)
            if overlap >= 0.5 * min(bottom - top, r_bottom - r_top):
                rows[-1] = (min(top, r_top), max(bottom, r_bottom))
                continue
        rows.append((top, bottom))
    return len(rows)


# A rotated stamp is a short line at the page edge (a side tab, a running head set sideways): no
# longer than this share of the page's longer side, and within this share of the shorter side
# from an edge. Rotated text in a block (a sideways table's rows) is content, never a stamp.
STAMP_MAX_SHARE = 0.5
STAMP_EDGE_SHARE = 0.15


def _near(a: Line, b: Line, gap: float) -> bool:
    return max(a.x0 - b.x1, b.x0 - a.x1, a.top - b.bottom, b.top - a.bottom) < gap


# A side tab is a rotated line of at most this many words whose centre sits within this share of
# the page width from an edge ("Pima County", a section tab): furniture even on a few pages.
TAB_MAX_WORDS = 3
TAB_EDGE_SHARE = 0.10


def _rotated_stamps(pages: list[PageContent]) -> set[int]:
    """ids of the rotated lines that are furniture. A side tab (see TAB_MAX_WORDS) is furniture
    wherever it appears. Otherwise a short, isolated margin line is furniture when its text repeats
    across the pages (see _repeats_across). A block of sideways lines (a table's rows) and longer
    sideways text are content, read where they sit."""
    stamps: set[int] = set()
    occurrences: dict[str, list[tuple[PageContent, Line]]] = {}
    for page in pages:
        longer, shorter = max(page.width, page.height), min(page.width, page.height)
        for line in page.lines:
            if not line.rotated:
                continue
            if any(o is not line and o.rotated and _near(o, line, 2 * line.size)
                   for o in page.lines):
                continue  # part of a block of sideways lines: content
            length = max(line.x1 - line.x0, line.bottom - line.top)
            if length > STAMP_MAX_SHARE * longer:
                continue
            centre = (line.x0 + line.x1) / 2
            if (len(line.text.split()) <= TAB_MAX_WORDS
                    and min(centre, page.width - centre) <= TAB_EDGE_SHARE * page.width):
                stamps.add(id(line))
                continue
            edge = min(line.x0, page.width - line.x1, line.top, page.height - line.bottom)
            if edge <= STAMP_EDGE_SHARE * shorter:
                occurrences.setdefault(_rotated_key(line.text), []).append((page, line))
    stamps |= {id(line) for occ in occurrences.values()
               if _repeats_across(occ, len(pages)) for _, line in occ}
    return stamps


# A watermark is text drawn faint (its ink on white below WATERMARK_INK: a pale or transparent
# fill, see extract._ink) or at a diagonal (more than DIAGONAL_MARGIN degrees from every axis).
# It repeats on the same pages at the same place (see _watermarks), and it is not a label on a
# filled drawing or an image: white or pale text on a banner is content.
WATERMARK_INK = 0.3
DIAGONAL_MARGIN = math.radians(15)


def _is_diagonal(line: Line) -> bool:
    """Whether a line's text runs at a diagonal: rotated, and not within DIAGONAL_MARGIN of a
    level, upright, or vertical reading."""
    if not line.rotated:
        return False
    a = line.angle % (math.pi / 2)
    return DIAGONAL_MARGIN < a < math.pi / 2 - DIAGONAL_MARGIN


def _on_backdrop(line: Line, page: PageContent) -> bool:
    """Whether a line sits on a filled drawing or an image: a label on a banner, not a watermark."""
    return (any(d.fills and geometry.inside(line.bbox, d.bbox, slack=1) for d in page.drawings)
            or any(geometry.inside(line.bbox, box, slack=1) for box in page.images))


def _watermarks(pages: list[PageContent]) -> dict[int, str]:
    """id(Line) → reason for the lines that are watermarks. A faint or diagonal line is a
    watermark when its text (digits ignored) repeats across the pages (see _repeats_across) and
    sits at the same centre on POSITION_SHARE of its appearances. A rotated line with other
    rotated lines beside it is a block (a table's sideways headers), which is content."""
    occurrences: dict[str, list[tuple[PageContent, Line, str]]] = {}
    for page in pages:
        for line in page.lines:
            if not any(ch.isalnum() for ch in line.text):
                continue
            diagonal = _is_diagonal(line)
            faint = line.ink < WATERMARK_INK and not _on_backdrop(line, page)
            if not (diagonal or faint):
                continue
            if line.rotated and any(o is not line and o.rotated and _near(o, line, 2 * line.size)
                                    for o in page.lines):
                continue
            kind = "diagonal" if diagonal else "faint"
            occurrences.setdefault(_rotated_key(line.text), []).append((page, line, kind))

    marks: dict[int, str] = {}
    for occ in occurrences.values():
        if not _repeats_across([(page, line) for page, line, _ in occ], len(pages)):
            continue
        centres = [((line.x0 + line.x1) / 2, (line.top + line.bottom) / 2) for _, line, _ in occ]
        mx, my = median(c[0] for c in centres), median(c[1] for c in centres)
        in_place = sum(abs(cx - mx) <= POSITION_TOLERANCE and abs(cy - my) <= POSITION_TOLERANCE
                       for cx, cy in centres)
        if in_place < POSITION_SHARE * len(occ):
            continue
        repeats = len({page.number for page, _, _ in occ})
        for _, line, kind in occ:
            marks[id(line)] = f"watermark: {kind} text repeated on {repeats} pages"
    return marks


def _margin_number_ids(page: PageContent) -> set[int]:
    """Isolated numbering columns, including single glyphs misread by OCR.

    A run must span the page beside body text, with a clear gutter and consecutive
    numbers or repeated rotated OCR fragments. Table cells and labels attached to their
    text are content.
    """
    found: set[int] = set()
    for left in (True, False):
        candidates = []
        for line in page.lines:
            text = line.text.strip()
            short = text.isalnum() and len(text) <= 3
            narrow = line.x1 - line.x0 <= max(18, 2 * line.size)
            edge = line.x1 <= page.width * 0.15 if left else line.x0 >= page.width * 0.85
            if narrow and edge and (short or line.ocr and line.rotated):
                candidates.append(line)
        columns: list[list[Line]] = []
        for line in sorted(candidates, key=lambda l: l.x1 if left else l.x0):
            anchor = line.x1 if left else line.x0
            if (columns and abs(anchor - median(l.x1 if left else l.x0 for l in columns[-1]))
                    <= max(8, line.size)):
                columns[-1].append(line)
            else:
                columns.append([line])
        for column in columns:
            column.sort(key=lambda l: l.top)
            top, bottom = column[0].top, max(l.bottom for l in column)
            size = median(l.size for l in column)
            if len(column) < 6 or bottom - top < page.height * 0.3:
                continue
            numbers = [int(l.text.strip()) for l in column if l.text.strip().isdecimal()]
            rotated_ocr = (len(column) >= 8 and all(l.ocr for l in column)
                           and sum(l.rotated for l in column) >= len(column) / 2)
            if not rotated_ocr and sum(b == a + 1 for a, b in zip(numbers, numbers[1:])) < 3:
                continue
            x0, x1 = min(l.x0 for l in column), max(l.x1 for l in column)
            content = [l for l in page.lines if l.bottom >= top and l.top <= bottom
                       and l.x1 - l.x0 >= page.width * 0.25]
            if not content:
                continue
            gap = min(l.x0 for l in content) - x1 if left else x0 - max(l.x1 for l in content)
            if gap < size:
                continue
            if any(d.rules >= MIN_RULED_RULES and d.bbox[0] <= x0 and d.bbox[2] >= x1
                   and d.bbox[1] <= top and d.bbox[3] >= bottom for d in page.drawings):
                continue
            found.update(id(l) for l in column)
    return found


def artifact_reasons(pages: list[PageContent], body: float | None = None) -> dict[int, str]:
    """id(Line) → why the line is dropped: running header/footer, page number, rotated or
    no letters or digits.

    A margin line (digits ignored) is running text when it repeats at the same height (see
    _repeats_in_place) and is not text of its own column (see _is_text_block). Its first
    appearance stays content when it is heading-sized: that is where the section starts, so
    it belongs in the outline once. The exception is a plain-weight line that runs on
    REPEAT_SHARE of all pages: it titles the whole document (the shell header, not the outline),
    while a bold one is a form's title block. A page number is dropped only when its value
    follows the page index (see _page_number_ids).
    """
    margin: dict[str, list[tuple[PageContent, Line]]] = {}
    for page in pages:
        band = _margin_band(page)
        for line in page.lines:
            if (key := _margin_key(line, band)) is not None:
                margin.setdefault(key, []).append((page, line))

    running: dict[int, str] = {}  # id(line) → reason, for repeating margin lines
    repeated: set[int] = set()  # the bold running lines past their first appearance
    for occurrences in margin.values():
        if not _repeats_in_place(occurrences, len(pages)):
            continue
        first = min(page.number for page, _ in occurrences)
        repeats = len({page.number for page, _ in occurrences})
        document_wide = repeats >= REPEAT_SHARE * len(pages)
        for page, line in occurrences:
            if (page.number == first and body is not None and line.size >= body * HEADING_SCALE
                    and not (document_wide and not line.bold)):
                continue  # the section's opening title: keep it once
            running[id(line)] = f"running header or footer, repeats on {repeats} pages"
            if page.number != first and line.bold and body is not None and line.size >= body - BODY_SIZE_SLACK:
                repeated.add(id(line))

    stamps = _rotated_stamps(pages)
    marks = _watermarks(pages)

    numbers = _page_number_ids(pages, running)
    reasons: dict[int, str] = {}
    for page in pages:
        margin_numbers = _margin_number_ids(page)
        for line in page.lines:
            if id(line) in margin_numbers:
                reasons[id(line)] = "margin line numbering"
            elif id(line) in stamps:
                reasons[id(line)] = "rotated text (margin stamp)"
            elif not any(ch.isalnum() for ch in line.text):
                reasons[id(line)] = NO_TEXT_WHY  # "____", "!"
            elif id(line) in numbers:
                reasons[id(line)] = "page number"
            elif id(line) in running and not _is_text_block(page, line, repeated):
                reasons[id(line)] = running[id(line)]
            elif id(line) in marks:
                reasons[id(line)] = marks[id(line)]
    return reasons


def printed_numbers(pages: list[PageContent]) -> dict[int, list[Folio]]:
    """The page numbers printed in each page's top or bottom margin, by 1-based page number:
    pages with none are left out."""
    found: dict[int, list[Folio]] = {}
    for page in pages:
        band = _margin_band(page, by_text=False)
        folios = [f for line in page.lines
                  if _margin_key(line, band) is not None and (f := _folio(line.text))]
        if folios:
            found[page.number] = folios
    return found
