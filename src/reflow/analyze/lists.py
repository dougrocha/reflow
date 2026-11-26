"""List items: marker chains, nesting, legal and roman labels, and the list grouping."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from reflow.analyze.blocks import (
    Block,
    Break,
    Marker,
    at_text_x,
    follows,
    leaves_item,
    legal,
    line_fills,
    marker_of,
    roman,
    shares_column,
    text_offset,
)
from reflow.analyze.constants import (
    ALIGN_TOL,
    BULLET_GLYPHS,
    HANGING_MIN,
    INDEX_REFERENCE,
    LEGAL_CHAIN,
    LIST_BULLETS,
    LIST_MARKER,
    PAGE_REFERENCE,
    ROMAN_VALUES,
)
from reflow.analyze.context import PageContext
from reflow.analyze.rhythm import Rhythm, line_gap
from reflow.extract.lines import Line
from reflow.patterns import NUMBER
from reflow.model import Node, Role


# An item indented at least this many font sizes past its parent is nested under it
NEST_INDENT = 0.5


def merge_list_markers(lines: list[Line]) -> list[Line]:
    """Join a bare list marker ("1.", "•") to the text beside it on the same row.
    Hanging indents put a tab-sized gap after the marker, which splits the line."""
    markers = [l for l in lines if LIST_MARKER.match(l.text.strip() + " ")
               and LIST_MARKER.match(l.text.strip() + " ").end() >= len(l.text.strip())]
    if not markers:
        return lines
    used: set[int] = set()
    out = []
    for line in lines:
        if id(line) in used:
            continue
        if line in markers:
            mate = min((m for m in lines if m is not line and id(m) not in used
                        and 0 <= m.x0 - line.x1 < 4 * line.size
                        and abs(m.top - line.top) < 0.5 * line.height),
                       key=lambda m: m.x0, default=None)
            if mate is not None:
                used.add(id(mate))
                line = line.cut(0, len(line.text), strip=True).joined(mate)
        out.append(line)
    return out


# What OCR makes of a printed bullet: one of these symbols, maybe a second ("¢+"), a space, then a
# word. Only these: a column of "|" table borders or "$" amounts is no list.
OCR_BULLET = re.compile(r"^([¢©®°+])[^\w\s]?\s+(?=[^\W\d_]|[“\"])")
OCR_BULLET_RUN = 3  # a glyph is a bullet when this many lines in a column start with it


def ocr_bullets(lines: list[Line]) -> list[Line]:
    """OCR reads a printed bullet as some other symbol ("¢ Mary Armes"). A column of OCR lines
    that each start with the same lone symbol is a list: the symbol becomes "•". The column is
    lines whose left edges are a font size apart at most, since a scan drifts down the page."""
    starts: dict[str, list[tuple[Line, int]]] = {}  # by symbol: each line and where its symbol ends
    for line in lines:
        m = OCR_BULLET.match(line.text) if line.ocr else None
        if m:
            starts.setdefault(m[1], []).append((line, m.end()))
    replaced: dict[int, Line] = {}
    for group in starts.values():
        group.sort(key=lambda found: found[0].x0)
        columns = [[group[0]]]
        for found in group[1:]:
            if found[0].x0 - columns[-1][-1][0].x0 <= found[0].size:
                columns[-1].append(found)
            else:
                columns.append([found])
        for column in columns:
            if len(column) >= OCR_BULLET_RUN:
                for line, end in column:
                    replaced[id(line)] = line.retext("• " + line.text[end:])
    return [replaced.get(id(line), line) for line in lines]


def index_items(blocks: list[Block]) -> list[Block]:
    """On an index page every entry is a list item (its sub-entries nest under it, see
    group_lists). A lone letter or symbol ("A", "Q") is a heading mark, not an entry. So is a
    title: a block set larger than the page's text, or a bold line with no page number or colon
    ("Table of Contents", "Alphabetical Index of Network Providers")."""
    body = Counter(round(b.size, 1) for b in blocks).most_common(1)[0][0] if blocks else 0.0
    for b in blocks:
        if len(re.sub(r"[^\w]", "", b.text)) < 2 or b.first.text.strip()[:1] in BULLET_GLYPHS:
            continue
        if b.size > body + 0.5:
            continue
        labelled = bool(INDEX_REFERENCE.search(b.text) or PAGE_REFERENCE.search(b.text)
                        or b.text.rstrip().endswith(":"))
        if b.bold and not labelled:
            continue
        b.item = True
        b.entry = True
        b.why = "index entry"
    return blocks


# A number this large leads a list only in a run of its own (461. 462.): alone it is a page
# or line reference wrapped to the start of a line ("582 U. S. 449," / "462. Moreover").
MAX_LONE_NUMBER = 99


def _large_number(m: Marker) -> bool:
    return m.kind == "num" and m.value > MAX_LONE_NUMBER


def _neighbour_marker(blocks: list[Block], i: int, step: int) -> Marker | None:
    """The list marker of the block one or two places before (step -1) or after (step 1) block i,
    if that block starts with a marker that has words after it."""
    for j in (i + step, i + 2 * step):
        if 0 <= j < len(blocks) and blocks[j].marker is not None:
            m = blocks[j].marker
            if blocks[j].text[len(m.text):].strip():
                return m
    return None


def _resolve_romans(blocks: list[Block]) -> None:
    """A single i, v or x is a roman numeral or a list letter. "(i)" after "(h)" is the letter;
    "(i)" before "(ii)", or "(v)" after "(iv)", is the numeral. Decided by the markers beside it."""
    for i, b in enumerate(blocks):
        m = b.marker
        if m is None or m.kind != "alpha" or m.initial:
            continue
        letter = m.text.strip("().").lower()
        if letter not in ROMAN_VALUES:
            continue
        before, after = _neighbour_marker(blocks, i, -1), _neighbour_marker(blocks, i, 1)
        value = ROMAN_VALUES[letter]
        same_case = before is not None and (before.text.strip("().").isupper()
                                            == m.text.strip("().").isupper())
        if same_case and before.kind == "alpha" and before.value == m.value - 1:
            continue  # "(h)" then "(i)": the letters run on; "(H)" then "(i)" does not
        if (after is not None and after.kind == "roman" and after.value == value + 1) or \
                (before is not None and before.kind == "roman" and before.value == value - 1):
            b.marker = Marker("roman", m.text, roman(letter))


def _sibling_above(blocks: list[Block], i: int) -> bool:
    """Whether block i is the next legal sibling of an item above it: "(B)" after "(A)", with the
    sub-items of "(A)" (indented under it) between them. Blocks in between must be deeper, and
    the sibling must share the column."""
    b = blocks[i]
    m = b.marker
    if m is None or not legal(m):
        return False
    x, indent = b.first.x0, HANGING_MIN * b.first.size
    for j in range(i - 1, -1, -1):
        p = blocks[j]
        if p.marker is not None and abs(p.first.x0 - x) <= ALIGN_TOL * b.first.size:
            return (p.item and p.marker.kind == m.kind and p.marker.value == m.value - 1
                    and shares_column(p.first, b.first))
        if p.first.x0 < x + indent:
            return False  # a block at this level or outside it: no sibling above
    return False


def _continues_chain(blocks: list[Block], chain: list[int], m: Marker) -> bool:
    """Whether marker m continues the run `chain`. After a chained label ("(a)(1)", then "(2)")
    the run goes on at either level: "(b)" follows the "(a)" the "(2)" sits under."""
    if follows(blocks[chain[-1]].marker, m):
        return True
    recent = [blocks[j].marker for j in chain[-3:]]
    if any(r.inner is not None for r in recent) and any(follows(r, m) for r in recent):
        return True
    # The outer label stays open for the whole chain: "(a)(1)", "(2)" ... "(4)", then "(b)".
    opened = [k for k, j in enumerate(chain) if blocks[j].marker.inner is not None]
    if not opened:
        return False
    tail = [blocks[j].marker for j in chain[opened[-1]:]]
    outer = tail[0]
    if any(t.kind == outer.kind and follows(outer, t) for t in tail[1:]):
        return False  # the outer level has moved on: its scope is closed
    return follows(outer, m)


def list_items(blocks: list[Block], counts: Counter[tuple[int, float]],
                lines: Sequence[Line] = (), rhythm: Rhythm | None = None) -> list[Block]:
    """Mark the blocks that are list items, and end each item where its list ends. A marker
    is a list only with list context: it runs with the next or previous block's consecutive
    marker (1. 2. / a. b.), or it is followed by a hanging indent (its wrapped lines sit at
    the item's text x, see text_offset, right of the marker). Bullet glyphs stand alone. Initials
    ("R. M. Hazen") and section numbers need a run of their own. Inside an item, only lines
    at the item's text x (or a lowercase line carried on directly under it) belong to it.
    `rhythm` is the page's leading on a scan (see rhythm.Rhythm)."""
    _resolve_romans(blocks)
    for b in blocks:
        if b.label is not None:
            b.item = True  # a worksheet line item: its number is the list's, see worksheet_rows
    # Chains of consecutive markers: each follows the one before it, with at most one other
    # block between (a body paragraph under each numbered heading, a column's fragment).
    chains: list[list[int]] = []
    for i, b in enumerate(blocks):
        m = b.marker
        if m is None or not b.text[len(m.text):].strip():
            continue  # a marker with no words after it is not an item (a stray bullet)
        # The chain it continues: the nearest one that ended within two blocks, so a stray
        # marker in between ("U. S., at 450." reads as an initial) does not break a run.
        for chain in reversed(chains):
            if i - chain[-1] <= 2 and _continues_chain(blocks, chain, m):
                chain.append(i)
                break
        else:
            chains.append([i])
    in_chain = {i: len(chain) for chain in chains for i in chain}
    cands = sorted(in_chain)  # blocks that start with a marker that has words after it
    for i in cands:
        b, m = blocks[i], blocks[i].marker
        run = in_chain[i] >= 2
        offset = text_offset(b.first.x0, counts)
        hanging = (len(b.lines) >= 2 and offset is not None
                   and at_text_x(b.lines[1].x0, b.first.x0 + offset, b.first.size))
        # "R. M. Hazen" is an initial, never a marker, unless a run of three spells out a
        # sequence (a worksheet's lines A to E); "a. You" needs its run.
        if m.initial and m.text.strip("().").isupper():
            b.item = in_chain[i] >= 3
        elif _large_number(m) and not run:
            b.item = False
        else:
            b.item = m.kind == "bullet" or run or (hanging and not m.initial)
    # Nested lists: a letter indented under an item is its sub-item, and a number that
    # is followed by an item one level deeper (its "a." sub-list) is the item it leads.
    # Repeat until nothing changes: "(a)" leads "(1)", which leads "(A)" only once "(A)" is an
    # item, and a pass in document order sees "(A)" after "(a)".
    for _ in range(len(cands)):
        before = sum(blocks[i].item for i in cands)
        for k, i in enumerate(cands):
            b, m = blocks[i], blocks[i].marker
            if b.item or (m.initial and m.text.strip("().").isupper()) or _large_number(m):
                continue
            indent = HANGING_MIN * b.first.size
            prev = cands[k - 1] if k else None
            nxt = cands[k + 1] if k + 1 < len(cands) else None
            under = (prev is not None and blocks[prev].item and i - prev <= 2
                     and m.kind == "alpha" and b.first.x0 > blocks[prev].first.x0 + indent)
            next_marker = blocks[nxt].marker if nxt is not None else None
            # In legal enumerations an uppercase letter can contain lowercase roman
            # numerals: (A) contains (i), though both are parsed as alpha before context.
            changes_legal_level = (next_marker is not None and m.text.startswith("(")
                                   and next_marker.text.startswith("(")
                                   and m.text[1:2].isupper()
                                   and next_marker.text[1:2].islower()
                                   and next_marker.text[1:2] in "ivx")
            leads = (nxt is not None and (blocks[nxt].item or changes_legal_level) and nxt - i <= 2
                     and (blocks[nxt].marker.kind != m.kind or changes_legal_level)
                     and blocks[nxt].first.x0 > b.first.x0 + indent)
            b.item = under or leads or _sibling_above(blocks, i)
        if sum(blocks[i].item for i in cands) == before:
            break
    for i in cands:
        b = blocks[i]
        if not b.item and b.why == Break.LIST_MARKER:
            b.why = "starts like a list marker, but no list context follows"
    out: list[Block] = []
    for b in blocks:
        if not b.item or b.label is not None:
            out.append(b)  # a worksheet line item is whole: its lines are its text
            continue
        offset = text_offset(b.first.x0, counts)
        text_x = b.first.x0 + offset if offset is not None else None
        piece = Block([b.lines[0]], why=b.why, marker=b.marker, item=True)
        for line in b.lines[1:]:
            if piece.item and leaves_item(piece, line, text_x, lines, rhythm):
                if not piece.text[len(b.marker.text):].strip():
                    piece.item = False  # a marker with no words after it is a paragraph
                out.append(piece)
                piece = Block([line], why="ends the list item: not at the item's text x")
            elif (not piece.item and len(piece.lines) == 1 and NUMBER.match(piece.text.strip())
                  and line.x0 < piece.first.x0 - 2 * piece.first.size):
                # The value set at the end of the item's row ("Other coinsurance ... 50%") does
                # not run on into the line under the item, which starts back at the left.
                out.append(piece)
                piece = Block([line], why="a value ends the item's row: the next line starts to its left")
            else:
                piece.lines.append(line)
        out.append(piece)
    out = _join_legal_wraps(out, lines, rhythm)
    joined: list[Block] = []
    i = 0
    while i < len(out):
        block = out[i]
        if (block.item and block.marker is not None and i + 1 < len(out)
                and not out[i + 1].item):
            nxt = out[i + 1]
            offset = text_offset(block.first.x0, counts)
            text_x = block.first.x0 + offset if offset is not None else None
            same_baseline = (nxt.first.size == block.lines[-1].size
                             and nxt.first.top <= block.lines[-1].bottom
                             - 0.1 * block.lines[-1].size)
            aligned = text_x is not None and at_text_x(nxt.first.x0, text_x, block.first.size)
            # The body is in the bullet's own column (under its text, not beside it) and is
            # plain text: a bold block of its own on that baseline is a heading, not its body.
            last = block.lines[-1]
            in_column = last.x0 <= nxt.first.x0 < last.x1
            plain = not (nxt.first.bold and not block.first.bold)
            if same_baseline and aligned and in_column and plain:
                joined.append(Block(block.lines + nxt.lines, why=block.why,
                                    marker=block.marker, item=True))
                i += 2
                continue
        joined.append(block)
        i += 1
    return joined


def _opens_legal_item(b: Block) -> bool:
    """Whether the block opens with a printed legal label and a capitalized word: "(c) INITIAL
    MANAGEMENT ORDER. After": an item of its own, never the wrapped end of the item above. A
    citation carried onto the next line ("(a) of this section") reads in lowercase."""
    m = b.marker
    return (m is not None and legal(m) and m.kind in ("alpha", "num", "roman") and not m.initial
            and b.text[len(m.text):].lstrip()[:1].isupper())


def _join_legal_wraps(blocks: list[Block], lines: Sequence[Line],
                      rhythm: Rhythm | None = None) -> list[Block]:
    """Join a block that wraps back to the margin under a legal item: "(2) at a place ... of the
    United States," / "States, in any manner ..." is one item, and so is "(1) ... 406 U. S., at"
    / "218. Whether ...". The block must sit left of the label, under a line that ran to the
    column's edge, at the same size, and not be a list item of its own."""
    out: list[Block] = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev is not None and prev.item and not b.item and legal(prev.marker) and lines \
                and not _opens_legal_item(b):
            last, line = prev.lines[-1], b.first
            gap = line_gap(last, line, rhythm)
            under = (-last.height < gap <= 0.5 * last.size
                     and line.x0 < last.x1 and last.x0 < line.x1)
            if (under and line.x0 < prev.first.x0 and line.bold == last.bold
                    and abs(line.size - last.size) < 0.5 and line_fills(last, lines)):
                prev.lines.extend(b.lines)
                continue
        out.append(b)
    return out


# A document is a legal text when this many of its blocks are items with a printed legal label
LEGAL_DOCUMENT_ITEMS = 30


def promote_legal_labels(contexts: list[PageContext]) -> None:
    """In a legal text (rules, statutes: hundreds of "(a)", "(1)", "(A)", "(ii)" items), a block
    that opens with a printed legal label and a word is an item even when it stands alone on its
    page or in its column, with no run of labels beside it to show it: the first "(c)" after a
    page break, or "(a) REAL PARTY IN INTEREST." over its "(1)". Documents with few legal items
    are left to the context rules of list_items."""
    pool = [b for ctx in contexts for b in ctx.blocks]
    if sum(1 for b in pool if b.item and legal(b.marker)) < LEGAL_DOCUMENT_ITEMS:
        return
    for b in pool:
        m = b.marker
        if (b.item or m is None or not legal(m) or m.initial or b.label is not None
                or m.kind not in ("alpha", "num", "roman") or _large_number(m)
                or not _opens_legal_item(b)):
            continue
        b.item = True
        b.why = "starts with a printed legal label in a legal text"


def continuable(b: Block) -> bool:
    """A legal item with words after its label, numbered or lettered (not a roman or initial)."""
    m = b.marker
    return (legal(m) and m.kind in ("alpha", "num") and not m.initial
            and bool(b.text[len(m.text):].strip()))


def _roman_label(m: Marker) -> int | None:
    """The value of a parenthesized lowercase roman label, or None. A lone "(i)" last on its
    page was never confirmed as a numeral by a "(ii)" after it, so it is still read as a letter."""
    if not m.text.startswith("(") or m.initial:
        return None
    if m.kind == "roman":
        return m.value
    return 1 if m.kind == "alpha" and m.text == "(i)" else None


def continue_roman_items(contexts: list[PageContext]) -> None:
    """A page can open with the next roman subitem of a list from the page before: "(i)" ends
    one page and "(ii)" starts the next. The sequence context is across the page break, so a
    parenthesized roman label that follows the previous page's last block, itself a list item
    with the previous roman label, is an item. Without that item before it, it stays a paragraph."""
    for prev, nxt in zip(contexts, contexts[1:]):
        if nxt.page.number != prev.page.number + 1 or not prev.blocks or not nxt.blocks:
            continue
        tail, head = prev.blocks[-1], nxt.blocks[0]
        if (tail.item and head.marker is not None and tail.marker is not None
                and head.marker.kind == "roman" and head.marker.text.startswith("(")
                and head.text[len(head.marker.text):].strip()
                and _roman_label(tail.marker) == head.marker.value - 1):
            head.item = True


def group_lists(nodes: list[Node]) -> list[Node]:
    """Wrap runs of list items in a list node; strip each item's marker into attrs. Then nest
    the items that are indented under the item above them (see _nest_items)."""
    out: list[Node] = []
    scope: tuple[Marker, float, float] | None = None  # the open chain: inner label, x, size
    for node in nodes:
        if node.role != Role.LI:
            out.append(node)
            scope = None
            continue
        contents = bool(node.attrs.get("contents"))  # contents entries have no marker
        ordered = False
        if not contents and not node.attrs.get("index"):
            marker = LIST_MARKER.match(node.text)
            chain = LEGAL_CHAIN.match(node.text)
            if "marker" in node.attrs:
                ordered = True  # a worksheet line number, already off the text (see worksheet_rows)
            elif chain is not None and (m := marker_of(node.text)) is not None and m.inner:
                x, size = node.attrs.get("x", 0.0), node.attrs.get("size", 10.0)
                head = Node(Role.LI, page=node.page, bbox=node.bbox, text="",
                            confidence=node.confidence, source=node.source,
                            attrs={**node.attrs, "marker": chain[1]})
                node.attrs = {**node.attrs, "marker": m.inner.text, "x": x + (NEST_INDENT + 0.01) * size}
                node.set_text(node.text[chain.end():])
                scope = (m.inner, x, size)
                ordered = True
                _add_item(out, head, ordered, contents)
            elif marker is None:
                scope = None
            else:
                scope = _shift_into_chain(node, scope)
                node.attrs["marker"] = marker[1]
                node.set_text(node.text[marker.end():])
                ordered = marker[1] not in LIST_BULLETS
        _add_item(out, node, ordered, contents)
    for lst in out:
        if lst.role == Role.LIST:
            lst.children = _nest_items(lst.children, lst.attrs["ordered"], lst.attrs["contents"])
    return out


def _add_item(out: list[Node], node: Node, ordered: bool, contents: bool) -> None:
    """Append a list item to the list that ends `out`, or open a list for it."""
    if not (out and out[-1].role == Role.LIST and out[-1].page == node.page
            and out[-1].attrs["ordered"] == ordered
            and bool(out[-1].attrs.get("contents")) == contents):
        out.append(Node(Role.LIST, page=node.page, bbox=node.bbox,
                        confidence=node.confidence,
                        attrs={"ordered": ordered, "contents": contents}))
    out[-1].children.append(node)


def _shift_into_chain(node: Node, scope: tuple[Marker, float, float] | None
                      ) -> tuple[Marker, float, float] | None:
    """After a chained label ("(a)(1)"), the next label at its indent that continues the inner
    level ("(2)") is a sub-item of the same outer item: move its x to the inner item's, so that
    _nest_items nests it there. Returns the chain still open, or None."""
    if scope is None:
        return None
    inner, x, size = scope
    m = marker_of(node.text)
    here = node.attrs.get("x", 0.0)
    if m is not None and abs(here - x) <= ALIGN_TOL * size and follows(inner, m):
        node.attrs["x"] = x + (NEST_INDENT + 0.01) * size
        return m, x, size
    return scope if here > x + ALIGN_TOL * size else None


def _nest_items(items: list[Node], ordered: bool, contents: bool) -> list[Node]:
    """Nest each item under the item above it when it is indented past that item's text (a
    lettered sub-item under "8.", a sub-entry under an index heading). Only the items of one
    column nest: a new column (the next item starts higher up) starts again at the top."""
    top: list[Node] = []
    stack: list[Node] = []  # the open items, outermost first
    for item in items:
        x = item.attrs.get("x", 0.0)
        size = item.attrs.get("size", 10.0)
        if stack and item.bbox and stack[-1].bbox and item.bbox[1] < stack[-1].bbox[1] - size:
            stack = []  # a new column
        while stack and x < stack[-1].attrs.get("x", 0.0) + NEST_INDENT * size:
            stack.pop()
        if not stack:
            top.append(item)
        else:
            parent = stack[-1]
            subs = [c for c in parent.children if c.role == Role.LIST]
            if not subs:
                sub = Node(Role.LIST, page=parent.page, bbox=item.bbox,
                           confidence=parent.confidence,
                           attrs={"ordered": ordered, "contents": contents})
                parent.children.append(sub)
            else:
                sub = subs[-1]
            sub.children.append(item)
        stack.append(item)
    return top
