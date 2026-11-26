"""Headings: which blocks are headings, their levels, and wrapped headings."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from reflow.patterns import CAPTION_BLOCK, CAPTION_TITLE
from reflow import geometry
from reflow.analyze.blocks import Block, ends_hyphenated, line_fills
from reflow.analyze.constants import (
    NUMBERED_TITLE,
    BODY_SIZE_SLACK,
    BULLET_GLYPHS,
    CALLOUT_LABEL,
    CENTRE_TOL,
    CONTINUED,
    DOT_LEADER,
    HANGING_MIN,
    HEADING_SCALE,
    INDEX_REFERENCE,
    PAGE_REFERENCE,
    TERMINAL,
    is_form_line_label,
)
from reflow.text_quality import ocr_gate
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.leaders import is_contents_entry
from reflow.model import BBox, HEADING_BY_LEVEL, HEADING_LEVEL, HEADING_ROLES, Node, Role, Why, code_of


class HeadingWhy(StrEnum):
    """The reasons of the heading rule that a later step branches on, as the code of a Why. Any
    other reason is prose alone."""
    SECTION_TITLE = "section-title"   # a section number with its title in capitals: ranks nothing
    ROW_TWIN = "row-twin"             # a grid of names or labels: demoted_by_twin reads it back
    INK = "ink"                       # a heavy stroke or an underline on the page image (ink_cue)


# The words of each reason that does not depend on the block. INK's name what the cue saw.
HEADING_WHY_TEXT: dict[HeadingWhy, str] = {
    HeadingWhy.SECTION_TITLE: "a section number with its title in capitals (#223)",
    HeadingWhy.ROW_TWIN: "same style beside it on the row (a grid of names or labels)",
}


def heading_why(code: HeadingWhy, text: str | None = None) -> Why:
    """The heading rule's reason `code`: its words, from HEADING_WHY_TEXT unless `text` says."""
    return Why(HEADING_WHY_TEXT[code] if text is None else text, code)


TWIN_GAP = 3.0  # see _has_row_twin
HEADING_MAX_CHARS = 200
BOLD_HEADING_MAX_CHARS = 120

# A one-line OCR block whose stroke weight (Line.stroke) is at least this many times the page's
# median is set in bold (ink_cue). Calibrated on the scanned samples: bold headings read
# 1.8 to 2.9, and no line of body text reaches 1.8 (the heaviest, 1.75 and 1.65, are a struck
# line and a paragraph in larger type than the page's small print).
STROKE_HEADING = 1.8
INK_MIN_LETTERS = 12
UNDERLINE_COVER = 0.9  # underlined words span this share of their line (underlined)
WORD_SPACE = 0.5       # a gap between words up to this share of the line's height is a space


# A page is an index when at least this many of its blocks end in page references, and at least
# this share of them do.
INDEX_PAGE_MIN = 5
INDEX_PAGE_SHARE = 0.25


# A stray dash that an OCR text layer puts before a title's second line: "- CURVE OF A ...".
TITLE_DASH = re.compile(r"^\s*-\s+")


# An index entry line: words, then page numbers, with no brackets (a phone number is not one).
ENTRY_REFERENCE = re.compile(r"^[A-Za-z][^()]*\s\d[\d,\s–-]*$")
# A sentence's modal verb in lower case: "Employers can register to use" is not a title.
MODALS = frozenset({"can", "could", "may", "might", "must", "should", "will", "would", "shall"})


# A lone roman numeral: a folio ("i") or a list letter, never a heading.
ROMAN_ONLY = re.compile(r"^(?=[ivxlcdm])m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})[.)]?$",
                        re.I)


SECTION_HEADING = re.compile(r"^section\s+\d+\s*[-–—:]\s+\S", re.I)


def is_section_heading(text: str | None) -> bool:
    return bool(SECTION_HEADING.match((text or "").strip()))


# A heading that opens with its section number: "3. PROPERTY IDENTIFICATION", "Section 3. VALUE".
NUMBERED_HEADING = re.compile(r"^(?:section\s+)?\d{1,2}[.)]\s+\S", re.I)
# Only a bare number ("3. ...") opens a form's numbered section; "Section 3." headings are
# instruction parts whose neighbours are sections of their own.
NUMBERED_SECTION = re.compile(r"^\d{1,2}[.)]\s+\S")


def is_numbered_heading(text: str | None) -> bool:
    return bool(NUMBERED_HEADING.match((text or "").strip()))


def is_label_caps(text: str | None) -> bool:
    """A bold line of capitals of two or more words ("PROPERTY TYPE"), the shape of a form's
    sub-label. One word ("CERTIFICATION") is a section of its own."""
    text = (text or "").strip()
    return (text.isupper() and 2 <= len(text.split()) and len(text) <= 60
            and not is_numbered_heading(text))


def body_size(pages: Iterable[PageContent]) -> float:
    sizes: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            sizes[line.size] += len(line.text)
    return sizes.most_common(1)[0][0] if sizes else 10.0


# A page sets its paragraphs in a larger size than the document's body text when at least
# PAGE_BODY_SHARE of its text, and PAGE_BODY_MIN_CHARS characters, are wrapped blocks at that size,
# and that size is a heading's (HEADING_SCALE times the document's body text or more).
PAGE_BODY_SHARE = 0.5
PAGE_BODY_MIN_CHARS = 400


def page_body(blocks: Sequence[Block], body: float) -> float:
    """The body size of one page: the document's, or the larger size this page's paragraphs are
    set in. A claim form's fields are set small and fill most of the document, so its pages of
    instructions, set larger, would read as headings line by line. Text that wraps is prose; a
    title of two or three large lines is too short to count. A page set only a little larger
    keeps the document's size, so its headings keep their levels."""
    sizes: Counter[float] = Counter()
    total = 0
    for b in blocks:
        chars = sum(len(l.text) for l in b.lines)
        total += chars
        if len(b.lines) >= 2:
            for l in b.lines:
                sizes[l.size] += len(l.text)
    if not sizes:
        return body
    size, chars = sizes.most_common(1)[0]
    if (size >= body * HEADING_SCALE and chars >= PAGE_BODY_MIN_CHARS
            and chars >= PAGE_BODY_SHARE * total):
        return size
    return body


SECTION_LABEL = re.compile(r"^section\s+\d{1,2}\.?$", re.I)
# A section number and what follows it on its line: "SECTION 3. Section 12 of the ... Code is
# amended to read:", "Sec. 25A.021. ACTIONS COMMENCED BEFORE SEPTEMBER 1, 2024."
SECTION_NUMBER_LINE = re.compile(r"^(?:section|sec\.)\s+\d+[a-z]?(?:\.\d+[a-z]?)*\.\s+(?P<rest>\S.*)$", re.I)
SECTION_TITLE_ROWS = 2  # a section's title wraps onto one more printed row at most


def _rows(block: Block) -> int:
    """The printed rows of the block: OCR reads a justified row as several lines side by side."""
    tops: list[float] = []
    for line in block.lines:
        if not any(abs(line.top - t) < 0.5 * line.size for t in tops):
            tops.append(line.top)
    return len(tops)


def section_title(text: str) -> str | None:
    """The title on a section number's line, when the line holds only the section's title in
    capitals ("Sec. 25A.021. ACTIONS COMMENCED BEFORE SEPTEMBER 1, 2024."); None when the number
    opens a sentence ("SECTION 46. Chapter 25A, Government Code, is amended ...")."""
    match = SECTION_NUMBER_LINE.match(text.strip())
    if match is None:
        return None
    title = match["rest"]
    return title if title.isupper() and sum(c.isalpha() for c in title) >= 3 else None


def _is_section_title(block: Block, body: float) -> bool:
    """A section number with its title, and nothing else: one printed row, or two when the title
    wraps, at about body size (OCR may read the size short, so NUMBERED_CAPS_SCALE is the floor),
    and the title ends in its full stop (a title cut at the end of its row goes on below, see
    join_wrapped_headings). The text says it is a heading, so the layout rules that read OCR's
    pieces of a justified row (a row of several lines, a twin beside it, a long line to the
    column's edge) do not refuse it."""
    text = block.text.strip()
    return (section_title(text) is not None and text.endswith(".")
            and len(text) <= BOLD_HEADING_MAX_CHARS and block.size >= body * NUMBERED_CAPS_SCALE
            and _rows(block) <= SECTION_TITLE_ROWS)


def _has_row_twin(block: Block, blocks: list[Block]) -> bool:
    """Another block in the same style right beside this one on the same row: a grid of
    names or labels (e.g. a paper's author list), not a heading. Blocks further apart
    than TWIN_GAP font sizes are in separate columns (directory listings) and don't
    count."""
    return bool(_row_twins(block, blocks))


def _row_twins(block: Block, blocks: list[Block]) -> list[Block]:
    """The blocks _has_row_twin counts beside `block`."""
    if block.bold and (NUMBERED_TITLE.match(block.text) or SECTION_LABEL.match(block.text)):
        return []  # a numbered section title is not one of a grid of labels
    top, bottom = block.bbox[1], block.bbox[3]
    twins = []
    for other in blocks:
        if other is block or other.size != block.size or other.bold != block.bold:
            continue
        if SECTION_LABEL.match(other.text):
            continue  # "SECTION 4." beside its title "VALUE": the two make one heading
        o = other.bbox
        gap = max(o[0] - block.bbox[2], block.bbox[0] - o[2])
        if gap > TWIN_GAP * block.size:
            continue
        # The row is the line a name sits on: a name joined with a second line ("(F)") does not
        # move the row of the names beside it.
        first = other.first
        if min(bottom, first.bottom) - max(top, first.top) > 0.5 * (bottom - top):
            twins.append(other)
    return twins


def demoted_by_twin(blocks: Sequence[Block], nodes: Sequence[Node], body: float,
                    ruled: Sequence[BBox] = (), fields: Sequence[BBox] = ()) -> list[Node]:
    """The paragraphs (nodes[i] is blocks[i]'s) the row-grid rule kept from being headings
    although a twin it counted is itself a heading, a heading in the same style starts at the
    paragraph's left edge, and without its twins heading_decision makes it a heading: a column
    of entries whose name sits level with a name in the next column. A grid of labels (its
    twins labels too), body text (no heading without its twins) or a "(continued)" label is
    never one."""
    node_of = {id(b): n for b, n in zip(blocks, nodes)}

    def heading(b: Block) -> bool:
        return node_of[id(b)].role in HEADING_ROLES

    found = []
    for block, node in zip(blocks, nodes):
        if node.role != Role.P or not any(code_of(w) == HeadingWhy.ROW_TWIN
                                          for w in node.attrs.get("why", [])):
            continue
        twins = _row_twins(block, blocks)
        if not any(heading(t) for t in twins) or CONTINUED.search(block.text):
            continue  # a "(continued)" heading is a context paragraph either way (layout)
        if not any(o is not block and o.size == block.size and o.bold == block.bold and heading(o)
                   and abs(o.first.x0 - block.first.x0) <= 1 for o in blocks):
            continue
        rest = [b for b in blocks if all(b is not t for t in twins)]
        if heading_decision(block, body, rest, ruled, fields)[0] is not None:
            found.append(node)
    return found


def _continues(block: Block, nxt: Block) -> bool:
    """Whether `nxt` is set directly under `block`, in the same column, close enough to go on
    with it."""
    last = block.lines[-1]
    gap = nxt.first.top - last.bottom
    return (-last.height < gap <= 0.5 * last.size
            and nxt.first.x0 < last.x1 and last.x0 < nxt.first.x1)


# How far, in font sizes, regular text may start from the end of the bold phrase it continues
RUN_IN_GAP = 1.5


def _runs_in(block: Block, nxt: Block, blocks: list[Block]) -> bool:
    """Whether the regular text of `nxt` goes on from the bold phrase that ends `block`, with no
    column gutter between them. It starts right after the phrase on its line (a gap under
    RUN_IN_GAP font sizes), or it is the next line of the same column, which proves a lead-in
    only when the phrase runs to the column's edge and wraps ("Trump account and new Form" /
    "4547. Recent ..."). A short bold line with body text under it is a heading."""
    lead, text = block.lines[-1], nxt.first
    if text.x0 < lead.x1 and lead.x0 < text.x1:
        return _fills_line(block, blocks)
    return -0.5 * lead.size <= text.x0 - lead.x1 <= RUN_IN_GAP * lead.size


# An OCR block of this many words, in plain weight and not in capitals, that runs to its
# column's edge is body text
OCR_BODY_WORDS = 8


def _is_fragment(block: Block, blocks: list[Block]) -> str | None:
    """Why this block is part of a sentence rather than a heading: it ends in a line-end
    hyphen (its word goes on in the next line), it reads as a sentence (a lower-case modal
    verb and no full stop: "Employers can register to use"), it is a long OCR line that runs
    to the column's edge (a scan's line read at a larger size, see OCR_BODY_WORDS; a title in
    capitals, centred on a title page, is not one), or the block
    right under it, in the same column, starts in lowercase or with a bracket (a bold lead-in or
    a split word: "Getting tax forms and publica" / "tions. Go to IRS.gov"). None when it
    stands on its own."""
    last = block.lines[-1]
    if ends_hyphenated(last.text):
        return "ends in a hyphenated split: the word goes on below"
    if (any(w in MODALS for w in block.text.split())
            and not block.text.rstrip().endswith((".", "?", "!"))):
        return "a sentence: a modal verb and no full stop, so it goes on below"
    if (all(l.ocr for l in block.lines) and not block.bold and not block.text.isupper()
            and len(block.text.split()) >= OCR_BODY_WORDS
            and line_fills(last, [l for b in blocks for l in b.lines])):
        return "OCR body text: eight or more words that run to the column's edge"
    idx = next((k for k, b in enumerate(blocks) if b is block), None)
    if idx is None or idx + 1 >= len(blocks):
        return None
    nxt = blocks[idx + 1]
    if (block.bold and not nxt.bold and nxt.first.size == last.size
            and not block.text.rstrip().endswith(TERMINAL)
            and nxt.first.top <= last.bottom - 0.1 * last.size
            and _runs_in(block, nxt, blocks)):
        return "regular text continues on the same baseline: a run-in lead, not a heading"
    if not _continues(block, nxt):
        return None
    head = blocks[idx + 1].first.text.strip()[:1]
    if (is_contents_entry(blocks[idx + 1].text) and not block.text.rstrip().endswith(TERMINAL)
            and not INDEX_REFERENCE.search(block.text)
            and (head.islower() or _fills_line(block, blocks))):
        return "a wrapped index entry: its page number is on the line below"
    if head.islower():
        return "the next line goes on in lowercase: a run-in lead or a split sentence"
    if head == "(" and nxt_word(blocks[idx + 1]):
        return "the next line opens with a bracket: a run-in lead or a split name"
    return None


def _index_evidence(block: Block, blocks: list[Block]) -> str | None:
    """Why a candidate that ends in a colon is an index entry rather than a heading, or None.
    A label such as "Requirements:" or "Detailed Budget Estimates by Agency:" stays a heading.
    Evidence: its indented sub-entries end in page references ("Emergency Fund ....1201"), or
    the page is an index (many of its lines end in page references)."""
    idx = next((k for k, b in enumerate(blocks) if b is block), None)
    if idx is not None and idx + 1 < len(blocks):
        nxt = blocks[idx + 1]
        if (nxt.first.x0 > block.first.x0 + HANGING_MIN * block.size
                and PAGE_REFERENCE.search(nxt.text)):
            return "its indented sub-entries end in page references"
    refs = sum(1 for b in blocks if PAGE_REFERENCE.search(b.text))
    if refs >= INDEX_PAGE_MIN and refs >= INDEX_PAGE_SHARE * len(blocks):
        return "the page is an index: many of its lines end in page references"
    return None


def _continues_label(block: Block, under: Block, blocks: list[Block]) -> bool:
    """Whether `under` is the second line of the same short label ("Sign" over "Here"): a
    one- or two-word line in the same style, set right under it, with the smaller type of a
    form row beside them ("Signature of" and "U.S. person"). A heading over a heading has none."""
    gap = under.first.top - block.lines[-1].bottom
    if not (len(under.text.split()) <= 2 and under.size == block.size and under.bold == block.bold
            and -block.size < gap <= block.size):
        return False
    top, bottom = block.bbox[1], block.bbox[3]
    return any(b is not block and b.size < block.size - 1.0 and b.bbox[0] >= block.bbox[2]
               and min(b.bbox[3], bottom) - max(b.bbox[1], top) > 0 for b in blocks)


# Not titles however large: an address, an amount, or a "PAY TO:" / "MAIL TO:" label with its value.
LABEL_VALUE = re.compile(
    r"^(?:pay|mail|send|make\s+checks\s+payable|return|submit|deliver|remit)\s+(?:to|payable\s+to)\s*:"
    r"|^(?:fees?|amount|total|cost|price|balance|payment)\b[^.]{0,40}\$\s?\d"
    r"|^(?:fax|phone|tel(?:ephone)?|e-?mail|attn|attention|address|from|to|date)\s*:\s+\S", re.I)
ADDRESS_TAIL = re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?$")


def is_label_value(text: str) -> bool:
    text = text.strip()
    return bool(LABEL_VALUE.match(text) or (ADDRESS_TAIL.search(text) and text[:1].isdigit()))


def _is_title_note(block: Block, blocks: list[Block]) -> bool:
    """A line set on the row of a numbered section title, to its right and in another font
    ("6. REASON FOR FILING APPEAL (FACTS)   See instructions before completing this section.")."""
    first = block.first
    return any(o is not block and o.bold and NUMBERED_TITLE.match(o.text)
               and o.first.font != first.font and first.x0 >= o.first.x1 - 1
               and abs(o.first.top - first.top) < 0.5 * first.height
               for o in blocks)


def _is_form_label(block: Block, body: float, ruled: Sequence[BBox], blocks: list[Block]) -> bool:
    """A one- or two-word label in small type, inside or just beside a ruled form area ("or",
    "Sign", "Here" on a tax form), with no body text under it. A title set in a ruled box is
    large, and a question with an answer under it is body text: both stay as they are."""
    if block.bold and NUMBERED_TITLE.match(block.text):
        return False  # "4. VALUE": a numbered section title, in a box or not
    slack = 1.5 * block.size
    if not (len(block.text.split()) <= 2 and block.size < 1.5 * body):
        return False
    box = next((box for box in ruled if geometry.inside(block.bbox, box, slack=slack)), None)
    if box is None:
        return False
    if _is_banner_label(block, body, box, slack, blocks):
        return False
    idx = next((k for k, b in enumerate(blocks) if b is block), None)
    if idx is None or idx + 1 >= len(blocks):
        return True
    # Only what is set inside the same box counts as under the label: a heading below the box
    # is the next section's, not the label's.
    under = next((b for b in blocks[idx + 1:idx + 6] if b.first.top >= block.bbox[1]
                  and b.first.top < box[3]
                  and b.first.x0 < block.bbox[2] and block.bbox[0] < b.bbox[2]), None)
    if under is None:
        return True  # nothing set under it, in its column
    if under.size >= body * HEADING_SCALE or (under.bold and under.size >= body - BODY_SIZE_SLACK):
        if not _continues_label(block, under, blocks):
            return False  # a heading under it: a section label of a directory, not a form label
    gap = under.first.top - block.lines[-1].bottom
    return not (-block.size < gap <= 2 * block.size and len(under.text.split()) > 3
                and under.size >= body - BODY_SIZE_SLACK)  # body text right under it stays body text


def _is_banner_label(block: Block, body: float, box: BBox, slack: float,
                     blocks: list[Block]) -> bool:
    """A label that is alone in its column of the ruled box it sits in, in heading type: the
    name on a section banner of a directory ("Adams" on a grey bar), not a field of a form,
    whose labels share a column with their entries. Scans have no such boxes to trust."""
    return (not any(l.ocr for l in block.lines)
            and (block.bold or block.size >= body * HEADING_SCALE)
            and not any(b is not block and geometry.inside(b.bbox, box, slack=slack)
                        and b.bbox[0] < block.bbox[2] and block.bbox[0] < b.bbox[2]
                        for b in blocks))


ROW_LABEL_WORDS = 3
ROW_LABEL_REACH = 12  # font sizes: how far from the label its value may sit on the row


def _is_row_label(block: Block, blocks: list[Block], fields: Sequence[BBox]) -> bool:
    """A label of up to ROW_LABEL_WORDS words that ends in a colon, one line, with other text or
    a form field (`fields`, the boxes of the page's widgets) on its row within ROW_LABEL_REACH
    font sizes: "Mail:" before an address, "Fax:" after "Phone:", "Other Agencies:" before its
    boxes. A heading has its text under it, not beside it."""
    text = block.text.strip()
    if not text.endswith(":") or len(block.lines) != 1 or len(text.split()) > ROW_LABEL_WORDS:
        return False
    top, bottom = block.bbox[1], block.bbox[3]
    reach = ROW_LABEL_REACH * block.size
    beside = [min(o.lines, key=lambda l: abs(l.top - top)).bbox for o in blocks if o is not block]
    for x0, otop, x1, obottom in [*beside, *fields]:
        if min(bottom, obottom) - max(top, otop) <= 0.5 * min(bottom - top, obottom - otop):
            continue  # not on the same row
        if 0 <= max(x0 - block.bbox[2], block.bbox[0] - x1) <= reach:
            return True
    return False


def _is_column_label(block: Block, blocks: list[Block]) -> bool:
    """A one- or two-word label set in light type on the same row as a larger block, but not under
    it: a column header ("Page" beside "TABLE OF CONTENTS"), not a heading of its own. A bold
    label is a heading, whatever is beside it."""
    if len(block.text.split()) > 2 or block.bold:
        return False
    top, bottom = block.bbox[1], block.bbox[3]
    for other in blocks:
        if other is block or other.size < block.size + 1.0:
            continue
        o = other.bbox
        if min(bottom, o[3]) - max(top, o[1]) <= 0.5 * (bottom - top):
            continue  # not on the same row
        if o[2] <= block.bbox[0] or o[0] >= block.bbox[2]:
            return True
    return False


SIDE_LABEL_WORDS = 3   # a side label is a word or three
SIDE_LABEL_SCALE = 2.0  # and the title it sits beside is at least this much larger


def _is_side_label(block: Block, blocks: list[Block]) -> bool:
    """A short label set in the corner beside a title at least SIDE_LABEL_SCALE times its size,
    on the same rows and clear of the title's lines: the kicker of a flyer ("EMPLOYER ACCIDENT
    INSURANCE" at the right edge of the lines of "How accident insurance can help ..."). It
    labels the page, it does not open a section, bold or not. Compared line by line: the title's
    box reaches under the label where its longest line runs on."""
    if len(block.text.split()) > SIDE_LABEL_WORDS:
        return False
    top, bottom = block.bbox[1], block.bbox[3]
    for other in blocks:
        if other is block or other.size < SIDE_LABEL_SCALE * block.size:
            continue
        for line in other.lines:
            if (min(bottom, line.bottom) - max(top, line.top) > 0.5 * (bottom - top)
                    and (line.x1 <= block.bbox[0] or line.x0 >= block.bbox[2])):
                return True
    return False


def _is_checklist_label(block: Block, body: float, blocks: list[Block]) -> bool:
    """A short bold label at body size with a bare number a few lines above or below it, in the
    same column: the column header of a checklist ("Publication" over "501") or its last label
    ("Schedule A (Form 1040)" after "970"). Such a label names the numbers, it is not a heading.
    A bold heading in larger type, or with no number beside it, is not affected."""
    if not (block.bold and body - BODY_SIZE_SLACK <= block.size < body * HEADING_SCALE
            and len(block.text.split()) <= 4):
        return False
    top, bottom = block.bbox[1], block.bbox[3]
    reach = 3 * block.size
    for other in blocks:
        if other is block or not other.text.strip().isdigit():
            continue
        o = other.bbox
        if min(block.bbox[2], o[2]) - max(block.bbox[0], o[0]) <= 0:
            continue  # not in the same column
        if max(o[1] - bottom, top - o[3]) <= reach:
            return True
    return False


STEP_LABEL = re.compile(r"^Step\s+[0-9]+:\s")


def _is_form_step_label(block: Block, blocks: list[Block]) -> bool:
    """A bold "Step N:" lead-in of a form section, set in a narrow left column so that its title
    wraps onto several lines ("Step 1:" / "Enter" / "Personal" / "Information" on the W-4), with
    the form's instructions beside it. Its line count alone does not make it body text. A run-in
    sentence whose text goes on in regular type is not bold all through, so it stays as it is."""
    if not (block.bold and len(block.lines) <= 5 and STEP_LABEL.match(block.text)):
        return False
    top, bottom = block.bbox[1], block.bbox[3]
    return any(b is not block and b.bbox[0] >= block.bbox[2] and b.bbox[1] < bottom
               and b.bbox[3] > top for b in blocks)


NUMBERED_CAPS = re.compile(r"^\d{1,2}\.\s+[A-Z][A-Z ,&'-]+$")
NUMBERED_CAPS_SCALE = 0.8  # of the body size
STACKED_TITLE_SCALE = 1.5  # a part title is set at least this much larger than the body text
STACKED_TITLE_LINES = 8
STACKED_TITLE_WORDS = 3  # words per line, on average: a title wraps in a narrow column, prose does not


def _is_stacked_title(block: Block, body: float) -> bool:
    """A title set large in a narrow column, so that it runs over many short lines ("Standard /
    Deduction, / Itemized / Deductions, and / Other / Deductions" under "Part Three."). Display
    type that big is a title, not a paragraph; prose would not be set in lines of a word or two."""
    if block.size < body * STACKED_TITLE_SCALE or len(block.lines) > STACKED_TITLE_LINES:
        return False
    if len(block.text) > HEADING_MAX_CHARS or any(l.size != block.size for l in block.lines):
        return False
    return len(block.text.split()) <= STACKED_TITLE_WORDS * len(block.lines)


ADDRESS = re.compile(r"\S@\S|\bwww\.|https?://", re.I)  # an email or a web address
REQUIRED_MARK = re.compile(r"\s\*$")  # "NAME *": a form marks its required fields


def _is_standalone_bold_sentence(block: Block, blocks: list[Block]) -> bool:
    """A subheading that is one bold sentence ("Ways to check on the status of your refund."):
    the whole block is bold, and the block under it is set in the regular weight, apart from it.
    A bold lead-in that runs on into its paragraph shares a line with regular type, so its line
    is not bold and the block is not either; a bold item of a list or a sentence that goes on
    below is not a subheading."""
    text = block.text.strip()
    if (not text.endswith(".") or block.item or block.marker is not None or block.entry
            or len(block.lines) > 2 or not text[:1].isupper() or len(text.split()) < 3
            or re.search(r"[.!?]\s", text) or ADDRESS.search(text) or _fills_line(block, blocks)):
        return False  # an email or a web address: an instruction ("email this form to ...")
    idx = next((k for k, b in enumerate(blocks) if b is block), None)
    if idx is None or idx + 1 >= len(blocks):
        return False
    under = blocks[idx + 1]
    head = under.first.text.strip()[:1]
    return (not any(l.bold for l in under.lines) and under.size >= block.size - 0.5
            and bool(head) and not head.islower()
            and under.first.top >= block.lines[-1].top
            and under.first.x0 < block.bbox[2] and block.bbox[0] < under.first.x1)


def _is_centred_caps(block: Block, body: float, blocks: list[Block], ruled: Sequence[BBox]) -> bool:
    """A title in capitals on one line at about body size, centred on the text of its page ("AERO-
    NAUTIC SYMBOLS" over a symbols table): a section title, not a paragraph. A line in a ruled box
    is a label of the box, not a title."""
    if len(block.lines) != 1 or len(block.text.split()) < 2 or len(block.text) > BOLD_HEADING_MAX_CHARS:
        return False
    # A numbered title ("1. FUNDAMENTAL AND DERIVED UNITS") is set a size under the title of the
    # page, and the body size of a document is the size of its long lines, so it may be smaller.
    floor = body * NUMBERED_CAPS_SCALE if NUMBERED_CAPS.match(block.text.strip()) else body - BODY_SIZE_SLACK
    if block.size < floor or not block.text.isupper() or any(geometry.inside(block.bbox, box) for box in ruled):
        return False
    x0 = min(b.bbox[0] for b in blocks)
    x1 = max(b.bbox[2] for b in blocks)
    return abs((block.bbox[0] + block.bbox[2]) - (x0 + x1)) / 2 <= CENTRE_TOL


@dataclass(frozen=True)
class InkCue:
    """Why a scanned line looks like a heading on the page image when its text cannot say."""
    stroke: float | None    # Line.stroke of the one line
    underlined: bool        # an underline runs under the whole line
    why: Why                # HeadingWhy.INK, with what the cue saw


# A sentence of SENTENCE_WORDS words or more, set larger than the body text but below
# DISPLAY_SCALE times it, is an instruction or a notice set large, not a heading.
SENTENCE_WORDS = 8
DISPLAY_SCALE = 1.5


NOTE_MARKS = " *†‡"  # a note mark after a sentence's full stop: "in Arizona.*"


def is_sentence(text: str) -> bool:
    text = text.strip().rstrip(NOTE_MARKS)
    return (text.endswith(".") and len(text.split()) >= SENTENCE_WORDS
            and not NUMBERED_TITLE.match(text))


def _is_large_sentence(block: Block, body: float) -> bool:
    """A sentence set a little larger than the body text ("Read, sign, and date the Release and
    Attestation on the next page.", "D. All dates must include the four-digit year."): see
    is_sentence. Display type, set DISPLAY_SCALE times the body text or more, is a title even
    when it is a sentence."""
    return block.size < body * DISPLAY_SCALE and is_sentence(block.text)


def _size_heading(block: Block, body: float) -> bool:
    return block.size >= body * HEADING_SCALE and len(block.text) <= HEADING_MAX_CHARS


def underlined(line: Line) -> bool:
    """Whether an underline runs under the whole OCR line: it has graded words (Line.grades), at
    least 2 of those with a letter, each of them underlined (GradedWord.underlined), and
    together they span UNDERLINE_COVER of the line's width. Words without a letter (a dash, a
    bracket) are left out, and each word is judged by its own box, never matched by its text. The
    spaces between the words count as covered (WORD_SPACE), as the underline runs on under them."""
    words = [w for w in line.grades or () if any(c.isalpha() for c in w.layer_text)]
    if len(words) < 2 or not all(w.underlined for w in words):
        return False
    covered, reach = 0.0, None
    for x0, x1 in sorted((w.bbox[0], w.bbox[2]) for w in words):
        if reach is not None and 0 < x0 - reach <= WORD_SPACE * line.height:
            x0 = reach
        start, end = max(x0, line.x0 if reach is None else reach), min(x1, line.x1)
        if end > start:
            covered, reach = covered + end - start, end
    return covered >= UNDERLINE_COVER * (line.x1 - line.x0)


def ink_cue(block: Block, body: float, gate: bool = True) -> InkCue | None:
    """The page-image cue of a one-line, all-OCR block that the size rule does not already make a
    heading: its stroke weight is at least STROKE_HEADING, or it is underlined. Only blocks of at
    least INK_MIN_LETTERS letters that do not end in a sentence's punctuation or a colon (a bold
    lead-in to a list or a value), and, with `gate`, whose text ocr_gate trusts. The heading rule
    tries it after every rule that reads the text (from_ink); refused_ink tries it without."""
    if len(block.lines) != 1 or not block.first.ocr or _size_heading(block, body):
        return None
    text = block.text.strip()
    if (sum(c.isalpha() for c in text) < INK_MIN_LETTERS or text.endswith((".", ",", ";", ":"))
            or (gate and ocr_gate(block.lines) is not None)):
        return None
    stroke, under = block.first.stroke, underlined(block.first)
    heavy = stroke is not None and stroke >= STROKE_HEADING
    if not heavy and not under:
        return None
    seen = [f"strokes {stroke:.2f}× as heavy as the page's text"] if heavy else []
    seen += ["underlined"] if under else []
    return InkCue(stroke, under,
                  heading_why(HeadingWhy.INK, f"{' and '.join(seen)} on the page image"))


def from_ink(block: Block, body: float, why: str) -> InkCue | None:
    """The cue that made the block a heading, given the heading rule's reason; None when a rule
    that reads the text did. Its style is not ranked and its node goes to review, so a heading
    the text already shows keeps its level and its confidence."""
    return ink_cue(block, body) if code_of(why) == HeadingWhy.INK else None


def refused_ink(block: Block, body: float) -> InkCue | None:
    """The ink cue of a line whose OCR text ocr_gate refuses as a heading: it looks like a heading
    on the page image, but its text may not be navigation, so it stays a paragraph and its cue
    goes to review ("heading-from-ink"), which asks the reviewer to read the heading."""
    if ocr_gate(block.lines) is None:
        return None
    cue = ink_cue(block, body, gate=False)
    # The heavy stroke of type, not an underline: on a tracked-change page an underline marks an
    # inserted passage ("technologies; and"), which its fragment of a sentence would not be.
    return cue if cue is not None and cue.stroke is not None and cue.stroke >= STROKE_HEADING \
        else None


def set_apart(block: Block, body: float) -> str | None:
    """What sets the block apart from the body text in its type, whatever the heading rule
    decided: "larger" (HEADING_SCALE times the body text), "bold" (at body size or more) or
    "ink" (a heading's stroke or underline on a scan, ink_cue). None when nothing does. The
    page-vision checks read it as a heading's witness (analyze.vision_gates)."""
    if block.size >= body * HEADING_SCALE:
        return "larger"
    if block.bold and block.size >= body - BODY_SIZE_SLACK:
        return "bold"
    return "ink" if ink_cue(block, body) is not None else None


def ranks(block: Block, body: float, why: str) -> bool:
    """Whether a heading's style takes part in ranking the levels (heading_levels), given the
    heading rule's reason. A heading its type makes does. One the page image (from_ink) or its
    text alone (a section number with its title) makes does not: its type says nothing of its
    rank, and OCR may read its size short, so it takes the level of the ranked style closest in
    size (style_level)."""
    return code_of(why) not in (HeadingWhy.SECTION_TITLE, HeadingWhy.INK)


def ranked_style(block: Block, body: float, blocks: list[Block], ruled: Sequence[BBox] = (),
                 fields: Sequence[BBox] = ()) -> tuple[float, bool] | None:
    """The style the block's type ranks in heading_levels, or None (see ranks). A word that a sure
    reading contradicts keeps its text out of navigation, not its style out of the ranking:
    the gate judges it as the unconfirmed word it was before (ocr_gate's `ranking`), so naca's
    garbled running heads still rank and its levels stay where they were."""
    style, why = _heading_style(block, body, blocks, ruled, fragments=True, fields=fields,
                                ranking=True)
    return style if style and ranks(block, body, why) else None


def demote(node: Node, reason: str) -> None:
    """Make a heading a paragraph because its text may not be navigation; the reason goes in its
    `why`, as the heading rule writes a refused candidate's."""
    node.role = Role.P
    for key in ("style_level", "label_caps", "index_letter", "outline"):
        node.attrs.pop(key, None)
    node.attrs.setdefault("why", []).append(f"paragraph: {reason}")


def _heading_style(block: Block, body: float, blocks: list[Block], ruled: Sequence[BBox],
                   fragments: bool, fields: Sequence[BBox] = (),
                   ranking: bool = False) -> tuple[tuple[float, bool] | None, str]:
    """The heading decision without its sentence check when `fragments` is False, and with the
    OCR text gate's `ranking` rule when `ranking` is True (ranked_style). The
    checks that need the blocks around the candidate are made here, so a candidate that a
    wrapped-heading join would complete is judged in the same context."""
    text = block.text
    if reason := ocr_gate(block.lines, ranking):
        return None, reason
    if block.label is not None:
        return None, "a worksheet line number: a list item, not a heading"
    if _is_section_title(block, body):
        return (block.size, block.bold), heading_why(HeadingWhy.SECTION_TITLE)
    if (len(block.lines) > 3 and not _is_form_step_label(block, blocks)
            and not _is_stacked_title(block, body)):
        return None, "more than 3 lines"
    if not text:
        return None, "no text"
    if block.foot:
        return None, "set in the bottom margin: a footer notice, nothing follows it"
    if block.entry:
        return None, "a wrapped index entry: its page number ends it"
    if text.startswith("*"):
        return None, "starts with an asterisk: a footnote to the form"
    if text.lstrip()[:1] in BULLET_GLYPHS:
        return None, "starts with a bullet"
    index_letter = is_index_letter(block, blocks)
    if not index_letter and len(re.sub(r"[^\w]", "", text)) < 2:
        return None, "a single character or symbol"
    if not index_letter and ROMAN_ONLY.match(text.strip()):
        return None, "a lone roman numeral (a folio or a list letter)"
    if not any(c.isalpha() for c in text):
        return None, "a number alone (a folio, a form number or a logo numeral)"
    if CALLOUT_LABEL.match(text.strip()):
        return None, "a callout label (TIP, CAUTION...): a note, not a heading"
    if is_form_line_label(text):
        return None, "the label of a line of a form (LINE 11): text that points at the line, not a heading"
    if fragments and (why := _is_fragment(block, blocks)) is not None:
        display_title = (block.bold and 1 < len(block.lines) <= 3
                         and block.size >= 2 * body and len(text) <= HEADING_MAX_CHARS)
        if not display_title:
            return None, why
    if DOT_LEADER.search(text):
        return None, "dot leaders: an index or table of contents entry"
    if CAPTION_BLOCK.match(text.lstrip()) or CAPTION_TITLE.match(text.lstrip()):
        # A caption at heading size is still a caption; it must not shift the heading levels
        return None, "a caption: starts with Figure, Table or Chart and a number"
    if text.endswith(":") and (why := _index_evidence(block, blocks)) is not None:
        return None, f"ends with a colon and {why}"
    if _is_title_note(block, blocks):
        return None, "a note set beside a numbered section title"
    if is_label_value(text):
        return None, "an address, a fee or a label with its value: form data, not a heading"
    if _has_row_twin(block, blocks):
        return None, heading_why(HeadingWhy.ROW_TWIN)
    if _is_form_label(block, body, ruled, blocks):
        return None, "a short label inside a ruled form area"
    if _is_column_label(block, blocks):
        return None, "a lone column header beside a larger heading"
    if _is_side_label(block, blocks):
        return None, "a short label set beside a much larger title: a kicker, not a section"
    if _is_row_label(block, blocks, fields):
        return None, "a label that ends in a colon with text beside it on its row: form data"
    if fields and REQUIRED_MARK.search(text):
        return None, "ends in a required-field mark on a page of form controls: a field label"
    if _is_checklist_label(block, body, blocks):
        return None, "a label of a checklist: the numbers in its column name it, not a heading"
    if _size_heading(block, body) and _is_large_sentence(block, body):
        return None, f"a sentence of {len(block.text.split())} words at {block.size:g} pt: set large, not a heading"
    if _size_heading(block, body):
        return (block.size, block.bold), f"{block.size:g} pt is {block.size / body:.2f}× body text ({body:g} pt)"
    if (block.bold and block.size >= body - BODY_SIZE_SLACK and len(text) <= BOLD_HEADING_MAX_CHARS
            and (not text.rstrip(NOTE_MARKS).endswith((".", ",", ";"))
                 or _is_standalone_bold_sentence(block, blocks) or NUMBERED_TITLE.match(text))):
        if block.size < body and not text.isupper():  # capitals are a section title, not an entry
            return None, f"bold but smaller than body text ({block.size:g} pt): an index entry"
        return (block.size, True), f"bold, short, at body size ({block.size:g} pt)"
    if _is_centred_caps(block, body, blocks, ruled):
        return (block.size, block.bold), f"one centred line of capitals at body size ({block.size:g} pt)"
    if (cue := ink_cue(block, body)) is not None:
        return (block.size, True), cue.why
    if block.size >= body * HEADING_SCALE:
        return None, f"longer than {HEADING_MAX_CHARS} characters"
    if block.bold and block.size >= body - BODY_SIZE_SLACK:
        return None, f"bold but longer than {BOLD_HEADING_MAX_CHARS} characters or ends with punctuation"
    return None, f"{block.size:g} pt is body text ({body:g} pt)"


def heading_decision(block: Block, body: float, blocks: list[Block], ruled: Sequence[BBox] = (),
                     fields: Sequence[BBox] = ()) -> tuple[tuple[float, bool] | None, str]:
    """(style, why): the (size, bold) style if this block looks like a heading, else None,
    and the reason either way. `ruled` are the boxes of ruled form areas on the page, `fields`
    those of its form widgets."""
    return _heading_style(block, body, blocks, ruled, fragments=True, fields=fields)


def nxt_word(block: Block) -> bool:
    """Whether a block that opens with a bracket goes on in words ("(ATIN)", "(F)") rather
    than a number ("(877) 276-9637" is a phone number, a line of its own)."""
    return block.first.text.strip()[1:2].isalpha()


INDEX_LINE_SHARE = 0.4  # on an index page, this share of the lines end in page numbers
INDEX_PAGE_LINES = 10  # and at least this many lines do: a short page of figures is not one
# An index line ends in a page number of two or more digits ("Losses 23", "Tax 62-66, 76")
INDEX_LINE = re.compile(r"^[A-Za-z][^()]*\s\d{2}[\d,\s–-]*$")


def is_index_lines(lines: Sequence[Line]) -> bool:
    """Whether a page is an index, by its lines: most end in page numbers, with or without
    dot leaders ("Losses 23", "Emergency Fund ....1201")."""
    refs = sum(1 for l in lines if INDEX_LINE.search(l.text.strip())
               or PAGE_REFERENCE.search(l.text))
    return refs >= INDEX_PAGE_LINES and refs >= INDEX_LINE_SHARE * len(lines)


def _index_page(blocks: list[Block]) -> bool:
    refs = sum(1 for b in blocks if PAGE_REFERENCE.search(b.text) or ENTRY_REFERENCE.search(b.text))
    return refs >= INDEX_PAGE_MIN and refs >= INDEX_PAGE_SHARE * len(blocks)


def is_index_letter(block: Block, blocks: list[Block]) -> bool:
    """Whether the block is a section letter of an index ("Q", "R"): a lone capital set larger
    than the index entries on its page. A single character is never a heading elsewhere, but
    on an index the entries are the smaller text, so the letter is a heading."""
    if not re.fullmatch(r"[A-Z]", block.text.strip()) or not _index_page(blocks):
        return False
    entry_sizes = [b.size for b in blocks
                   if PAGE_REFERENCE.search(b.text) or ENTRY_REFERENCE.search(b.text)]
    return bool(entry_sizes) and block.size > max(entry_sizes)


def _fills_line(block: Block, blocks: list[Block]) -> bool:
    """Whether the block's last line runs to the right edge of its column (the widest block that
    sits beside it), so that the text went on to a next line rather than ending there."""
    right = max([b.bbox[2] for b in blocks if b is not block
                 and b.bbox[0] < block.bbox[2] and b.bbox[2] > block.bbox[0]] or [block.bbox[2]])
    return block.lines[-1].x1 >= right - 4 * block.size


def _wrap_kind(top: Block, nxt: Block, body: float, blocks: list[Block],
               ruled: Sequence[BBox], indexy: bool) -> str | None:
    """How `nxt` completes the heading candidate `top`, so that the two are one unit: "index"
    for a wrapped index entry and its page number ("Telecommunications Relay Services Fund,
    Federal Communications" / "Commission 1163"), "style" for a name or title set on two lines
    in the same style ("Felix Tacoronte, Carmen L, MD" / "(F)"), "title" for the dashed second
    line of a centred title (_is_title_line), or None. A run-in lead in
    another style is not joined: heading_decision demotes it."""
    if len(top.lines) + len(nxt.lines) > 3 or not _continues(top, nxt):
        return None
    if DOT_LEADER.search(nxt.text) or is_contents_entry(nxt.text):
        return None  # a contents entry: its own path (see reflow.leaders)
    if not _fills_line(top, blocks) or _heading_style(top, body, blocks, ruled, False)[0] is None:
        return None
    if (indexy and top.size <= body + 0.5 and not top.text.rstrip().endswith(TERMINAL)
            and len(nxt.text.split()) <= 4
            and ENTRY_REFERENCE.search(nxt.text) and not re.search(r"\(\s*cont", top.text, re.I)):
        return "index"
    head = nxt.first.text.strip()[:1]
    if nxt.size == top.size and nxt.bold == top.bold and (head.islower() or (head == "(" and nxt_word(nxt))):
        return "style"
    if _is_title_line(top, nxt):
        return "title"
    return None


def _is_title_line(top: Block, nxt: Block) -> bool:
    """Whether `nxt` is the second line of a centred title whose text layer starts it with a
    stray dash ("- CURVE OF A VIBRATING CANTILEVER BEAM" under "EFFECT OF ... ELASTIC"): the same
    style, and both lines centred on one axis."""
    if nxt.size != top.size or nxt.bold != top.bold or not TITLE_DASH.match(nxt.first.text):
        return False
    return abs((top.bbox[0] + top.bbox[2]) - (nxt.bbox[0] + nxt.bbox[2])) / 2 <= CENTRE_TOL


def _title_wraps(top: Block, nxt: Block) -> bool:
    """Whether `nxt` is the rest of a section title that `top` cuts at the end of its row ("Sec.
    25A.0041. RULES RELATED TO JURISDICTIONAL" / "DETERMINATION."): `top` is one row holding a
    section number and capitals with no full stop, and `nxt` is the next row, at the column's
    left, in the same style, in capitals that end in the title's full stop. The body text that
    runs on after the title on that row is a block of its own (a printed legal label opens it)."""
    head = top.text.strip()
    if section_title(head) is None or head.endswith(".") or _rows(top) != 1 or _rows(nxt) != 1:
        return False
    tail = nxt.text.strip()
    if not (tail.isupper() and tail.endswith(".")) or nxt.size != top.size or nxt.bold != top.bold:
        return False
    gap = nxt.first.top - top.first.bottom
    return 0 <= gap <= 2 * top.size and nxt.bbox[0] <= top.bbox[0]


CHAPTER_NUMERAL = re.compile(r"^\d{1,2}\.$")  # a chapter number set alone on its line: "10."


def _numeral_joins(top: Block, nxt: Block, body: float, blocks: list[Block],
                   ruled: Sequence[BBox]) -> bool:
    """Whether `top` is a chapter numeral set alone above the title `nxt` that starts at the same
    left edge ("10." over "Standard Deduction"): the two are one heading. The numeral is set in
    display type and the title sits under it within a line of the numeral. A list number hangs
    to the left of its text and so never overlaps it; a bare number with no title is no heading."""
    if len(top.lines) != 1 or not CHAPTER_NUMERAL.match(top.text.strip()):
        return False
    if top.size < body * HEADING_SCALE:
        return False
    gap = nxt.first.top - top.lines[-1].bottom
    if not 0 <= gap <= top.size:
        return False
    if not (nxt.first.x0 < top.lines[-1].x1 and top.first.x0 < nxt.first.x1):
        return False
    return _heading_style(nxt, body, blocks, ruled, False)[0] is not None


def join_wrapped_headings(blocks: list[Block], body: float,
                          ruled: Sequence[BBox] = ()) -> list[Block]:
    """Join the blocks that one heading candidate runs into (see _wrap_kind). A joined index
    entry is marked as one (Block.entry): its page number ends it, so it is never a heading."""
    blocks = list(blocks)
    indexy = _index_page(blocks)
    i = 0
    while i < len(blocks) - 1:
        top, nxt = blocks[i], blocks[i + 1]
        if i + 2 < len(blocks):
            continuation = blocks[i + 2]
            if _wraps_around_overlay(top, nxt, continuation, body, blocks, ruled):
                blocks[i:i + 3] = [Block(top.lines + continuation.lines, why=top.why), nxt]
                continue
        if SECTION_LABEL.match(top.text) and top.bold:  # "SECTION 4." and its title "VALUE", set beside it
            title = next((b for b in blocks[i + 1:] if b.bold and b.size == top.size
                          and b.first.x0 >= top.first.x1 - 1
                          and abs(b.first.top - top.first.top) < 0.5 * top.size), None)
            if title is not None:
                blocks.remove(title)
                blocks[i] = Block(top.lines + title.lines, why=top.why)
                continue
        if _title_wraps(top, nxt):
            blocks[i:i + 2] = [Block(top.lines + nxt.lines, why=top.why)]
            continue
        if _numeral_joins(top, nxt, body, blocks, ruled):
            blocks[i:i + 2] = [Block(top.lines + nxt.lines, why=top.why)]
            continue  # the joined block may take another line
        if (kind := _wrap_kind(top, nxt, body, blocks, ruled, indexy)) is not None:
            lines = nxt.lines
            if kind == "title":  # the stray dash is in the text layer only, not on the page
                dash = TITLE_DASH.match(lines[0].text)
                lines = [lines[0].cut(dash.end(), len(lines[0].text)) if dash else lines[0]] + lines[1:]
            blocks[i:i + 2] = [Block(top.lines + lines, why=top.why, entry=kind == "index")]
            continue  # the joined block may take another line
        i += 1
    return blocks


def _wraps_around_overlay(top: Block, overlay: Block, continuation: Block, body: float,
                          blocks: list[Block], ruled: Sequence[BBox]) -> bool:
    """Keep a display heading together when a small, right-aligned label overlaps its wrap."""
    return (
        bool(_heading_style(top, body, blocks, ruled, False)[0])
        and _continues(top, continuation)
        and continuation.size == top.size
        and continuation.bold == top.bold
        and continuation.first.text.strip()[:1].islower()
        and abs(continuation.first.x0 - top.first.x0) <= 3
        and len(overlay.lines) <= 2
        and overlay.text.isupper()
        and len(overlay.text.split()) <= 3
        and overlay.size <= 0.6 * top.size
        and overlay.first.x0 > top.first.x0 + (top.bbox[2] - top.bbox[0]) / 2
        and min(top.bbox[3], overlay.bbox[3]) > max(top.bbox[1], overlay.bbox[1])
    )


# A heading style gets its own level only if this share of headings (and at least 2)
# use it; one-off cover text and decorative numbers would otherwise take the top levels.
FREQUENT_STYLE = 0.002


# The share is capped at this many uses. Without the cap, a large document needs more uses of a
# style than a smaller one to give it a level, so one extra style (18 pt plan names: 96 uses in
# maricopa, 12 in pima) moved every smaller style down a level in one directory and not the other.
FREQUENT_USES = 10
MIN_HEADINGS_FOR_FILTER = 20  # short documents: every style counts


RARE_LARGER_MAX_STYLES = 4  # see heading_levels: a form or short notice, not a noisy scan or a book
RARE_LARGER_GAP = 0.4  # points: a style this much larger than the frequent ones ranks above them
RARE_LARGER_SCALE = 1.25  # but not display type: one-off huge text takes the nearest level


def heading_levels(styles: Counter) -> dict[tuple[float, bool], int]:
    """Map heading styles to levels 2-6 (level 1 is reserved for the document title).
    Frequent styles are ranked larger first, bold before regular at the same size;
    rare styles take the level of the frequent style closest in size."""
    total = sum(styles.values())
    short = total < MIN_HEADINGS_FOR_FILTER
    min_uses = 1 if short else max(2, min(FREQUENT_STYLE * total, FREQUENT_USES))
    # In a short document the largest style, used once, is a display line (a form number such as
    # "W-4"), not a section style. It takes the level nearest its size instead of a level of its own,
    # so the sections under it start at h2 rather than h3.
    display = None
    if short and len(styles) > 1:
        top = min(styles, key=lambda st: (-st[0], not st[1]))
        if styles[top] == 1:
            display = top
    frequent = sorted((st for st, n in styles.items() if n >= min_uses and st != display),
                      key=lambda st: (-st[0], not st[1]))
    if frequent and len(styles) <= RARE_LARGER_MAX_STYLES:
        # In a short document a style set larger than every frequent one, even once, is a part
        # title over the sections ("Information and Instructions" over "Section 1."), so it
        # ranks above them instead of sharing their level.
        top_size = frequent[0][0]
        frequent = sorted({*frequent, *(st for st in styles if st != display
                                        and top_size + RARE_LARGER_GAP <= st[0] <= RARE_LARGER_SCALE * top_size)},
                          key=lambda st: (-st[0], not st[1]))
    levels = {st: min(i + 2, 6) for i, st in enumerate(frequent)}
    for st in styles:
        if st not in levels:
            nearest = min(frequent, key=lambda f: (abs(f[0] - st[0]), f[0] < st[0]),
                          default=None)
            levels[st] = levels[nearest] if nearest else 2
    return levels


def style_level(levels: dict[tuple[float, bool], int], style: tuple[float, bool]) -> int:
    """The level of a heading style. A style left out of the ranking (see ranks: an ink
    heading's or a section title's must not move the levels of the headings their type makes)
    takes the level of the ranked style closest in size, as a rare style does."""
    if style in levels:
        return levels[style]
    nearest = min(levels, key=lambda f: (abs(f[0] - style[0]), f[0] < style[0]), default=None)
    return levels[nearest] if nearest else 2


def fix_heading_levels(nodes: list[Node]) -> None:
    """No skipped levels (OUT-H-001), with siblings kept level.

    Each heading's current level is its "natural" level. A stack holds the natural levels
    of the open sections; a heading closes every section at its level or deeper, opens
    its own, and its output level is its depth in the stack (h2 at the top, under the
    h1 title). So natural levels 2, 3, 5, 5 come out as h2, h3, h4, h4.
    """
    stack: list[int] = []
    index_level: int | None = None  # the natural level of the nearest "Index" heading
    # The numbered form section open on the page (its natural level and page): a bold-caps label
    # of the same level under it is the section's sub-label, one level down, until the next heading.
    section: tuple[int, int] | None = None
    for node in nodes:
        if node.role not in HEADING_ROLES:
            continue
        if node.role == Role.H1:  # the title, from choose_title
            stack = []
            index_level = None
            continue
        natural = max(HEADING_LEVEL[node.role], 2)
        if node.text.strip().lower() == "index":
            index_level = natural
        elif node.attrs.get("index_letter") and index_level is not None:
            # The section letters of an index sit under its heading, one level down at most
            # to the third: the document-wide size ranking puts them with the entries.
            natural = min(index_level + 1, 3)
        if NUMBERED_SECTION.match(node.text.strip()):
            section = (natural, node.page)
        elif (section and node.attrs.get("label_caps") and natural == section[0]
                and node.page == section[1] and is_label_caps(node.text)):
            natural = min(natural + 1, 6)
        else:
            section = None
        while stack and stack[-1] >= natural:
            stack.pop()
        stack.append(natural)
        node.role = HEADING_BY_LEVEL[min(len(stack) + 1, 6) - 1]
