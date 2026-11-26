"""Figures: picture and drawing boxes, chart labels and axis text, legends and
icon marks.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from reflow.patterns import CAPTION_KEPT, CAPTION_LABEL
from reflow import geometry
from reflow.analyze.constants import CONF_FIGURE_NO_ALT, CONF_PARAGRAPH
from reflow.extract.images import Icon
from reflow.extract.lines import SOFT_HYPHEN, Line
from reflow.extract.pages import PageContent
from reflow.extract.ocrwords import GradedWord
from reflow.extract.scan import TILE, InkRegion
from reflow.extract.tables.cells import TableData
from reflow.ingest.ocr_trust import PEN_UNREAD, Grade, colour_cast
from reflow.model import BBox, Node, Role, Source
from reflow.text_quality import squash


# Images smaller than this share of the page are decorations (bullets, rules, logos).
MIN_FIGURE_AREA = 0.01


# A cluster of vector drawing operations is a figure (chart, diagram) when it has at
# least this many paths, covers MIN_DRAWING_AREA of the page, and text covers less than
# MAX_DRAWING_TEXT of its area. Ruled tables are full of text, so they don't qualify.
MIN_DRAWING_PATHS = 8
MIN_DRAWING_AREA = 0.01
MAX_DRAWING_TEXT = 0.3


# Solid shapes with no outlines (banners, backdrops, bars) with this many lines of words on
# them are text on a background, not a picture: the lines stay text. Diagrams and charts have
# strokes (box outlines, arrows, axes) and are never fill-only.
BACKDROP_WORD_LINES = 2
# Short lines (labels) this close to a vector figure belong to it.
LABEL_DISTANCE = 12.0
LABEL_MAX_CHARS = 20


# A shape with this many prose-length lines (≥ PROSE_CHARS) inside is a backdrop behind
# text (shaded boxes, header bars, side tabs), not a figure.
PROSE_LINES = 3
PROSE_CHARS = 20


# Two images are pieces of one picture when they touch or overlap along a whole side: a collage
# cut on a diagonal is two overlapping image objects with the same top and bottom edges. A gutter
# wider than this between them (a grid of photos) keeps them apart.
PIECE_GAP = 1.0
PIECE_EDGE = 3.0  # their shared edges (top and bottom, or left and right) agree within this


def _pieces(a: BBox, b: BBox) -> bool:
    """Whether two boxes together make one rectangle: they overlap or touch across one axis
    (within PIECE_GAP) and have the same extent along the other (within PIECE_EDGE)."""
    def joined(lo: int, hi: int, plo: int, phi: int) -> bool:
        return (max(a[lo], b[lo]) - min(a[hi], b[hi]) <= PIECE_GAP
                and abs(a[plo] - b[plo]) <= PIECE_EDGE and abs(a[phi] - b[phi]) <= PIECE_EDGE)
    return joined(0, 2, 1, 3) or joined(1, 3, 0, 2)


def _piece_groups(boxes: list[BBox]) -> list[tuple[BBox, list[BBox]]]:
    """Merge the images that are pieces of one rectangular picture (see _pieces) into one box each,
    with the boxes it was joined from."""
    groups = [(b, [b]) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i, (a, members) in enumerate(groups):
            j = next((j for j in range(i + 1, len(groups)) if _pieces(a, groups[j][0])), None)
            if j is not None:
                b, more = groups.pop(j)
                groups[i] = ((min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])),
                             members + more)
                merged = True
                break
    return groups


# Figures that are columns or rows of one picture (a diagram drawn as three side-by-side
# networks) touch along one side and sit within this many body-text sizes of each other on the
# other; they are one figure when one caption sits under all of them, each one under its span.
FIGURE_GUTTER = 3.0
FIGURE_CAPTION_GAP = 2.0


def figure_groups(figures: Sequence[BBox], captions: Sequence[BBox], body: float) -> list[list[BBox]]:
    """Group the figure boxes that are pieces of one figure (see FIGURE_GUTTER), each group
    sitting above a caption that spans every piece. Other boxes stay alone."""
    def adjacent(a: BBox, b: BBox) -> bool:
        gap_x = max(a[0], b[0]) - min(a[2], b[2])
        gap_y = max(a[1], b[1]) - min(a[3], b[3])
        near = FIGURE_GUTTER * body
        return (gap_x <= 0 and gap_y <= near) or (gap_y <= 0 and gap_x <= near)

    parent = list(range(len(figures)))

    def root(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    for i, a in enumerate(figures):
        for j in range(i + 1, len(figures)):
            if adjacent(a, figures[j]):
                parent[root(j)] = root(i)
    components: dict[int, list[BBox]] = {}
    for i, f in enumerate(figures):
        components.setdefault(root(i), []).append(f)
    groups: list[list[BBox]] = []
    for members in components.values():
        bottom = max(m[3] for m in members)
        capped = any(
            0 <= cap[1] - bottom <= FIGURE_CAPTION_GAP * body
            and all(min(cap[2], m[2]) > max(cap[0], m[0]) for m in members)
            for cap in captions)
        if len(members) > 1 and capped:
            groups.append(members)
        else:
            groups.extend([m] for m in members)
    return groups


@dataclass(frozen=True)
class UnreadEvidence:
    """What reflow knows of ink on a scanned page that no trusted text explains."""
    # The words OCR left out of the text layer (PageWords.omitted) centred in the figure, in
    # OCR's order: its guess, for the inspector only.
    words: tuple[GradedWord, ...] = ()
    pen_share: float | None = None     # the most chromatic of its pen regions (InkRegion.pen_share)
    kind: Literal["writing", "signature"] = "writing"
    role: str | None = None            # a signature's role (SignatureMatch.role)

    @property
    def ocr_guess(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class FigureCandidate:
    """One figure as detection found it, carried unchanged from detection to the FIGURE node."""
    bbox: BBox
    source: Literal["object", "picture", "drawing", "chart", "pen"]
    why: str                                # inspector text only; never parsed
    # Indexes into PageContent.lines of the lines that move into the figure, in reading order.
    # None: the figure takes no text, and a ruled table it overlaps vetoes it. (): it keeps its
    # words in the flow beside it, and it survives that veto.
    labels: tuple[int, ...] | None = None
    unread: UnreadEvidence | None = None    # writing no text transcribes: a crop, not a picture
    # A signature's role line (PageContent.lines index): the figure goes right before its node.
    anchor: int | None = None


# A pen region is a signature when it is no larger than SIGNATURE_MAX (w, h) and a short printed
# line (at most SIGNATURE_WORDS) or a ruling lies in its lower third or within SIGNATURE_GAP under
# it, overlapping it across by SIGNATURE_OVERLAP of the narrower width. A region's box is rounded
# out to scan tiles, so its lower third starts up to SIGNATURE_REACH higher; and a signature may
# hang below its line (a stroke's tail): a line the region crosses is on it (budget p4's Secretary
# signature, whose line crosses it at 0.64 of its height, 19 pt above its foot). Budget p5's scan finds
# only the right part of the affidavit signature: it overlaps "For the Golden Transcript" by 0.49.
# A region with print beside it on its row (see signature_label) is writing within print instead.
# A signature line is one thin rule (SIGNATURE_RULE thick at most) under half the page wide: a
# table's rules (the permit ledgers) span the table, or come as a cluster of rules. A signature
# sits on its line: a region whose ink stops more than SIGNATURE_REACH (one scan tile) above the
# signature line under it is writing above an empty line (legislation p1's "Dyson", written
# under its own line, above Johnson's), never that line's signature.
SIGNATURE_MAX = (260.0, 90.0)
SIGNATURE_GAP = 30.0
SIGNATURE_TIE = 4.0
SIGNATURE_WORDS = 4
SIGNATURE_OVERLAP = 0.4
SIGNATURE_RULE = 4.0
SIGNATURE_REACH = 8.0


@dataclass(frozen=True)
class SignatureMatch:
    line: int | None    # index into the lines of the role line; None: on a ruling, no line
    role: str | None    # the role line's text, only when every one of its words is READ


def signature_label(region: BBox, lines: Sequence[Line], rulings: Sequence[BBox],
                    page_width: float, words: Sequence[GradedWord] = ()) -> SignatureMatch | None:
    """Whether a pen region is a signature, and which printed line it signs: the nearest short
    OCR line under it, else a signature line under it (no line, see SIGNATURE_RULE). Two lines at nearly the same gap
    (SIGNATURE_TIE) are ambiguous: the signature line alone decides, as it does with no line
    (legislation p1's "On the" and "of the House", one printed row OCR read in two pieces). A
    region with neither is None (see SIGNATURE_MAX).
    A signature stands above its line: a region with a line on its row (over half the line's
    height beside its upper two thirds, within two line heights across, as a typewriter spaces
    its words) is a word in print that OCR could not read, a struck passage or a filled-in
    blank, never a signature. A line of nothing but OCR remnants (GradedWord.remnant) of OCR
    lines that hold no struck word (`words`, the page's graded words) is what OCR kept of
    writing (legislation p1's "h" of a handwritten "Leach"): no print, neither beside a
    signature nor its role line. Beside a struck passage OCR left out, a remnant is print
    (legislation p37's "county")."""
    x0, top, x1, bottom = region
    if x1 - x0 > SIGNATURE_MAX[0] or bottom - top > SIGNATURE_MAX[1]:
        return None
    lower_third = top + 2 * (bottom - top) / 3

    struck = {w.ocr_line for w in words if w.struck}

    def printed(line: Line) -> bool:
        return not line.rotated and not (line.grades and all(
            w.remnant and w.ocr_line not in struck for w in line.grades))

    def on_row(box: BBox) -> bool:
        height = box[3] - box[1]
        return (box[1] < lower_third and min(bottom, box[3]) - max(top, box[1]) >= 0.5 * height
                and max(x0 - box[2], box[0] - x1) <= 2 * height)

    if any(on_row(l.bbox) for l in lines if printed(l)):
        return None

    def gap(box: BBox, crossed: bool = False) -> float | None:
        across = min(x1, box[2]) - max(x0, box[0])
        if (lower_third - SIGNATURE_REACH <= box[1] <= bottom + SIGNATURE_GAP
                and across >= SIGNATURE_OVERLAP * min(x1 - x0, box[2] - box[0])):
            return max(0.0, box[1] - bottom) if crossed else abs(box[1] - bottom)
        return None

    signature_lines = [r for r in rulings
                       if r[3] - r[1] <= SIGNATURE_RULE and r[2] - r[0] < page_width / 2]
    if min((g for r in signature_lines if (g := gap(r, crossed=True)) is not None), default=0.0) > SIGNATURE_REACH:
        return None
    near = sorted((g, i) for i, l in enumerate(lines)
                  if l.ocr and printed(l) and len(l.text.split()) <= SIGNATURE_WORDS
                  and (g := gap(l.bbox)) is not None)
    if near and not (len(near) > 1 and near[1][0] - near[0][0] < SIGNATURE_TIE):
        line = lines[near[0][1]]
        read = bool(line.grades) and all(w.grade is Grade.READ for w in line.grades or ())
        return SignatureMatch(near[0][1], line.text.strip() if read else None)
    if any(gap(r) is not None for r in signature_lines):
        return SignatureMatch(None, None)
    return None


def line_indexes(page: PageContent, lines: Sequence[Line]) -> tuple[int, ...]:
    """The indexes of `lines` (objects of page.lines) in page.lines, in the order given."""
    index = {id(l): i for i, l in enumerate(page.lines)}
    return tuple(index[id(l)] for l in lines if id(l) in index)


def figure_boxes(page: PageContent, lines: list[Line], body: float | None = None
                 ) -> list[FigureCandidate]:
    """Raster images and vector drawings that look like figures, with the text labels
    next to them (axis labels, legends) folded into the figure's box. `body` is the body
    text size; labels are no larger than it. A chart (a drawing with chart marks, see
    chart_marks) that a backdrop gate would reject is accepted; its labels are the lines that
    move into it. Its caption, notes and source stay in the flow."""
    area = page.width * page.height
    found = _raster_figures(page, lines, area)
    for d in page.drawings:
        box = d.bbox
        # Only rules (table lines, underlines): never a picture. Its text stays text,
        # even if the table itself wasn't recognized.
        if d.only_rules or d.paths < MIN_DRAWING_PATHS:
            continue
        if not MIN_DRAWING_AREA * area <= geometry.area(box) < 0.9 * area:
            continue
        inside = [l for l in lines if geometry.inside(l.bbox, box, slack=6)]
        # A backdrop (prose on a shape, or words on solid fills) is text, not a picture, unless
        # the shape has chart marks: repeated bars, dots, wedges or ticks (see extract.chart_marks).
        prose_backdrop = sum(1 for l in inside if len(l.text.strip()) >= PROSE_CHARS) >= PROSE_LINES
        solid_backdrop = (d.fill_only
                          and sum(1 for l in inside if len(l.text.split()) >= 2) >= BACKDROP_WORD_LINES)
        if (prose_backdrop or solid_backdrop) and not d.marks:
            continue  # its text is content and stays text (prose), or a banner (solid fills)
        # A banner with a short label on it (a county name on a grey bar) is text: no outlined
        # shapes, no chart marks, and the label is a live line at least body size. Tiny logo
        # lettering and OCR lines do not make a banner.
        label_banner = (d.outlines == 0 and not d.marks and 1 <= len(inside) <= 2
                        and all(len(l.text.split()) <= 4 and l.size >= (body or 0)
                                and not l.ocr for l in inside))
        if label_banner:
            continue
        if sum(geometry.area(l.bbox) for l in inside) >= MAX_DRAWING_TEXT * geometry.area(box):
            continue
        grown = _with_labels(box, lines, body)
        if d.marks:
            grown = _with_axis_text(grown, lines, [l for l in page.lines if l.rotated], body)
        kept = chart_labels(grown, lines)
        if prose_backdrop or solid_backdrop:
            found.append(FigureCandidate(
                grown, "chart", f"chart: {_mark_text(d.marks)}, {size_text(grown)}, "
                f"not a {'prose' if prose_backdrop else 'solid'} backdrop", line_indexes(page, kept)))
            continue
        # The text inside the box moves with it, a chart's axis text as much as the words on a
        # drawn panel ("On average, ~60% of Hospitals…"): no text is left only in the picture.
        # A plain drawing with no text left takes none (labels None): an empty tuple would keep
        # it as a figure over the ruled table it draws (see find_figures).
        labels = (line_indexes(page, kept) if d.marks
                  else line_indexes(page, in_columns(kept)) if kept else None)
        found.append(FigureCandidate(
            grown, "chart" if d.marks else "drawing",
            f"vector drawing of {d.paths} paths ({d.outlines} outlined), {size_text(grown)}, "
            "with outlines, not a solid backdrop", labels))
    return _with_unread(page, found)


# A pen region lying this much inside the image objects found as figures belongs to them, and
# turns unread each object its regions cover this much of (the ledger strips of permit p1); in
# any other it is the object's own ink (a label of Carmel p10's timeline graphic). One overlapping
# a picture by this IoU is the picture's own ink too (a map's linework, a photographed sign).
UNREAD_INSIDE = 0.6
UNREAD_COVER = 0.5
UNREAD_PICTURE_IOU = 0.5
UNREAD_WHY = "unread: ink that no trusted text explains"


def _with_unread(page: PageContent, found: list[FigureCandidate]) -> list[FigureCandidate]:
    """The candidates with the page scan's pen regions (PageScan.pen) as unread figures, on a
    page whose text OCR read or that has none. A region inside image objects turns them unread,
    keeping their boxes; one over a picture adds nothing; any other is a figure of its own,
    which keeps the words beside it in the flow (labels ())."""
    if not page.scan.pen or (page.lines and not any(l.ocr for l in page.lines)):
        return found
    merged: dict[int, list[InkRegion]] = {}
    new: list[FigureCandidate] = []
    # A scanned signature line is pixels (a rule region), not a vector.
    rulings = ([d.bbox for d in page.drawings if d.only_rules]
               + [r.bbox for r in page.scan.regions if r.kind == "rule"])
    for region in page.scan.pen:
        box = region.bbox
        objects = [i for i, f in enumerate(found)
                   if f.source == "object" and geometry.overlap(box, f.bbox) > 0]
        if objects and sum(geometry.overlap(box, found[i].bbox) for i in objects) \
                >= UNREAD_INSIDE * geometry.area(box):
            for i in objects:
                merged.setdefault(i, []).append(region)
        elif not any(f.source == "picture" and geometry.iou(box, f.bbox) >= UNREAD_PICTURE_IOU
                     for f in found):
            evidence = _evidence(page, box, [region.pen_share])
            match = signature_label(box, page.lines, rulings, page.width,
                                    page.words.words if page.words is not None else ())
            if match is not None:
                evidence = replace(evidence, kind="signature", role=match.role)
            new.append(FigureCandidate(box, "pen", f"{UNREAD_WHY}, {size_text(box)}", (),
                                       evidence, None if match is None else match.line))
    unread = {i: regions for i, regions in merged.items()
              if sum(geometry.overlap(r.bbox, found[i].bbox) for r in regions)
              >= UNREAD_COVER * geometry.area(found[i].bbox)}
    return [replace(f, why=f"{f.why}, {UNREAD_WHY}",
                    unread=_evidence(page, f.bbox, [r.pen_share for r in unread[i]]))
            if i in unread else f for i, f in enumerate(found)] + new


def _evidence(page: PageContent, box: BBox, shares: Sequence[float | None]) -> UnreadEvidence:
    # On a page whose type is tinted as much as any ink (naca's brown paper), pen colour is no sign
    # of writing: ink OCR could not read there (stacked fractions) claims no kind (#258).
    cast = page.words is not None and colour_cast((w.text, w.conf, w.pen) for w in page.words.words)
    known = [s for s in shares if s is not None and not cast]
    words = tuple(w for w in (page.words.omitted if page.words is not None else ())
                  if box[0] <= (w.bbox[0] + w.bbox[2]) / 2 <= box[2]
                  and box[1] <= (w.bbox[1] + w.bbox[3]) / 2 <= box[3])
    return UnreadEvidence(words, max(known) if known else None)


def _raster_figures(page: PageContent, lines: Sequence[Line], area: float) -> list[FigureCandidate]:
    """The raster figures. A page the scan skipped (images under SCANNED_MIN_COVERAGE of it) keeps
    the object rule: each image object between MIN_FIGURE_AREA and 90% of the page. On a scanned
    or noisy page the figures are the scan's (PageScan.pictures_for): the pictures found in the
    page image, never the whole scan page. A picture found in the pixels takes the OCR lines
    centred in it as labels, so Tesseract's reading of a photo leaves the flow; live text over it
    stays. An image object that is a figure itself takes no labels: a plain one holds no line
    (None: a ruled table it overlaps vetoes it), and a text one with ink beyond its
    text keeps its words in the flow beside it and survives that veto (())."""
    scan = page.scan
    if scan.status == "skipped":
        return [FigureCandidate(b, "object", f"raster image, {size_text(b)}")
                for b, _ in _piece_groups(
                    [b for b in page.images
                     if MIN_FIGURE_AREA * area <= geometry.area(b) < 0.9 * area])]
    objects = {o.bbox: o.kind for o in scan.objects}
    found = []
    for box, members in _piece_groups(scan.pictures_for(area)):
        if all(m in objects for m in members):
            plain = all(objects[m] == "plain" for m in members)
            found.append(FigureCandidate(box, "object", f"raster image, {size_text(box)}",
                                         None if plain else ()))
            continue
        # The scan knows a picture's edges to a tile: a line centred within a tile of the edge
        # (the second line of a title over a map) is not read as inside it.
        inner = (box[0] + TILE, box[1] + TILE, box[2] - TILE, box[3] - TILE)
        held = [l for l in lines if l.ocr and geometry.centre_in(l.bbox, inner)]
        found.append(FigureCandidate(box, "picture", f"picture in the page image, {size_text(box)}"
                                     + (f", {len(held)} OCR lines in it" if held else ""),
                                     line_indexes(page, held)))
    return found


MARK_WORDS = {"bars": "repeated bars", "dots": "dots of one size",
              "wedges": "wedges sharing a centre", "ticks": "axis ticks"}


def _mark_text(marks: tuple[tuple[str, int], ...]) -> str:
    return " + ".join(f"{n} {MARK_WORDS.get(kind, kind)}" for kind, n in marks)


def size_text(b: BBox) -> str:
    return f"{round(b[2] - b[0])}×{round(b[3] - b[1])} pt"


NOTE_KEPT = re.compile(r"^(notes?|sources?)\s*[:.]", re.I)


def _stacked(prev: Line, line: Line, same_size: bool = True) -> bool:
    """Whether `line` is set directly under `prev` (the caller also checks the reverse): a gap
    under half the font size, overlapping columns, and, unless `same_size` is False, the same size."""
    height = prev.bbox[3] - prev.bbox[1]
    gap = line.bbox[1] - prev.bbox[3]
    return (-height < gap <= 0.5 * prev.size and (not same_size or abs(line.size - prev.size) <= 0.5)
            and min(line.bbox[2], prev.bbox[2]) > max(line.bbox[0], prev.bbox[0]))


def chart_labels(box: BBox, lines: Sequence[Line]) -> list[Line]:
    """The lines inside a chart's box that move into the figure as its labels (its axis names,
    category and value labels). The caption, with the title and subtitle set under it, and the
    notes and source, with the lines set against them, stay in the flow (see CAPTION_KEPT)."""
    inside = sorted((l for l in lines if geometry.inside(l.bbox, box, slack=6)),
                    key=lambda l: (l.bbox[1], l.bbox[0]))
    caption = [l for l in inside if CAPTION_KEPT.match(l.text.strip())]
    notes = [l for l in inside if NOTE_KEPT.match(l.text.strip())]
    grew = True
    while grew:
        grew = False
        for l in inside:
            if any(l is k for k in caption + notes):
                continue
            # A caption's title is often set larger than the caption, so its run ignores size.
            if any(_stacked(k, l, same_size=False) or _stacked(l, k, same_size=False) for k in caption):
                caption.append(l)
            elif any(_stacked(k, l) or _stacked(l, k) for k in notes):
                notes.append(l)
            else:
                continue
            grew = True
    return [l for l in inside if not any(l is k for k in caption + notes)]


def in_columns(lines: Sequence[Line]) -> list[Line]:
    """The lines of a drawn panel in reading order: column by column, left to right, and top
    to bottom in each. Lines whose spans overlap across the page share a column, so three
    callouts set side by side read "HOSPITALS / On average, / ~60% of Hospitals / …" and then
    the next one, not one row of each at a time."""
    columns: list[list[Line]] = []
    for line in sorted(lines, key=lambda l: l.bbox[0]):
        hits = [c for c in columns
                if any(min(line.bbox[2], k.bbox[2]) > max(line.bbox[0], k.bbox[0]) for k in c)]
        merged = [line] + [k for c in hits for k in c]
        columns = [c for c in columns if not any(c is h for h in hits)] + [merged]
    columns.sort(key=lambda c: min(l.bbox[0] for l in c))
    return [l for c in columns for l in sorted(c, key=lambda l: (l.bbox[1], l.bbox[0]))]


def _with_labels(box: BBox, lines: list[Line], body: float | None = None) -> BBox:
    """Grow a vector figure's box to take in nearby short labels (axis names, arrows).
    Distances are measured from the drawing itself, never from the grown box, so the
    box can't chain through a column of short lines (phone numbers, "Language: ...")."""
    x0, top, x1, bottom = box
    for line in lines:
        text = line.text.strip()
        if len(text) > LABEL_MAX_CHARS or CAPTION_LABEL.match(text):
            continue
        if body is not None and line.size > body:
            continue
        l = line.bbox
        dx = max(box[0] - l[2], l[0] - box[2], 0)
        dy = max(box[1] - l[3], l[1] - box[3], 0)
        if dx <= LABEL_DISTANCE and dy <= LABEL_DISTANCE:
            x0, top, x1, bottom = min(x0, l[0]), min(top, l[1]), max(x1, l[2]), max(bottom, l[3])
    return (x0, top, x1, bottom)


AXIS_PAD = 2.0  # pt of margin around axis text the box grows to take in, so descenders aren't cut


def _with_axis_text(box: BBox, lines: Sequence[Line], rotated: Sequence[Line],
                    body: float | None = None) -> BBox:
    """Grow a chart's box to the rest of its axis text that _with_labels stopped short of: the
    wrapped second and third lines of a category label ("Race and" / "ethnicity"), each set
    directly under a label already in the box, and a rotated axis title right beside it.
    Only short lines no larger than the body text, in the chart's own columns, so a caption,
    a note or a body paragraph under the chart is never taken in."""
    x0, top, x1, bottom = box
    held = [l for l in lines if geometry.inside(l.bbox, (x0, top, x1, bottom), slack=6)
            and _is_axis_label(l, body)]
    grew = True
    while grew:
        grew = False
        for line in lines:
            if any(line is h for h in held) or not _is_axis_label(line, body):
                continue
            if not x0 <= (line.bbox[0] + line.bbox[2]) / 2 <= x1:
                continue
            if any(_stacked(h, line) for h in held):
                held.append(line)
                bottom = max(bottom, line.bbox[3] + AXIS_PAD)
                grew = True
    for line in rotated:
        l = line.bbox
        if (_is_axis_label(line, body) and top <= (l[1] + l[3]) / 2 <= bottom
                and max(x0 - l[2], l[0] - x1, 0) <= LABEL_DISTANCE):
            x0, x1 = min(x0, l[0] - AXIS_PAD), max(x1, l[2] + AXIS_PAD)
    return (x0, top, x1, bottom)


def _is_axis_label(line: Line, body: float | None) -> bool:
    """A short line no larger than the body text that can be a chart's label: not a caption,
    a note, or a body-length line."""
    text = line.text.strip()
    return (len(text) <= LABEL_MAX_CHARS and not CAPTION_KEPT.match(text)
            and not NOTE_KEPT.match(text) and not CAPTION_LABEL.match(text)
            and (body is None or line.size <= body))


def rotated_labels(box: BBox, rotated: Sequence[Line], body: float | None = None) -> list[Line]:
    """The rotated lines inside a figure that are its labels: a chart's y-axis title ('error (%)'),
    which reads up or down beside the axis. They are laid out apart from the page's text (see
    rotated.rotated_nodes), so without this they would be running text next to the figure."""
    return [l for l in rotated if geometry.inside(l.bbox, box, slack=6) and _is_axis_label(l, body)]


# A legend entry is an icon with a short label right after it: "✓ Accepting New Patients".
LEGEND_MAX_CHARS = 40
LEGEND_GAP = 1.0  # label starts within this many icon widths of the icon's right edge
# The icon → label pair repeats on this many pages (a legend in every page's footer)...
LEGEND_MIN_PAGES = 3
# ...and is this many times more common than any other label beside that icon.
LEGEND_DOMINANCE = 3
# An icon marks the text line on its row within this many icon widths, on either side.
ICON_REACH = 1.5


def _same_row(icon: BBox, line: Line) -> bool:
    overlap = min(icon[3], line.bottom) - max(icon[1], line.top)
    return overlap >= 0.5 * min(icon[3] - icon[1], line.height)


def _label_after(icon: Icon, lines: list[Line]) -> Line | None:
    reach = LEGEND_GAP * icon.width
    best = min((l for l in lines if -1 <= l.x0 - icon.bbox[2] <= reach
                and _same_row(icon.bbox, l)), key=lambda l: l.x0, default=None)
    if best is None or len(best.text.strip()) > LEGEND_MAX_CHARS:
        return None
    return best


def find_legend(pages: list[PageContent]) -> dict[str, str]:
    """Icon key → meaning, from legend entries: an icon followed by a short label.

    A pair counts when it repeats on LEGEND_MIN_PAGES pages, or appears at least once
    while the icon also marks other text elsewhere; either way the label must be at
    least LEGEND_DOMINANCE times as common (in pages) as any other text after that icon,
    so icons that simply precede names ("✓ Smith, Jane") don't become a legend.
    """
    pairs: dict[str, dict[str, set[int]]] = {}
    uses: Counter[str] = Counter()
    labelled: Counter[str] = Counter()
    for page in pages:
        for icon in page.icons:
            uses[icon.key] += 1
            label = _label_after(icon, page.lines)
            if label is not None:
                labelled[icon.key] += 1
                text = squash(label.text)
                pairs.setdefault(icon.key, {}).setdefault(text, set()).add(page.number)
    legend = {}
    for key, labels in pairs.items():
        ranked = sorted(labels.items(), key=lambda kv: -len(kv[1]))
        text, on = ranked[0]
        runner_up = len(ranked[1][1]) if len(ranked) > 1 else 0
        if len(on) < LEGEND_DOMINANCE * runner_up:
            continue
        if len(on) >= LEGEND_MIN_PAGES or uses[key] > labelled[key]:
            legend[key] = text
    return legend


def mark_icons(page: PageContent, legend: dict[str, str]) -> tuple[dict[int, str], set[int]]:
    """Apply the legend to one page. Returns (id(line) → meaning) for text lines that a
    legend icon marks, and the ids of the legend's own label lines."""
    marks: dict[int, str] = {}
    legend_lines: set[int] = set()
    for icon in page.icons:
        meaning = legend.get(icon.key)
        if meaning is None:
            continue
        label = _label_after(icon, page.lines)
        if label is not None and squash(label.text) == meaning:
            legend_lines.add(id(label))  # the legend entry itself
            continue
        reach = ICON_REACH * icon.width
        near = [(max(l.x0 - icon.bbox[2], icon.bbox[0] - l.x1), l) for l in page.lines
                if _same_row(icon.bbox, l)]
        near = [(gap, l) for gap, l in near if -1 <= gap <= reach]
        if near:
            marks.setdefault(id(min(near, key=lambda g: g[0])[1]), meaning)
    # A legend entry's label is never a provider's line, even when another legend icon
    # sits right after it ("✓ Accepting New Patients ✖ Not Accepting New Patients").
    for line_id in legend_lines:
        marks.pop(line_id, None)
    return marks, legend_lines


def mostly_overlap(a: BBox, b: BBox) -> bool:
    return geometry.overlap(a, b) > 0.5 * min(geometry.area(a), geometry.area(b))


def covered(figure: BBox, tables: Sequence[TableData]) -> bool:
    return any(mostly_overlap(figure, t.bbox) for t in tables)


def find_figures(page: PageContent, lines: list[Line], body: float, artifacts: set[int],
                 ruled: Sequence[TableData], grids: Sequence[BBox],
                 held_boxes: Sequence[BBox] = ()) -> tuple[list[FigureCandidate], set[int]]:
    """The page's figures, each with why it is one and the lines it keeps as labels, and the
    ids of the rotated lines that became labels."""
    # A table's rules are drawings too; they mustn't become a picture of the table.
    found = [f for f in figure_boxes(page, lines, body)
             if f.labels == () or not covered(f.bbox, ruled)]
    # The fills of a month grid's days are its cells' backgrounds, not a picture.
    found = [f for f in found if not any(geometry.inside(f.bbox, box) for box in held_boxes)]
    # Pieces of one figure that share a caption beneath them are one figure (see figure_groups).
    captions = [l.bbox for l in lines if CAPTION_KEPT.match(l.text.strip())]
    unread = [f for f in found if f.unread is not None]   # each is its own crop
    found = [f for f in found if f.unread is None]
    figures: list[FigureCandidate] = []
    for group in figure_groups([f.bbox for f in found], captions, body):
        pieces = [next(f for f in found if f.bbox is g) for g in group]
        found = [f for f in found if not any(f is p for p in pieces)]
        if len(pieces) == 1:
            figures.append(pieces[0])
            continue
        box = (min(g[0] for g in group), min(g[1] for g in group),
               max(g[2] for g in group), max(g[3] for g in group))
        # Once one piece of a drawn figure keeps its text as labels, the text of the whole figure
        # does: no text is left in the flow beside a picture that holds it (a line across two
        # pieces too). Raster pieces keep only their own labels (the OCR read inside a picture):
        # live text over a photo, and text in the gutter between them, stay in the flow.
        if all(p.labels is None for p in pieces):
            labels = None
        elif all(p.source in ("object", "picture") for p in pieces):
            labels = tuple(dict.fromkeys(i for p in pieces for i in p.labels or ()))
        else:
            labels = line_indexes(page, chart_labels(box, lines))
        sources = {p.source for p in pieces}
        figures.append(FigureCandidate(
            box, sources.pop() if len(sources) == 1 else "drawing",
            f"{len(group)} pieces under one caption, {size_text(box)}", labels))
    figures += unread
    # The text of a vetoed grid lies in its chart. Whichever route took the chart, its labels
    # move into the figure (as a rescued chart's do), so no text is left only in a picture.
    figures = [replace(f, labels=line_indexes(page, chart_labels(f.bbox, lines)))
               if f.labels is None and any(mostly_overlap(f.bbox, g) for g in grids)
               else f for f in figures]
    # A chart's rotated axis titles are its labels as well. They join the figure's labels, in
    # reading order, and are kept out of the rotated text laid out beside the page.
    turned_lines = [l for l in page.lines if l.rotated and id(l) not in artifacts]
    turned: set[int] = set()
    for k, f in enumerate(figures):
        extra = rotated_labels(f.bbox, turned_lines, body)
        if extra:
            turned.update(id(l) for l in extra)
            merged = sorted([page.lines[i] for i in f.labels or ()] + extra,
                            key=lambda l: (l.bbox[1], l.bbox[0]))
            figures[k] = replace(f, labels=line_indexes(page, merged))
    return figures, turned


# An unread figure says only what reflow knows: writing in pen colour is handwriting; on a
# grey scan, or one whose colour cast tints print as much as ink, it may be print too badly
# scanned to read.
NOT_TRANSCRIBED = ", not transcribed."
UNREAD_KINDS = {True: ("handwriting", f"Handwriting{NOT_TRANSCRIBED}", "unread-handwriting"),
                False: ("unread-text", f"Writing that OCR could not read{NOT_TRANSCRIBED}",
                        "unread-text")}


def read_alt(alt: str) -> str:
    """An unread figure's alt once a model's reading of it passed its checks (analyze.assist):
    what it is, without saying it is not transcribed."""
    return alt.removesuffix(NOT_TRANSCRIBED) + "." if alt.endswith(NOT_TRANSCRIBED) else alt


def figure_node(fig: FigureCandidate, page: int) -> Node:
    """A figure's node: one that needs alt text, or an unread figure (fig.unread) whose alt is
    reflow's note and whose review error is its code (UNREAD_KINDS)."""
    why = [f"figure: {fig.why}"]
    node = Node(Role.FIGURE, page=page, bbox=fig.bbox, confidence=CONF_FIGURE_NO_ALT)
    if fig.unread is None:
        node.attrs = {"needs": "alt", "why": why}
        return node
    pen = fig.unread.pen_share is not None and fig.unread.pen_share >= PEN_UNREAD
    kind, alt, code = UNREAD_KINDS[pen]
    if fig.unread.kind == "signature":
        # Only a role every word of which OCR read is quoted; a doubtful one is in the flow after it.
        # A signature is not text to transcribe: "Signature" is its reading, and a model may add
        # the name (analyze.assist.signer).
        role = fig.unread.role
        kind, pen, code = "signature", True, "unread-signature"
        alt = f"Signature above “{role}”." if role else "Signature."
    make_unread(node, kind, alt, code, pen, fig.unread.ocr_guess, why)
    if fig.unread.kind == "signature" and fig.unread.role:
        node.attrs["signs"] = fig.unread.role
    return node


def make_unread(node: Node, kind: str, alt: str, code: str, pen: bool, ocr_guess: str,
                why: list[str]) -> None:
    """Make `node` an unread figure: its attrs, review code and fact. The box makes the crop a
    review target, so a page's grouped item draws every crop. The node keeps its id, page and
    box; any facts it had were about text no longer shown."""
    node.attrs = {"unread": True, "kind": kind, "alt": alt, "handwritten": pen,
                  "ocr_guess": ocr_guess, "why": why}
    node.confidence, node.source = CONF_FIGURE_NO_ALT, Source.HEURISTIC
    node.facts = []
    node.fact(code, Source.HEURISTIC, f"{alt} Check the original page.", box=list(node.bbox or ()))


def label_node(line: Line, page: int) -> Node:
    """A figure's label: the line's text and box, read from the page like any paragraph."""
    return Node(Role.P, page=page, bbox=line.bbox, text=line.text.replace(SOFT_HYPHEN, ""),
                attrs={"figure_label": True}, confidence=CONF_PARAGRAPH,
                source=Source.OCR if line.ocr else Source.HEURISTIC)
