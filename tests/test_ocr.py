"""The OCR stage (phase-next U6): page selection, the atomic rewrite, the facts, the runners.

Tesseract and OCRmyPDF are optional and not needed here: a FakeRunner stamps a text object
onto the requested pages, so the pipeline is tested without an OCR engine.
"""

import importlib
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from conftest import make_pdf, make_text_pdf
from pikepdf import Dictionary, Name

from reflow.analyze.confidence import CODES
from reflow.ingest.pages import ingest
from reflow.ingest import ocr_trust
from reflow.ingest.ocr import (
    OcrmypdfRunner,
    OcrPage,
    OcrPlan,
    OcrResult,
    OcrUnavailable,
    make_runner,
    needs_ocr,
    ocr_pages,
    record,
)
from reflow.model import Document, Node, PageInfo, PageKind, Role, Source
from PIL import Image, ImageDraw

W, H = 612, 792


class FakeRunner:
    """Copies src to dst after stamping a text object onto the requested (1-based) pages."""
    name = "fake"
    grades = False

    def __init__(self, text="Recognized words"):
        self.text = text
        self.calls = []

    def environment(self, langs):
        return "fake engine data"

    def run(self, src, dst, plan, langs, evidence):
        pages = sorted(plan.pages)
        self.calls.append((pages, langs))
        with pikepdf.open(src) as pdf:
            font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                                BaseFont=Name.Helvetica))
            for n in pages:
                page = pdf.pages[n - 1]
                page.Resources.Font = Dictionary(F9=font)
                stamp = f"BT /F9 12 Tf 72 700 Td ({self.text}) Tj ET\n".encode()
                page.Contents = pdf.make_stream(page.Contents.read_bytes() + stamp)
            pdf.save(dst)


def write_grade(evidence, page, mode, text="Recognized words", conf=92):
    """The evidence file the grading plugin writes for `page`: `text`'s words, READ."""
    words = tuple(ocr_trust.Word((300 + 200 * i, 360, 480 + 200 * i, 410), w, conf,
                                 ocr_trust.Grade.READ, None, 1.0, layer_text=w)
                  for i, w in enumerate(text.split()))
    grade = ocr_trust.PageGrade(page, mode, (2550, 3300), words)
    (Path(evidence) / f"{page}.json").write_text(json.dumps(grade.to_dict()))


class GradingRunner(FakeRunner):
    """FakeRunner that grades, as OcrmypdfRunner with its plugin does; `evidence` edits what it
    leaves: "missing", "corrupt", "wrong-mode" or "extra" on the last planned page."""
    grades = True

    def __init__(self, evidence="ok", text="Recognized words"):
        super().__init__(text)
        self.evidence, self.dirs, self.environ = evidence, [], []

    def run(self, src, dst, plan, langs, evidence):
        self.dirs.append(evidence)
        self.environ.append(dict(os.environ))
        super().run(src, dst, plan, langs, evidence)
        last = max(plan.pages)
        for n, page in plan.pages.items():
            write_grade(evidence, n, page.mode, self.text)
        target = Path(evidence) / f"{last}.json"
        if self.evidence == "missing":
            target.unlink()
        elif self.evidence == "corrupt":
            target.write_text(target.read_text()[:30])
        elif self.evidence == "wrong-mode":
            data = json.loads(target.read_text())
            data["mode"] = "force" if data["mode"] == "redo" else "redo"
            target.write_text(json.dumps(data))
        elif self.evidence == "extra":
            write_grade(evidence, last + 1, "redo")


class BrokenRunner:
    name = "broken"
    grades = False

    def environment(self, langs):
        return None

    def run(self, src, dst, plan, langs, evidence):
        dst.write_bytes(b"half a file")
        raise RuntimeError("engine crashed")


class AbsentRunner:
    name = "absent"
    grades = False

    def environment(self, langs):
        return None

    def run(self, src, dst, plan, langs, evidence):
        raise OcrUnavailable("no engine")


def page_text(path, number):
    pdf = pdfium.PdfDocument(path)
    try:
        page = pdf[number - 1]
        textpage = page.get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            textpage.close()
            page.close()
    finally:
        pdf.close()


@pytest.fixture
def scans(tmp_path):
    return make_pdf(tmp_path / "scans.pdf", ["text", "scan", "scan+text", "scan"])


def image_pdf(path, texts=(), *, marks=(), photo=False, form=False, extra=b""):
    """One Letter page: a full-page image (white paper, or grey noise when `photo`) with black
    rectangles `marks` ((x0, top, x1, bottom) in points), under `texts`: (x, top, size, text,
    render mode) drawn in Helvetica, inside a form XObject when `form`. `extra` is appended to
    the page's content stream."""
    image = Image.new("L", (W // 4, H // 4), 255)
    if photo:
        rng = random.Random(7)
        image.putdata([rng.randrange(256) for _ in range(image.width * image.height)])
    draw = ImageDraw.Draw(image)
    for x0, top, x1, bottom in marks:
        draw.rectangle((x0 // 4, top // 4, x1 // 4 - 1, bottom // 4 - 1), fill=0)
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    xobject = pikepdf.Stream(pdf, image.tobytes(), Type=Name.XObject, Subtype=Name.Image,
                             Width=image.width, Height=image.height,
                             ColorSpace=Name.DeviceGray, BitsPerComponent=8)
    words = "\n".join(f"BT {mode} Tr /F1 {pt} Tf {x} {H - top - pt} Td ({text}) Tj ET"
                      for x, top, pt, text, mode in texts).encode()
    page = pdf.add_blank_page(page_size=(W, H))
    resources = Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im0=xobject))
    if form:
        resources.XObject.Fm0 = pikepdf.Stream(
            pdf, words, Type=Name.XObject, Subtype=Name.Form, BBox=[0, 0, W, H],
            Resources=Dictionary(Font=Dictionary(F1=font)))
        words = b"q /Fm0 Do Q"
    page.Resources = resources
    page.Contents = pdf.make_stream(b"q 612 0 0 792 0 0 cm /Im0 Do Q\n" + words + b"\n" + extra)
    pdf.save(path)
    return path


def plan_of(path):
    return needs_ocr(ingest(path), path)


# An invisible layer down the top half of a page image, and ink under it that no character
# covers: a page whose layer was not found would be a textless band.
LAYER = [(72, 100 + 40 * i, 12, "Recognized words of an old layer", 3) for i in range(8)]
UNDER = [(72, 600, 540, 620)]


@pytest.mark.parametrize("extra, form", [([], False), ([(300, 760, 10, "iii", 0)], False),
                                         ([], True)], ids=["layer", "folio", "form-xobject"])
def test_a_readable_invisible_layer_over_a_page_image_is_kept(tmp_path, extra, form):
    path = image_pdf(tmp_path / "a.pdf", LAYER + extra, marks=UNDER, form=form)
    assert ingest(path).pages[0].kind is PageKind.MIXED
    assert plan_of(path).pages == {}
    visible = image_pdf(tmp_path / "b.pdf", [line[:4] + (0,) for line in LAYER], marks=UNDER)
    assert plan_of(visible).pages[1].reason == "textless-band"


def test_ink_under_a_visible_title_only_is_a_textless_band(tmp_path):
    path = image_pdf(tmp_path / "a.pdf", [(72, 30, 24, "Committee Report Title", 0)],
                     marks=[(72, 200, 540, 220), (72, 400, 540, 420)])
    (page,) = plan_of(path).pages.values()
    assert (page.reason, page.mode) == ("textless-band", "redo")
    ((x0, top, x1, bottom),) = page.region_boxes
    assert (x0, x1, bottom) == (0, W, H)
    assert 50 < top < 70   # just under the title's padded character boxes


def test_a_tagged_document_has_no_textless_band(tmp_path):
    # OCRmyPDF writes no tag tree: OCR on one page would strip the whole document's tags.
    path = image_pdf(tmp_path / "a.pdf", [(72, 30, 24, "Committee Report Title", 0)],
                     marks=[(72, 200, 540, 220)])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.StructTreeRoot = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        pdf.save(path)
    assert plan_of(path).pages == {}


def test_two_textless_bands_are_kept_top_to_bottom(tmp_path):
    path = image_pdf(tmp_path / "a.pdf", [(72, 30, 24, "Committee Report Title", 0),
                                          (72, 400, 12, "A line of the letter body", 0)],
                     marks=[(72, 200, 540, 220), (72, 600, 540, 620)])
    (page,) = plan_of(path).pages.values()
    first, second = page.region_boxes
    # The body line is drawn at top 400: its glyphs reach from about 403 to 412.
    assert first[1] < 70 and 395 < first[3] < 403
    assert 412 < second[1] < 420 and second[3] == H


def test_a_white_page_image_with_visible_text_and_empty_gaps_is_not_selected(tmp_path):
    path = image_pdf(tmp_path / "a.pdf", [(72, 30, 24, "Committee Report Title", 0),
                                          (72, 600, 12, "A line of the letter body", 0)])
    assert plan_of(path).pages == {}


def test_readable_text_down_a_whole_photo_is_not_selected(tmp_path):
    lines = [(72, 20 + 50 * i, 12, "Readable words over a photo", 0) for i in range(16)]
    assert plan_of(image_pdf(tmp_path / "a.pdf", lines, photo=True)).pages == {}


def test_a_photo_under_one_title_line_is_a_textless_band(tmp_path):
    path = image_pdf(tmp_path / "a.pdf", [(72, 20, 24, "Annual Report Title", 0)], photo=True)
    (page,) = plan_of(path).pages.values()
    assert (page.reason, page.mode) == ("textless-band", "redo")


def test_unreadable_visible_text_is_forced_and_a_lone_digit_is_redone(tmp_path):
    junk = image_pdf(tmp_path / "a.pdf", [(72, 100, 12, "ab 12345 67890 #%& 1234 5678 99", 0)])
    assert plan_of(junk).pages == {1: OcrPage("unreadable-text", "force")}
    digit = image_pdf(tmp_path / "b.pdf", [(300, 760, 10, "5", 0)])
    assert plan_of(digit).pages == {1: OcrPage("unreadable-text", "redo")}


def test_an_unreadable_invisible_layer_is_unreadable_text_on_redo(tmp_path):
    path = image_pdf(tmp_path / "a.pdf", [(72, 100, 12, "ab 12345 67890 #%& 1234 5678 99", 3)])
    assert plan_of(path).pages == {1: OcrPage("unreadable-text", "redo")}


def test_a_sparse_page_of_outlines_is_outlined_text_on_force(tmp_path):
    path = make_text_pdf(tmp_path / "a.pdf", [[(300, 760, 9, "15")]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        ops = " ".join(f"{50 + i * 4} 680 m {52 + i * 4} 686 l {54 + i * 4} 680 l h f"
                       for i in range(40))
        pdf.pages[0].Contents = pdf.make_stream(pdf.pages[0].Contents.read_bytes() + ops.encode())
        pdf.save(path)
    assert plan_of(path).pages == {1: OcrPage("outlined-text", "force")}


def test_outlined_words_over_a_page_image_are_a_scan_redone(tmp_path):
    # Paths over a full-page image and no character: the page is SCANNED, not sparse, so it is
    # no-text on redo, which reads the paths with the image (redo keeps only visible text).
    ops = " ".join(f"{50 + i * 4} 680 m {52 + i * 4} 686 l {54 + i * 4} 680 l h f"
                   for i in range(40))
    path = image_pdf(tmp_path / "a.pdf", extra=ops.encode())
    assert ingest(path).pages[0].kind is PageKind.SCANNED
    assert plan_of(path).pages == {1: OcrPage("no-text", "redo")}


def test_the_plan_lists_modes_force_first_and_keys_without_the_bands():
    plan = OcrPlan({1: OcrPage("no-text", "redo"), 2: OcrPage("unreadable-text", "force"),
                    3: OcrPage("textless-band", "redo", ((0, 10, 612, 200),))})
    assert list(plan.by_mode().items()) == [("force", [2]), ("redo", [1, 3])]
    assert OcrPlan({1: OcrPage("no-text", "redo")}).by_mode() == {"redo": [1]}
    other = OcrPlan({**plan.pages, 3: OcrPage("textless-band", "redo", ((0, 50, 612, 90),))})
    assert other.key() == plan.key()
    assert OcrPlan({**plan.pages, 1: OcrPage("no-text", "force")}).key() != plan.key()


def scan_plan(*pages):
    return OcrPlan({n: OcrPage("no-text", "redo") for n in pages})


def blank_doc(kinds):
    pages = [PageInfo(i + 1, 612, 792, kind=k) for i, k in enumerate(kinds)]
    return Document(root=Node(Role.DOC), pages=pages)


def test_needs_ocr_picks_scanned_and_poor_mixed(tmp_path):
    path = make_pdf(tmp_path / "a.pdf", ["text", "scan", "scan+text"])
    doc = ingest(path)
    assert [p.kind for p in doc.pages] == [PageKind.BORN_DIGITAL, PageKind.SCANNED,
                                           PageKind.MIXED]
    # make_pdf's page image is black: the band under page 3's one line holds ink and no text.
    plan = needs_ocr(doc, path)
    assert plan.pages[2] == OcrPage("no-text", "redo")
    assert (plan.pages[3].reason, sorted(plan.pages)) == ("textless-band", [2, 3])


def test_needs_ocr_redoes_a_mixed_page_with_a_junk_text_layer(tmp_path):
    path = make_pdf(tmp_path / "a.pdf", ["scan+text"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.Contents = pdf.make_stream(
            b"q 612 0 0 792 0 0 cm /Im0 Do Q\nBT /F1 12 Tf 72 720 Td (1234 5678 #%&) Tj ET\n")
        pdf.save(path)
    doc = ingest(path)
    assert doc.pages[0].kind is PageKind.MIXED
    assert needs_ocr(doc, path).pages == {1: OcrPage("unreadable-text", "redo")}


def test_needs_ocr_is_empty_for_a_born_digital_document(tmp_path):
    path = make_pdf(tmp_path / "a.pdf", ["text", "text", "empty"])
    assert needs_ocr(ingest(path), path).pages == {}


def test_ocr_pages_stamps_text_on_the_requested_pages_only(scans):
    runner = FakeRunner()
    result = ocr_pages(scans, scan_plan(2, 4), runner, langs=("eng", "por"))
    assert runner.calls == [([2, 4], ("eng", "por"))]
    assert (result.attempted, result.done, result.failed) == ([2, 4], [2, 4], {})
    assert result.runner == "fake"
    assert "Recognized words" in page_text(scans, 2)
    assert "Recognized words" in page_text(scans, 4)
    assert "Recognized words" not in page_text(scans, 1)
    assert not scans.with_suffix(".ocr.tmp").exists()


def test_ocr_makes_scanned_pages_readable_by_ingest(scans):
    before = ingest(scans)
    plan = needs_ocr(before, scans)
    assert sorted(plan.pages) == [2, 3, 4]
    ocr_pages(scans, plan, FakeRunner())
    after = ingest(scans)
    assert after.pages[1].kind is PageKind.MIXED
    assert {p.reason for p in needs_ocr(after, scans).pages.values()} == {"textless-band"}


def test_ocr_pages_without_a_runner_writes_nothing(scans):
    before = scans.read_bytes()
    result = ocr_pages(scans, scan_plan(2, 4), None)
    assert (result.attempted, result.done, result.failed) == ([2, 4], [], {})
    assert result.runner == "none"
    assert scans.read_bytes() == before
    assert not scans.with_suffix(".ocr.tmp").exists()


def test_ocr_pages_with_an_unavailable_engine_writes_nothing(scans):
    before = scans.read_bytes()
    result = ocr_pages(scans, scan_plan(2), AbsentRunner())
    assert (result.attempted, result.done, result.failed) == ([2], [], {})
    assert result.runner == "unavailable"
    assert scans.read_bytes() == before


def test_ocr_pages_failure_is_all_or_nothing(scans):
    before = scans.read_bytes()
    result = ocr_pages(scans, scan_plan(2, 4), BrokenRunner())
    assert result.done == []
    assert result.attempted == [2, 4]
    assert list(result.failed) == [0]
    assert "engine crashed" in result.failed[0]
    assert scans.read_bytes() == before
    assert not scans.with_suffix(".ocr.tmp").exists()


@pytest.mark.parametrize("evidence, says", [("missing", "page 4: no word grades"),
                                             ("corrupt", "page 4: unreadable word grades"),
                                             ("wrong-mode", "page 4: word grades of page 4 in force"),
                                             ("extra", "page 5: word grades for a page that was not planned")])
def test_ocr_pages_fails_the_run_when_the_word_grades_do_not_match_the_plan(scans, evidence, says):
    before = scans.read_bytes()
    runner = GradingRunner(evidence)
    result = ocr_pages(scans, scan_plan(2, 4), runner)
    assert result.done == [] and not result.graded
    assert says in result.failed[0]
    assert scans.read_bytes() == before
    assert not ocr_trust.sidecar_path(scans).exists()
    assert not runner.dirs[0].exists()


def test_ocr_pages_writes_the_pdf_then_the_sidecar_with_its_hash(scans):
    runner = GradingRunner()
    result = ocr_pages(scans, scan_plan(2, 4), runner)
    assert result.done == [2, 4] and result.graded
    read = ocr_trust.read_sidecar(scans)
    assert read.status == "ok" and sorted(read.pages) == [2, 4]
    assert read.pages[2].mode == "redo" and [w.text for w in read.pages[2].words] == [
        "Recognized", "words"]
    assert json.loads(ocr_trust.sidecar_path(scans).read_text())["pdf_sha256"] == \
        ocr_trust.file_hash(scans)
    assert not runner.dirs[0].exists()           # the temp directory is gone


def test_a_runner_that_does_not_grade_leaves_no_sidecar(scans):
    result = ocr_pages(scans, scan_plan(2), FakeRunner())
    assert result.done == [2] and not result.graded
    assert not ocr_trust.sidecar_path(scans).exists()


def test_each_conversion_has_its_own_evidence_directory_and_no_environment(tmp_path):
    import threading
    before = dict(os.environ)
    runners, docs = [GradingRunner(), GradingRunner()], []
    for i in range(2):
        docs.append(make_pdf(tmp_path / f"d{i}.pdf", ["scan", "scan"]))
    threads = [threading.Thread(target=ocr_pages, args=(d, scan_plan(1, 2), r))
               for d, r in zip(docs, runners)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert runners[0].dirs[0] != runners[1].dirs[0]
    assert all(r.environ[0] == before for r in runners) and dict(os.environ) == before
    assert all(ocr_trust.read_sidecar(d).status == "ok" for d in docs)


def test_ocr_pages_with_no_pages_does_not_call_the_runner(scans):
    runner = FakeRunner()
    result = ocr_pages(scans, OcrPlan(), runner)
    assert runner.calls == []
    assert result.done == []


def test_record_writes_page_and_document_facts():
    doc = blank_doc([PageKind.BORN_DIGITAL, PageKind.SCANNED, PageKind.SCANNED,
                     PageKind.SCANNED])
    result = OcrResult(scan_plan(2, 3, 4), done=[2], runner="ocrmypdf")
    record(doc, result)
    assert [f.code for f in doc.pages[0].facts] == []
    (done,) = doc.pages[1].facts
    assert (done.code, done.source, done.detail, done.data) == (
        "ocr-page", Source.OCR, "text recognized by ocrmypdf",
        {"reason": "no-text", "mode": "redo"})
    for page in doc.pages[2:]:
        (missing,) = page.facts
        assert (missing.code, missing.source, missing.detail) == (
            "ocr-missing", Source.HEURISTIC, "image-only page, no text layer")
    (summary,) = doc.root.facts
    assert (summary.code, summary.detail) == ("ocr-doc", "2 image-only pages without text")


def test_record_without_a_runner_flags_every_scanned_page():
    doc = blank_doc([PageKind.SCANNED, PageKind.SCANNED])
    record(doc, OcrResult(scan_plan(1, 2)))
    assert [f.code for f in doc.pages[0].facts] == ["ocr-missing"]
    assert [f.code for f in doc.root.facts] == ["ocr-doc"]


def test_record_adds_no_document_fact_when_everything_was_recognized():
    doc = blank_doc([PageKind.SCANNED])
    record(doc, OcrResult(scan_plan(1), done=[1], runner="fake"))
    assert [f.code for f in doc.pages[0].facts] == ["ocr-page"]
    assert doc.root.facts == []


BOTH = OcrPlan({1: OcrPage("unreadable-text", "force"), 3: OcrPage("no-text", "redo"),
                4: OcrPage("unreadable-text", "redo"), 5: OcrPage("textless-band", "redo")})


def ocrmypdf_module(monkeypatch, module):
    """Install `module` as the importable ocrmypdf, or make it not importable when None."""
    real = importlib.import_module

    def import_module(name, *args, **kwargs):
        if name != "ocrmypdf":
            return real(name, *args, **kwargs)
        if module is None:
            raise ImportError(name)
        return module

    monkeypatch.setattr(importlib, "import_module", import_module)


def test_ocrmypdf_runner_without_module_or_binary_is_unavailable(monkeypatch, tmp_path):
    ocrmypdf_module(monkeypatch, None)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    with pytest.raises(OcrUnavailable):
        OcrmypdfRunner().run(tmp_path / "a.pdf", tmp_path / "b.pdf", scan_plan(1), ("eng",),
                             tmp_path)
    assert not OcrmypdfRunner.available()


def test_ocrmypdf_runner_uses_the_module_force_first_then_redo(monkeypatch, tmp_path):
    calls = []

    class FakeModule:
        @staticmethod
        def ocr(src, dst, **kwargs):
            calls.append((src, dst, kwargs))
            dst.write_bytes(b"%PDF")

    ocrmypdf_module(monkeypatch, FakeModule)
    src, dst = tmp_path / "a.pdf", tmp_path / "b.pdf"
    OcrmypdfRunner().run(src, dst, BOTH, ("eng", "por"), tmp_path)
    middle = tmp_path / "b.force.tmp"
    common = {"pdf_renderer": "fpdf2", "output_type": "pdf", "language": "eng+por",
              "progress_bar": False, "plugins": [OcrmypdfRunner.PLUGIN],
              "reflow_evidence": str(tmp_path), "reflow_drop": "on"}
    assert calls == [(src, middle, {"pages": "1", "force_ocr": True, **common}),
                     (middle, dst, {"pages": "3-5", "redo_ocr": True, **common})]
    assert not middle.exists()


def test_ocrmypdf_runner_with_one_mode_makes_one_pass(monkeypatch, tmp_path):
    calls = []

    class FakeModule:
        @staticmethod
        def ocr(src, dst, **kwargs):
            calls.append((src, dst, kwargs))

    ocrmypdf_module(monkeypatch, FakeModule)
    OcrmypdfRunner().run(tmp_path / "a.pdf", tmp_path / "b.pdf", scan_plan(2, 4), ("eng",),
                         tmp_path)
    assert calls == [(tmp_path / "a.pdf", tmp_path / "b.pdf", {
        "pages": "2,4", "redo_ocr": True, "pdf_renderer": "fpdf2", "output_type": "pdf",
        "language": "eng", "progress_bar": False, "plugins": [OcrmypdfRunner.PLUGIN],
        "reflow_evidence": str(tmp_path), "reflow_drop": "on"})]


def test_ocrmypdf_runner_falls_back_to_the_binary(monkeypatch, tmp_path):
    seen = []
    ocrmypdf_module(monkeypatch, None)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/usr/bin/ocrmypdf")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)))
    OcrmypdfRunner().run(tmp_path / "a.pdf", tmp_path / "b.pdf", BOTH, ("eng",), tmp_path)
    (force, f_options), (redo, r_options) = seen
    middle = str(tmp_path / "b.force.tmp")
    grading = ["--plugin", str(OcrmypdfRunner.PLUGIN), "--reflow-evidence", str(tmp_path),
               "--reflow-drop", "on"]
    assert OcrmypdfRunner.PLUGIN.name == "ocr_trust.py" and OcrmypdfRunner.PLUGIN.is_file()
    assert force == ["/usr/bin/ocrmypdf", "--pages", "1", "--force-ocr", "--pdf-renderer", "fpdf2",
                     "--output-type", "pdf", "-l", "eng", *grading, str(tmp_path / "a.pdf"),
                     middle]
    assert redo == ["/usr/bin/ocrmypdf", "--pages", "3-5", "--redo-ocr", "--pdf-renderer", "fpdf2",
                    "--output-type", "pdf", "-l", "eng", *grading, middle, str(tmp_path / "b.pdf")]
    assert f_options["check"] is True and r_options["check"] is True


def test_ocrmypdf_runner_removes_the_force_output_when_the_redo_pass_fails(monkeypatch, tmp_path):
    class FakeModule:
        @staticmethod
        def ocr(src, dst, **kwargs):
            dst.write_bytes(b"%PDF")
            if kwargs.get("redo_ocr"):
                raise RuntimeError("redo crashed")

    ocrmypdf_module(monkeypatch, FakeModule)
    with pytest.raises(RuntimeError, match="redo crashed"):
        OcrmypdfRunner().run(tmp_path / "a.pdf", tmp_path / "b.pdf", BOTH, ("eng",), tmp_path)
    assert not (tmp_path / "b.force.tmp").exists()


def test_make_runner_modes(monkeypatch):
    monkeypatch.setattr(OcrmypdfRunner, "available", staticmethod(lambda: False))
    assert make_runner("none") is None
    assert make_runner("auto") is None
    with pytest.raises(OcrUnavailable):
        make_runner("ocrmypdf")
    monkeypatch.setattr(OcrmypdfRunner, "available", staticmethod(lambda: True))
    assert isinstance(make_runner("auto"), OcrmypdfRunner)
    assert isinstance(make_runner("ocrmypdf"), OcrmypdfRunner)
    with pytest.raises(ValueError):
        make_runner("tesseract")


def test_real_ocrmypdf_recognizes_a_scanned_page(tmp_path):
    if not OcrmypdfRunner.available():
        pytest.skip("ocrmypdf is not installed (module or binary)")
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    path = make_pdf(tmp_path / "scan.pdf", ["scan"])
    doc = ingest(path)
    result = ocr_pages(path, needs_ocr(doc, path), make_runner("ocrmypdf"))
    assert result.failed == {}
    assert result.done == [1]


# End to end through reflow.prepare, with OcrmypdfRunner driving a fake ocrmypdf module that
# stamps "<mode> words" onto the pages of each pass.

def _spec_pages(spec):
    out = []
    for part in spec.split(","):
        first, _, last = part.partition("-")
        out += range(int(first), int(last or first) + 1)
    return out


class StampingModule:
    """A stand-in for the ocrmypdf module: copies src to dst with "<mode> words" stamped on the
    pages of the pass, or raises `fail` on the pass of that mode."""

    def __init__(self, fail=None, error=RuntimeError("redo crashed")):
        self.calls, self.fail, self.error = [], fail, error

    def ocr(self, src, dst, *, pages, **kwargs):
        mode = "force" if kwargs.get("force_ocr") else "redo"
        self.calls.append((Path(src), Path(dst), mode, pages))
        if mode == self.fail:
            raise self.error
        FakeRunner(f"{mode} words").run(src, dst, scan_plan(*_spec_pages(pages)), (), None)
        for n in _spec_pages(pages):     # what the grading plugin writes (ingest.ocr_trust)
            write_grade(Path(kwargs["reflow_evidence"]), n, mode, f"{mode} words")


def merge(path, parts):
    out = pikepdf.new()
    for part in parts:
        with pikepdf.open(part) as pdf:
            out.pages.extend(pdf.pages)
    out.save(path)
    return path


TITLE = (72, 30, 24, "Committee Report Title", 0)
BODY = (72, 400, 12, "A line of the letter body", 0)


@pytest.fixture
def planned(tmp_path):
    """Page 1 unreadable-text/force, 2 no-text/redo, 3 a kept invisible layer (not in the plan),
    4 textless-band/redo with two bands."""
    parts = [image_pdf(tmp_path / "p1.pdf", [(72, 100, 12, "ab 12345 67890 #%& 1234 5678 99", 0)]),
             image_pdf(tmp_path / "p2.pdf", marks=[(72, 200, 540, 220)]),
             image_pdf(tmp_path / "p3.pdf", LAYER, marks=UNDER),
             image_pdf(tmp_path / "p4.pdf", [TITLE, BODY],
                       marks=[(72, 200, 540, 220), (72, 600, 540, 620)])]
    return merge(tmp_path / "planned.pdf", parts)


def prepare_with(monkeypatch, tmp_path, pdf, module=None, ocr="auto"):
    """reflow.prepare with OcrmypdfRunner over `module` (no runner when None); returns the Run
    and the working copy's bytes just before OCR."""
    from reflow import prepare as prepare_module
    from reflow.run import Options, Workers

    ocrmypdf_module(monkeypatch, module)
    monkeypatch.setattr(prepare_module, "make_runner",
                        lambda name: OcrmypdfRunner() if module is not None else None)
    before = {}
    real = prepare_module.ocr_pages

    def spy(working, plan, runner, *args):
        before["bytes"], before["plan"] = working.read_bytes(), plan
        return real(working, plan, runner, *args)

    monkeypatch.setattr(prepare_module, "ocr_pages", spy)
    stage = tmp_path / "stage"
    stage.mkdir()
    run = prepare_module.prepare(pdf, stage, Options(ocr=ocr), Workers.serial())
    return run, before


def page_facts(run, n):
    return [f for f in run.doc.pages[n - 1].facts]


def test_prepare_runs_force_then_redo_on_the_force_output(monkeypatch, tmp_path, planned):
    module = StampingModule()
    run, _ = prepare_with(monkeypatch, tmp_path, planned, module)
    ((src1, dst1, mode1, pages1), (src2, dst2, mode2, pages2),
     (src3, dst3, mode3, pages3)) = module.calls
    assert (mode1, pages1, mode2, pages2) == ("force", "1", "redo", "2,4")
    assert src2 == dst1 and dst1.suffix == ".tmp" and not dst1.exists()
    # Page 3's own layer is kept: a pass of its own reads it for its grades only.
    assert (mode3, pages3, dst3.suffix) == ("redo", "3", ".tmp") and not dst3.exists()
    assert run.ocr.plan.keep == (3,)
    assert "force words" in page_text(run.working, 1)
    assert "redo words" in page_text(run.working, 2)
    assert run.ocr.done == [1, 2, 4] and run.ocr.failed == {}
    expected = {1: ("unreadable-text", "force"), 2: ("no-text", "redo"),
                4: ("textless-band", "redo")}
    bands = [v for box in run.ocr.plan.pages[4].region_boxes for v in box]
    for n, (reason, mode) in expected.items():
        (fact,) = page_facts(run, n)
        assert (fact.code, fact.detail) == ("ocr-page", "text recognized by ocrmypdf")
        assert fact.data == {"reason": reason, "mode": mode,
                             **({"region_boxes": bands} if n == 4 else {})}
    assert page_facts(run, 3) == [] and "redo words" not in page_text(run.working, 3)


def test_a_failing_redo_pass_leaves_the_working_copy_and_flags_by_reason(monkeypatch, tmp_path,
                                                                        planned):
    run, before = prepare_with(monkeypatch, tmp_path, planned, StampingModule(fail="redo"))
    assert run.working.read_bytes() == before["bytes"]
    assert run.ocr.done == [] and "redo crashed" in run.ocr.failed[0]
    assert run.ocr.plan == before["plan"]
    codes = {n: [f.code for f in page_facts(run, n)] for n in (1, 2, 3, 4)}
    assert codes == {1: ["ocr-missing"], 2: ["ocr-missing"], 3: [], 4: ["ocr-band-missing"]}
    (band,) = page_facts(run, 4)
    assert band.data["region_boxes"] == [v for box in run.ocr.plan.pages[4].region_boxes
                                         for v in box]
    assert [f.code for f in run.doc.root.facts if f.code == "ocr-doc"] == ["ocr-doc"]
    assert sorted(p.name for p in run.working.parent.iterdir()
                  if p.suffix == ".tmp" or ".tmp" in p.name) == []


@pytest.mark.parametrize("module", [None, StampingModule(
    fail="force", error=type("MissingDependencyError", (Exception,), {})("no tesseract"))],
    ids=["no-runner", "unavailable"])
def test_without_ocr_only_textless_pages_are_errors_and_bands_keep_their_boxes(
        monkeypatch, tmp_path, planned, module):
    run, before = prepare_with(monkeypatch, tmp_path, planned, module)
    assert run.ocr.plan == before["plan"] and run.ocr.done == []
    assert run.working.read_bytes() == before["bytes"]
    assert [f.code for f in page_facts(run, 1)] == ["ocr-missing"]
    assert [f.code for f in page_facts(run, 2)] == ["ocr-missing"]
    assert page_facts(run, 3) == []   # the file's own layer is kept, as before
    (band,) = page_facts(run, 4)
    assert (band.code, CODES[band.code].severity) == ("ocr-band-missing", "warning")
    boxes = band.data["region_boxes"]
    assert len(boxes) == 8 and boxes == [v for box in run.ocr.plan.pages[4].region_boxes
                                         for v in box]
    # The page's bands: under the title down to the body line, and under it to the page foot.
    assert boxes[0] == 0 and boxes[2] == W and boxes[1] < 70 and 395 < boxes[3] < 403
    assert boxes[4] == 0 and boxes[6] == W and 412 < boxes[5] < 420 and boxes[7] == H


def test_an_empty_plan_calls_nothing_and_records_nothing(monkeypatch, tmp_path):
    module = StampingModule()
    pdf = make_pdf(tmp_path / "a.pdf", ["text", "text"])
    run, _ = prepare_with(monkeypatch, tmp_path, pdf, module)
    assert module.calls == [] and run.ocr.plan.pages == {}
    assert all(p.facts == [] for p in run.doc.pages) and run.doc.root.facts == []


def test_a_photo_under_a_title_is_redone_and_keeps_its_title(monkeypatch, tmp_path):
    module = StampingModule()
    pdf = image_pdf(tmp_path / "a.pdf", [(72, 20, 24, "Annual Report Title", 0)], photo=True)
    run, _ = prepare_with(monkeypatch, tmp_path, pdf, module)
    assert [(mode, pages) for _, _, mode, pages in module.calls] == [("redo", "1")]
    text = page_text(run.working, 1)
    assert "Annual Report Title" in text and "redo words" in text


# --------------------------------------------------------------------------- render order

class _Page:
    def __init__(self, pageno):
        self.pageno = pageno


def test_the_grafter_renders_the_text_layers_in_page_order(monkeypatch):
    """OCRmyPDF hands its fpdf2 renderer the pages in the order their OCR finished, and the
    renderer's shared font numbers glyphs in the order it draws them, so the output's bytes
    followed the workers' timing. The plugin sorts the pages before the grafter renders."""
    import sys
    import types

    seen = []

    class OcrGrafter:
        def __init__(self):
            self.fpdf2_hocr_pages = [_Page(2), _Page(0), _Page(1)]
            self.fpdf2_parsed_pages = []

        def finalize(self):
            seen.append([p.pageno for p in self.fpdf2_hocr_pages])
            return "out.pdf"

    graft = types.ModuleType("ocrmypdf._graft")
    graft.OcrGrafter = OcrGrafter
    monkeypatch.setitem(sys.modules, "ocrmypdf", types.ModuleType("ocrmypdf"))
    monkeypatch.setitem(sys.modules, "ocrmypdf._graft", graft)
    ocr_trust._graft_in_page_order()
    ocr_trust._graft_in_page_order()    # idempotent: wraps once
    assert OcrGrafter().finalize() == "out.pdf"
    assert seen == [[0, 1, 2]]
    assert OcrGrafter.finalize.__closure__ is not None
    assert [p.pageno for p in ocr_trust.in_page_order([_Page(1), _Page(0)])] == [0, 1]


def test_the_page_order_patch_is_a_no_op_without_ocrmypdf(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "ocrmypdf._graft", None)   # ImportError on import
    ocr_trust._graft_in_page_order()
