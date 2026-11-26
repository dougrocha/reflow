"""One scan of a page's pixels: the pictures, pen ink and rules that the text layer does not explain.

A scanned page reaches extraction as one raster (OCR with --force-ocr) or as image objects with
text over them. Its photos are pixels inside that raster, so they are found here, from the page
rendered once at 1 px/pt, and nowhere else: the figure finder reads `PageScan.pictures_for`, and
its unread figures `PageScan.pen`. A pure function of the working copy, run once per page in
extraction (Pillow only).

Born-digital pages, whose image objects cover less than SCANNED_MIN_COVERAGE of the page, are not
rendered: their scan is `skipped` and the figure finder keeps its object rule.
"""

from __future__ import annotations

import statistics
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageMath

from reflow import geometry
from reflow.ingest.ocr_trust import Grade, pen_share, pixel_box, stroke_weight
from reflow.model import BBox
from reflow.text_quality import ocr_noise_reason

if TYPE_CHECKING:
    import pypdfium2 as pdfium

    from reflow.extract.lines import Line
    from reflow.extract.ocrwords import GradedWord, PageWords

RegionKind = Literal["picture", "pen", "rule"]
ObjectKind = Literal["text", "empty", "plain"]
ScanStatus = Literal["skipped", "scanned", "noisy"]

# Ink: a pixel darker than this (grey, 0-255) that no text box or vector rule explains.
DARK = 180
# Tinted paper (a buff or salmon report cover, grey ~180-200) is scaled to white first, so that
# its tone is neither ink nor a photo's midtone. The paper is the page's most common grey.
TINTED_PAPER = 240
# An image object with less residual ink than this share of its pixels shows nothing: a blank
# strip of a sliced scan, or one holding only a rule.
EMPTY_INK = 0.005
# A text image: this many characters in this many lines of words (any line centre also makes one).
TEXT_CHARS = 150
TEXT_LINES = 3
TEXT_WORDS = 4
# Line boxes are padded by this share of the line height (at least 2 pt): big glyphs overshoot
# their character boxes. Vector rules are padded by RULING_PAD.
LINE_PAD = 0.25
RULING_PAD = 1.0

# Texture: a TILE pt square is textured when the grey levels of its unmasked pixels spread by
# at least TEXTURE_STD. A page with more than NOISY_SHARE of its tiles textured is grain, not
# pictures: its scan is "noisy" and finds no region.
TILE = 4
TEXTURE_STD = 6.0
NOISY_SHARE = 0.6
CLOSE_STEPS = 3  # first: fills a photo's smooth patches (sky, a wall) between its textured parts
OPEN_STEPS = 2   # then: removes what stays thin (pen strokes, specks, a text line OCR missed)
# A picture is a component of textured tiles at least this big, filling half of its box, whose
# pixels are mostly tone: its share of continuous-tone pixels plus its share of dense 24 pt tiles
# (a logo, a stamp; see _dense_tiles) is at least PHOTO_MIDTONE. Linework and handwriting leave
# most of their box paper.
PICTURE_MIN_SIDE = 24.0
PICTURE_MIN_AREA = 0.005   # of the page
PICTURE_MIN_FILL = 0.5
# Closing bridges gaps of up to 2 * CLOSE_STEPS tiles. A component is cut again where GAP_TILES
# tile rows in a row hold almost no textured tile (GAP_SHARE): a photo and the band
# under it (its caption bar) are two things, and the band's words stay out of the photo.
GAP_TILES = 3
GAP_SHARE = 0.05
MIDTONE = (32, 192)        # grey levels of continuous tone, between ink and paper
PHOTO_MIDTONE = 0.5
DENSE_INK = 0.2            # a 24 pt tile with this share of dark pixels is dense
# A cluster of thin ink holding this many OCR lines of plain words is printed linework with
# its labels (a map, a plan): a picture, not pen.
PRINTED_LINES = 3
# A cluster whose ink lies at least this much inside the boxes of words OCR left out of the text
# layer is writing, whatever printed lines it holds (a handwritten ledger under its column
# heads): pen, not a map. The permit ledgers reach 0.75, the printed maps 0.03 to 0.26.
WRITTEN_INK = 0.5
# A cluster with this share of dense 24 pt tiles is a solid mark (a flat logo, a stamp): a
# picture, not pen. Handwriting reaches 0.24 on the calibration pages (the permit ledger), the
# logos and stamps 0.28 to 0.44.
DENSE_MARK = 0.4

# A cluster of residual ink is a rule when RULE_ROW_SHARE of its ink lies in RULE_ROWS pixel rows,
# or it is RULE_ASPECT times longer than it is high (or high than long).
RULE_ROWS = 3
RULE_ROW_SHARE = 0.8
RULE_ASPECT = 12.0
RULE_SPAN = 0.5   # a pixel row crossing half an image object is a printed rule, not content
# Pen ink is at least this wide and high (pt): a cluster any thinner is a rule or nothing.
PEN_MIN = (24, 16)
# Pen clusters of one piece of writing are joined (_join_writing): the gaps between a signature's
# strokes, or an answer's words and symbols, cut it into several clusters. Two share a line when
# their rows overlap by LINE_SHARE of the shorter, and join when the gap between them is at most
# LINE_GAP of the taller one's height. On the calibration pages the gaps inside one piece of
# writing reach 1.0 (chem-math p1's first answer); the nearest pieces that are apart reach 1.1 (its
# "Mg(NO3)2" and the answer beside it). Two lines of working join when they touch (one 8 pt tile
# between them, TILE_PT) and start at the same margin, within STACK_MARGIN.
LINE_SHARE = 0.5
LINE_GAP = 1.0
STACK_MARGIN = 8
TILE_PT = 8   # the cluster grid (_tiles)
# A cluster with STRAIGHT_SHARE of its ink in horizontal or vertical runs of RULE_RUN pixels is
# linework, not pen: the grid of a scanned table (naca p2, 0.81 and 0.95), the ticks of a drawn
# timeline (Carmel p10, 0.92 and 1.0). Handwriting reaches 0.53 (legislation p1's "HB 40",
# written on its printed line).
RULE_RUN = 12
STRAIGHT_SHARE = 0.6
# A pen cluster holding SIGNED_RULINGS or more rulings of its own (pixel rows of ink straight for
# RULING_RUN pt: a scanned form's signature lines) is the writing of that many lines, joined by
# strokes that cross them: it is cut along them (_cut_at_rulings), and each ruling is a rule region.
# Legislation p1's signature lines run 75 to 198 pt (where print does not cover them); its nine
# signatures have no straight run over 60 pt. A cluster with a vertical run as long is a grid (a
# handwritten ledger, permit p3, whose rules a vertical rule joins), and so is one with a ruling
# that a vertical run of RULE_RUN meets at both ends (a printed form's cell, claim-form booklet
# p3; a stroke crosses at most one end of a signature line): its rows are not cut.
RULING_RUN = 60
SIGNED_RULINGS = 2
# Pen ink is often coloured: pen_share (shared with the OCR word grades, ingest.ocr_trust)
# measures the chromatic share of a box's dark pixels.

# The figure rule (pictures_for).
PAGE_SIZED = 0.9         # an object covering this share of the page is the page itself
MIN_OBJECT_FIGURE = 0.01  # a plain image object under this share of the page is a decoration
REGION_INSIDE = 0.5      # a region belongs to an object when this share of it lies inside
SNAP_IOU = 0.8           # a region this close to a plain photo object takes the object's box
SAME_IOU = 0.5           # two results this close are one figure ...
SAME_INSIDE = 0.8        # ... as is one lying this much inside the other
# A text object whose line boxes cover this share of it is a band of type (a title's white words
# on flat colour), not a smooth photo under a title: its ink beyond the lines is the band's ground.
TITLE_BAND_COVER = 0.5

# Stroke weight (stroke_ratios, with ocr_trust.stroke_weight): a line with fewer than
# STROKE_MIN_CHARS non-space characters is not measured.
STROKE_MIN_CHARS = 6


@dataclass(frozen=True)
class InkRegion:
    bbox: BBox                        # page points, top-left origin
    kind: RegionKind
    pen_share: float | None = None    # chromatic share of its dark pixels; None on a grey page
    lines: tuple[int, ...] = ()       # indexes of the lines centred inside (pictures)


@dataclass(frozen=True)
class ScannedObject:
    bbox: BBox                        # one image object of the page
    kind: ObjectKind                  # text: holds a line centre or passes the text-image test;
                                      # empty: no line, residual_share < EMPTY_INK;
                                      # plain: anything else (a photo, a logo)
    lines: tuple[int, ...] = ()       # indexes of the lines (live or OCR) centred inside
    residual_share: float = 0.0       # dark pixels left in the box after the line and rule
                                      # masks, over its pixels (0.0 when nothing was rendered)
    text_image: bool = False          # passes the text-image test (_text_heavy): an image of
                                      # text, whose words are read from it, not a photo with a
                                      # line laid over it
    text_cover: float = 0.0           # share of its pixels under the padded line and rule masks


@dataclass(frozen=True)
class PageScan:
    status: ScanStatus = "skipped"    # skipped: image coverage < 20%, nothing was rendered
    objects: tuple[ScannedObject, ...] = ()
    regions: tuple[InkRegion, ...] = ()   # always () when status is "noisy"
    # Each line's stroke weight over the page median (stroke_ratios), indexed like
    # PageContent.lines; () when nothing was rendered.
    strokes: tuple[float | None, ...] = ()

    @property
    def pen(self) -> tuple[InkRegion, ...]:
        return tuple(r for r in self.regions if r.kind == "pen")

    @property
    def text_images(self) -> tuple[BBox, ...]:
        """The images of text (ScannedObject.text_image): the text read inside them is OCR."""
        return tuple(o.bbox for o in self.objects if o.text_image)

    def pictures_for(self, page_area: float) -> list[BBox]:
        """The figure boxes of a scanned or noisy page: picture regions inside text and
        page-size objects, plain objects of at least MIN_OBJECT_FIGURE of the page, and (on a
        scanned page) a text object with ink beyond its text and no picture found in it.
        Overlapping results are one figure. Decided from the stored values alone."""
        pictures = [r for r in self.regions if r.kind == "picture"]
        plain = [o.bbox for o in self.objects if o.kind == "plain"
                 and MIN_OBJECT_FIGURE * page_area <= geometry.area(o.bbox) < PAGE_SIZED * page_area]
        out: list[BBox] = []
        for obj in self.objects:
            size = geometry.area(obj.bbox)
            if obj.kind == "text" or size >= PAGE_SIZED * page_area:
                found = [geometry.intersection(r.bbox, obj.bbox) for r in pictures
                         if geometry.share(r.bbox, obj.bbox) >= REGION_INSIDE]
                out += found
                # A smooth photo under a live title, which the texture test can miss. Never on a
                # noisy page: grain alone puts most text objects over the bound. Never on a band
                # of type that its lines cover (TITLE_BAND_COVER).
                if (not found and self.status == "scanned" and obj.kind == "text"
                        and size < PAGE_SIZED * page_area and obj.residual_share >= EMPTY_INK
                        and obj.text_cover < TITLE_BAND_COVER):
                    out.append(obj.bbox)
            elif obj.kind == "plain" and obj.bbox in plain:
                out.append(obj.bbox)
        snapped = [next((p for p in plain if geometry.iou(b, p) >= SNAP_IOU), b) for b in out]
        return _merge_same(snapped)


def scan_page(page: pdfium.PdfPage, lines: Sequence[Line], words: PageWords | None,
              images: Sequence[BBox], rulings: Sequence[BBox]) -> PageScan:
    """Render the page once at 1 px/pt and scan it (see scan_image); nothing is rendered when
    its image objects cover less than SCANNED_MIN_COVERAGE of it. The text masks its ink (_mask)."""
    width, height = page.get_size()
    if not _scanned(images, width, height):
        return PageScan()
    bitmap = page.render(scale=1)
    try:
        image = bitmap.to_pil()
    finally:
        bitmap.close()
    return scan_image(image, lines, words, images, rulings)


def scan_image(image: Image.Image, lines: Sequence[Line], words: PageWords | None,
               images: Sequence[BBox], rulings: Sequence[BBox]) -> PageScan:
    """Scan a page rendered at 1 px/pt: classify its image objects, find picture regions by
    texture, and cluster the remaining ink into pen and rule regions."""
    width, height = image.size
    if not _scanned(images, width, height):
        return PageScan()
    rgb = image.convert("RGB")
    grey = _on_white(rgb.convert("L"))
    strokes = stroke_ratios(grey, lines, visible_ocr(images, lines, width, height))
    mask = _mask(grey.size, lines, rulings, words)
    ink = _residual_ink(grey, mask)
    objects = _classify_objects(ink, mask, lines, images)
    grid, share = _texture(grey, mask)
    if share > NOISY_SHARE:
        return PageScan("noisy", objects, strokes=strokes)
    pictures = _pictures(grid, grey, mask, ink, lines)
    loose = ink.copy()
    for region in pictures:
        loose.paste(0, pixel_box(region.bbox, loose.size))
    clusters = _ink_clusters(loose)
    rules = [box for box in clusters if _is_rule(loose, box)]
    # Thin linework with printed labels (a map) is a picture, not pen, and so is a solid mark (a
    # flat logo, whose texture is only at its edges), measured with any picture in it left in.
    dropped = _dropped_lines(words)
    written = _omitted_ink(ink, words)
    printed = [box for box in clusters if box not in rules
               and ((_printed(box, lines, dropped) and _share(written, ink, box) < WRITTEN_INK)
                    or _dense_tiles(ink, box) >= DENSE_MARK)]
    pictures += [InkRegion(box, "picture", lines=tuple(i for i, line in enumerate(lines)
                                                         if geometry.centre_in(line.bbox, box)))
                 for box in printed]
    whole: list[BBox] = []
    cut: list[BBox] = []
    for box in clusters:
        if (box in rules or box in printed
                or box[2] - box[0] < PEN_MIN[0] or box[3] - box[1] < PEN_MIN[1]
                or _straight(loose, box) >= STRAIGHT_SHARE
                or any(geometry.share(box, p.bbox) >= REGION_INSIDE for p in pictures)):
            continue
        rulings_in, parts = _cut_at_rulings(loose, box)
        if not rulings_in:
            whole.append(box)
            continue
        rules += [r for r in rulings_in if r not in rules]
        cut += parts
    # Clusters whose boxes overlap cut the same ink: their parts are one region (_merge_same).
    pens = [InkRegion(box, "pen", pen_share(rgb, box)) for box in _join_writing(whole)]
    pens += [InkRegion(part, "pen", pen_share(rgb, part)) for part in _merge_same(cut)]
    return PageScan("scanned", objects,
                    tuple(pictures + [InkRegion(box, "rule") for box in rules] + pens), strokes)


def visible_ocr(images: Sequence[BBox], lines: Sequence[Line], width: float, height: float) -> bool:
    """Whether the page's text is a visible OCR layer: a page-sized raster (the scan evidence of
    ingest.classify, whatever the text's render mode) and no invisible line. A page with an
    invisible layer has its OCR there: its visible text is the file's own (a title the scan was
    stamped with, kept by redo OCR)."""
    return any(max(0, min(x1, width) - max(x0, 0)) * max(0, min(y1, height) - max(y0, 0))
               >= 0.9 * width * height for x0, y0, x1, y1 in images) and not any(l.ocr for l in lines)


def stroke_ratios(grey: Image.Image, lines: Sequence[Line],
                  visible: bool = False) -> tuple[float | None, ...]:
    """Each OCR line's stroke weight over the median of the page's measured OCR lines, in `lines`
    order; None for a line that is not measured (born-digital, rotated, under STROKE_MIN_CHARS
    characters, or no ink in its box). With `visible` (visible_ocr) every line is OCR. A line's
    stroke is ocr_trust.stroke_weight of its box, which separates bold from regular type at
    1 px/pt."""
    raw = [stroke_weight(grey, line.bbox)
           if ((line.ocr or visible) and not line.rotated
               and sum(not c.isspace() for c in line.text) >= STROKE_MIN_CHARS) else None
           for line in lines]
    measured = [w for w in raw if w is not None]
    if not measured:
        return tuple(raw)
    page = statistics.median(measured)
    return tuple(None if w is None or page <= 0 else w / page for w in raw)


def _on_white(grey: Image.Image) -> Image.Image:
    """The page with its paper made white: on tinted paper (its most common grey under
    TINTED_PAPER) every level is scaled by 255 / paper."""
    histogram = grey.histogram()
    paper = max(range(256), key=histogram.__getitem__)
    if paper >= TINTED_PAPER or paper == 0:
        return grey
    return grey.point(lambda v: min(255, round(v * 255 / paper)))


# -- the parts --------------------------------------------------------------------------------

def _scanned(images: Sequence[BBox], width: float, height: float) -> bool:
    """Whether image objects cover enough of the page to scan it (classify's scanned-page bound)."""
    from reflow.ingest.classify import SCANNED_MIN_COVERAGE, boxes_coverage   # ingest imports extract

    return boxes_coverage(images, width, height) >= SCANNED_MIN_COVERAGE


LEADERS = frozenset("_-.…")


def _reliable(text: str) -> bool:
    """Text that is mostly letters and digits explains its ink; OCR noise does not. A leader
    ("Length ______", a fill-in line) is print, and counts for neither."""
    chars = [c for c in text if not c.isspace() and c not in LEADERS]
    return sum(c.isalnum() for c in chars) >= 0.5 * len(chars)


def _mask(size: tuple[int, int], lines: Sequence[Line], rulings: Sequence[BBox],
          words: PageWords | None = None) -> Image.Image:
    """255 over the text and the vector rules, padded (see LINE_PAD). On a page whose OCR words
    bound (graded or partial), an OCR line they grade is masked by the boxes of the words
    the text layer kept, so script beside print on one line stays ink, and a word OCR left out
    (`words.omitted`) masks nothing: its ink is pen. A word of a kept OCR layer that no
    reading confirms, like any other line, live text or OCR left ungraded, masks its box
    only when its text is reliable."""
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    by_word = words is not None and words.status != "misaligned"
    boxes = [line.bbox for line in lines
             if _reliable(line.text) and not (by_word and line.ocr and line.grades is not None)]
    if by_word:
        boxes += [w.bbox for w in words.words if w.grade is not Grade.UNREAD
                  and (_reliable(w.text) or not w.unconfirmed)]
    for x0, top, x1, bottom in boxes:
        pad = max(2.0, LINE_PAD * (bottom - top))
        draw.rectangle((x0 - pad, top - pad, x1 + pad, bottom + pad), fill=255)
    for x0, top, x1, bottom in rulings:
        draw.rectangle((x0 - RULING_PAD, top - RULING_PAD, x1 + RULING_PAD, bottom + RULING_PAD),
                       fill=255)
    return mask


def _residual_ink(grey: Image.Image, mask: Image.Image) -> Image.Image:
    """255 on the dark pixels outside the masked text and rules."""
    return ImageChops.subtract(grey.point(lambda v: 255 if v < DARK else 0), mask)


def _text_heavy(box: BBox, lines: Sequence[Line]) -> bool:
    """An image holding at least TEXT_CHARS characters in TEXT_LINES lines of TEXT_WORDS words."""
    inside = [line for line in lines
              if box[0] - 6 <= line.bbox[0] and line.bbox[2] <= box[2] + 6
              and box[1] - 6 <= line.bbox[1] and line.bbox[3] <= box[3] + 6]
    return (sum(len(line.text) for line in inside) >= TEXT_CHARS
            and sum(len(line.text.split()) >= TEXT_WORDS for line in inside) >= TEXT_LINES)


def _classify_objects(ink: Image.Image, mask: Image.Image, lines: Sequence[Line],
                      images: Sequence[BBox]) -> tuple[ScannedObject, ...]:
    """Each image object: text (it holds a line's centre, or is a text image), empty (no line
    and almost no ink left after the masks) or plain (a photo, a logo)."""
    objects = []
    for box in images:
        crop = pixel_box(box, ink.size)
        pixels = (crop[2] - crop[0]) * (crop[3] - crop[1])
        residual = _unruled(ink.crop(crop)) / pixels if pixels else 0.0
        cover = mask.crop(crop).histogram()[255] / pixels if pixels else 0.0
        held = tuple(i for i, line in enumerate(lines) if geometry.centre_in(line.bbox, box))
        heavy = _text_heavy(box, lines)
        if held or heavy:
            kind: ObjectKind = "text"
        elif residual < EMPTY_INK:
            kind = "empty"
        else:
            kind = "plain"
        objects.append(ScannedObject(box, kind, held, round(residual, 6), heavy, round(cover, 6)))
    return tuple(objects)


def _unruled(ink: Image.Image) -> int:
    """The ink pixels of an object's crop, less its printed rules: runs of at most RULE_ROWS
    pixel rows (or columns) that ink crosses for at least RULE_SPAN of the crop. A strip holding
    a rule and a folio shows nothing; a dark photo keeps its ink."""
    total = ink.histogram()[255]
    if not total:
        return 0
    data = ink.convert("F")
    ruled = 0.0
    for size, length in (((1, ink.height), ink.width), ((ink.width, 1), ink.height)):
        means = array("f", data.resize(size, Image.Resampling.BOX).tobytes())
        run: list[float] = []
        for mean in [*means, 0.0]:
            if mean >= RULE_SPAN * 255:
                run.append(mean / 255 * length)
            else:
                if len(run) <= RULE_ROWS:
                    ruled += sum(run)
                run = []
    return max(0, total - round(ruled))


def _texture(grey: Image.Image, mask: Image.Image) -> tuple[list[list[bool]], float]:
    """The textured TILE pt tiles (rows of columns), and their share of all tiles. Only the
    unmasked pixels of a tile count, and a tile that is mostly masked is not textured."""
    g = grey.convert("F")
    u = mask.point(lambda v: 0 if v else 1).convert("F")
    gu = ImageMath.lambda_eval(lambda a: a["g"] * a["u"], g=g, u=u)
    g2u = ImageMath.lambda_eval(lambda a: a["g"] * a["g"] * a["u"], g=g, u=u)
    means = [array("f", im.reduce(TILE).tobytes()) for im in (u, gu, g2u)]
    columns, rows = -(-grey.width // TILE), -(-grey.height // TILE)
    floor = TEXTURE_STD * TEXTURE_STD
    grid = []
    textured = 0
    for y in range(rows):
        row = []
        for x in range(columns):
            i = y * columns + x
            share, s1, s2 = means[0][i], means[1][i], means[2][i]
            hit = share >= 0.5 and s2 / share - (s1 / share) ** 2 >= floor
            textured += hit
            row.append(hit)
        grid.append(row)
    return grid, textured / (rows * columns) if rows and columns else 0.0


def _grid_image(grid: list[list[bool]]) -> Image.Image:
    rows, columns = len(grid), len(grid[0]) if grid else 0
    return Image.frombytes("L", (columns, rows),
                           bytes(255 if hit else 0 for row in grid for hit in row))


def _solid(tiles: Image.Image) -> Image.Image:
    """Close the textured tiles (CLOSE_STEPS), then open them (OPEN_STEPS): a photo's sparse
    texture becomes one solid area, and strokes and specks that stay thin are removed."""
    for _ in range(CLOSE_STEPS):
        tiles = tiles.filter(ImageFilter.MaxFilter(3))
    for _ in range(CLOSE_STEPS + OPEN_STEPS):
        tiles = tiles.filter(ImageFilter.MinFilter(3))
    for _ in range(OPEN_STEPS):
        tiles = tiles.filter(ImageFilter.MaxFilter(3))
    return tiles


def _fill_holes(tiles: Image.Image) -> Image.Image:
    """Light the unlit tiles that the lit ones enclose: a photo's smooth middle (a wall, a
    sign board) inside its textured edges."""
    columns, rows = tiles.size
    data = bytearray(tiles.tobytes())
    outside = [(x, y) for x in range(columns) for y in (0, rows - 1)]
    outside += [(x, y) for y in range(rows) for x in (0, columns - 1)]
    seen = bytearray(len(data))
    todo = [(x, y) for x, y in outside if not data[y * columns + x]]
    for x, y in todo:
        seen[y * columns + x] = 1
    while todo:
        x, y = todo.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < columns and 0 <= ny < rows:
                i = ny * columns + nx
                if not seen[i] and not data[i]:
                    seen[i] = 1
                    todo.append((nx, ny))
    filled = bytes(255 if data[i] or not seen[i] else 0 for i in range(len(data)))
    return Image.frombytes("L", (columns, rows), filled)


def _components(tiles: Image.Image) -> list[list[tuple[int, int]]]:
    """The 8-connected components of the lit tiles."""
    columns = tiles.width
    data = tiles.tobytes()
    lit = {(i % columns, i // columns) for i, v in enumerate(data) if v}
    components = []
    while lit:
        first = lit.pop()
        todo, comp = [first], [first]
        while todo:
            x, y = todo.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in lit:
                        lit.remove(n)
                        comp.append(n)
                        todo.append(n)
        components.append(comp)
    return components


def _split_at_gaps(comp: list[tuple[int, int]], grid: list[list[bool]]) -> list[list[tuple[int, int]]]:
    """The component cut at its blank bands: runs of GAP_TILES tile rows across its box in which
    at most GAP_SHARE of the tiles were textured before closing joined what lies above and below.
    Only rows: a row of photos or of drawn circles stays one picture."""
    top, bottom = min(y for _, y in comp), max(y for _, y in comp)
    left, right = min(x for x, _ in comp), max(x for x, _ in comp)
    width = right - left + 1
    cuts, run = [], []
    for y in range(top, bottom + 1):
        if sum(grid[y][left:right + 1]) <= GAP_SHARE * width:
            run.append(y)
            continue
        if len(run) >= GAP_TILES and run[0] > top:
            cuts.append((run[0], run[-1]))
        run = []
    edges = [top - 1] + [y for band in cuts for y in band] + [bottom + 1]
    parts = [[t for t in comp if start < t[1] < end] for start, end in zip(edges[0::2], edges[1::2])]
    return [part for part in parts if part]


def _pictures(grid: list[list[bool]], grey: Image.Image, mask: Image.Image, ink: Image.Image,
              lines: Sequence[Line]) -> list[InkRegion]:
    """Components of textured tiles that are pictures: big, solid, continuous tone or dense."""
    if not grid or not grid[0]:
        return []
    tiles = _fill_holes(_solid(_grid_image(grid)))
    page_area = grey.width * grey.height
    unmasked = ImageChops.invert(mask)
    found = []
    for comp in (part for whole in _components(tiles) for part in _split_at_gaps(whole, grid)):
        xs, ys = [x for x, _ in comp], [y for _, y in comp]
        box: BBox = (min(xs) * TILE, min(ys) * TILE,
                     min(grey.width, (max(xs) + 1) * TILE), min(grey.height, (max(ys) + 1) * TILE))
        w, h = box[2] - box[0], box[3] - box[1]
        area = len(comp) * TILE * TILE
        if w < PICTURE_MIN_SIDE or h < PICTURE_MIN_SIDE or area < PICTURE_MIN_AREA * page_area:
            continue
        if area < PICTURE_MIN_FILL * w * h:
            continue
        if _midtone_share(grey, unmasked, box) + _dense_tiles(ink, box) < PHOTO_MIDTONE:
            continue
        held = tuple(i for i, line in enumerate(lines) if geometry.centre_in(line.bbox, box))
        found.append(InkRegion(box, "picture", lines=held))
    return sorted(found, key=lambda r: (r.bbox[1], r.bbox[0]))


def _midtone_share(grey: Image.Image, unmasked: Image.Image, box: BBox) -> float:
    """The share of the box's unmasked pixels that are continuous tone: neither paper nor ink.
    Linework and handwriting leave most of their box paper; a photo fills it."""
    crop = pixel_box(box, grey.size)
    histogram = grey.crop(crop).histogram(mask=unmasked.crop(crop))
    total = sum(histogram)
    return sum(histogram[MIDTONE[0]:MIDTONE[1] + 1]) / total if total else 0.0


def _dense_tiles(ink: Image.Image, box: BBox) -> float:
    """The share of the box's 24 pt tiles that are dense ink (DENSE_INK of their pixels): all of
    a logo or a stamp, a few tiles of handwriting."""
    x0, top, x1, bottom = pixel_box(box, ink.size)
    dense = count = 0
    for y in range(top, bottom, 24):
        for x in range(x0, x1, 24):
            tile = ink.crop((x, y, min(x + 24, x1), min(y + 24, bottom)))
            count += 1
            dense += tile.histogram()[255] >= 24 * 24 * DENSE_INK
    return dense / count if count else 0.0


def _dropped_lines(words: PageWords | None) -> list[tuple[str, BBox]]:
    """The runs of OCR words left out of the text layer whole, as (text, box): a line of
    them, split where the gap between two words is wider than they are tall, as the text layer's
    lines split. They are no Line, but they are still OCR's reading of the ink."""
    if words is None:
        return []
    by_line: dict[int, list[GradedWord]] = {}
    for w in words.words:
        by_line.setdefault(w.ocr_line, []).append(w)
    runs: list[list[GradedWord]] = []
    for ws in by_line.values():
        if any(w.layer_text for w in ws):
            continue
        ws = sorted(ws, key=lambda w: w.bbox[0])
        runs.append([ws[0]])
        for prev, w in zip(ws, ws[1:]):
            tall = max(prev.bbox[3] - prev.bbox[1], w.bbox[3] - w.bbox[1])
            if w.bbox[0] - prev.bbox[2] > tall:
                runs.append([])
            runs[-1].append(w)
    return [(" ".join(w.text for w in run),
             (min(w.bbox[0] for w in run), min(w.bbox[1] for w in run),
              max(w.bbox[2] for w in run), max(w.bbox[3] for w in run))) for run in runs]


def _omitted_ink(ink: Image.Image, words: PageWords | None) -> Image.Image:
    """The ink inside the boxes of the words OCR left out of the text layer."""
    boxes = Image.new("L", ink.size, 0)
    draw = ImageDraw.Draw(boxes)
    for w in words.omitted if words is not None else ():
        draw.rectangle(w.bbox, fill=255)
    return ImageChops.multiply(ink, boxes)


def _share(part: Image.Image, whole: Image.Image, box: BBox) -> float:
    """The share of the box's lit pixels in `whole` that are lit in `part`."""
    crop = pixel_box(box, whole.size)
    total = whole.crop(crop).histogram()[255]
    return part.crop(crop).histogram()[255] / total if total else 0.0


def _printed(box: BBox, lines: Sequence[Line], dropped: Sequence[tuple[str, BBox]] = ()) -> bool:
    """Whether a cluster of thin ink holds PRINTED_LINES OCR lines of plain words: the labels of
    a printed map or plan. OCR of handwriting gives broken words. The lines OCR left out of the
    text layer count too (`dropped`)."""
    words = 0
    read = [(line.text, line.unicode_errors, line.bbox) for line in lines if line.ocr]
    for text, errors, bbox in [*read, *((t, 0, b) for t, b in dropped)]:
        if geometry.centre_in(bbox, box) and ocr_noise_reason(text, errors) is None:
            tokens = text.replace("“", "").replace("”", "").replace('"', "").split()
            words += bool(tokens) and all(sum(c.isalpha() for c in t) >= 2 for t in tokens)
    return words >= PRINTED_LINES


def _tiles(ink: Image.Image) -> dict[tuple[int, int], int]:
    """The 8 pt tiles (column, row) holding at least 5 ink pixels, with their counts; the
    page's edge tiles are left out."""
    counts = array("f", ink.convert("F").reduce(8).tobytes())
    columns = -(-ink.width // 8)
    tiles = {}
    for y in range(1, (ink.height - 16) // 8 + (1 if (ink.height - 16) % 8 else 0)):
        for x in range(1, (ink.width - 16) // 8 + (1 if (ink.width - 16) % 8 else 0)):
            count = round(counts[y * columns + x] * 64 / 255)
            if count >= 5:
                tiles[x, y] = count
    return tiles


def _ink_clusters(ink: Image.Image) -> list[BBox]:
    """Substantial clusters of thin ink: an 8 pt grid connects pen strokes without treating
    individual scan specks as content. Rules and pen are told apart afterwards (see _is_rule)."""
    tiles = _tiles(ink)
    regions = []
    while tiles:
        first, count = tiles.popitem()
        pending, group, total = [first], [first], count
        while pending:
            x, y = pending.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (x + dx, y + dy)
                    if neighbour in tiles:
                        total += tiles.pop(neighbour)
                        group.append(neighbour)
                        pending.append(neighbour)
        x0 = min(x for x, _ in group) * 8
        top = min(y for _, y in group) * 8
        x1 = (max(x for x, _ in group) + 1) * 8
        bottom = (max(y for _, y in group) + 1) * 8
        width, height = x1 - x0, bottom - top
        density = total / (width * height)
        if total >= 160 and max(width, height) >= 24 and 0.015 <= density <= 0.4:
            regions.append((float(x0), float(top), float(x1), float(bottom)))
    return sorted(regions)


def _rulings(ink: Image.Image, box: BBox) -> tuple[list[BBox], Image.Image]:
    """The rulings inside a cluster (page boxes, top to bottom), and their ink (a crop of the
    box): pixel rows of ink straight for RULING_RUN pt, a row's pieces less than 4 pt apart one
    ruling, and touching rows one ruling."""
    crop = ink.crop(pixel_box(box, ink.size))
    long = _runs(crop, 1, 0, RULING_RUN)
    found: list[list[int]] = []
    for y in range(long.height):
        row = long.crop((0, y, long.width, y + 1)).tobytes()
        xs = [x for x, v in enumerate(row) if v]
        pieces: list[list[int]] = []
        for x in xs:
            if pieces and x - pieces[-1][1] <= 4:
                pieces[-1][1] = x + 1
            else:
                pieces.append([x, x + 1])
        for x0, x1 in pieces:
            prev = next((r for r in found if r[3] == y and r[0] < x1 and x0 < r[2]), None)
            if prev is None:
                found.append([x0, y, x1, y + 1])
            else:
                prev[:] = [min(prev[0], x0), prev[1], max(prev[2], x1), y + 1]
    left, top = pixel_box(box, ink.size)[:2]
    return ([(float(left + x0), float(top + y0), float(left + x1), float(top + y1))
             for x0, y0, x1, y1 in sorted(found, key=lambda r: (r[1], r[0]))], long)


def _closed(ink: Image.Image, ruling: BBox) -> bool:
    """Whether a vertical run of RULE_RUN meets the ruling at both ends: a cell's border."""
    x0, top, x1, bottom = (round(v) for v in ruling)
    return all(_runs(ink.crop((x - 4, top - RULE_RUN - 2, x + 4, bottom + RULE_RUN + 2)), 0, 1)
               .getbbox() is not None for x in (x0, x1))


def _cut_at_rulings(ink: Image.Image, box: BBox) -> tuple[list[BBox], list[BBox]]:
    """A pen cluster cut along its rulings: the rulings, and the parts. A cluster with
    fewer than SIGNED_RULINGS rulings is one part, and has none (a signature on its own line
    keeps the strokes that cross it), and so is a grid (see RULING_RUN). Else each 8 pt tile of
    its ink off the rulings goes to the nearest ruling at or below it in its column (rulings
    overlapping across, the one nearest the tile), or under the column's last ruling: one part
    per ruling and per column's foot. No ink of the cluster is lost: a part is a piece of it,
    never a speck, and a cut that would leave a part too small for pen (PEN_MIN) is none: its
    rulings are the underlines and strikes of a line of print (legislation p34's "[remaining]")."""
    rulings, long = _rulings(ink, box)
    if (len(rulings) < SIGNED_RULINGS
            or _runs(ink.crop(pixel_box(box, ink.size)), 0, 1, RULING_RUN).getbbox() is not None
            or any(_closed(ink, r) for r in rulings)):
        return [], [box]
    columns: list[list[BBox]] = []
    for r in sorted(rulings):
        if columns and r[0] < max(c[2] for c in columns[-1]):
            columns[-1].append(r)
        else:
            columns.append([r])
    rest = Image.new("L", ink.size, 0)
    crop = pixel_box(box, ink.size)
    rest.paste(ImageChops.subtract(ink.crop(crop), long), crop[:2])
    parts: dict[tuple[int, BBox | None], list[tuple[int, int]]] = {}
    for tx, ty in _tiles(rest):
        x, y = tx * 8 + 4, ty * 8 + 4
        k = min(range(len(columns)), key=lambda i: max(
            0, min(r[0] for r in columns[i]) - x, x - max(r[2] for r in columns[i])))
        below = [r for r in columns[k] if y <= r[3]]
        under = min(below, key=lambda r: r[1]) if below else None
        parts.setdefault((k, under), []).append((tx, ty))
    cut = sorted((float(min(t[0] for t in tiles) * 8), float(min(t[1] for t in tiles) * 8),
                  float((max(t[0] for t in tiles) + 1) * 8), float((max(t[1] for t in tiles) + 1) * 8))
                 for tiles in parts.values())
    if any(b[2] - b[0] < PEN_MIN[0] or b[3] - b[1] < PEN_MIN[1] for b in cut):
        return [], [box]   # rulings closer than a line of writing: a struck or underlined print
    return rulings, cut


def _is_rule(ink: Image.Image, box: BBox) -> bool:
    """A cluster that is a printed rule: long and thin, or nearly all its ink in a few rows."""
    width, height = box[2] - box[0], box[3] - box[1]
    if max(width / height, height / width) >= RULE_ASPECT:
        return True
    crop = ink.crop(pixel_box(box, ink.size)).convert("F")
    rows = array("f", crop.resize((1, crop.height), Image.Resampling.BOX).tobytes())
    ordered = sorted(rows, reverse=True)
    total = sum(ordered)
    return bool(total) and sum(ordered[:RULE_ROWS]) >= RULE_ROW_SHARE * total


def _straight(ink: Image.Image, box: BBox) -> float:
    """The share of the box's ink in horizontal or vertical runs of at least RULE_RUN pixels."""
    crop = ink.crop(pixel_box(box, ink.size))
    total = crop.histogram()[255]
    if not total:
        return 0.0
    runs = ImageChops.lighter(_runs(crop, 1, 0), _runs(crop, 0, 1))
    return runs.histogram()[255] / total


def _runs(ink: Image.Image, dx: int, dy: int, run: int = RULE_RUN) -> Image.Image:
    """The ink in runs of at least `run` pixels along (dx, dy): an erosion by the run, then a
    dilation back. The margin keeps ImageChops.offset from wrapping ink round the edges."""
    pad = Image.new("L", (ink.width + 2 * run, ink.height + 2 * run), 0)
    pad.paste(ink, (run, run))
    eroded = pad
    for k in range(1, run):
        eroded = ImageChops.darker(eroded, ImageChops.offset(pad, -k * dx, -k * dy))
    runs = eroded
    for k in range(1, run):
        runs = ImageChops.lighter(runs, ImageChops.offset(eroded, k * dx, k * dy))
    return runs.crop((run, run, run + ink.width, run + ink.height))


def _merge_same(boxes: list[BBox]) -> list[BBox]:
    """One box for results that overlap by SAME_IOU or lie SAME_INSIDE one inside the other."""
    boxes = list(boxes)
    merged = True
    while merged:
        merged = False
        for i, a in enumerate(boxes):
            j = next((j for j in range(i + 1, len(boxes))
                      if geometry.iou(a, boxes[j]) >= SAME_IOU
                      or geometry.share(a, boxes[j]) >= SAME_INSIDE
                      or geometry.share(boxes[j], a) >= SAME_INSIDE), None)
            if j is not None:
                b = boxes.pop(j)
                boxes[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                merged = True
                break
    return boxes


def _join_writing(boxes: list[BBox]) -> list[BBox]:
    """One box for the pen clusters of one piece of writing: first the clusters of each line, then
    the lines that touch (LINE_SHARE, LINE_GAP, STACK_MARGIN). The lines are joined after all the
    clusters are, so a tall piece of working does not widen the gap that joins what stands beside it."""
    return _join(_join(sorted(boxes), _on_one_line), _stacked)


def _join(boxes: list[BBox], same: Callable[[BBox, BBox], bool]) -> list[BBox]:
    joined = True
    while joined:
        joined = False
        for i, a in enumerate(boxes):
            j = next((j for j in range(i + 1, len(boxes)) if same(a, boxes[j])), None)
            if j is not None:
                b = boxes.pop(j)
                boxes[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                joined = True
                break
    return boxes


def _on_one_line(a: BBox, b: BBox) -> bool:
    rows = min(a[3], b[3]) - max(a[1], b[1])
    gap = max(a[0], b[0]) - min(a[2], b[2])
    return (rows >= LINE_SHARE * min(a[3] - a[1], b[3] - b[1])
            and gap <= LINE_GAP * max(a[3] - a[1], b[3] - b[1]))


def _stacked(a: BBox, b: BBox) -> bool:
    return (min(a[2], b[2]) > max(a[0], b[0]) and max(a[1], b[1]) - min(a[3], b[3]) <= TILE_PT
            and abs(a[0] - b[0]) <= STACK_MARGIN)
