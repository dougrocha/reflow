"""Words printed on a born-digital page that never reach its transcript (text_trust.flag_missing,
#310): OCR of the ink the visible text layer does not draw, counted against the transcript, gives
the page "words-missing" with the boxes of the missing words."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import threading
from pathlib import Path

import pikepdf
import pytest
from conftest import make_text_pdf

from reflow.analyze import page_integrity
from reflow.analyze.confidence import CODES
from reflow.convert import convert
from reflow.ingest import witness
from reflow.ingest.text_trust import FACT_MISSING, TrustReport
from reflow.ingest.text_trust import flag_missing as flag
from reflow.ingest.witness import WitnessWord
from reflow.model import Document, Node, PageInfo, Role
from reflow.names import DOCUMENT_JSON, REVIEW_JSON

needs_tesseract = pytest.mark.skipif(not witness.available() or witness.languages("") is None,
                                     reason="Tesseract with English data is not installed")
needs_gs = pytest.mark.skipif(shutil.which("gs") is None, reason="outlines are drawn by Ghostscript")

PARAGRAPH = [
    (72, 140, 11, "The staff filed five official files after the final fit check of the year."),
    (72, 156, 11, "Our office flew the flag of the field team in the fall, as it does often."),
    (72, 172, 11, "A brief note of thanks for the effort of everyone who offered to help."),
]
PARAGRAPH_BOX = (72.0, 140.0, 460.0, 186.0)
LOCK = threading.RLock()


def flag_missing(*args, **kw):
    """text_trust.flag_missing with the figure rule the pipeline gives it."""
    return flag(*args, covers=page_integrity.covers_words, **kw)


def outlined(tmp_path: Path, text: list[tuple], drawn: list[tuple], name: str = "page") -> Path:
    """One Letter page with `text` as real text and `drawn` (make_text_pdf lines) as outlines
    over it: Ghostscript's -dNoOutputFonts, overlaid as a form XObject of paths."""
    base = make_text_pdf(tmp_path / f"{name}-text.pdf", [text])
    glyphs = make_text_pdf(tmp_path / f"{name}-glyphs.pdf", [drawn])
    paths = tmp_path / f"{name}-paths.pdf"
    subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                    "-dNoOutputFonts", f"-sOutputFile={paths}", str(glyphs)], check=True)
    out = tmp_path / f"{name}.pdf"
    with pikepdf.open(base) as pdf, pikepdf.open(paths) as over:
        pdf.pages[0].add_overlay(over.pages[0])
        pdf.save(out)
    return out


def document(*nodes: Node) -> Document:
    return Document(root=Node(Role.DOC, children=list(nodes)), pages=[PageInfo(1, 612, 792)])


def paragraph() -> Node:
    return Node(Role.P, page=1, bbox=PARAGRAPH_BOX, text=" ".join(t for *_, t in PARAGRAPH))


def facts(doc: Document) -> list:
    return [f for f in doc.pages[0].facts if f.code == FACT_MISSING]


def boxes(fact) -> list[tuple[float, ...]]:
    flat = fact.data["region_boxes"]
    return [tuple(flat[i:i + 4]) for i in range(0, len(flat), 4)]


def overlaps(a, b) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def test_the_code_is_an_error_of_weight_zero_on_pages():
    spec = CODES[FACT_MISSING]
    assert (spec.weight, spec.severity, spec.scope) == (0.0, "error", "page")


@needs_tesseract
@needs_gs
def test_an_outlined_heading_over_a_good_paragraph_is_missing_at_the_heading(tmp_path):
    pdf = outlined(tmp_path, PARAGRAPH, [(72, 72, 24, "Quarterly Harbour Survey", "B")])
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    doc = json.loads((out / DOCUMENT_JSON).read_text())
    [fact] = [f for f in doc["pages"][0]["facts"] if f["code"] == FACT_MISSING]
    assert {"Quarterly", "Harbour", "Survey"} <= set(fact["data"]["words"])
    [box] = [tuple(fact["data"]["region_boxes"][i:i + 4])
             for i in range(0, len(fact["data"]["region_boxes"]), 4)]
    assert 60 < box[1] < box[3] < 110 and not overlaps(box, PARAGRAPH_BOX)


@needs_tesseract
@needs_gs
def test_a_described_figure_covers_its_words_and_a_placeholder_alt_does_not(tmp_path):
    pdf = outlined(tmp_path, PARAGRAPH, [(100, 300, 20, "Harbour traffic doubled"),
                                         (100, 330, 20, "between spring and autumn")])
    figure_box = (90.0, 290.0, 400.0, 360.0)
    described = Node(Role.FIGURE, page=1, bbox=figure_box,
                     attrs={"alt": "Chart: harbour traffic doubled from spring to autumn"})
    doc = document(paragraph(), described)
    report = flag_missing(doc, pdf, None, lock=LOCK)
    assert report.assessed == [1] and report.figures == {1: 1} and not facts(doc)
    # A placeholder alt hides nothing: a fallback, a generic word, a decorative "".
    for attrs in ({}, {"alt": "image"}, {"alt": "", "decorative": True}):
        placeholder = Node(Role.FIGURE, page=1, bbox=figure_box, attrs=attrs)
        doc = document(paragraph(), placeholder)
        flag_missing(doc, pdf, None, lock=LOCK)
        [fact] = facts(doc)
        assert {"Harbour", "traffic", "doubled", "autumn"} <= set(fact.data["words"]), attrs
        assert all(overlaps(b, figure_box) for b in boxes(fact))
    # Unless a finding already sends a reviewer to the figure: alt-missing is the figure's error.
    from reflow.model import Fact, Source
    questioned = Node(Role.FIGURE, page=1, bbox=figure_box,
                      facts=[Fact("alt-missing", Source.HEURISTIC, "no alt")])
    doc = document(paragraph(), questioned)
    assert flag_missing(doc, pdf, None, lock=LOCK).figures == {1: 1} and not facts(doc)


@needs_tesseract
@needs_gs
def test_each_missing_occurrence_of_a_repeated_number_is_found(tmp_path):
    text = [*PARAGRAPH, (72, 400, 12, "Total due: 100")]
    pdf = outlined(tmp_path, text, [(72, 440, 12, "Fee: 100"), (72, 470, 12, "Tax: 100"),
                                    (72, 500, 12, "Levy: 100")])
    total = Node(Role.P, page=1, bbox=(72.0, 400.0, 160.0, 414.0), text="Total due: 100")
    doc = document(paragraph(), total)
    flag_missing(doc, pdf, None, lock=LOCK)
    [fact] = facts(doc)
    # the surviving "100" of the layer covers none of the three drawn ones
    assert sum(w == "100" for w in fact.data["words"]) == 3
    # a transcript that holds one "100" more than the layer (as OCR'd text would) covers one
    doc = document(paragraph(), Node(Role.P, page=1, bbox=total.bbox, text="Total due: 100 100"))
    flag_missing(doc, pdf, None, lock=LOCK)
    [fact] = facts(doc)
    assert sum(w == "100" for w in fact.data["words"]) == 2



@needs_tesseract
@needs_gs
@pytest.mark.parametrize("size", [8, 10])
def test_a_lone_two_digit_number_left_out_passes_the_ink_gate(tmp_path, size):
    text = [*PARAGRAPH, (72, 400, 12, "Total due: 12")]
    pdf = outlined(tmp_path, text, [(72, 440, size, "12"), (300, 470, size, "12")])
    total = Node(Role.P, page=1, bbox=(72.0, 400.0, 160.0, 414.0), text="Total due: 12")
    doc = document(paragraph(), total)
    flag_missing(doc, pdf, None, lock=LOCK)
    [fact] = facts(doc)
    assert fact.data["words"] == ["12", "12"]

def test_a_witnessed_page_reports_the_words_outside_its_layer_lines(tmp_path, monkeypatch):
    """The preflight's whole-page witness is used as is: no second OCR. Its words over the
    layer's words are the comparison's business (text-disputed), not this pass's; a word over a
    described figure is counted apart."""
    monkeypatch.setattr(witness, "read_image", lambda *a, **k: pytest.fail("OCR ran"))
    monkeypatch.setattr(witness, "available", lambda: True)
    monkeypatch.setattr(witness, "languages", lambda lang: ("eng",))
    pdf = make_text_pdf(tmp_path / "a.pdf", [PARAGRAPH])
    trust = TrustReport(witnessed={1: (
        WitnessWord("staff", (100.0, 140.0, 125.0, 152.0), 96.0),          # over the layer
        WitnessWord("Harbour", (72.0, 300.0, 130.0, 316.0), 95.0),         # outside it
        WitnessWord("Survey", (136.0, 300.0, 190.0, 316.0), 92.0),
        WitnessWord("faint", (200.0, 300.0, 230.0, 316.0), 41.0),          # not confident
        WitnessWord("Chart", (72.0, 500.0, 120.0, 516.0), 95.0))})         # in a described figure
    figure = Node(Role.FIGURE, page=1, bbox=(60.0, 490.0, 300.0, 560.0),
                  attrs={"alt": "Chart of the harbour's monthly traffic"})
    doc = document(paragraph(), figure)
    report = flag_missing(doc, pdf, trust, lock=LOCK)
    [fact] = facts(doc)
    assert fact.data["words"] == ["Harbour", "Survey"] and fact.data["count"] == 2
    assert boxes(fact) == [(72.0, 300.0, 190.0, 316.0)]
    assert report.figures == {1: 1} and fact.data["figures_covered"] == 1


@needs_tesseract
@needs_gs
def test_running_twice_replaces_the_pass_s_own_fact(tmp_path):
    pdf = outlined(tmp_path, PARAGRAPH, [(72, 72, 24, "Quarterly Harbour Survey", "B")])
    doc = document(paragraph())
    flag_missing(doc, pdf, None, lock=LOCK)
    first = facts(doc)
    flag_missing(doc, pdf, None, lock=LOCK)
    assert facts(doc) == first and len(first) == 1


@needs_gs
def test_an_empty_inked_page_is_empty_transcript_and_not_words_missing(tmp_path, monkeypatch):
    pdf = outlined(tmp_path, [], [(72, 72, 24, "Quarterly Harbour Survey", "B")])
    monkeypatch.setattr(witness, "read_image", lambda *a, **k: pytest.fail("OCR ran"))
    doc = document()
    report = flag_missing(doc, pdf, None, lock=LOCK)
    page_integrity.flag_empty(doc, pdf)
    assert [f.code for f in doc.pages[0].facts] == ["empty-transcript"] and not report.assessed


def test_a_clean_page_starts_no_tesseract(tmp_path, monkeypatch):
    monkeypatch.setattr(witness, "read_image", lambda *a, **k: pytest.fail("OCR ran"))
    monkeypatch.setattr(witness, "available", lambda: True)
    monkeypatch.setattr(witness, "languages", lambda lang: ("eng",))
    pdf = make_text_pdf(tmp_path / "a.pdf", [PARAGRAPH])
    doc = document(paragraph())
    assert flag_missing(doc, pdf, None, lock=LOCK).assessed == [1] and not facts(doc)


def test_pages_reflow_ocrd_and_runs_without_ocr_are_not_assessed(tmp_path, monkeypatch):
    monkeypatch.setattr(witness, "read_image", lambda *a, **k: pytest.fail("OCR ran"))
    pdf = make_text_pdf(tmp_path / "a.pdf", [PARAGRAPH])
    report = flag_missing(document(paragraph()), pdf, None, lock=LOCK, skip={1})
    assert not report.assessed and not report.unavailable
    report = flag_missing(document(paragraph()), pdf, None, lock=LOCK, enabled=False)
    assert not report.assessed and report.unavailable[1].code == "ocr-off"
    assert "--ocr none" in str(report.unavailable[1])


@needs_tesseract
@pytest.mark.parametrize("kind", ["white-text", "tiny-text", "hidden-text"])
def test_words_no_reader_can_see_raise_nothing(tmp_path, kind):
    """#336 leaves invisible text out of the transcript on purpose: it is no missing word."""
    from robustness.kinds import KINDS
    [damage] = [k for k in KINDS if k.id == kind]
    clean = make_text_pdf(tmp_path / "clean.pdf", [PARAGRAPH])
    damaged = tmp_path / "damaged.pdf"
    damage.transform(clean, damaged, random.Random(310))
    doc = document(paragraph())
    flag_missing(doc, damaged, None, lock=LOCK)
    assert not facts(doc)


def annotated(tmp_path: Path) -> Path:
    """The robustness kind text-in-annotation on a prose page: its heading is drawn by a stamp
    annotation's appearance, which no text reader reads."""
    from robustness.kinds import KINDS
    [kind] = [k for k in KINDS if k.id == "text-in-annotation"]
    clean = make_text_pdf(tmp_path / "clean.pdf", [[(72, 72, 24, "Quarterly Harbour Survey", "B"),
                                                    *PARAGRAPH]])
    kind.transform(clean, tmp_path / "annotated.pdf", random.Random(310))
    return tmp_path / "annotated.pdf"


@needs_tesseract
def test_fresh_and_reused_conversions_keep_the_fact_and_its_crop(tmp_path, monkeypatch):
    from testkit import no_build
    pdf = annotated(tmp_path)
    convert(pdf, tmp_path / "uncached", page_images=False)
    review = json.loads((tmp_path / "uncached" / REVIEW_JSON).read_text())
    [item] = [i for i in review if any(t["code"] == FACT_MISSING for t in i.get("targets", []))]
    assert item["severity"] == "error" and item["page"] == 1
    [target] = [t for t in item["targets"] if t["code"] == FACT_MISSING]
    assert target["box"][1] < 110 and not overlaps(target["box"], PARAGRAPH_BOX)   # the heading
    root = tmp_path / "root"
    assert convert(pdf, tmp_path / "kept", page_images=False, stage_cache=root).model == "built, kept"
    no_build(monkeypatch)
    summary = convert(pdf, tmp_path / "reused", page_images=False, stage_cache=root)
    assert summary.model == "stage cache"
    for name in (DOCUMENT_JSON, REVIEW_JSON):
        assert (tmp_path / "reused" / name).read_bytes() == (tmp_path / "uncached" / name).read_bytes()



@needs_tesseract
def test_a_page_whose_ocr_failed_is_not_kept_in_a_saved_model(tmp_path, monkeypatch):
    read = witness.read_image
    def broken(image, langs, scale=witness.SCALE, sparse=False):
        if sparse:   # flag_missing's read, not the preflight's
            raise RuntimeError("tesseract exited 1")
        return read(image, langs, scale)
    monkeypatch.setattr(witness, "read_image", broken)
    summary = convert(annotated(tmp_path), tmp_path / "out", page_images=False,
                      stage_cache=tmp_path / "root")
    assert summary.model.startswith("built, not kept: the text witness failed on pages [1]")

def test_a_word_is_three_letters_or_digits_or_a_two_digit_number():
    from reflow.ingest.text_trust import _word
    assert [_word(t) for t in ("100", "12", "Fee:", "U.S.A.", "ol", "SS", "™", "..I—l", "W=9")] \
        == [True, True, True, False, False, False, False, False, False]


def test_a_short_token_set_tall_is_a_word():
    # fw9's title "W-9" drawn by an annotation: OCR reads "W=9", 18 pt tall (#342). The pieces
    # OCR makes of rules, boxes and cut edges are a glyph's height, and a checkbox is one "O".
    from reflow.ingest.text_trust import _tall
    assert _tall(WitnessWord("W=9", (56, 36, 96, 54), 80))
    assert not _tall(WitnessWord("W=9", (56, 36, 76, 45), 80))     # 9 pt: body text's "W-9"
    assert not _tall(WitnessWord("O", (324, 180, 332, 198), 85))   # one letter or digit
    assert not _tall(WitnessWord("|—", (10, 10, 20, 60), 85))      # no letter or digit


def test_a_word_missing_at_the_same_place_on_two_pages_is_a_running_mark(tmp_path, monkeypatch):
    monkeypatch.setattr(witness, "available", lambda: True)
    monkeypatch.setattr(witness, "languages", lambda lang: ("eng",))
    pdf = make_text_pdf(tmp_path / "a.pdf", [PARAGRAPH, PARAGRAPH, PARAGRAPH])
    logo = WitnessWord("Harbourside", (400.0, 30.0, 480.0, 44.0), 95.0)
    moved = WitnessWord("Harbourside", (400.0, 300.0, 480.0, 314.0), 95.0)
    trust = TrustReport(witnessed={1: (logo,), 2: (logo,), 3: (moved,)})
    doc = Document(root=Node(Role.DOC, children=[
        Node(Role.P, page=n, bbox=PARAGRAPH_BOX, text=paragraph().text) for n in (1, 2, 3)]),
        pages=[PageInfo(n, 612, 792) for n in (1, 2, 3)])
    flag_missing(doc, pdf, trust, lock=LOCK)
    assert [[f.data["words"] for f in p.facts if f.code == FACT_MISSING] for p in doc.pages] \
        == [[], [], [["Harbourside"]]]
