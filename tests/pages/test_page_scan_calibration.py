"""Calibration of the page scan (reflow.extract.scan) on real scanned pages.

Each page of fixtures/page-scan.json is scanned from the working copy of its document's page-check
conversion (the OCR'd copy that extraction reads) and compared with the hand-drawn truth: the
pictures, the text-only pages, the rules, whether there is pen ink and where, and every image
object's kind.
TEXTURE_STD, PHOTO_MIDTONE and NOISY_SHARE were tuned here.
Run with `pytest -o pythonpath=src -m pages tests/pages/test_page_scan_calibration.py`.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from corpus import Corpus
from reflow import geometry
from reflow.extract.pages import extract_pages
from reflow.extract.scan import PageScan, scan_page

pytestmark = pytest.mark.pages

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "page-scan.json").read_text(encoding="utf-8"))
PAGES = FIXTURE["pages"]


@pytest.fixture(scope="module")
def corpus() -> Corpus:
    c = Corpus()
    needed: dict[str, set[int]] = {}
    for entry in PAGES:
        needed.setdefault(entry["doc"], set()).add(entry["page"])
    c.prepare(needed, pages=needed)
    return c


def _scan(corpus: Corpus, slug: str, page: int) -> PageScan:
    doc = corpus.doc(slug)
    number = doc.excerpt_page(page)
    working = doc.out_dir / "working.pdf"
    [content] = list(extract_pages(working, number, number))
    with pdfium.PdfDocument(str(working)) as pdf, closing(pdf[number - 1]) as pdf_page:
        rulings = [d.bbox for d in content.drawings if d.only_rules]
        return scan_page(pdf_page, content.lines, content.words, content.images, rulings)


@pytest.mark.parametrize("entry", PAGES, ids=[f"{e['doc']}-{e['page']}" for e in PAGES])
def test_page_scan_matches_the_hand_drawn_truth(corpus: Corpus, entry: dict) -> None:
    scan = _scan(corpus, entry["doc"], entry["page"])
    where = f"{entry['doc']} p{entry['page']}"
    assert scan.status == entry["status"], f"{where}: status {scan.status}"
    assert [o.kind for o in scan.objects] == entry["objects"], f"{where}: object kinds"
    pictures = [r.bbox for r in scan.regions if r.kind == "picture"]
    if "pictures" in entry:
        if not entry["pictures"]:
            assert pictures == [], f"{where}: pictures {pictures}"
        for box in entry["pictures"]:
            best = max((geometry.iou(tuple(box), p) for p in pictures), default=0.0)
            assert best >= 0.8, f"{where}: picture {box} matched at IoU {best:.2f} by {pictures}"
    if entry.get("text_only"):
        assert scan.regions == (), f"{where}: regions {scan.regions}"
    for box in entry.get("rules", []):
        rules = [r.bbox for r in scan.regions if r.kind == "rule"]
        assert any(geometry.overlap(tuple(box), r) > 0 for r in rules), f"{where}: rules {rules}"
    if "pen" in entry:
        assert bool(scan.pen) == entry["pen"], f"{where}: pen {[r.bbox for r in scan.pen]}"
    pens = [r.bbox for r in scan.pen]
    for box in entry.get("pen_boxes", []):
        best = max((geometry.iou(tuple(box), p) for p in pens), default=0.0)
        assert best >= 0.5, f"{where}: pen {box} matched at IoU {best:.2f} by {pens}"
    for box in entry.get("pen_missed", []):
        best = max((geometry.iou(tuple(box), p) for p in pens), default=0.0)
        assert best < 0.5, f"{where}: pen {box} is found now (IoU {best:.2f}): move it to pen_boxes"
    for box in entry.get("typed", []):
        over = [p for p in pens if geometry.overlap(tuple(box), p) > 0]
        assert not over, f"{where}: pen regions {over} lie over the typed text at {box}"

