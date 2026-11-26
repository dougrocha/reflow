"""The working copy puts each page in the frame its text is read in (reflow.ingest.frame)."""

from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from PIL import ImageChops
from pikepdf import Array, Dictionary, Name

from conftest import make_tagged_pdf, make_text_pdf
from reflow.extract.pdfnav import Destinations
from reflow.ingest.load import normalize

LINES = [[(72, 72 + 20 * k, 11, f"Line {k} of the upright page reads across.") for k in range(6)]]


def _rotate(path: Path) -> int:
    with pikepdf.open(path) as pdf:
        return int(pdf.pages[0].obj.get("/Rotate", 0))


def test_a_page_turned_off_its_upright_text_is_read_unturned(tmp_path):
    src = make_text_pdf(tmp_path / "upright.pdf", LINES)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Rotate = 90
        pdf.save(src)
    normalize(src, tmp_path / "working.pdf")
    assert _rotate(tmp_path / "working.pdf") == 0


def test_a_page_turned_to_stand_its_sideways_text_upright_keeps_its_turn(tmp_path):
    """A landscape table drawn sideways on a portrait sheet: /Rotate 90 turns it upright."""
    src = make_text_pdf(tmp_path / "sideways.pdf", LINES)
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.contents_add(pdf.make_stream(b"q 0 1 -1 0 612 0 cm\n"), prepend=True)
        page.contents_add(pdf.make_stream(b"\nQ\n"))
        page.obj.Rotate = 90
        pdf.save(src)
    normalize(src, tmp_path / "working.pdf")
    assert _rotate(tmp_path / "working.pdf") == 90


def _offset(src: Path, dst: Path, dx: float, dy: float) -> None:
    """`src` with every page's boxes, content, annotations and destinations moved by (dx, dy),
    as a file made off the origin has them: it renders the same."""
    with pikepdf.open(src) as pdf:
        for page in pdf.pages:
            x0, y0, x1, y1 = (float(v) for v in page.obj.MediaBox)
            page.obj.MediaBox = Array([x0 + dx, y0 + dy, x1 + dx, y1 + dy])
            page.contents_add(pdf.make_stream(f"q 1 0 0 1 {dx} {dy} cm\n".encode()), prepend=True)
            page.contents_add(pdf.make_stream(b"\nQ\n"))
        page = pdf.pages[0]
        go = pdf.make_indirect(Dictionary(S=Name.GoTo,   # one action, two links
                                          D=[pdf.pages[1].obj, Name.XYZ, 90 + dx, 400 + dy, 0]))
        page.obj.Annots = pdf.make_indirect(Array([pdf.make_indirect(Dictionary(
            Type=Name.Annot, Subtype=Name.Link, Rect=[72 + dx, 600 + dy, 200 + dx, 620 + dy],
            Dest=[pdf.pages[1].obj, Name.XYZ, 72 + dx, 500 + dy, 0]))] + [
            pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Link, A=go,
                                         Rect=[72 + dx, y + dy, 200 + dx, y + 20 + dy]))
            for y in (300, 200)]))
        chapter = pdf.Root.Outlines.First
        chapter.Dest[3] = float(chapter.Dest[3]) + dy
        names = pdf.Root.Names.Dests.Names
        names[1].D[2] = float(names[1].D[2]) + dy
        pdf.Root.Dests.tbl[2] = float(pdf.Root.Dests.tbl[2]) + dy
        pdf.save(dst)


def _targets(path: Path) -> list:
    with pikepdf.open(path) as pdf:
        dests = Destinations(pdf)
        outline = [dests.resolve(item.get("/Dest") or item.A.D)
                   for item in (pdf.Root.Outlines.First, pdf.Root.Outlines.First.First,
                                pdf.Root.Outlines.Last)]
        links = [(dests.resolve(a.get("/Dest") or a.A.D), [round(float(v), 2) for v in a.Rect])
                 for a in pdf.pages[0].obj.get("/Annots", [])]
        return [outline, links, [[float(v) for v in p.obj.MediaBox] for p in pdf.pages]]


def test_a_page_off_the_origin_moves_to_it_with_everything_on_it(tmp_path):
    clean = make_tagged_pdf(tmp_path / "tagged.pdf")
    _offset(clean, tmp_path / "linked.pdf", 0, 0)
    _offset(clean, tmp_path / "offset.pdf", 100, 60)
    normalize(tmp_path / "offset.pdf", tmp_path / "working.pdf")

    assert _targets(tmp_path / "working.pdf") == _targets(tmp_path / "linked.pdf")
    a, b = pdfium.PdfDocument(tmp_path / "linked.pdf"), pdfium.PdfDocument(tmp_path / "working.pdf")
    for i in range(len(a)):
        ra, rb = a[i].render(scale=1).to_pil(), b[i].render(scale=1).to_pil()
        assert ra.size == rb.size
        assert ImageChops.difference(ra.convert("L"), rb.convert("L")).getbbox() is None


def test_a_page_at_the_origin_is_written_as_it_was(tmp_path):
    clean = make_tagged_pdf(tmp_path / "tagged.pdf")
    normalize(clean, tmp_path / "working.pdf")
    with pikepdf.open(clean) as a, pikepdf.open(tmp_path / "working.pdf") as b:
        for pa, pb in zip(a.pages, b.pages):
            assert "/Rotate" not in pb.obj
            assert pa.obj.Contents.read_bytes() == pb.obj.Contents.read_bytes()
