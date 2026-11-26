"""The grading plugin inside the real OCRmyPDF, in both OCR modes.

A three-page image PDF whose page 3 holds a typed line, a purple scribble, a lone digit and a
comma is OCR'd with `--pages 3`, the plugin and `--reflow-drop on`, once with --redo-ocr and
once with --force-ocr. Skipped without the ocrmypdf binary; about ten seconds."""
import random
import shutil
import subprocess
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFont

from reflow.extract.ocrwords import bind_words, layer_words, same_place
from reflow.ingest.ocr_trust import Grade, read_evidence

PLUGIN = Path(__file__).resolve().parents[2] / "src" / "reflow" / "ingest" / "ocr_trust.py"
TYPED = "Typed words stay in the text layer"

pytestmark = pytest.mark.skipif(shutil.which("ocrmypdf") is None, reason="no ocrmypdf binary")


def _image_pdf(path: Path) -> Path:
    pages = []
    for n in range(1, 4):
        image = Image.new("RGB", (1275, 1650), "white")   # Letter at 150 dpi
        draw = ImageDraw.Draw(image)
        if n == 3:
            font = ImageFont.load_default(size=44)
            draw.text((120, 200), TYPED, fill="black", font=font)
            draw.text((120, 420), "7", fill="black", font=font)
            draw.text((260, 420), ",", fill="black", font=font)
            rng = random.Random(83)   # a purple scrawl: tilted, jumping letters
            x = 120
            for ch in "Kmwnrslbq Dvhuz":
                glyph = Image.new("L", (90, 110), 0)
                ImageDraw.Draw(glyph).text((10, 5), ch, fill=255,
                                           font=ImageFont.load_default(size=rng.randrange(50, 80)))
                glyph = glyph.rotate(rng.randrange(-35, 35), expand=True)
                image.paste((110, 30, 170), (x, 700 + rng.randrange(-40, 40)), glyph)
                x += rng.randrange(40, 70)
        pages.append(image)
    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=150)
    return path


def _ocr(src: Path, out: Path, mode: str, evidence: Path) -> None:
    subprocess.run(["nice", "ocrmypdf", "--pages", "3", f"--{mode}-ocr", "--pdf-renderer", "fpdf2",
                    "--output-type", "pdf", "-l", "eng", "--plugin", str(PLUGIN),
                    "--reflow-evidence", str(evidence), "--reflow-drop", "on", str(src), str(out)],
                   check=True, capture_output=True)


@pytest.mark.parametrize("mode", ["redo", "force"])
def test_the_plugin_grades_page_3_and_drops_its_unread_words(tmp_path, mode):
    src = _image_pdf(tmp_path / "in.pdf")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    _ocr(src, tmp_path / "out.pdf", mode, evidence)
    assert sorted(p.name for p in evidence.iterdir()) == ["3.json"]
    grade = read_evidence(evidence / "3.json")
    assert (grade.page, grade.mode) == (3, mode)
    read = [w.text for w in grade.words if w.grade is Grade.READ]
    assert {"Typed", "words", "layer"} <= set(read)
    assert any(w.grade is Grade.UNREAD and w.pen and w.pen > 0.9 for w in grade.words)   # the scrawl
    pdf = pdfium.PdfDocument(tmp_path / "out.pdf")
    try:
        page = pdf[2]
        layer = layer_words(page)
        texts = [t for t, _ in layer]
        for word in TYPED.split():
            assert word in texts
        scale = page.get_size()[0] / grade.image_px[0]
        for w in grade.words:
            box = tuple(v * scale for v in w.box)
            here = [t for t, b in layer if same_place(box, b)]
            assert (w.text in here) == (w.grade is not Grade.UNREAD), (w.text, w.grade, here)
        assert bind_words(grade, page, None).status == "graded"
        for n in (0, 1):
            assert layer_words(pdf[n]) == []
    finally:
        pdf.close()
