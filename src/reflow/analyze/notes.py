"""Footnotes: NOTE nodes and their references.

A footnote block is a paragraph at the foot of a page, set smaller than the body, that starts
with a marker (a number, *, †, a letter) which also appears as a reference in the page's body
text (a superscript run, or a bracketed number). Such a block is changed like this:

  * the block becomes Role.NOTE with attrs {"marker": "3"}, placed at the end of its page's
    flow (after the body, before the next page) so reading order is not interrupted;
  * the body node carrying the reference gets Span(start, end, "noteref", note.id);
  * the renderer emits <a role="doc-noteref" href="#n-<id>">3</a> and
    <aside role="doc-footnote" id="n-<id>"> (DPUB-ARIA), see render.links.

Signals come from the lines: font size (Line.size vs body size), position (the bottom
FOOT_SHARE of the page), and a superscript (Line.sup: a run of small glyphs raised above the
line's baseline, recorded by the extractor). The heuristic engine knows the body size
(headings.body_size). Only a note and a reference that agree on the marker are changed.
"""

from __future__ import annotations

import re
from statistics import median

from reflow.extract.lines import SOFT_HYPHEN
from reflow import geometry
from reflow.extract.pages import PageContent
from reflow.model import Node, Role, SUBHEADING_ROLES, Span

FOOT_SHARE = 0.25      # footnotes sit in the bottom quarter of the page
NOTE_SIZE_MAX = 0.9    # x body size: footnotes are set smaller
MARKER = re.compile(r"^(\d{1,3}|[*†‡§¶]|[a-z])[\s.)]")
# Roles whose text can hold a reference.
HOSTS = frozenset({Role.P, Role.LI, Role.CAPTION, Role.TD, Role.TH, *SUBHEADING_ROLES})
# Characters before the marker that identify its place in the node's text.
CONTEXT = 12


def attach_footnotes(nodes: list[Node], pages: list[PageContent], body: float) -> int:
    """Turn footnote paragraphs into NOTE nodes and annotate their references in place.
    Returns the number of notes made. A foot paragraph with no matching reference stays a
    paragraph (a footer line is more likely); only matched pairs change. Each NOTE moves to
    the end of its page's nodes, keeping the notes' order; the body node gets a "noteref"
    Span over the marker; the note's text loses its leading marker (attrs["marker"] has it)."""
    made = 0
    all_feet = {page.number: _candidates([n for n in nodes if n.page == page.number], page, body)
                for page in pages}
    lettered = _letters_are_a_sequence({m for feet in all_feet.values() for _, m in feet})
    for page in pages:
        on_page = [n for n in nodes if n.page == page.number]
        feet = [(n, m) for n, m in all_feet[page.number] if lettered or not m.isalpha()]
        if not feet:
            continue
        refs = find_references(page, {marker for _, marker in feet})
        foot_ids = {id(n) for n, _ in feet}
        hosts = [h for top in on_page if id(top) not in foot_ids
                 for h in top.walk() if h.role in HOSTS and h.bbox and h.text
                 and not h.attrs.get("figure_label")]
        notes: list[Node] = []
        for note, marker in feet:
            placed = _place(note, marker, refs, page, hosts)
            if not placed:
                continue
            _turn_into_note(note, marker)
            for count, (host, start, end) in enumerate(placed, 1):
                # The first reference targets the note's id; later ones "<id>#<n>".
                target = note.id if count == 1 else f"{note.id}#{count}"
                host.spans.append(Span(start, end, "noteref", target))
                host.spans.sort(key=lambda s: s.start)
            note.attrs["refs"] = len(placed)
            notes.append(note)
        if not notes:
            continue
        moved = {id(n) for n in notes}
        nodes[:] = [n for n in nodes if id(n) not in moved]
        rest = [i for i, n in enumerate(nodes) if n.page == page.number and n.role != Role.ARTIFACT]
        at = rest[-1] + 1 if rest else len(nodes)
        nodes[at:at] = notes
        made += len(notes)
    return made


def find_references(page: PageContent, markers: set[str]) -> list[tuple[int, str, int]]:
    """(line index, marker, char offset) of superscript references on the page whose marker
    is in `markers`: the raised runs the extractor recorded (Line.sup), in line order. The
    offset is where the marker starts in that line's text."""
    found = []
    for i, line in enumerate(page.lines):
        if line.rotated:
            continue
        for start, end in line.sup:
            marker = line.text[start:end]
            if marker in markers:
                found.append((i, marker, start))
    return found


def _candidates(on_page: list[Node], page: PageContent, body: float) -> list[tuple[Node, str]]:
    """The paragraphs at the foot of the page that are set small and open with a marker."""
    out = []
    for node in on_page:
        if node.role != Role.P or not node.bbox or not node.text:
            continue
        if node.bbox[1] < page.height * (1 - FOOT_SHARE):
            continue
        match = MARKER.match(node.text)
        if match is None:
            continue
        sizes = [l.size for l in page.lines if not l.rotated and geometry.centre_in(l.bbox, node.bbox)]
        if sizes and median(sizes) <= NOTE_SIZE_MAX * body:
            out.append((node, match.group(1)))
    return out


def _letters_are_a_sequence(markers: set[str]) -> bool:
    """Whether the lettered foot paragraphs of the document form a run of consecutive letters
    (a, b, c), as lettered notes do. A single raised letter is more likely an exponent or an
    ordinal than a note, so letter markers are used only when the document's notes agree."""
    letters = sorted(ord(m) for m in markers if len(m) == 1 and m.isalpha())
    return len(letters) >= 2 and letters[-1] - letters[0] == len(letters) - 1


def _place(note: Node, marker: str, refs, page: PageContent,
           hosts: list[Node]) -> list[tuple[Node, int, int]]:
    """The host node and offsets of every reference to `marker` that sits above the note and
    can be found in a node's text, in reading order. The spans found are claimed as they go
    (a temporary span) so two references do not land on the same text."""
    placed: list[tuple[Node, int, int]] = []
    for index, found, offset in refs:
        if found != marker:
            continue
        line = page.lines[index]
        if geometry.centre_in(line.bbox, note.bbox) or line.top >= note.bbox[1]:
            continue
        context = line.text[:offset].replace(SOFT_HYPHEN, "")[-CONTEXT:]
        if not context:
            continue
        inside = [h for h in hosts if geometry.centre_in(line.bbox, h.bbox)]
        for host in sorted(inside, key=lambda n: geometry.area(n.bbox)):
            at = _find_free(host, context + marker, len(context), placed)
            if at is not None:
                start = at + len(context)
                placed.append((host, start, start + len(marker)))
                break
    return placed


def _find_free(host: Node, needle: str, tail: int,
               claimed: list[tuple[Node, int, int]] = ()) -> int | None:
    """The first place `needle` occurs in the host's text where no span covers the part from
    offset `tail` on (the marker; the context before it may sit next to another reference)."""
    text = host.text or ""
    at = text.find(needle)
    while at >= 0:
        start, end = at + tail, at + len(needle)
        taken = [(s.start, s.end) for s in host.spans]
        taken += [(a, b) for h, a, b in claimed if h is host]
        if not any(a < end and start < b for a, b in taken):
            return at
        at = text.find(needle, at + 1)
    return None



def _turn_into_note(note: Node, marker: str) -> None:
    text = note.text or ""
    cut = len(marker) + 1                      # the marker and the one separator MARKER matched
    shift = cut + (len(text[cut:]) - len(text[cut:].lstrip()))
    note.role = Role.NOTE
    note.set_text(text[shift:])                # moves the note's own spans and marks too
    note.attrs["marker"] = marker
    note.attrs.setdefault("why", []).append(f"footnote: marker {marker} is referenced above")
