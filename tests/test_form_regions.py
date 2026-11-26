"""Region routing for the form repair pass: which gaps in a page's text are worth reading
again, found from the page's rules and unread ink, never from fixed coordinates."""

from pathlib import Path

import checkout
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from reflow.analyze import form_regions as fr
from reflow.analyze import scan_rules as sr
from reflow.model import Document, Fact, Node, PageInfo, PageKind, Role, Source

S = sr.SCALE
REPO = Path(__file__).resolve().parents[1]


DENTAL = checkout.sample(REPO, "samples/azblue/site/dental-claim-form.pdf")


class Page:
    """A white US Letter page at the rules' resolution, drawn in points."""

    def __init__(self):
        self.image = Image.new("L", (round(612 * S), round(792 * S)), 255)
        self.draw = ImageDraw.Draw(self.image)

    def box(self, x0, y0, x1, y1):
        self.draw.rectangle([round(x0 * S), round(y0 * S), round(x1 * S), round(y1 * S)], fill=0)

    def hline(self, x0, x1, y):
        self.box(x0, y, x1, y + 0.6)

    def vline(self, x, y0, y1):
        self.box(x, y0, x + 0.6, y1)

    def word(self, x, y, letters=5, h=5.0):
        """Letter-sized pieces of ink in a row, as a printed word is."""
        for i in range(letters):
            self.box(x + i * 3.5, y, x + i * 3.5 + 2.4, y + h)
        return (x, y, x + letters * 3.5, y + h)

    def survey(self):
        return sr.survey(self.image, [], 1)


def ruled_row(page, top=320.0, bottom=345.0, walls=(20, 120, 220, 320, 420, 520, 590)):
    page.hline(walls[0], walls[-1], top)
    page.hline(walls[0], walls[-1], bottom)
    for x in walls:
        page.vline(x, top, bottom)


def test_unread_labels_in_a_ruled_row_route_the_whole_row():
    page = Page()
    ruled_row(page)
    for x in (30, 130, 230, 330):
        page.word(x, 326)
        page.word(x + 25, 326, 4)
        page.word(x, 334, 6)
    regions, skips = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [])
    assert skips == []
    [region] = regions
    assert region.task is fr.Task.FIELDS
    x0, y0, x1, y1 = region.bbox
    # From the left wall to the right wall and rule to rule, even past the last unread cell.
    assert x0 <= 20 and x1 >= 590 and y0 <= 320 and y1 >= 345 and y1 - y0 < 35
    assert region.columns == 6
    assert "ruled row" in region.reason


def test_text_the_layer_holds_is_not_a_gap():
    page = Page()
    ruled_row(page)
    texts = [fr.Text(page.word(x, 326), "Label") for x in (30, 130, 230, 330)]
    texts += [fr.Text(page.word(x, 334, 6), "Labels") for x in (30, 130, 230, 330)]
    assert fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), texts) == ([], [])


def test_a_speck_and_a_picture_are_not_text():
    page = Page()
    page.word(100, 100, 3)          # too few glyphs
    for y in (200, 208, 216):        # a logo's lettering, inside a picture
        page.word(300, y, 8)
    regions, _ = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [],
                               pictures=[(290, 195, 340, 225)])
    assert regions == []


def test_unruled_unread_lines_are_text_and_keep_the_read_words_they_cut():
    page = Page()
    for y in (500, 508):
        page.word(100, y, 6)
        page.word(125, y, 5)
    read = fr.Text((80, 498, 104, 505), "Note:")
    [region], _ = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [read])
    assert region.task is fr.Task.TEXT and region.columns == 0
    assert region.bbox[0] <= 80 and region.known == ("Note:",)


def test_a_region_taller_than_a_band_is_too_large_to_read():
    page = Page()
    for y in range(100, 500, 8):
        page.word(100, y, 8)
    regions, skips = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [])
    assert regions == [] and [s.reason for s in skips] == ["too-large"]


def test_regions_past_the_page_budget_are_skipped_largest_first():
    page = Page()
    for i in range(fr.MAX_PER_PAGE + 2):
        page.word(100, 40 + 80 * i, 6 + i)
        page.word(125 + 3.5 * i, 40 + 80 * i, 5)
    regions, skips = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [])
    assert len(regions) == fr.MAX_PER_PAGE
    assert [s.reason for s in skips] == ["budget", "budget"]
    assert min(r.unread for r in regions) > 11   # the two smallest gaps were left


def test_field_numbers_and_the_gaps_in_their_run():
    texts = [fr.Text((10, 10 * n, 30, 10 * n + 5), f"{n}. ") for n in (1, 2, 3, 4, 5, 9)]
    texts.append(fr.Text((40, 60, 70, 65), "9a. "))
    found = fr.field_ids(texts)
    assert [f for f, _ in found] == ["1", "2", "3", "4", "5", "9", "9a"]
    missing = fr.missing_fields(found)
    assert [f for f, _ in missing] == ["6", "7", "8"]
    assert missing[0][1][1] == 50 and missing[0][1][3] == 95   # between 5's box and 9's
    assert fr.missing_fields(found[:3]) == []    # too short a run to say anything


def test_born_digital_text_is_never_routed():
    doc = Document(Node(Role.DOC), [PageInfo(1, 612, 792), PageInfo(2, 612, 792)])
    def read(number):
        raise AssertionError("a born-digital page is never read")

    routing = fr.route(doc, read)
    assert routing.regions == ()
    assert [s.reason for s in routing.skipped] == ["native-text", "native-text"]


def test_origin_tells_this_runs_ocr_from_a_kept_layer():
    page = PageInfo(1, 612, 792, kind=PageKind.SCANNED)
    assert fr.origin(page, ocr_nodes=False) is None
    assert fr.origin(page, ocr_nodes=True) is fr.Origin.OCR_LAYER
    page.facts.append(Fact("ocr-page", Source.OCR, "text recognized by ocrmypdf"))
    assert fr.origin(page, ocr_nodes=True) is fr.Origin.OCR


@pytest.mark.skipif(not DENTAL.exists(), reason="the dental claim form sample is gitignored")
def test_dental_services_header_is_found_when_its_labels_go_unread():
    """The ADA claim form's services header (fields 24 to 31): with its words taken out of the
    text layer, as OCR leaves them on a scan, the row is found from the rules and the ink."""
    pdf = pdfium.PdfDocument(DENTAL)
    try:
        page = pdf[0]
        texts = fr.page_texts(page)
        image = page.render(scale=S, grayscale=True).to_pil().convert("L")
    finally:
        pdf.close()
    header = [t for t in texts if 327 < (t.bbox[1] + t.bbox[3]) / 2 < 348]
    assert any(t.text.startswith("29a.") for t in header)    # the native layer has the labels
    unread = [t for t in texts if t not in header]
    regions, _ = fr.route_page(1, (612, 792), fr.Origin.OCR, sr.survey(image, [], 1), unread)
    [region] = [r for r in regions if r.task is fr.Task.FIELDS and r.bbox[1] < 340 < r.bbox[3]]
    x0, y0, x1, y1 = region.bbox
    assert x0 <= 20 and x1 >= 595 and 318 <= y0 <= 330 and 345 <= y1 <= 355
    assert set(region.expected) >= {str(n) for n in range(24, 32)}
    assert region.inked == 10   # the ten labels: 24 to 31 with 29a and 29b
    # With the labels read, the same page routes nothing there.
    regions, _ = fr.route_page(1, (612, 792), fr.Origin.OCR, sr.survey(image, [], 1), texts)
    assert not any(r.bbox[1] < 340 < r.bbox[3] for r in regions)


def test_rows_a_rule_parts_are_two_regions():
    page = Page()
    walls = (20, 220, 420, 590)
    page.hline(20, 590, 300)
    page.hline(20, 590, 315)
    page.hline(20, 590, 330)
    for x in walls:
        page.vline(x, 300, 330)
    for y in (304, 319):
        for x in (30, 230):
            page.word(x, y, 6, h=7)
            page.word(x + 25, y, 5, h=7)
    regions, _ = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [])
    assert [(r.task, r.columns, r.inked) for r in regions] == [(fr.Task.FIELDS, 3, 2)] * 2
    assert regions[0].bbox[1] < 300 < regions[0].bbox[3] < regions[1].bbox[3] and regions[1].bbox[1] > 312


def _doc_with(nodes):
    info = PageInfo(1, 612, 792, kind=PageKind.SCANNED)
    info.facts.append(Fact("ocr-page", Source.OCR, "text recognized by ocrmypdf"))
    return Document(Node(Role.DOC, children=nodes), [info])


def _read(page):
    return lambda number: fr.PageRead((612, 792), [], page.image)


def test_an_unread_figure_is_not_a_picture_to_mask():
    page = Page()
    for y in (500, 508):
        page.word(100, y, 6)
        page.word(125, y, 5)
    box = (95.0, 495.0, 150.0, 515.0)
    unread = Node(Role.FIGURE, page=1, bbox=box, attrs={"unread": True})
    assert len(fr.route(_doc_with([unread]), _read(page)).regions) == 1
    picture = Node(Role.FIGURE, page=1, bbox=box)
    assert fr.route(_doc_with([picture]), _read(page)).regions == ()


def test_the_document_budget_goes_to_the_most_unread_ink():
    page = Page()
    page.word(100, 100, 8)             # 8 glyphs, higher on the page
    page.word(100, 400, 8)
    page.word(130, 400, 8)             # 16 glyphs, lower
    routing = fr.route(_doc_with([]), _read(page), max_regions=1)
    [region] = routing.regions
    assert region.unread == 16 and region.bbox[1] > 390
    assert [s.reason for s in routing.skipped] == ["budget"]


def _two_rows_and(outside):
    page = Page()
    for y in (300, 315, 330):
        page.hline(20, 420, y)
    for x in (20, 220, 420):
        page.vline(x, 300, 330)
    for y in (304, 319):
        page.word(385, y, 8, h=7)
    outside(page)
    regions, _ = fr.route_page(1, (612, 792), fr.Origin.OCR, page.survey(), [])
    return sorted(r.task for r in regions)


@pytest.mark.parametrize("outside", [
    lambda page: page.word(424, 311, 8, h=7),      # text just right of the table, level with both rows
    lambda page: page.word(424, 308, 8, h=13.5),   # taller: level with both rows' letters at once
])
def test_ink_beside_a_table_does_not_join_its_rows(outside):
    assert _two_rows_and(outside) == [fr.Task.FIELDS, fr.Task.FIELDS, fr.Task.TEXT]
