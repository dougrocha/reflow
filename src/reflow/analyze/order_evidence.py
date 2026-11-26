"""Reading-order evidence: where another witness reads a page in a different order.

The heuristic engine decides the reading order. Two sources read the same page
independently: the PDF's tag tree, whose order is the author's (reflow.extract.tags), and
Docling, whose layout model orders the page it was run on (hybrid hard pages). When the
engine's order and a witness' order disagree, one of them is wrong, and the page needs a
person: the page gets an "order-disagreement" fact (a review item; weight in
reflow.analyze.confidence). Docling is a weaker witness (it reads some multi-column pages
wrongly, resnet p1), so its verdict is the separate "order-disagreement-docling" fact: a small
penalty that corroborates other doubts and never makes a page item on its own.

How a page is compared:

  1. Both sides become *blocks*, the text-bearing leaves in reading order. Heuristic side:
     the tree's nodes (lists, tables, rows and groups opened; a paragraph, list item, cell or
     field is a block). Tag side: the outermost standard elements that are not pure wrappers
     (a /P, an /H2, a /TD; /Sect, /Div, /Table, /TR are opened). Docling: same as the heuristic.
  2. Each heuristic block is matched to the witness block it sits in. Geometry decides
     (the share of the smaller box inside the other, at least MATCH_SHARE); text breaks ties
     (shared words). Text is only a tie-break because a text layer can be stored visually or
     with ligatures reordered (the Arabic fact sheet's tags read right to left, letter by
     letter backwards), and geometry still matches there.
  3. Inside one witness block the order is the text's: a heuristic block whose first words
     occur in the witness block at offset k sorts by k (a tagged /P holding a whole row of
     check boxes still orders the boxes). That holds only when every block in the witness
     block has its own offset; otherwise they collapse into one anchor, so a broad tag block
     over many heuristic blocks gives no verdict instead of a false agreement.
  4. A witness block the tag tree sets aside is not evidence of order: a note or aside the
     author tagged in a section of its own after the main flow, though it sits inside the
     main flow's extent (a chart's footnote, tagged last, printed under the chart). Where it
     is read is a choice, not a fact, so neither it nor the heuristic block that sits in it
     is compared. Left in, one such block costs a short page 0.2 to 0.4 of tau.
  5. The agreement is Kendall's tau-b between the heuristic order and the witness order over
     the matched pairs, ties on the witness side counted. 1.0 is the same order, 0 unrelated,
     -1 reversed. A page with fewer than MIN_MATCHED distinct anchors has no verdict.

Calibration of TAU_MIN: tau against the tag tree over every tagged document of the
page-check corpus that is within MAX_TAG_PAGES (208 pages with at least MIN_MATCHED anchors, in
11 documents; the untagged and over-cap documents have no tag verdict):

  - pages with a contains_in_order xfail (the known-bad order): cdc-hepb-arabic p1 0.026,
    practitioner-data-form p4 0.891.
  - pages with a passing contains_in_order check (17 with a verdict): 0.818 (cdc-antibiotics-
    trifold p1), 0.881 (p17 p94), 0.905 (census-p60-273 p22), 0.954 (fw9 p1), 0.956
    (practitioner-data-form p1), 0.964 (irs-w4-2026 p1), then 0.975 to 1.0 (nine pages).
  - the other 189 pages: 139 in 0.95 to 1.0, 18 in 0.90 to 0.95, 32 below 0.90 (p17 and
    census-p60-273 hold most; a few are near 0).

The two groups overlap: a passing check covers a few strings, not the whole page, so a page
that passes can still differ from the tags (the tags, not we, may be wrong). No threshold
separates them. 0.97 flagged 77 of the 208 pages and caught nothing 0.92 does not; it only added
three known-good pages (0.954, 0.956, 0.964). 0.92 catches both known-bad pages with 0.03 to
spare above the closer one (0.891), flags 41 pages, and keeps the review queue under its 5% cap
(tests/pages/test_calibration.py). Do not go below 0.90: practitioner-data-form p4 is lost.

This module is self-contained on purpose. The Docling comparison (compare_docling) covers the
hard pages the hybrid engine sent to Docling (Routing.docling), and only for a document with
no tag read: where the tags exist they are the author's order and win. Role and attribute
evidence is the separate pass in evidence.fuse; `apply` is this module's one entry point and
`compare_page` the testable core.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from reflow import geometry
from reflow.model import BBox, Document, Fact, Node, Role, Source

if TYPE_CHECKING:
    from reflow.extract.tags import TagElement, TagPage
    from reflow.run import Run

# Below this tau-b the page gets an "order-disagreement" fact.
TAU_MIN = 0.92
# A page with fewer matched blocks has no order to compare.
MIN_MATCHED = 6
# A block and a witness block are the same region when this share of the smaller box lies in
# the other (the hybrid engine's REPLACE_SHARE).
MATCH_SHARE = 0.5
# Words of a block looked up inside a witness block to place it within that block.
ANCHOR_WORDS = 3
# Wrappers: opened, not compared as a block.
HEURISTIC_WRAPPERS = frozenset({Role.LIST, Role.TABLE, Role.TR, Role.GROUP, Role.SECTION,
                                Role.BOX})
TAG_WRAPPERS = frozenset({"/Document", "/Part", "/Art", "/Sect", "/Div", "/NonStruct",
                          "/L", "/Table", "/THead", "/TBody", "/TFoot", "/TR", "/TOC",
                          "/TOCI", "/BlockQuote", "/Index"})

_log = logging.getLogger(__name__)
_WORD = re.compile(r"\w+")


def words(text: str) -> list[str]:
    """The comparison form of a text: NFKC, case folded, word characters only."""
    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


@dataclass(frozen=True)
class Block:
    bbox: BBox
    words: tuple[str, ...]
    aside: bool = False   # tag side only: tagged out of the main flow (see tag_blocks)


@dataclass(frozen=True)
class OrderAgreement:
    page: int
    source: Source
    tau: float            # Kendall tau-b over the matched blocks
    matched: int          # distinct anchors compared (heuristic blocks collapsed per witness block)
    blocks: int           # heuristic blocks on the page

    @property
    def disagrees(self) -> bool:
        return self.tau < TAU_MIN

    def detail(self) -> str:
        who = {Source.TAGS: "the PDF's tag tree", Source.DOCLING: "Docling"}.get(
            self.source, self.source.value)
        return (f"Reading order agrees {self.tau:.2f} with {who} over {self.matched} "
                f"matched blocks (flagged below {TAU_MIN:.2f})")


# -- blocks -------------------------------------------------------------------------------


def _block(box: BBox | None, text: str | None) -> Block | None:
    if box is None or not text or not geometry.area(box):
        return None
    tokens = words(text)
    return Block(tuple(box), tuple(tokens)) if tokens else None  # type: ignore[arg-type]


def node_blocks(nodes: Iterable[Node]) -> dict[int, list[Block]]:
    """Per page, the text-bearing leaves of `nodes` in order. A node with text is one block
    and is not opened (a cell holding fields counts once); wrappers and textless nodes are
    opened; artifacts and figures are not read."""
    pages: dict[int, list[Block]] = {}

    def visit(node: Node, page: int | None) -> None:
        page = node.page if node.page is not None else page
        if node.role in (Role.ARTIFACT, Role.FIGURE):
            return
        if node.role not in HEURISTIC_WRAPPERS and node.text and node.text.strip():
            block = _block(node.bbox, node.text)
            if block is not None and page is not None:
                pages.setdefault(page, []).append(block)
            return
        for child in node.children:
            visit(child, page)

    for node in nodes:
        visit(node, None)
    return pages


def tag_blocks(page: TagPage) -> list[Block]:
    """The blocks of one tagged page: elements in tree order, wrappers with text-bearing
    children opened, anything else taken whole.

    A later wrapper holding a single block, whose box lies inside an earlier sibling wrapper
    holding several, is tagged out of the flow it is printed in: its block is an `aside`
    (a chart's footnote, a sidebar), kept in the list but not compared."""

    def visit(element: TagElement) -> list[Block]:
        kids = [c for c in element.children if c.text.strip()]
        if element.tag in TAG_WRAPPERS and kids:
            parts = [visit(child) for child in element.children]
            several = [i for i, part in enumerate(parts) if len(part) > 1]
            for j, part in enumerate(parts):
                if len(part) == 1 and any(_inside(element.children[j], element.children[i])
                                          for i in several if i < j):
                    parts[j] = [replace(part[0], aside=True)]
            return [block for part in parts for block in part]
        if element.tag == "/Artifact":
            return []
        block = _block(element.bbox, element.text)
        return [block] if block is not None else []

    return [block for element in page.elements for block in visit(element)]


def _inside(element: TagElement, other: TagElement) -> bool:
    """Whether most of `element`'s box lies in `other`'s."""
    if element.bbox is None or other.bbox is None:
        return False
    own = geometry.area(element.bbox)
    return bool(own) and geometry.overlap(element.bbox, other.bbox) / own >= MATCH_SHARE


# -- agreement ----------------------------------------------------------------------------


def _match(block: Block, witness: Sequence[Block]) -> int | None:
    """Index of the witness block `block` sits in, or None.

    A block mostly inside a witness block is placed in it (the shared words break ties, then
    the smaller witness block wins). A block that instead spans several witness blocks (a
    paragraph the witness split, a table the witness reads cell by cell) is placed where it
    starts: at the first witness block that lies mostly inside it."""
    own = set(block.words)
    best: tuple[float, float, float] | None = None
    found = None
    first = None
    area = geometry.area(block.bbox)
    x0, top, x1, bottom = block.bbox
    for i, other in enumerate(witness):
        ox0, otop, ox1, obottom = other.bbox
        # A share of at least MATCH_SHARE (> 0) needs an overlap: most witness blocks are clear
        # of this one, and are passed over before anything is measured.
        width, height = min(x1, ox1) - max(x0, ox0), min(bottom, obottom) - max(top, otop)
        if width <= 0 or height <= 0:
            continue
        shared = width * height
        if shared / area >= MATCH_SHARE:
            text = len(own & set(other.words)) / len(own)
            key = (text, -geometry.area(other.bbox))
            if best is None or key > best:
                best, found = key, i
        elif first is None and shared / geometry.area(other.bbox) >= MATCH_SHARE:
            first = i
    return found if found is not None else first


def _offset(block: Block, other: Block) -> int | None:
    """Where the block's first words occur in the witness block, or None."""
    anchor = block.words[:ANCHOR_WORDS]
    for i in range(len(other.words) - len(anchor) + 1):
        if other.words[i:i + len(anchor)] == anchor:
            return i
    return None


def kendall_tau_b(keys: Sequence[tuple[int, int | None]]) -> float | None:
    """Tau-b of the sequence order (the first side, never tied) against `keys` (the second
    side). A key is (block, offset): different blocks order by block; the same block orders by
    offset when both have one, else ties. None when every pair is tied."""
    concordant = discordant = tied = 0
    n = len(keys)
    for i in range(n):
        for j in range(i + 1, n):
            (a, ao), (b, bo) = keys[i], keys[j]
            if a != b:
                sign = 1 if a < b else -1
            elif ao is not None and bo is not None and ao != bo:
                sign = 1 if ao < bo else -1
            else:
                tied += 1
                continue
            if sign > 0:
                concordant += 1
            else:
                discordant += 1
    pairs = n * (n - 1) // 2
    if pairs == tied:
        return None
    return (concordant - discordant) / math.sqrt(pairs * (pairs - tied))


def _anchors(blocks: Sequence[Block], witness: Sequence[Block]) -> list[tuple[int, int | None]]:
    """One (witness block, offset) key per distinct anchor, in the heuristic order.

    Several heuristic blocks can sit in one broad witness block. They are ordered by their
    word offsets only when every one of them has an offset and no two share one; otherwise
    the witness did not say how they are ordered, and the whole group is a single anchor (at
    the position of its first block). Counting them separately would make ties that shrink
    the comparison until a reordered page scores near-perfect."""
    placed: list[tuple[int, int | None]] = []
    members: dict[int, list[int | None]] = {}
    for block in blocks:
        at = _match(block, witness)
        if at is not None and not witness[at].aside:
            key = (at, _offset(block, witness[at]))
            placed.append(key)
            members.setdefault(at, []).append(key[1])
    anchored = {at for at, offsets in members.items()
                if len(offsets) > 1 and None not in offsets and len(set(offsets)) == len(offsets)}
    keys: list[tuple[int, int | None]] = []
    seen: set[int] = set()
    for at, offset in placed:
        if at in anchored or len(members[at]) == 1:
            keys.append((at, offset if at in anchored else None))
        elif at not in seen:
            seen.add(at)
            keys.append((at, None))
    return keys


def compare_page(page: int, source: Source, blocks: Sequence[Block],
                 witness: Sequence[Block]) -> OrderAgreement | None:
    """The agreement of `blocks` (heuristic order) with `witness` (the witness' order) on one
    page; None when fewer than MIN_MATCHED blocks are placed."""
    keys = _anchors(blocks, witness)
    if len(keys) < MIN_MATCHED:
        return None
    tau = kendall_tau_b(keys)
    if tau is None:
        return None
    return OrderAgreement(page, source, round(tau, 4), len(keys), len(blocks))


def compare_tags(doc: Document, tags: dict[int, TagPage]) -> list[OrderAgreement]:
    """The tag tree's verdict on each tagged page that has one."""
    blocks = node_blocks(doc.root.children)
    out = []
    for page in sorted(tags):
        verdict = compare_page(page, Source.TAGS, blocks.get(page, []), tag_blocks(tags[page]))
        if verdict is not None:
            out.append(verdict)
    return out


def compare_docling(doc: Document, docling: Sequence[Node]) -> list[OrderAgreement]:
    """Docling's verdict on each page it read (hybrid hard pages)."""
    blocks = node_blocks(doc.root.children)
    theirs = node_blocks(docling)
    out = []
    for page in sorted(theirs):
        verdict = compare_page(page, Source.DOCLING, blocks.get(page, []), theirs[page])
        if verdict is not None:
            out.append(verdict)
    return out


def flag(doc: Document, agreements: Iterable[OrderAgreement]) -> list[OrderAgreement]:
    """Put an "order-disagreement" fact (a Docling verdict: "order-disagreement-docling", which
    never makes a review item by itself) on the page of each disagreeing verdict (one per
    page and source; idempotent). Returns the verdicts that flagged."""
    pages = {p.number: p for p in doc.pages}
    flagged = []
    for verdict in agreements:
        info = pages.get(verdict.page)
        if info is None or not verdict.disagrees:
            continue
        code = "order-disagreement-docling" if verdict.source == Source.DOCLING \
            else "order-disagreement"
        if any(f.code == code and f.source == verdict.source for f in info.facts):
            continue
        info.facts.append(Fact(code, verdict.source, verdict.detail(),
                               {"tau": verdict.tau, "matched": verdict.matched,
                                "blocks": verdict.blocks}))
        flagged.append(verdict)
    return flagged


def _max_pages() -> int:
    from reflow.extract.tags import MAX_TAG_PAGES
    return MAX_TAG_PAGES


def apply(run: Run, docling: Sequence[Node] = (),
          tags: dict[int, TagPage] | None = None) -> list[OrderAgreement]:
    """Compare the tree's reading order with the tag tree's and flag the pages that disagree.
    Where there is no tag read (untagged, over the page cap, or failed), `docling` (the nodes
    the hybrid engine already got from Docling, hard pages only) is the witness instead.
    Docling is never run for this.

    `tags` is a tag read the caller already has; otherwise the run's shared read is used
    (reflow.extract.tags.shared_tags: made once for the whole conversion, capped at
    MAX_TAG_PAGES, a failure recorded as an "order-unchecked" fact)."""
    doc = run.doc
    verdicts: list[OrderAgreement] = []
    if tags is None:
        from reflow.extract.tags import shared_tags
        tags = shared_tags(run)
    if tags:
        verdicts += compare_tags(doc, tags)
    elif docling and len(doc.pages) <= _max_pages():
        try:
            verdicts += compare_docling(doc, docling)
        except Exception as error:  # optional evidence: never aborts the conversion
            _log.warning("Docling order comparison skipped: %s: %s", type(error).__name__, error)
    flag(doc, verdicts)
    return verdicts
