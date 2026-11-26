"""The chunks of the transcript: the pages with their wrapper, label and notes, in chunks sized
for the viewer's first load (make_chunks), and the links between them (resolve_links)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from markupsafe import escape

from reflow.analyze.sections import heading_of
from reflow.model import HEADING_LEVEL, Document, Node, PageInfo, Role, is_ocr
from reflow.names import CHUNKS_DIR, PAGES_DIR
from reflow.render.html import NodeRenderer, field_source
from reflow.render.links import PAGE_LINK, attribute
from reflow.render.pagenames import page_text
from reflow.render.pages import page_filename

# Chunks are whole pages. The first chunk is small, because the viewer fetches it before
# anything else and its size is the viewer's first-load cost: it takes page 1, then more pages
# only while they keep it within FIRST_TARGET_BYTES. Later chunks start a new chunk at a page
# boundary once the current one reaches TARGET_BYTES, or at a page that opens a top-level
# section once it has MIN_BYTES. The shell (index.html) inlines chunks until the same rule
# would close the section (see inline_count).
FIRST_TARGET_BYTES = 30_000


MIN_BYTES = 40_000


TARGET_BYTES = 150_000


@dataclass
class Chunk:
    number: int
    pages: list[int] = field(default_factory=list)
    html: str = ""
    first_heading: str | None = None
    opens_section: bool = False  # its first page opens with a top-level heading

    @property
    def href(self) -> str:
        return f"{CHUNKS_DIR}/{self.number:04d}.html"

    @property
    def page_range(self) -> str:
        return (str(self.pages[0]) if len(self.pages) == 1
                else f"{self.pages[0]}–{self.pages[-1]}")


@dataclass
class Slice:
    """The part of a SECTION that falls on one page."""
    section: Node
    items: list[Node | Slice]


def split_by_page(children: list[Node], out: dict[int, list[Node | Slice]]) -> None:
    """Distribute reading-order nodes over pages, keeping each SECTION as a Slice on every
    page it touches. Artifacts are dropped; nodes on pages outside `out` are dropped."""
    for node in children:
        if node.role == Role.SECTION:
            heading_of(node)  # Validate even sections whose children have no page.
            inner: dict[int, list[Node | Slice]] = {number: [] for number in out}
            split_by_page(node.children, inner)
            for number, items in inner.items():
                if items:
                    out[number].append(Slice(node, items))
        elif node.role != Role.ARTIFACT and node.page in out:
            out[node.page].append(node)


def leaves(items: list[Node | Slice]) -> Iterator[Node]:
    for item in items:
        if isinstance(item, Slice):
            yield from leaves(item.items)
        else:
            yield item


FORM_NOTE = ('<p class="form-note" role="note">This document has fillable fields. They are '
             'here for reading and typing along: what you type stays in your '
             'browser and is not sent anywhere. You can download or print it from the '
             'contents.</p>')


DRAWN_FORM_NOTE = ('<p class="form-note" role="note">This form draws its check boxes on the page and '
                   'has no fillable fields of its own, so the check boxes here were found by reading '
                   'the page and some may be missing or in the wrong place. Typing is for reading '
                   'along: what you type stays in your browser and is not sent anywhere. You can '
                   'download or print the transcript with what you typed from the contents.</p>')


SCAN_FORM_NOTE = ('<p class="form-note" role="note">The fields in this document were found by '
                  'reading the scanned page, so some may be missing or in the wrong place. '
                  'Typing is for reading along: what you type stays in your browser and is not '
                  'sent anywhere. You can download or print the transcript with what you typed '
                  'from the contents.</p>')


BLANK_FORM_NOTE = ('<p class="form-note" role="note">Some fields in this document were made from the '
                   'blank lines printed on the page, so they are not in the PDF itself. Typing is '
                   'for reading along: what you type stays in your browser and is not sent '
                   'anywhere. You can download or print the transcript with what you typed from '
                   'the contents.</p>')


FIELD_NOTES = {"scan": SCAN_FORM_NOTE, "drawn": DRAWN_FORM_NOTE, "blank": BLANK_FORM_NOTE}


def form_note(doc: Document) -> str:
    """The note that opens a document with form fields, or "": it says how the fields were
    found when some are not in the PDF (render.html.field_source), the scan's note first."""
    fields = [n for n in doc.root.walk() if n.role == Role.FIELD]
    if not fields:
        return ""
    found = {field_source(n.attrs["detected"]) for n in fields if n.attrs.get("detected")}
    return next((note for source, note in FIELD_NOTES.items() if source in found), FORM_NOTE)


def ocr_note(page: PageInfo, has_ocr_text: bool) -> str:
    """The note that says OCR read the page, or "": for a page reflow OCR'd, how many
    words it left out; for OCR text the PDF brought with it, that nobody checked it. Marked
    `data-generated`: the reader's Find skips text reflow wrote itself."""
    facts = {f.code: f for f in page.facts}
    if "ocr-page" in facts:
        part = (facts["ocr-page"].data or {}).get("reason") == "textless-band"
        text = "Part of the text on this page was read by OCR." if part else \
            "Text on this page was read by OCR."
        if omitted := facts.get("ocr-words-omitted"):
            n = int(omitted.data.get("count", 0))
            text += (f" {n} words it could not read are not shown; see the original page." if n != 1
                     else " 1 word it could not read is not shown; see the original page.")
    elif has_ocr_text:
        text = "Text on this page comes from an OCR layer in the PDF and was not checked."
    else:
        return ""
    return f'<p class="ocr-note" role="note" data-generated>{escape(text)}</p>'


REVIEW_NOTE_WORDS = 5   # words a page note names before it counts the rest


def review_note(items: list[Node | Slice]) -> str:
    """The note that says a reviewer corrected words on the page (docs/review-decisions.md), or
    "": what OCR read and what the page shows, at most REVIEW_NOTE_WORDS words and then a count.
    Marked `data-generated`: the reader's Find skips it; the corrected word itself is page text."""
    fixes = [(m.alt, m.text) for item in leaves(items) for n in item.walk() for m in n.marks
             if m.kind == "fix"]
    if not fixes:
        return ""

    def listed(words: list[str]) -> str:
        shown = ", ".join(f"‘{w}’" for w in words[:REVIEW_NOTE_WORDS])
        more = len(words) - REVIEW_NOTE_WORDS
        return f"{shown} and {more} more" if more > 0 else shown

    n = len(fixes)
    text = (f"A reviewer corrected {n} word{'s' if n != 1 else ''} on this page. "
            f"OCR read {listed([w for w, _ in fixes])}; "
            f"the page shows {listed([w for _, w in fixes])}.")
    return f'<p class="review-note" role="note" data-generated>{escape(text)}</p>'


def font_note(items: list[Node | Slice]) -> str:
    """The note that says text on the page was decoded from a font whose text layer was wrong
    (ingest.font_map), or "": what the PDF read and what the page shows, at most
    REVIEW_NOTE_WORDS words and then a count. A node keeps the layer's reading in
    attrs["layer_text"]; a node whose text is that reading again (a reviewer rejected the map)
    is not counted. Marked `data-generated`: the reader's Find skips it."""
    pairs: dict[tuple[str, str], None] = {}
    for item in leaves(items):
        for n in item.walk():
            was, now = n.attrs.get("layer_text"), n.text or ""
            if isinstance(was, str) and was != now and len(was) == len(now):
                for a, b in zip(was.split(" "), now.split(" "), strict=False):
                    if a != b:
                        pairs.setdefault((a, b), None)
    if not pairs:
        return ""
    words = list(pairs)

    def listed(ws: list[str]) -> str:
        shown = ", ".join(f"‘{w}’" for w in ws[:REVIEW_NOTE_WORDS])
        more = len(ws) - REVIEW_NOTE_WORDS
        return f"{shown} and {more} more" if more > 0 else shown

    text = ("Text on this page was decoded from a font whose text layer was wrong. "
            f"The PDF read {listed([a for a, _ in words])}; "
            f"the page shows {listed([b for _, b in words])}.")
    return f'<p class="review-note font-note" role="note" data-generated>{escape(text)}</p>'


def _items_html(items: list[Node | Slice], r: NodeRenderer) -> str:
    def one(item: Node | Slice) -> str:
        if isinstance(item, Slice):
            heading = item.items[0] if item.items[0] is heading_of(item.section) else None
            return r.section(heading, r.section_items(heading, item.items, lambda items: _items_html(items, r), item.section))
        return r.render(item)

    return r.cell_fields(items, one)


def _page_html(number: int, label: str, items: list[Node | Slice], r: NodeRenderer,
               has_image: bool, note: str = "") -> str:
    name = page_text(number, label)
    view = (f" <a {attribute('href', f'{PAGES_DIR}/', page_filename(number), r.version_query)}>View original page {escape(name)}</a>"
            if has_image else "")
    return (f'<div class="pg" {attribute("id", "p", number)} {attribute("data-page", number)}>'
            f'<p class="pg-label"><span>Page {escape(name)}</span>{view}</p>{note}'
            f'{_items_html(items, r)}</div>')


def resolve_links(html: str, chunk_of_page: dict[int, Chunk]) -> str:
    """Point the page-number placeholders at the chunk file holding each page. The links are
    chunks/NNNN.html#pN from every page, because chunk.html sets <base href="../">: a bare
    "#pN" or "NNNN.html#pN" would resolve against the output root, not against chunks/."""
    def link(match: re.Match[str]) -> str:
        page = int(match.group(1))
        chunk = chunk_of_page.get(page)
        if chunk is None:
            # attach_links publishes a link to a page the document lacks as plain text, so a
            # placeholder here is a bug; an href="#" would jump a reader to the top.
            raise ValueError(f"page link to page {page}, which no chunk holds")
        return f"{chunk.href}#{match.group(2) or f'p{page}'}"
    # A NUL from the PDF (a tooltip, alt text) is not valid in HTML: the parser reports it and
    # shows U+FFFD, so it is dropped here, where the placeholders no longer need it apart.
    return PAGE_LINK.sub(link, html).replace("\x00", "")


def opens_top_section(items: list[Node | Slice]) -> bool:
    """Whether the page starts a top-level section: its first item is the slice of a level-2
    SECTION that begins here, with the heading as its first node."""
    first = items[0] if items else None
    return (isinstance(first, Slice) and first.section.attrs.get("level") == 2
            and first.items[0] is heading_of(first.section))


def make_chunks(doc: Document, r: NodeRenderer, has_images: bool) -> list[Chunk]:
    by_page: dict[int, list[Node | Slice]] = {p.number: [] for p in doc.pages}
    split_by_page(doc.root.children, by_page)

    chunks = [Chunk(1)]
    size = 0
    note = form_note(doc)
    infos = {p.number: p for p in doc.pages}
    for number, items in by_page.items():
        opens_section = opens_top_section(items)
        notes = note if number == next(iter(by_page)) else ""
        notes += ocr_note(infos[number], any(is_ocr(n) for item in leaves(items)
                                             for n in item.walk()))
        notes += review_note(items)
        notes += font_note(items)
        html = _page_html(number, r.labels.get(number, str(number)), items, r, has_images,
                          notes)
        first = len(chunks) == 1
        if chunks[-1].pages and (
                size + len(html) > FIRST_TARGET_BYTES if first
                else size >= TARGET_BYTES or (opens_section and size >= MIN_BYTES)):
            chunks.append(Chunk(len(chunks) + 1))
            size = 0
        chunk = chunks[-1]
        if not chunk.pages:
            chunk.opens_section = opens_section
        chunk.pages.append(number)
        chunk.html += html
        size += len(html)
        if chunk.first_heading is None:
            chunk.first_heading = next((n.text for n in leaves(items)
                                        if n.role in HEADING_LEVEL and n.role != Role.H1), None)
    return chunks


def inline_count(chunks: list[Chunk]) -> int:
    """How many chunks the shell (index.html) carries: the first section, as it was before the
    first chunk was made small. Chunks are taken in order until the next one opens a top-level
    section and MIN_BYTES are in, or TARGET_BYTES are in. Always at least one."""
    total = len(chunks[0].html)
    count = 1
    for chunk in chunks[1:]:
        if total >= TARGET_BYTES or (chunk.opens_section and total >= MIN_BYTES):
            break
        total += len(chunk.html)
        count += 1
    return count
