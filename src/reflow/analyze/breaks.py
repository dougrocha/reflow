"""Block breaks: where one block ends and the next begins, and the pass that turns
ordered lines into blocks.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from reflow.analyze.blocks import (
    Block,
    Break,
    at_text_x,
    column_right,
    ends_hyphenated,
    ends_mid_phrase,
    line_fills,
    marker_of,
    reads_rtl,
    shares_column,
    text_offset,
    wrap_offsets,
    wraps_entry,
)
from reflow.analyze.constants import (
    ALIGN_TOL,
    CALLOUT_LABEL,
    CONTINUED,
    INDEX_REFERENCE,
    LIST_MARKER,
    NUMBERED_TITLE,
    WORKSHEET_SUB_START,
)
from reflow import geometry
from reflow.analyze.context import PageContext
from reflow.analyze.headings import underlined
from reflow.analyze.lists import continuable, index_items, list_items
from reflow.analyze.rhythm import Rhythm, line_gap
from reflow.analyze.worksheets import worksheet_rows
from reflow.extract.lines import Line
from reflow.model import HEADING_ROLES, BBox, Node, Role, Source
from reflow.leaders import is_contents_entry


BARE_CONTINUED = re.compile(r"^\(\s*continued\s*\)$", re.I)


# A sentence that ends this many font sizes short of its column's right edge ends its paragraph
PARAGRAPH_GAP = 5.0


def break_reason(prev: Line, line: Line, starts: bool | None = None,
                 text_x: float | None = None, item: bool = False,
                 rhythm: Rhythm | None = None,
                 page_lines: Sequence[Line] | None = None) -> str | None:
    """Why `line` starts a new block after `prev`, or None if it continues the block.
    `starts` says whether the line begins with a list marker (make_blocks decides that
    with the line before it; by default the marker alone decides). `text_x` is the x where
    the text of the list item that prev's block may open starts: a line set at that x is the
    item's hanging indent, not the first-line indent of a new paragraph. `item` says prev's
    block opens with a marker: a lowercase line set directly under it carries its sentence on
    (its x is checked later, see leaves_item). `rhythm` is the page's leading on a scan: a line
    set at it is directly under `prev`, with no gap (see rhythm.line_gap). `page_lines` are the
    page's lines, on a contents page (see wraps_entry)."""
    if is_contents_entry(line.text) and not wraps_entry(prev, line, page_lines):
        return "contents entry"
    if is_contents_entry(prev.text):
        return "after a contents entry"
    if (prev.x0 >= line.x1 and line.top >= prev.bottom - 0.25 * prev.height
            and not (prev.ocr or line.ocr)):
        # A column label ("Page" at the right edge of a contents page) over a line that ends
        # before it begins: the two share no column, so neither carries the other on. Lines
        # side by side on one row are the line reader's to join, and the boxes of OCR lines are
        # too loose to say (the lines of a scanned form).
        return "the line above sits wholly to the right of this one (a column label)"
    if ends_hyphenated(prev.text) and not shares_column(prev, line):
        # "afford-" at the end of a column is not "afford" + the next column's "rural".
        return Break.HYPHEN_COLUMN
    if abs(prev.size - line.size) > 0.5:
        return f"font size changes from {prev.size:g} to {line.size:g} pt"
    # An inserted passage of a tracked change is underlined too, and it carries its sentence on
    # into the next line.
    if (underlined(prev) and not underlined(line) and not ends_mid_phrase(prev.text)
            and not prev.text.rstrip().endswith((".", "!", "?"))
            and not line.text.strip()[:1].islower()):
        return "underlined line ends"
    # A bold word split by its hyphen ("de-" then "duction.") is one word, and the rest of it
    # is set in the body's weight: the weight change does not end the paragraph.
    word_split = ends_hyphenated(prev.text) and line.text.strip()[:1].islower()
    # A bold sentence that wraps ("... appeal. **Do not**" / "**attach** ...") has a plain line
    # then a bold one, but the bold run goes on across the wrap: the weight did not change. The
    # next line must carry the sentence on (lowercase): a bold heading in capitals under a
    # paragraph that ends in a bold run ("SECTION 6.") still starts a block.
    continues = (prev.edge_style("tail")[0] == line.edge_style("head")[0]
                 and line.text.strip()[:1].islower())
    if prev.bold != line.bold and not word_split and not continues:
        return Break.BOLD_CHANGES
    gap = line_gap(prev, line, rhythm)
    if (prev.bold and prev.font != line.font and gap < -0.5 * prev.height
            and NUMBERED_TITLE.match(prev.text.strip())):
        return "a note set beside a numbered section title, in another font"
    if gap > 0.5 * max(prev.size, line.size):
        return f"gap of {gap:.1f} pt is more than half the font size"
    if gap < -prev.height:
        return "jumps back up the page (new column)"
    hanging = text_x is not None and at_text_x(line.x0, text_x, prev.size)
    # A centred, all-capital heading that wraps is set solid, and each line is centred, so
    # their x0 differ without being a first-line indent. Only a real wrap counts: the first
    # line ends where a sentence cannot (a comma, a semicolon, a hyphen, a connective). Two
    # distinct stacked headings ("DEPARTMENT OF THE TREASURY" / "INTERNAL REVENUE SERVICE")
    # stay apart.
    centered_caps = (prev.text.strip().isupper() and line.text.strip().isupper()
                     and abs((prev.x0 + prev.x1) / 2 - (line.x0 + line.x1) / 2) <= prev.size
                     and gap <= 0.25 * max(prev.size, line.size)
                     and ends_mid_phrase(prev.text))
    carried = (item and line.text.strip()[:1].islower()
               and -prev.height < gap <= 0.5 * prev.size
               and line.x0 < prev.x1 and prev.x0 < line.x1)
    indent, limit = line.x0 - prev.x0, 0.8 * max(prev.size, line.size)
    if indent > limit and reads_rtl(prev.text):
        # Right-to-left lines start at their right edge, so the indent is measured from there:
        # a short last line set flush right ("(711") is not indented.
        indent = prev.x1 - line.x1
    if (not hanging and not carried and not centered_caps
            and line.top >= prev.top + 0.5 * prev.height
            and indent > limit
            and not wraps_entry(prev, line, page_lines)):
        return Break.FIRST_LINE_INDENT
    if starts is None:
        starts = LIST_MARKER.match(line.text) is not None
    if starts:
        return Break.LIST_MARKER
    return None


def make_blocks(lines: list[Line], counts: Counter[tuple[int, float]] | None = None,
                index: bool = False, rhythm: Rhythm | None = None) -> list[Block]:
    """Group lines into blocks. A line that starts with a list marker begins a block; whether
    that block is a list item is decided afterwards, from the blocks around it. `counts` is
    the document's hanging indents (see wrap_offsets); by default this page's own. On an
    index page (`index`) each entry is a block of its own, and the entries are items. `rhythm`
    is the page's leading on a scan (see rhythm.Rhythm)."""
    if counts is None:
        counts = wrap_offsets(lines)
    labels: dict[int, str] = {}
    members: set[int] = set()
    if not index:
        lines, labels, members = worksheet_rows(lines)
    blocks: list[Block] = []
    prev_text = ""
    row_open = False  # the block is a worksheet line item, and its band goes on
    held: Block | None = None  # a callout label that fell inside a word split by its hyphen
    for pos, line in enumerate(lines):
        if not line.text.strip():
            continue
        if (held is None and blocks and CALLOUT_LABEL.match(line.text.strip())
                and ends_hyphenated(blocks[-1].lines[-1].text) and pos + 1 < len(lines)
                and lines[pos + 1].text.strip()[:1].islower()):
            held = Block([line], why="a callout label between a hyphenated line and its word: "
                          "set after the paragraph")
            continue
        if id(line) in labels and not labels[id(line)]:
            blocks.append(Block([line], row=True,
                                why="worksheet row: its letter, text and answer on one paragraph"))
            row_open = True
            prev_text = line.text
            continue
        if id(line) in labels:
            label = labels[id(line)]
            blocks.append(Block([line], label=label, why="worksheet line number" if label.isdigit()
                                else "worksheet sub-line: a lettered item of the line above"))
            row_open = True
            prev_text = line.text
            continue
        if row_open and id(line) in members and line.text.strip()[:1] not in WORKSHEET_SUB_START:
            blocks[-1].lines.append(line)
            prev_text = line.text
            continue
        row_open = False
        marker = marker_of(line.text, prev_text)
        prev_text = line.text
        if blocks:
            head = blocks[-1]
            offset = text_offset(head.first.x0, counts) if head.marker else None
            text_x = head.first.x0 + offset if offset is not None else None
            # A bare number ("519.") with no words after it continues the sentence above it.
            starts = marker is not None and bool(line.text.strip()[len(marker.text):].strip())
            reason = break_reason(head.lines[-1], line, starts=starts,
                                  text_x=text_x, item=head.marker is not None, rhythm=rhythm,
                                  page_lines=None if index else lines)
            if reason is None and head.row:
                reason = "ends the worksheet row"
            if index and reason in (Break.FIRST_LINE_INDENT, Break.BOLD_CHANGES) \
                    and _carries_on(head.lines[-1], line):
                reason = None  # a wrapped index entry: a hanging indent, or a bracket in roman type
            if reason is None and index and not wraps_entry(head.lines[-1], line) \
                    and _entry_starts(head.lines[-1], line, lines):
                reason = "a new index entry"
            if reason is None and _paragraph_ends(head.lines[-1], line, lines):
                reason = "a short last line ends the paragraph"
            if reason is None and _centred_title_ends(head.lines[-1], line, lines):
                reason = "a centred title in capitals ends over the paragraph"
        else:
            reason = "first line of the region"
        if reason is None:
            blocks[-1].lines.append(line)
        else:
            if held is not None:
                blocks.append(held)
                held = None
            blocks.append(Block([line], why=reason, marker=marker))
    if held is not None:
        blocks.append(held)
    for b in blocks:
        if b.label is not None and b.label.isdigit() and len(b.lines) == 1:
            b.label = None  # a number with no text beside it is a paragraph of its own
    if index:
        return index_items(blocks)
    return list_items(blocks, counts, lines, rhythm)


def _carries_on(prev: Line, line: Line) -> bool:
    """Whether `line` goes on with `prev` in an index: it opens in lower case or in brackets,
    or `prev` leaves a bracket open ("Estimated tax (See this heading:" / "Underpayment ...)")."""
    return (line.text.strip()[:1] in ("(",) or line.text.strip()[:1].islower()
            or prev.text.count("(") > prev.text.count(")"))


def _entry_starts(prev: Line, line: Line, lines: Sequence[Line]) -> bool:
    """Whether `line` begins a new index entry after `prev`. An entry starts with a capital.
    A lower-case line, or one in brackets, carries the entry on; so does a capital that
    wraps a long title, a line that has not reached the column edge, and does not end the
    entry (no page number, no colon)."""
    if _carries_on(prev, line) or not line.text.strip()[:1].isupper():
        return False
    if INDEX_REFERENCE.search(prev.text) or prev.text.rstrip().endswith(":"):
        return True
    return not line_fills(prev, lines)


def _paragraph_ends(prev: Line, line: Line, lines: Sequence[Line]) -> bool:
    """Whether a short last line ends the paragraph: a sentence ends it well short of the column
    edge, and the next line starts a new sentence at the same indent. ("…sign the return." is
    set short, then "Nonresident alien…" starts a paragraph.)"""
    if not (prev.text.rstrip().endswith((".", "?", "!")) and line.text.strip()[:1].isupper()):
        return False
    if abs(line.x0 - prev.x0) > 1.0 or not shares_column(prev, line):
        return False
    return column_right(prev, lines) - prev.x1 > PARAGRAPH_GAP * prev.size


# A centred title is set in from both ends of the paragraph line under it by this many font sizes
TITLE_INSET = 1.5


def _centred_title_ends(prev: Line, line: Line, lines: Sequence[Line]) -> bool:
    """Whether `prev` is a title in capitals centred over the paragraph that `line` starts, set
    solid on it: it does not end a sentence, it is set in from both ends of `line` and centred
    on it, and `line` starts at the left edge of the line under it, as a paragraph does, or is
    a paragraph of one line with the next title under it. Each line of a centred paragraph has
    its own left edge. ("RECLAMOS DE BLACK LUNG Y FECA" over "El proveedor acepta ...")"""
    text = prev.text.strip()
    inset = TITLE_INSET * prev.size
    if not (text.isupper() and len(text.split()) >= 2 and not text.endswith(".")
            and prev.x0 - line.x0 > inset and line.x1 - prev.x1 > inset
            and abs((prev.x0 + prev.x1) / 2 - (line.x0 + line.x1) / 2) <= prev.size):
        return False
    at = next((i for i, l in enumerate(lines) if l is line), None)
    under = lines[at + 1] if at is not None and at + 1 < len(lines) else None
    if under is None or under.top <= line.top or not shares_column(line, under):
        return False
    return abs(under.x0 - line.x0) <= 1.0 or (under.text.strip().isupper()
                                              and under.x0 - line.x0 > inset)


def link_pages(contexts: list[PageContext]) -> None:
    """Continue a legal run across a page break: "(a)" at the foot of one page and "(b)" at the
    head of the next are one list, as they would be on one page. Only the last legal label of
    a page and the first item at its indent on the next are compared: the same kind, the next
    value. Sub-items at the top of the next page continue the sub-list, not this one, so they
    are skipped. Anything else is left as it was."""
    for prev, nxt in zip(contexts, contexts[1:]):
        if nxt.page.number != prev.page.number + 1:
            continue
        tail = next((b for b in reversed(prev.blocks) if continuable(b)), None)
        if tail is None:
            continue
        head = next((b for b in nxt.blocks if continuable(b)
                     and abs(b.first.x0 - tail.first.x0) <= ALIGN_TOL * b.first.size), None)
        if head is None:
            continue
        t, h = tail.marker, head.marker
        if t.kind != h.kind or h.value != t.value + 1:
            continue
        if t.kind == "alpha" and t.text.strip("().").isupper() != h.text.strip("().").isupper():
            continue
        tail.item = head.item = True
        tail.why = head.why = "continues a list across the page break"


def _join_banner_stacks(blocks: list[Block]) -> list[Block]:
    """Join the lines of a page-top banner that wraps onto several lines: "PSYCHIATRIC NURSE"
    over "PRACTITIONER (continued)" is one banner, so one breadcrumb. Only bold banners at the
    top of the column, one directly above a "(continued)" banner of the same size, are joined;
    a text block whose line breaks differently (an indent) would otherwise split the banner."""
    blocks = list(blocks)
    merged = True
    while merged:
        merged = False
        for i in range(len(blocks) - 1):
            top, nxt = blocks[i], blocks[i + 1]
            if i and not (blocks[i - 1].bold and CONTINUED.search(blocks[i - 1].text)):
                break  # the banner stack starts at the top of the column
            if not (top.bold and nxt.bold and CONTINUED.search(nxt.text)
                    and not CONTINUED.search(top.text) and top.size == nxt.size):
                continue
            gap = nxt.first.top - top.lines[-1].bottom
            if not (-top.lines[-1].height < gap <= 0.5 * top.size
                    and top.bbox[0] < nxt.bbox[2] and nxt.bbox[0] < top.bbox[2]):
                continue
            blocks[i:i + 2] = [Block(top.lines + nxt.lines, why=top.why)]
            merged = True
            break
    return blocks


def split_banners(blocks: list[Block], boxes: Sequence[BBox], lines: Sequence[Line]) -> list[Block]:
    """Part a block whose neighbouring lines each fill a ruled box of their own (the banners of
    two columns that sit on one row, read one after the other): a banner is its own block."""
    def banner(line: Line) -> BBox | None:
        for box in boxes:
            if geometry.inside(line.bbox, box, slack=2):
                held = [o for o in lines if geometry.inside(o.bbox, box, slack=2)]
                return box if held == [line] else None
        return None

    out: list[Block] = []
    for block in blocks:
        if len(block.lines) < 2:
            out.append(block)
            continue
        homes = [banner(l) for l in block.lines]
        cuts = [i for i in range(1, len(homes))
                if homes[i - 1] is not None and homes[i] is not None and homes[i - 1] != homes[i]]
        if not cuts:
            out.append(block)
            continue
        edges = [0, *cuts, len(block.lines)]
        for k, (a, b) in enumerate(zip(edges, edges[1:])):
            out.append(Block(block.lines[a:b], why=block.why if k == 0 else
                             "a banner in a ruled box of its own", marker=block.marker if k == 0 else None))
    return out


def attach_continued(blocks: list[Block], artifacts: list[Line]) -> list[Block]:
    """Give a "(continued)" set on its own line back to the heading right above it: the
    previous block (a centered suffix reads as an indent and breaks the block), or a
    running-header line already taken out as an artifact."""
    out: list[Block] = []
    for block in blocks:
        if not BARE_CONTINUED.match(block.text):
            out.append(block)
            continue
        first = block.first

        def above(l: Line) -> bool:
            return (abs(l.size - first.size) <= 0.5 and l.bold == first.bold
                    and -l.height < first.top - l.bottom <= 0.5 * l.size
                    and l.x0 < first.x1 and first.x0 < l.x1)

        if out and above(out[-1].lines[-1]):
            out[-1].lines.extend(block.lines)
        else:
            head = [l for l in artifacts if above(l)]
            out.append(Block(head[-1:] + block.lines))
    # After the bare "(continued)" lines have joined their banners, so each banner is whole.
    return _join_banner_stacks(out)


def _context_label(text: str) -> str:
    """ "FAMILY NURSE PRACTITIONER (continued)" → "Family Nurse Practitioner"."""
    label = CONTINUED.sub("", text).strip()
    if label.isupper():
        label = re.sub(r"[A-Za-z]+('[A-Za-z]+)?", lambda m: m[0].capitalize(), label)
    return label


def merge_context(nodes: list[Node]) -> list[Node]:
    """Fold the "X (continued)" context paragraphs that open a page into one breadcrumb,
    "Continued: Non-Physicians › Family Nurse Practitioner › Gilbert". When the page then
    starts with something other than a heading (addresses of a provider named on the
    previous page), say so. Context paragraphs further down keep their own line."""
    lead = 0
    while lead < len(nodes) and nodes[lead].attrs.get("context"):
        lead += 1
    for node in nodes[lead:]:
        if node.attrs.get("context"):
            # A label, not page text in place: no span or mark means anything on it.
            node.spans, node.marks = [], []
            node.set_text(f"Continued: {_context_label(node.text or '')}")
    if not lead:
        return nodes
    head, rest = nodes[:lead], nodes[lead:]
    trail = [label for n in head if (label := _context_label(n.text or ""))]
    text = "Continued: " + " › ".join(trail)
    if rest and rest[0].role not in HEADING_ROLES:
        text += " (continued from the previous page)"
    boxes = [n.bbox for n in head if n.bbox]
    bbox = ((min(b[0] for b in boxes), min(b[1] for b in boxes),
             max(b[2] for b in boxes), max(b[3] for b in boxes)) if boxes else None)
    crumb = Node(Role.P, page=head[0].page, bbox=bbox, text=text,
                 attrs={"context": True, "trail": trail},
                 confidence=min(n.confidence for n in head), source=Source.HEURISTIC)
    return [crumb, *rest]
