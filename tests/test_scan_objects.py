"""Text layers over raster images must remain readable in the transcript, beside the
pictures the page scan finds in them."""

import random

import pikepdf
import pytest
from PIL import Image, ImageDraw
from pikepdf import Dictionary, Name

from conftest import make_text_pdf
from reflow.analyze.layout import analyze
from reflow.analyze.review import queue
from reflow.extract.pages import extract_pages
from reflow.extract.scan import InkRegion, PageScan, ScannedObject
from reflow.model import Document, Node, PageInfo, Role, Source


@pytest.mark.parametrize("picture", [False, True])
@pytest.mark.parametrize("full_page", [False, True])
def test_text_image_emits_reviewable_text_and_keeps_only_pictures(tmp_path, picture, full_page):
    lines = ["This is the first paragraph of the annual report to the district.",
             "The accounts show the income and expenses for the fiscal year.",
             "The board approved the budget after reviewing these figures."]
    path = make_text_pdf(tmp_path / "scan.pdf", [
        [(72, 120 + i * 18, 10, text) for i, text in enumerate(lines)]])
    image = Image.new("L", (612, 792), 255)
    if picture:   # a photograph: continuous tone with texture, which the page scan finds
        rng = random.Random(0)
        image.paste(Image.frombytes("L", (170, 150), bytes(rng.randrange(40, 200)
                                                            for _ in range(170 * 150))), (80, 300))
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        raster = pdf.make_stream(image.tobytes())
        raster.update(Dictionary(Type=Name.XObject, Subtype=Name.Image, Width=612,
                                 Height=792, ColorSpace=Name.DeviceGray, BitsPerComponent=8))
        page.Resources.XObject = Dictionary(Im0=raster)
        transform = "612 0 0 792 0 0" if full_page else "570 0 0 720 20 36"
        page.Contents = pdf.make_stream(
            f"q {transform} cm /Im0 Do Q\n".encode() + page.Contents.read_bytes())
        pdf.save(path)

    content = list(extract_pages(path))
    nodes = analyze(content)
    text_nodes = [n for top in nodes for n in top.walk() if n.text and n.role != Role.ARTIFACT]
    assert " ".join(n.text for n in text_nodes) == " ".join(lines)
    assert all(n.source == Source.OCR and n.confidence <= 0.35 for n in text_nodes)
    assert sum(n.role == Role.FIGURE for n in nodes) == int(picture)
    doc = Document(root=Node(Role.DOC, children=nodes),
                   pages=[PageInfo(1, 612, 792)], meta={"lang": "en"})
    items = queue(doc)
    assert any(i["label"] == "Text recovered from an image" for i in items)
    assert all(i["confidence"] <= 0.35 for i in items if i["source"] == "ocr")


def test_a_visible_title_beside_an_invisible_ocr_layer_is_the_file_s_own_text(tmp_path):
    # Redo OCR keeps a scan's visible title and reads its body into an invisible layer: the page
    # image is then an image of text, but only the invisible lines were read from it.
    body = ["This is the first paragraph of the annual report to the district.",
            "The accounts show the income and expenses for the fiscal year.",
            "The board approved the budget after reviewing these figures."]
    path = make_text_pdf(tmp_path / "scan.pdf", [
        [(150, 60, 20, "CONFERENCE COMMITTEE REPORT")]
        + [(72, 200 + i * 18, 10, text) for i, text in enumerate(body)]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        image = Image.new("L", (612, 792), 255)
        for i in range(len(body)):   # the scan's ink under each line the layer was read from
            ImageDraw.Draw(image).line((72, 205 + i * 18, 400, 205 + i * 18), fill=0)
        raster = pdf.make_stream(image.tobytes())
        raster.update(Dictionary(Type=Name.XObject, Subtype=Name.Image, Width=612, Height=792,
                                 ColorSpace=Name.DeviceGray, BitsPerComponent=8))
        page.Resources.XObject = Dictionary(Im0=raster)
        ops = page.Contents.read_bytes().split(b"\n")
        page.Contents = pdf.make_stream(b"q 612 0 0 792 0 0 cm /Im0 Do Q\n" + ops[0] + b"\n"
                                        + b"\n".join(op.replace(b"BT ", b"BT 3 Tr ")
                                                     for op in ops[1:]))
        pdf.save(path)

    nodes = [n for top in analyze(list(extract_pages(path))) for n in top.walk()
             if n.text and n.role != Role.ARTIFACT]
    title, *read = nodes
    assert title.text == "CONFERENCE COMMITTEE REPORT"
    assert title.source == Source.HEURISTIC and title.confidence > 0.5
    assert "ocr-image-text" not in [f.code for f in title.facts]
    assert " ".join(n.text for n in read) == " ".join(body)
    assert all(n.source == Source.OCR and n.confidence <= 0.35 for n in read)


PHOTO = (80.0, 300.0, 250.0, 450.0)


def _picture_scan(box, lines):
    """The scan of a page-size image holding text and one photograph."""
    return PageScan(status="scanned",
                    objects=(ScannedObject(box, "text", tuple(range(len(lines))), 0.05, True),),
                    regions=(InkRegion(PHOTO, "picture"),))


def test_picture_scan_keeps_its_ruled_table_and_its_picture():
    from reflow.extract.lines import Line
    from reflow.extract.pages import PageContent
    from reflow.extract.tables.cells import TableData

    box = (0, 0, 612, 792)
    line = Line(1, (72, 80, 350, 90), "A scan with text, a table and a photograph.",
                10, False, False, "OCR")
    page = PageContent(1, 612, 792, [line], [box], scan=_picture_scan(box, [line]))
    table = TableData(1, (72, 200, 400, 250), [["Year", "Revenue"], ["2024", "$10"]])
    nodes = analyze([page], {1: [table]})
    assert [n.role for n in nodes].count(Role.TABLE) == 1
    assert [n.role for n in nodes].count(Role.FIGURE) == 1
    assert any(n.text == "$10" for top in nodes for n in top.walk())
    assert next(n for n in nodes if n.role == Role.TABLE).source == Source.OCR


def test_picture_scan_keeps_its_aligned_table():
    from reflow.extract.lines import Line
    from reflow.extract.pages import PageContent

    box = (0, 0, 612, 792)
    lines = [Line(1, (72, 80, 350, 90), "A scan with text, a table and a photograph.",
                  10, False, False, "OCR")]
    for i, row in enumerate((("Year", "Revenue"), ("2024", "$100"),
                             ("2025", "$200"), ("2026", "$300"))):
        for x, text in zip((72, 250), row):
            lines.append(Line(1, (x, 200 + i * 12, x + 30, 210 + i * 12), text,
                              10, False, False, "OCR"))
    page = PageContent(1, 612, 792, lines, [box], scan=_picture_scan(box, lines))
    nodes = analyze([page])
    assert sum(n.role == Role.TABLE for n in nodes) == 1
    assert sum(n.role == Role.FIGURE for n in nodes) == 1
    assert any(n.text == "$300" for top in nodes for n in top.walk())
