"""Unread figures from the page scan of a real scanned page: what makes one, and what does
not."""

from contextlib import closing
from types import SimpleNamespace

import pikepdf
import pypdfium2 as pdfium
import pytest
from pikepdf import Name
from PIL import Image, ImageDraw

from conftest import make_pdf
from reflow.analyze.figures import FigureCandidate, _evidence, figure_node
from reflow.analyze.layout import analyze
from reflow.analyze.review import queue
from reflow.extract.lines import Line
from reflow.extract.pages import extract_pages
from reflow.extract.ocrwords import GradedWord, PageWords
from reflow.extract.scan import scan_page
from reflow.ingest.pages import ingest
from reflow.ingest.ocr_trust import Grade
from reflow.model import Node, Role


def _scanned(tmp_path, draw_page):
    """A one-page scan (a page-size raster under the line "Hello accessible world") whose
    pixels `draw_page(ImageDraw)` draws, and its ingested document."""
    path = make_pdf(tmp_path / "signed.pdf", ["scan+text"], lang="en")
    image = Image.new("RGB", (612, 792), "white")
    draw_page(ImageDraw.Draw(image))
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        raster = pdf.pages[0].Resources.XObject.Im0
        raster.Width, raster.Height = image.size
        raster.ColorSpace = Name.DeviceRGB
        raster.write(image.tobytes())
        pdf.save(path)
    return path, ingest(path)


def _contents(path, extra=(), words=None):
    """The extracted pages with `extra` lines added to page 1's text layer, and `words` its OCR
    grades, which is then scanned again, as extraction would have scanned it with those lines."""
    contents = list(extract_pages(path))
    page = contents[0]
    page.lines.extend(extra)
    page.words = words
    with pdfium.PdfDocument(str(path)) as pdf, closing(pdf[0]) as pdf_page:
        page.scan = scan_page(pdf_page, page.lines, words, page.images,
                              [d.bbox for d in page.drawings if d.only_rules])
    return contents


def _signature(draw):
    draw.line([(300, 230), (310, 200), (315, 235), (325, 215), (330, 230),
               (345, 200), (350, 230), (365, 215), (370, 230), (390, 210)],
              fill="black", width=2)
    # Scanner border and a printed rule must not count as handwriting.
    draw.line((0, 791, 611, 791), fill="black", width=2)
    draw.line((100, 400, 500, 400), fill="black", width=1)


def _unread(doc, contents):
    """The unread figures of page 1 laid out from `contents`, and the scored review items."""
    doc.root = Node(Role.DOC, children=analyze(contents))
    items = queue(doc)
    return [n for n in doc.root.walk() if n.attrs.get("unread")], items


@pytest.mark.parametrize("recognized", ["none", "noise"])
def test_signature_ink_outside_reliable_ocr_is_an_unread_figure(tmp_path, recognized):
    path, doc = _scanned(tmp_path, _signature)
    extra = []
    if recognized != "none":
        extra = [Line(1, (296, 196, 400, 240), "|/?!!", 10, False, False, "OCR", ocr=True)]
    figures, items = _unread(doc, _contents(path, extra))
    [figure] = figures
    assert figure.attrs["kind"] == "unread-text" and figure.attrs["handwritten"] is False
    assert [(i["page"], i["role"], i["label"]) for i in items if i["id"] == figure.id] == [
        (1, "figure", "Writing OCR could not read")]


def test_signature_ink_read_as_words_is_still_a_region(tmp_path):
    # OCR reads the signature as "Sara Smith": "Sara" is doubtful and stays in the text layer,
    # "Smith" is tall script it leaves out. Only the kept word masks its ink.
    path, doc = _scanned(tmp_path, _signature)
    sara = GradedWord((296, 196, 330, 240), "Sara", "Sara", 66, Grade.DOUBT, None, 1.0, line=1)
    smith = GradedWord((332, 196, 400, 240), "Smith", "", 40, Grade.UNREAD, None, 2.0)
    line = Line(1, sara.bbox, "Sara", 10, False, False, "OCR", ocr=True, grades=(sara,))
    figures, _ = _unread(doc, _contents(path, [line], PageWords("graded", (sara, smith), 20.0)))
    [figure] = figures
    assert figure.attrs["kind"] == "unread-text" and figure.attrs["ocr_guess"] == "Smith"
    assert figure.bbox[2] > smith.bbox[0]


def _header_rule(draw):
    draw.line((56, 104, 552, 104), fill="black", width=1)   # 496 of 612 pt: 81% of the width


def _logo(draw):
    draw.rectangle((40, 40, 160, 110), fill="black")         # a dense block with a white mark
    draw.ellipse((70, 50, 130, 100), fill="white")
    draw.ellipse((85, 62, 115, 88), fill=(30, 60, 140))


def _map(draw):
    for x in range(100, 501, 40):                            # thin streets
        draw.line((x, 300, x + 30, 600), fill="black", width=1)
    for y in range(300, 601, 50):
        draw.line((100, y, 530, y + 10), fill="black", width=1)


MAP_LABELS = [Line(1, (150, 330, 230, 342), "CROSBY STREET", 9, False, False, "OCR", ocr=True),
              Line(1, (300, 420, 380, 432), "Gletzen Road", 9, False, False, "OCR", ocr=True),
              Line(1, (200, 520, 290, 532), "Cemetery Lane", 9, False, False, "OCR", ocr=True)]


@pytest.mark.parametrize("draw_page, extra", [(_header_rule, []), (_logo, []), (_map, MAP_LABELS)],
                         ids=["81%-wide rule", "dense logo", "printed map"])
def test_rules_logos_and_printed_maps_are_no_unread_figure(tmp_path, draw_page, extra):
    path, doc = _scanned(tmp_path, draw_page)
    contents = _contents(path, extra)
    assert contents[0].scan.status == "scanned"
    assert _unread(doc, contents)[0] == []


@pytest.mark.parametrize("type_pen, kind", [(0.0, "handwriting"), (0.95, "unread-text")])
def test_pen_colour_is_a_sign_of_writing_only_where_the_type_is_not_tinted(type_pen, kind):
    # A stacked fraction OCR could not read, as inky as the page's print (naca p2, on tinted paper).
    box = (256.0, 552.0, 312.0, 640.0)
    typed = [GradedWord((60.0, 60 + 20 * i, 200.0, 72 + 20 * i), "Aerodynamic", "Aerodynamic", 95,
                        Grade.READ, type_pen, 1.0) for i in range(5)]
    page = SimpleNamespace(words=PageWords("graded", tuple(typed), 12.0))
    figure = FigureCandidate(box, "pen", "unread", (), _evidence(page, box, [0.98]))
    assert figure_node(figure, 2).attrs["kind"] == kind
