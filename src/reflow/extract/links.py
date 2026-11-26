"""Link annotations → inline spans: reads the annotations, places each on the words its
rectangle covers, and attaches them to the nodes that hold those words (`attach_links`).

Targets: /URI → the URL; /GoTo or /Dest → "#p{n}" for the destination page, with the
destination's y on it (`Link.dest_top`); /GoToR, /Launch, /JavaScript → dropped.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Container, Sequence
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from pikepdf import Name

from reflow.extract.lines import SOFT_HYPHEN
from reflow import geometry
from reflow.extract.pagecontent import PageReader, Word
from reflow.extract.pdfnav import Destinations, frame_of, rect_to_model
from reflow.model import BBox, HEADING_ROLES, Node, Role, Source, Span


@dataclass(frozen=True)
class Link:
    page: int
    bbox: BBox
    target: str                 # URL, or "#p12"
    kind: str                   # "uri" | "page"
    words: tuple[str, ...]      # the words under the rectangle, in content order
    dest_top: float | None = None  # for page links: the destination's y on the target page


type Pending = dict[int, list[tuple[BBox, str, str, float | None]]]


def scan_links(pdf: Path, pages: list[int] | None = None) -> Pending:
    """The usable link annotations of the pages (all, when None), by page, in /Annots order:
    box, target, kind and destination height. pikepdf only; no page is parsed."""
    wanted = None if pages is None else set(pages)
    pending: Pending = {}
    with pikepdf.open(pdf) as doc:
        dests = Destinations(doc)
        for number, page in enumerate(doc.pages, start=1):
            if wanted is not None and number not in wanted:
                continue
            frame = frame_of(page.obj)
            for annot in page.obj.get(Name.Annots, []):
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                rect = annot.get(Name.Rect)
                if annot.get(Name.Subtype) != Name.Link or rect is None or len(rect) != 4:
                    continue
                target = _target(annot, dests)
                if target is None:
                    continue
                pending.setdefault(number, []).append((rect_to_model(frame, rect), *target))
    return pending


def read_links(pdf: Path, pages: list[int] | None = None, *,
               reader: PageReader | None = None,
               scanned: Pending | None = None) -> dict[int, list[Link]]:
    """Per page, the usable link annotations in /Annots order. {} when there are none. A
    conversion's `reader` (reflow.extract.pagecontent) supplies the parsed pages, which the
    tag tree shares; without one the pages are parsed here, in this process. `scanned` is
    scan_links(pdf, pages) when the caller has made it already."""
    pending = scanned if scanned is not None else scan_links(pdf, pages)

    links: dict[int, list[Link]] = {}
    if pending:
        reader = reader or PageReader(pdf, jobs=1)
        for number, page in sorted(reader.get(sorted(pending), words=True).items()):
            kept = []
            centres = _Centres(page.words or ())
            for box, target, kind, dest_top in pending[number]:
                under = centres.within(box)
                if under:
                    kept.append(Link(number, box, target, kind, under, dest_top))
            if kept:
                links[number] = kept
    return links


class _Centres:
    """A page's words by the height of their centres, so the words whose centre lies in a
    link's box are found without testing every word: the same words, in content order."""

    def __init__(self, words: Sequence[Word]) -> None:
        self._words = words
        self._by_height = sorted(range(len(words)), key=lambda i: self._cy(words[i]))
        self._heights = [self._cy(words[i]) for i in self._by_height]

    @staticmethod
    def _cy(word: Word) -> float:
        return (word[3] + word[4]) / 2

    def within(self, box: BBox) -> tuple[str, ...]:
        x0, top, x1, bottom = box
        lo, hi = bisect_left(self._heights, top), bisect_right(self._heights, bottom)
        found = sorted(self._by_height[lo:hi])
        return tuple(self._words[i][0] for i in found
                     if x0 <= (self._words[i][1] + self._words[i][2]) / 2 <= x1)


def _target(annot: pikepdf.Dictionary, dests: Destinations
            ) -> tuple[str, str, float | None] | None:
    """(target, kind, dest_top) for a link annotation, or None when it is not usable."""
    action = annot.get(Name.A)
    if action is not None:
        kind = action.get(Name.S)
        if kind == Name.URI:
            uri = action.get(Name.URI)
            return (str(uri), "uri", None) if uri is not None else None
        if kind != Name.GoTo:
            return None
        dest = action.get(Name.D)
    else:
        dest = annot.get(Name.Dest)
    resolved = dests.resolve(dest)
    if resolved is None:
        return None
    number, top = resolved
    return (f"#p{number}", "page", top)


def _folded(text: str) -> tuple[str, list[int]]:
    """Text without whitespace and soft hyphens, lower-cased, with each kept character's
    index in `text`."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        if ch.isspace() or ch == SOFT_HYPHEN:
            continue
        for low in ch.lower():
            chars.append(low)
            origin.append(i)
    return "".join(chars), origin


def offsets_of(words: tuple[str, ...], text: str,
               taken: Sequence[Span] = ()) -> tuple[int, int] | None:
    """Where the words sit in `text` as one run (start, end), or None when they don't
    appear in order. Whitespace and soft hyphens between the words are tolerated. A place
    that overlaps a span in `taken` is passed over, so a repeated word ("12" twice in a
    contents line) takes the next free occurrence."""
    needle, _ = _folded("".join(words))
    if not needle:
        return None
    haystack, origin = _folded(text)
    at = haystack.find(needle)
    while at >= 0:
        start, end = origin[at], origin[at + len(needle) - 1] + 1
        if not any(s.start < end and start < s.end for s in taken):
            return start, end
        at = haystack.find(needle, at + 1)
    return None


# Roles whose own text can carry a link (containers such as lists and tables hold none).
LINK_HOSTS = frozenset({Role.P, Role.LI, Role.CAPTION, Role.TD, Role.TH, Role.NOTE, *HEADING_ROLES})


def attach_links(nodes: list[Node], links: dict[int, list[Link]],
                 pages: Container[int] | None = None) -> int:
    """Annotate the exact text each link rectangle covers with a Span(kind="link"). The host
    is the smallest text node whose box holds the rectangle's centre; a table cell has no box
    of its own, so the cells of the table holding the centre are tried next. The offsets are
    where the covered words sit in the host's text. A link whose words can't be found in the
    host (the text was joined or reworded) is dropped, as is one that overlaps a span already
    there. Each host that gains a span records a "link" Fact. Returns the number of spans
    added. Page links keep "#p{n}"; the renderer points them at the chunk holding the page.
    A page link to a page not in `pages` (the document's) gets no span, so its words stay
    plain text, and its host records a "link-unresolved" Fact with the link's box for review."""
    hosts_on: dict[int, list[Node]] = {}
    tables_on: dict[int, list[Node]] = {}
    for top in nodes:
        for node in top.walk():
            if node.page not in links or not node.bbox:
                continue
            if node.role in LINK_HOSTS and node.text:
                hosts_on.setdefault(node.page, []).append(node)
            elif node.role == Role.TABLE:
                tables_on.setdefault(node.page, []).append(node)
    added = 0
    for page, found in links.items():
        for link in found:
            cx = (link.bbox[0] + link.bbox[2]) / 2
            cy = (link.bbox[1] + link.bbox[3]) / 2
            candidates = sorted((n for n in hosts_on.get(page, []) if geometry.contains(n.bbox, cx, cy)),
                                key=lambda n: geometry.area(n.bbox))
            for table in tables_on.get(page, []):
                if geometry.contains(table.bbox, cx, cy):
                    candidates += [c for c in table.walk()
                                   if c.role in (Role.TD, Role.TH) and c.text and not c.bbox]
            unresolved = (link.kind == "page" and pages is not None
                          and int(link.target[2:]) not in pages)
            for host in candidates:
                at = offsets_of(link.words, host.text or "", host.spans)
                if at is None:
                    continue
                if unresolved:
                    host.fact("link-unresolved", Source.TAGS, link.target, box=list(link.bbox))
                else:
                    host.spans.append(Span(at[0], at[1], "link", link.target))
                    host.spans.sort(key=lambda s: s.start)
                    host.fact("link", Source.TAGS, link.target)
                    added += 1
                break
    return added

