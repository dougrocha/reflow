"""The PDF's own structure: tag tree, bookmarks. Offered as a Witness (reflow.analyze.evidence)
rather than as a second tree: the heuristics keep the text and the backbone; the tags confirm,
dispute or fill.

How the chars are reached: pdfplumber >= 0.11 puts `mcid` and `tag` on every char (fw9
page 1: 5,615 of 5,615 chars carry one). The struct tree is walked with pikepdf from
/StructTreeRoot through /K to the leaves, which are MCIDs (ints, or /MCR dicts) on a page
(/Pg); a leaf's box is the union of its chars' boxes. /RoleMap maps custom names to standard
ones. /Alt, /ActualText and table /TH /Scope are read where present.

Well-formedness (PageClaims.trusted): a page is trusted when at least TRUSTED_SHARE of its
chars sit under a standard structural role other than /Div, /Span, /NonStruct and /Artifact.
A tree can cover only some pages (budget-app.pdf: 5 of 1,298, nasa-se-handbook.pdf: 1 of
356); the others are absent from read_tags and from TagWitness.pages(), and a page whose
chars sit mostly under /Div or /Span (uscis-i9.pdf page 1: 31%) is read but not trusted.

Cost: a page needs about 0.07 s of CPU to parse (p17: 142 pages, 10 s). The parsing
is reflow.extract.pagecontent's: a PageReader parses pages on up to MAX_WORKERS processes for
a long document, once for the tags and the links together, and can keep them on disk.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import Executor
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf

from reflow import geometry
from reflow.extract.claims import Claim, PageClaims, WitnessKind
from reflow.extract.pagecontent import PlumberPage, PageReader, reader_for
from reflow.extract.pdfnav import Destinations, displayed_width, page_numbers
from reflow.model import BBox, Fact, HEADING_BY_LEVEL, HEADING_LEVEL, Role, Source
from reflow.text_quality import fold, squash

TRUSTED_SHARE = 0.8
# Standard structure types → model roles. Anything else maps to None (order only).
ROLE_OF_TAG: dict[str, Role | None] = {
    "/H1": Role.H1, "/H2": Role.H2, "/H3": Role.H3, "/H4": Role.H4, "/H5": Role.H5,
    "/H6": Role.H6, "/H": None,  # /H: a heading at a level the nesting decides
    "/P": Role.P, "/L": Role.LIST, "/LI": Role.LI, "/LBody": None, "/Lbl": None,
    "/Table": Role.TABLE, "/TR": Role.TR, "/TH": Role.TH, "/TD": Role.TD,
    "/Figure": Role.FIGURE, "/Caption": Role.CAPTION, "/Formula": None,
    "/Link": None, "/Note": Role.NOTE, "/Reference": None, "/Sect": None, "/Art": None,
    "/Div": None, "/Span": None, "/Form": Role.FIELD, "/Document": None, "/Part": None,
}


@dataclass
class TagElement:
    """A structure element with content on one page, in tree order."""
    tag: str                      # the standard type after /RoleMap, e.g. "/H2"
    page: int
    bbox: BBox
    text: str                     # the chars under it, in content order
    depth: int                    # nesting depth in the tree
    alt: str | None = None        # /Alt
    actual_text: str | None = None
    scope: str | None = None      # /TH /Scope: "row", "col", "both"
    mcids: list[int] = field(default_factory=list)
    children: list[TagElement] = field(default_factory=list)


@dataclass
class TagPage:
    page: int
    elements: list[TagElement]    # top-level elements with content on this page, in order
    tagged_share: float           # share of the page's chars under a standard role


@dataclass(frozen=True)
class Bookmark:
    title: str
    level: int                    # 1 at the top of the outline
    page: int | None              # resolved /Dest or GoTo target; None when unresolvable
    top: float | None             # the destination's y on that page (top-left origin)
    width: float | None = None    # that page's width, for BookmarkWitness' claim boxes




_NOT_COUNTED = {"/Div", "/Span", "/NonStruct", "/Artifact", "/Document", "/Part", "/Sect",
                "/Art"}
STANDARD = frozenset(ROLE_OF_TAG) - _NOT_COUNTED
_SCOPE = {"/Row": "row", "/Column": "col", "/Both": "both"}
_MAX_DEPTH = 200
_DEFAULT_WIDTH = 612.0
_KEY_HEAD, _KEY_TAIL = 40, 20


@dataclass
class _Raw:
    """A structure element as the pikepdf walk saw it, before any page content is attached."""
    tag: str
    depth: int
    alt: str | None = None
    actual_text: str | None = None
    scope: str | None = None
    kids: list = field(default_factory=list)   # _Raw | (page, mcid), in tree order
    by_page: dict[int, list[int]] = field(default_factory=dict)  # subtree MCIDs, per page


def _text_attr(obj, key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    try:
        text = str(value)
    except (TypeError, ValueError, UnicodeError, pikepdf.PdfError):
        return None
    return text or None


def _scope_of(elem) -> str | None:
    """/Scope from the element's /A attribute object(s), which may be a dict or an array."""
    attrs = elem.get("/A")
    if attrs is None:
        return None
    objs = list(attrs) if isinstance(attrs, pikepdf.Array) else [attrs]
    for obj in objs:
        if isinstance(obj, pikepdf.Dictionary) and obj.get("/Scope") is not None:
            return _SCOPE.get(str(obj.Scope))
    return None


def _rolemap(root) -> dict[str, str]:
    mapping = root.get("/RoleMap")
    if not isinstance(mapping, pikepdf.Dictionary):
        return {}
    return {str(k): str(v) for k, v in mapping.items() if isinstance(v, pikepdf.Name)}


def _resolve_tag(tag: str, rolemap: dict[str, str]) -> str:
    for _ in range(16):
        if tag in ROLE_OF_TAG or tag not in rolemap:
            break
        tag = rolemap[tag]
    return tag


def _as_list(kids) -> list:
    if kids is None:
        return []
    return list(kids) if isinstance(kids, pikepdf.Array) else [kids]


class _Walker:
    """Depth-first walk of /StructTreeRoot carrying the inherited /Pg."""

    def __init__(self, rolemap: dict[str, str], page_of: dict[tuple[int, int], int]):
        self.rolemap = rolemap
        self.page_of = page_of
        self.seen: set[tuple[int, int]] = set()
        self.standard_mcids: set[tuple[int, int]] = set()  # (page, mcid) under a standard role

    def page_number(self, pg) -> int | None:
        return self.page_of.get(pg.objgen) if pg is not None else None

    def walk(self, elem, pg, depth: int, inside_standard: bool) -> _Raw | None:
        if elem.objgen != (0, 0):
            if elem.objgen in self.seen or depth > _MAX_DEPTH:
                return None
            self.seen.add(elem.objgen)
        tag = _resolve_tag(str(elem.get("/S", "/NonStruct")), self.rolemap)
        standard = inside_standard or tag in STANDARD
        raw = _Raw(tag, depth, _text_attr(elem, "/Alt"), _text_attr(elem, "/ActualText"),
                   _scope_of(elem))
        own_pg = elem.get("/Pg", pg)
        for leaf in _as_list(elem.get("/K")):
            self._add(raw, leaf, own_pg, depth, standard)
        return raw

    def _add(self, raw: _Raw, leaf, pg, depth: int, standard: bool) -> None:
        if isinstance(leaf, int):
            mcid, page = leaf, self.page_number(pg)
        elif isinstance(leaf, pikepdf.Dictionary):
            if "/MCID" in leaf:
                mcid, page = int(leaf.MCID), self.page_number(leaf.get("/Pg", pg))
            elif "/S" in leaf:
                child = self.walk(leaf, pg, depth + 1, standard)
                if child is not None:
                    raw.kids.append(child)
                    for number, mcids in child.by_page.items():
                        raw.by_page.setdefault(number, []).extend(mcids)
                return
            else:
                return  # /OBJR and anything unknown
        else:
            return
        if page is not None:
            raw.kids.append((page, mcid))
            raw.by_page.setdefault(page, []).append(mcid)
            if standard:
                self.standard_mcids.add((page, mcid))


def _build(raw: _Raw, page: int, content: PlumberPage) -> TagElement | None:
    mcids = raw.by_page.get(page)
    if not mcids:
        return None
    box, text = content.summary(mcids)
    children = [e for kid in raw.kids if isinstance(kid, _Raw)
                if (e := _build(kid, page, content)) is not None]
    box = box or geometry.union(e.bbox for e in children)
    if box is None:
        return None
    return TagElement(raw.tag, page, (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                      text, raw.depth, raw.alt, raw.actual_text, raw.scope, list(mcids), children)


@dataclass
class Walked:
    """The tag tree read from the file, before any page content is: its top-level elements and
    the (page, mcid) pairs under a standard role. Empty for an untagged or unreadable PDF."""
    top: list[_Raw] = field(default_factory=list)
    standard_mcids: set[tuple[int, int]] = field(default_factory=set)

    @property
    def pages(self) -> list[int]:
        """The pages the tree has content on, in order."""
        return sorted({p for raw in self.top for p in raw.by_page})


def walk_tags(pdf: Path) -> Walked:
    """Walk the structure tree (pikepdf only, no page is parsed)."""
    try:
        with pikepdf.open(pdf) as doc:
            root = doc.Root.get("/StructTreeRoot")
            if root is None or root.get("/K") is None:
                return Walked()
            walker = _Walker(_rolemap(root), page_numbers(doc))
            top = [raw for leaf in _as_list(root.get("/K"))
                   if isinstance(leaf, pikepdf.Dictionary) and "/S" in leaf
                   if (raw := walker.walk(leaf, None, 0, False)) is not None]
    except pikepdf.PdfError:
        return Walked()
    return Walked(top, walker.standard_mcids)


def read_tags(pdf: Path, pages: list[int] | None = None, *, jobs: int | None = None,
              executor: Executor | None = None,
              reader: PageReader | None = None, walked: Walked | None = None
              ) -> dict[int, TagPage]:
    """Per page, the structure elements that have content on it. {} for an untagged PDF.
    Pages without content under the tree are absent. `jobs` caps the workers that parse
    pages (None: up to MAX_WORKERS for a long document; 1: this process only). With
    `executor` (a conversion's pool, reflow.run.Workers) the page work is mapped on it and
    no pool is made here. A `reader` (a conversion's, see reflow.extract.pagecontent) supplies
    the parsed pages instead, and `jobs` and `executor` are not used. `walked` is the tree
    when the caller has walked it already (walk_tags)."""
    walked = walked if walked is not None else walk_tags(pdf)
    top = walked.top
    wanted = [p for p in walked.pages if pages is None or p in pages]
    under_standard: dict[int, list[int]] = defaultdict(list)
    for page, mcid in walked.standard_mcids:
        under_standard[page].append(mcid)
    result: dict[int, TagPage] = {}
    reader = reader or PageReader(pdf, jobs=jobs, executor=executor)
    for number, content in reader.get(wanted).items():
        elements = [e for raw in top if (e := _build(raw, number, content)) is not None]
        if not elements:
            continue
        tagged = sum(len(content.chars.get(m, ())) for m in under_standard[number])
        share = tagged / content.total if content.total else 0.0
        result[number] = TagPage(number, elements, min(share, 1.0))
    return result


def _outline_items(first):
    """The outline's items in document order with their level, without recursion."""
    stack = [(first, 1)]
    seen: set[tuple[int, int]] = set()
    while stack:
        item, level = stack.pop()
        while item is not None and item.objgen not in seen:
            seen.add(item.objgen)
            yield item, level
            child, nxt = item.get("/First"), item.get("/Next")
            if child is None:
                item = nxt
                continue
            if nxt is not None:
                stack.append((nxt, level))
            item, level = child, level + 1


def _bookmark_destination(item):
    """What a bookmark points at: its /Dest, or the target of a GoTo action."""
    dest = item.get("/Dest")
    if dest is None:
        action = item.get("/A")
        if isinstance(action, pikepdf.Dictionary) and str(action.get("/S")) == "/GoTo":
            dest = action.get("/D")
    return dest


def read_bookmarks(pdf: Path) -> list[Bookmark]:
    """The outline, flattened in order with levels. [] when there is none."""
    try:
        with pikepdf.open(pdf) as doc:
            outlines = doc.Root.get("/Outlines")
            first = outlines.get("/First") if isinstance(
                outlines, pikepdf.Dictionary) else None
            if first is None:
                return []
            dests = Destinations(doc)
            result = []
            for item, level in _outline_items(first):
                title = squash(str(item.get("/Title", "")))
                page = top = width = None
                resolved = dests.resolve(_bookmark_destination(item))
                if resolved is not None:
                    page, top = resolved
                    width = displayed_width(dests.frame(page))
                result.append(Bookmark(title, level, page, top, width))
            return result
    except pikepdf.PdfError:
        return []


def _key(text: str) -> str:
    """The text key of a claim: the whole text when short, else its head and tail."""
    if len(text) <= _KEY_HEAD + _KEY_TAIL:
        return text
    return text[:_KEY_HEAD] + "…" + text[-_KEY_TAIL:]


def _heading_role(level: int) -> Role:
    return HEADING_BY_LEVEL[min(max(level, 1), 6) - 1]


# Documents longer than this skip the tag-tree read (a page costs about 0.07 s of CPU to parse).
MAX_TAG_PAGES = 300

_log = logging.getLogger(__name__)


def shared_tags(run) -> dict[int, TagPage]:
    """The run's tag read: made once, bounded and failure-safe, then reused by every pass
    (evidence.witness for facts and fills, order_evidence.apply for the reading order).

    {} for an untagged PDF, a document over MAX_TAG_PAGES, or a read that failed; a failure
    leaves an "order-unchecked" fact on the root and a log line, and never aborts the
    conversion. The result is kept in `run.state.tags`."""
    state = run.state
    if state.tags is None:
        tags: dict[int, TagPage] = {}
        if len(run.doc.pages) <= MAX_TAG_PAGES:
            walked, state.walked = state.walked, None
            try:
                tags = read_tags(run.working, reader=reader_for(run), walked=walked)
            except Exception as error:  # optional evidence: any failure leaves the tags unread
                detail = f"{type(error).__name__}: {error}"
                _log.warning("tag tree unreadable, structure and reading-order checks "
                             "skipped: %s", detail)
                run.doc.root.facts.append(Fact(
                    "order-unchecked", Source.TAGS,
                    f"The tag tree could not be read ({detail})"))
        state.tags = tags
    return state.tags


class TagWitness:
    """The tag tree as a Witness: one Claim per element with a role or with content, in
    tree order (which is the author's reading order).

    Pure wrappers are not claims: a role-less element with child elements (/Document, /Sect,
    a /Div around blocks) would match everything on its page. A role-less element is claimed
    only when it holds text itself and no ancestor was claimed, so a /Span inside a /P adds
    nothing but a bare /Div paragraph or a /Formula still counts."""
    source = Source.TAGS
    kind = WitnessKind.TAG_TREE

    def __init__(self, tags: dict[int, TagPage]):
        self._tags = tags
        self._claims: dict[int, PageClaims | None] = {}   # the heading, body-line and fuse passes ask for the same pages

    @classmethod
    def open(cls, pdf: Path, *, jobs: int | None = None,
             executor: Executor | None = None) -> TagWitness | None:
        """None for an untagged PDF, so callers can skip it. `jobs` and `executor` go to
        read_tags: a conversion passes its own pool so no process is added beyond it."""
        tags = read_tags(pdf, jobs=jobs, executor=executor)
        return cls(tags) if tags else None

    def pages(self) -> set[int]:
        return set(self._tags)

    def claims(self, page: int) -> PageClaims | None:
        """The page's claims, built on the first ask and shared after it: callers read them."""
        if page not in self._claims:
            self._claims[page] = self._page_claims(page)
        return self._claims[page]

    def _page_claims(self, page: int) -> PageClaims | None:
        tagged = self._tags.get(page)
        if tagged is None:
            return None
        order: list[Claim] = []

        def visit(element: TagElement, sects: int, claimed: bool) -> None:
            sects += element.tag == "/Sect"
            role = ROLE_OF_TAG.get(element.tag)
            level = None
            if element.tag == "/H":
                level = min(max(sects, 1), 6)
                role = _heading_role(level)
            elif role in HEADING_LEVEL:
                level = HEADING_LEVEL[role]
            own = role is not None or (
                not claimed and not element.children and bool(element.text))
            if own:
                attrs: dict = {} if level is None else {"level": level}
                if element.tag == "/H":
                    attrs["generic"] = True   # the level is a guess from /Sect nesting
                for name in ("alt", "actual_text", "scope"):
                    if getattr(element, name) is not None:
                        attrs[name] = getattr(element, name)
                order.append(Claim(element.bbox, role, _key(element.text) or None, attrs))
            for child in element.children:
                visit(child, sects, claimed or own)

        for element in tagged.elements:
            visit(element, 0, False)
        return PageClaims(page, order, tagged.tagged_share >= TRUSTED_SHARE)


class BookmarkWitness:
    """The outline as heading claims: a bookmark whose title matches a heading's text on its
    page claims that heading at the bookmark's level. Trusted when the outline has at least
    MIN_BOOKMARKS entries and 80% of them resolve to a page.

    A claim needs a box: the page's full width (Bookmark.width, 612 when unknown) and the
    band [top - 2, top + 14]. A bookmark without a destination y is skipped."""
    source = Source.TAGS
    kind = WitnessKind.BOOKMARKS
    MIN_BOOKMARKS = 3
    RESOLVED_SHARE = 0.8

    def __init__(self, bookmarks: list[Bookmark], page_texts: dict[int, list[str]]):
        self._bookmarks = bookmarks
        self._texts = page_texts

    def pages(self) -> set[int]:
        return {b.page for b in self._bookmarks if b.page is not None and b.top is not None}

    def _trusted(self) -> bool:
        total = len(self._bookmarks)
        resolved = sum(b.page is not None for b in self._bookmarks)
        return total >= self.MIN_BOOKMARKS and resolved >= self.RESOLVED_SHARE * total

    def claims(self, page: int) -> PageClaims | None:
        lines = [fold(line) for line in self._texts.get(page, ())]
        found = []
        for bookmark in self._bookmarks:
            if bookmark.page != page or bookmark.top is None:
                continue
            title = fold(bookmark.title)
            if title and any(line.startswith(title) for line in lines):
                found.append(bookmark)
        if not found:
            return None
        found.sort(key=lambda b: b.top)
        order = [Claim((0.0, b.top - 2, b.width or _DEFAULT_WIDTH, b.top + 14),
                       _heading_role(b.level), _key(b.title), {"level": b.level})
                 for b in found]
        return PageClaims(page, order, self._trusted())
