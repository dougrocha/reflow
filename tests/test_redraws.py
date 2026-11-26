"""A glyph drawn again in (nearly) the same place is read once (reflow.extract.redraws): by
pdfium for the page's lines, and by pdfminer for link words and tag text (issue #323)."""

from pathlib import Path

import pdfplumber
import pikepdf
import pypdfium2 as pdfium
import pytest
from conftest import make_text_pdf
from testkit import NOT_COMMITTED, sample

from reflow.extract.pagecontent import PlumberPage
from reflow.extract.redraws import Redraws
from reflow.extract.text import page_lines

# Forty lines in one page: pdfium drops a copy only when it follows what it repeats closely,
# so a page drawn whole a second time gets through it.
DENSE = [[(72, 72 + 14 * k, 11, f"Line {k} of the dense page holds words such as office and "
           f"filing.") for k in range(40)]]


@pytest.fixture(scope="module")
def pages(tmp_path_factory):
    folder = tmp_path_factory.mktemp("redraws")
    clean = make_text_pdf(folder / "clean.pdf", DENSE)
    made = {"clean": clean}
    with pikepdf.open(clean) as pdf:
        page = pdf.pages[0]
        body = page.Contents.read_bytes()
        for name, copy in (("in-place", body), ("fake-bold", body.replace(b" Td", b" Td 0.3 0 Td"))):
            page.Contents = pdf.make_stream(b"q " + body + b" Q " + copy)
            pdf.save(folder / f"{name}.pdf")
            made[name] = folder / f"{name}.pdf"
    return made


def _lines(path: Path) -> list[str]:
    with pdfium.PdfDocument(path) as doc:
        return [line.text for line in page_lines(doc[0], 1)]


def _words(path: Path) -> list[str]:
    with pdfplumber.open(path) as plumber:
        return [w[0] for w in PlumberPage.from_page(plumber.pages[0], words=True).words]


@pytest.mark.parametrize("name", ["in-place", "fake-bold"])
def test_a_page_drawn_twice_reads_once(pages, name):
    assert _lines(pages[name]) == _lines(pages["clean"])
    assert _words(pages[name]) == _words(pages["clean"])


def test_one_objects_repeated_box_is_a_ligature_not_a_redraw():
    seen = Redraws()
    assert not seen.seen("f", (10, 10, 16, 20), 10, run=1)
    assert not seen.seen("f", (10, 10, 16, 20), 10, run=1)   # "ff" drawn as one glyph
    assert seen.seen("f", (10.3, 10, 16.3, 20), 10, run=2)   # the same glyph drawn again


def test_narrow_letters_side_by_side_are_not_redraws():
    seen = Redraws()
    assert not seen.seen("l", (10, 10, 12.2, 20), 10, run=1)
    assert not seen.seen("l", (12.2, 10, 14.4, 20), 10, run=2)   # "ll" a 0.22 em advance apart
    assert not seen.seen("l", (10, 25, 12.2, 35), 10, run=2)     # the next line


def _one_page(path: Path, content: bytes) -> Path:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=pdf.make_indirect(
        pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                           BaseFont=pikepdf.Name.Helvetica))))
    page.Contents = pdf.make_stream(content)
    pdf.save(path)
    return path


HEADING = b"BT /F1 12 Tf 72 700 Td (Heading) Tj ET"


def _content(path: Path) -> PlumberPage:
    with pdfplumber.open(path) as plumber:
        return PlumberPage.from_page(plumber.pages[0], words=True)


def test_an_artifact_copy_drawn_first_keeps_the_tagged_text(tmp_path):
    content = _content(_one_page(tmp_path / "artifact-first.pdf", b"/Artifact BMC " + HEADING
                                 + b" EMC /H1 <</MCID 0>> BDC " + HEADING + b" EMC"))
    assert content.summary([0])[1] == "Heading"
    assert [w[0] for w in content.words] == ["Heading"]


def test_a_copy_tagged_with_another_mcid_reads_once(tmp_path):
    content = _content(_one_page(tmp_path / "two-mcids.pdf", b"/H1 <</MCID 0>> BDC " + HEADING
                                 + b" EMC /H1 <</MCID 1>> BDC " + HEADING + b" EMC"))
    assert content.summary([0, 1])[1] == "Heading"


def test_a_redraw_scaled_by_the_text_matrix_is_found(tmp_path):
    """`1 Tf` scaled to 12 pt by `Tm`: the shift allowed is a share of the size as drawn. Three
    lines, then the three again, so pdfium does not drop the copies itself."""
    from reflow.extract.tables.ruled import _redrawn

    line = b"BT /F1 1 Tf 12 0 0 12 %s %d Tm (layer) Tj ET"
    lines = [line % (x, 700 - 20 * k) for x in (b"72", b"72.3") for k in range(3)]
    path = _one_page(tmp_path / "scaled.pdf", b" ".join(lines))
    with pdfium.PdfDocument(path) as doc:
        assert len(_redrawn(doc[0].get_textpage())) == 3 * len("layer")


def test_a_ruled_table_with_open_sides_drawn_twice_reads_once(tmp_path):
    """ResNet's Table 1 (page 5) takes its cells from pdfium's text in each box
    (tables.ruled._complete_open_sides)."""
    from reflow.extract.tables.ruled import find_tables

    resnet = sample("resnet.pdf")
    if not resnet.exists():
        pytest.skip(f"samples/resnet.pdf {NOT_COMMITTED}")
    with pikepdf.open(resnet) as pdf:
        for i in reversed(range(len(pdf.pages))):
            if i != 4:
                del pdf.pages[i]
        pdf.save(tmp_path / "clean.pdf")
        page = pdf.pages[0]
        body = page.Contents.read_bytes() if isinstance(page.Contents, pikepdf.Stream) else \
            b"\n".join(c.read_bytes() for c in page.Contents)
        page.Contents = pdf.make_stream(b"q " + body + b" Q " + body)
        pdf.save(tmp_path / "twice.pdf")
    clean = [t.rows for t in find_tables(tmp_path / "clean.pdf", [1])[1]]
    assert clean and clean[0][0][0] == "layer name"
    assert [t.rows for t in find_tables(tmp_path / "twice.pdf", [1])[1]] == clean
