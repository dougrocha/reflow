"""The text-trust preflight (reflow.ingest.text_trust) and its witness (reflow.ingest.witness):
a page whose text layer looks wrong is read again by OCR, and where the two disagree the page
gets text-disputed; the transcript keeps the layer's text."""

from __future__ import annotations

import json
import threading
from concurrent.futures import Future
from pathlib import Path

import pikepdf
import pytest
from conftest import make_text_pdf
from testkit import shifted_pdf, walk_json

from reflow.analyze.confidence import CODES
from reflow.convert import convert
from reflow.fontcensus import FontCensus, font_census
from reflow.ingest import font_map, text_trust, witness
from reflow.ingest.text_trust import (
    CensusChar,
    LayerLine,
    MissingReport,
    TrustReport,
    Unavailable,
    WhyNot,
    compare,
    failed,
    judge_page,
)
from reflow.ingest.witness import WitnessWord
from reflow.names import DOCUMENT_JSON, OCR_WORDS

PROSE = [[
    (72, 72, 18, "Annual office report", "B"),
    (72, 110, 11, "The staff filed five official files after the final fit check of the year."),
    (72, 126, 11, "Our office flew the flag of the field team in the fall, as it does often."),
    (72, 142, 11, "A brief note of thanks for the effort of everyone who offered to help."),
    (72, 158, 11, "The board met in the spring and voted to keep the hours the same for now."),
    (72, 174, 11, "Each member of the public may ask for a copy of the minutes at the desk."),
    (72, 210, 11, "Each fluent reader of this file will find it clear and in the right order."),
]]
needs_tesseract = pytest.mark.skipif(not witness.available() or witness.languages("") is None,
                                     reason="Tesseract with English data is not installed")


def chars(text: str, font: str = "Helvetica", error: bool = False) -> list[CensusChar]:
    return [CensusChar(ch, font, error, (float(i), 0.0, i + 1.0, 10.0)) for i, ch in enumerate(text)]


def shift(text: str) -> str:
    return "".join(chr((ord(c) - 97 + 1) % 26 + 97) if "a" <= c <= "z" else
                   chr((ord(c) - 65 + 1) % 26 + 65) if "A" <= c <= "Z" else c for c in text)


ENGLISH = " ".join(line[3] for line in PROSE[0]) * 2


# --------------------------------------------------------------------------- the judge

def test_clean_english_is_trusted():
    assert judge_page(chars(ENGLISH)).verdict == "trust"


def test_a_shifted_alphabet_is_suspect_by_its_font_and_its_lost_common_words_corroborate():
    tokens, letters, vowels, common = text_trust.latin_stats(shift(ENGLISH))
    assert 0.2 < vowels < 0.3 and common < text_trust.COMMON_WORDS   # about Docling's 25% line
    census = FontCensus(remapped={"Helvetica": [1]})
    judged = judge_page(chars(shift(ENGLISH)), census=census, page=1)
    assert judged.suspect and any("common words" in r for r in judged.reasons)


def test_few_common_words_alone_do_not_make_a_page_suspect():
    directory = "Smith Jordan Maria Phoenix Scottsdale Mesa Tempe Gilbert Chandler Peoria " * 6
    assert judge_page(chars(directory)).verdict == "trust"


def test_a_legacy_font_and_private_use_text_are_gibberish():
    assert judge_page(chars("Diné bizaad", "TimesNewRomanNavajo")).verdict == "gibberish"
    pua = chars(ENGLISH[:200]) + chars("")
    assert judge_page(pua).verdict == "gibberish"


def test_pdfiums_soft_hyphen_mark_is_not_a_control_character():
    assert judge_page(chars(ENGLISH.replace("office", "of\x02fice"))).verdict == "trust"


def test_a_page_in_a_language_the_latin_rules_do_not_read_is_at_most_doubt():
    assert judge_page(chars(shift(ENGLISH)), "ja").verdict == "trust"
    assert judge_page(chars("Diné bizaad", "TimesNewRomanNavajo"), "ja").verdict == "doubt"


def test_cjk_letters_are_not_consonants_of_latin_text():
    assert judge_page(chars("基本医疗保险 " * 80 + ENGLISH[:100]), "en-US").verdict == "trust"


def test_a_remapped_font_makes_its_page_doubt_and_a_flagged_tex_font_alone_does_not():
    remapped = FontCensus(remapped={"ABCDEF+Courier": [1]})
    assert judge_page(chars("chapter one", "ABCDEF+Courier"), census=remapped, page=1).verdict == "doubt"
    assert judge_page(chars("chapter one", "ABCDEF+Courier"), census=remapped, page=2).verdict == "trust"
    tex = FontCensus(no_unicode={"CMR10": [1]})
    assert judge_page(chars(ENGLISH, "CMR10"), census=tex, page=1).verdict == "trust"


def test_unmapped_characters_make_doubt():
    judged = judge_page(chars(ENGLISH[:100]) + chars("x" * 5, error=True))
    assert judged.verdict == "doubt" and "unmapped" in judged.reasons[0]


# --------------------------------------------------------------------------- the comparison

def _line(text: str, top: float) -> LayerLine:
    return LayerLine(text, (72.0, top, 400.0, top + 11), ())


def test_a_line_the_witness_reads_otherwise_is_disputed_and_one_it_skipped_is_not():
    lines = [_line("Gpsn W-9 Sfrvftu gps Ubyqbzfs", 72), _line("Request for Taxpayer", 100),
             _line("unread by the witness here", 130)]
    words = [WitnessWord(w, (80 + 40 * i, 73, 110 + 40 * i, 82), 90) for i, w in
             enumerate("Form W-9 Request for Taxpayer".split())]
    words += [WitnessWord(w, (80 + 40 * i, 101, 110 + 40 * i, 110), 90) for i, w in
              enumerate("Request for Taxpayer".split())]
    disputed, bad, seen = compare(lines, words)
    assert disputed == [lines[0].box]
    assert bad == len("gpsnw9sfrvftugpsubyqbzfs") and seen == bad + len("requestfortaxpayer")


def test_a_line_of_private_use_characters_is_disputed():
    # A /ToUnicode into the Private Use Area (#342) leaves the layer no letter: its private-use
    # characters stand in for them, so the witness's letters can dispute the line.
    pua = "".join(chr(0xE000 + ord(c)) if c.isalpha() else c for c in "Request for Taxpayer")
    lines = [_line(pua, 72), _line("Request for Taxpayer", 100)]
    words = [WitnessWord(w, (80 + 40 * i, top + 1, 110 + 40 * i, top + 10), 90)
             for top in (72, 100) for i, w in enumerate("Request for Taxpayer".split())]
    disputed, bad, seen = compare(lines, words)
    assert disputed == [lines[0].box]
    assert bad == len("requestfortaxpayer") and seen == 2 * bad
    assert judge_page(chars(pua)).verdict == "gibberish"


# --------------------------------------------------------------------------- whole conversions

def _doc(out: Path) -> dict:
    return json.loads((out / DOCUMENT_JSON).read_text(encoding="utf-8"))


def _page_facts(out: Path, code: str) -> list[dict]:
    return [f for p in _doc(out)["pages"] for f in p.get("facts", []) if f["code"] == code]


def _text(out: Path) -> str:
    return " ".join(n.get("text", "") for n in walk_json(_doc(out)["root"]))


def test_the_codes_are_registered_at_weight_zero_on_pages():
    for code in text_trust.CODES:
        assert CODES[code].weight == 0.0 and CODES[code].scope == "page"


def test_a_clean_page_starts_no_tesseract_and_gets_no_fact(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("a clean page was witnessed")
    monkeypatch.setattr(witness, "read_pages", refuse)
    out = tmp_path / "out"
    convert(make_text_pdf(tmp_path / "a.pdf", PROSE), out, page_images=False)
    assert not _page_facts(out, "text-disputed") and not _page_facts(out, "text-witness-unavailable")


@needs_tesseract
def test_a_shifted_tounicode_is_disputed_over_its_lines_and_the_transcript_keeps_it(
        tmp_path, monkeypatch):
    # the preflight alone: the font repair (ingest.font_map, tests/test_font_map.py) is off
    monkeypatch.setattr(font_map, "repair", lambda *_a, **_k: font_map.Repair())
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    damaged = shifted_pdf(clean, tmp_path / "shifted.pdf")
    out = tmp_path / "out"
    convert(damaged, out, page_images=False)
    [fact] = _page_facts(out, "text-disputed")
    boxes = fact["data"]["region_boxes"]
    assert len(boxes) % 4 == 0 and len(boxes) >= 4 * 5   # the shifted lines, each its own box
    assert "Bmm" in _text(out) or "Uif" in _text(out)     # "All" / "The": the layer's text is kept
    assert not (out / OCR_WORDS).exists()                 # the witness never writes the OCR sidecar


@needs_tesseract
def test_a_suspect_page_without_a_model_for_its_language_is_unavailable(tmp_path):
    src = make_text_pdf(tmp_path / "a.pdf", [[(72, 72, 11, "Diné bizaad " * 6)]])
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        fonts = pdf.pages[0].Resources.Font
        fonts.FR.BaseFont = pikepdf.Name("/TimesNewRomanNavajo")
        pdf.Root.Lang = pikepdf.String("ja")
        pdf.save(src)
    out = tmp_path / "out"
    convert(src, out, page_images=False)
    [fact] = _page_facts(out, "text-witness-unavailable")
    assert "language" in fact["detail"] and fact["data"]["verdict"] == "doubt"


def test_with_ocr_off_a_suspect_page_is_unavailable_not_trusted(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("--ocr none ran the witness")
    monkeypatch.setattr(witness, "read_pages", refuse)
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    out = tmp_path / "out"
    convert(shifted_pdf(clean, tmp_path / "shifted.pdf"), out, page_images=False, ocr="none")
    [fact] = _page_facts(out, "text-witness-unavailable")
    assert "--ocr none" in fact["detail"]
    assert not _page_facts(out, "text-disputed")


def test_a_failed_witness_is_unavailable_and_its_model_is_not_kept(tmp_path, monkeypatch):
    def broken(working, number, langs, read=None):
        return witness.WitnessPage(number, error="RuntimeError: tesseract exited 1")
    monkeypatch.setattr(witness, "witness_page", broken)
    monkeypatch.setattr(witness, "available", lambda: True)
    monkeypatch.setattr(witness, "languages", lambda lang: ("eng",))
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    out = tmp_path / "out"
    summary = convert(shifted_pdf(clean, tmp_path / "shifted.pdf"), out, page_images=False,
                      stage_cache=tmp_path / "root")
    [fact] = _page_facts(out, "text-witness-unavailable")
    assert "OCR failed" in fact["detail"]
    assert summary.model.startswith("built, not kept: the text witness failed on pages [1]")


def test_a_page_is_failed_by_the_code_of_its_reason_not_by_its_words():
    # the prose each code says, which the facts print
    assert str(Unavailable(WhyNot.OCR_OFF)) == "OCR is off (--ocr none)"
    assert str(Unavailable(WhyNot.NO_TESSERACT)) == "Tesseract is not installed"
    assert str(Unavailable(WhyNot.NO_MODEL, "ja")) == "no OCR model here for the document's language (ja)"
    assert str(Unavailable(WhyNot.OCR_FAILED, "RuntimeError: x")) == "OCR failed: RuntimeError: x"
    assert str(Unavailable(WhyNot.UNREAD)) == text_trust.UNREAD
    for report in (TrustReport(), MissingReport()):
        report.unavailable = {1: Unavailable(WhyNot.OCR_FAILED, "no words in this one"),
                              2: Unavailable(WhyNot.UNREAD), 3: Unavailable(WhyNot.OCR_OFF),
                              4: Unavailable(WhyNot.NO_MODEL, "OCR failed"),
                              5: Unavailable(WhyNot.OCR_FAILED, "OCR failed: again")}
        assert failed(report) == [1, 5]


def test_a_witness_that_reads_none_of_the_lines_is_unavailable_and_its_model_is_kept(
        tmp_path, monkeypatch):
    def blank(working, number, langs, read=None):   # Tesseract ran and read nothing it trusts
        return witness.WitnessPage(number)
    monkeypatch.setattr(witness, "witness_page", blank)
    monkeypatch.setattr(witness, "available", lambda: True)
    monkeypatch.setattr(witness, "languages", lambda lang: ("eng",))
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    out = tmp_path / "out"
    summary = convert(shifted_pdf(clean, tmp_path / "shifted.pdf"), out, page_images=False,
                      stage_cache=tmp_path / "root")
    [fact] = _page_facts(out, "text-witness-unavailable")
    assert text_trust.UNREAD in fact["detail"]
    assert not _page_facts(out, "text-disputed")
    assert "not kept" not in summary.model   # the same image reads the same way again


@needs_tesseract
def test_a_sideways_shifted_page_is_never_silently_trusted(tmp_path):
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    damaged = shifted_pdf(clean, tmp_path / "shifted.pdf")
    with pikepdf.open(damaged, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Rotate = 90   # its lines now run down the displayed page
        pdf.save(damaged)
    out = tmp_path / "out"
    convert(damaged, out, page_images=False)
    decoded = [f for f in _doc(out)["root"].get("facts", []) if f["code"] == "font-map"]
    assert (_page_facts(out, "text-disputed") or _page_facts(out, "text-witness-unavailable")
            or decoded)   # the witness read it sideways, and the font repair decoded the font


def test_a_shifted_font_a_form_xobject_draws_is_remapped(tmp_path):
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    damaged = shifted_pdf(clean, tmp_path / "shifted.pdf")
    nested = tmp_path / "nested.pdf"
    with pikepdf.open(damaged) as src, pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=tuple(float(v) for v in src.pages[0].mediabox[2:]))
        pdf.pages[0].add_overlay(pdf.copy_foreign(src.pages[0].as_form_xobject()))
        pdf.save(nested)
    with pikepdf.open(nested) as pdf:
        assert "/Font" not in pdf.pages[0].Resources       # its only font is the form's
        census = font_census(pdf)
    assert census.remapped_on(1) and not census.not_embedded and not census.no_unicode


def test_one_suspect_page_is_read_on_the_pool_and_a_serial_read_holds_the_pdfium_lock(tmp_path):
    submitted = []

    class Recording:
        def submit(self, fn, *args):
            submitted.append(args[1])
            future = Future()
            future.set_result(fn(*args))
            return future

    def read(working, number, langs):
        return witness.WitnessPage(number, (WitnessWord("held" if lock.locked() else "free",
                                                        (0, 0, 1, 1), 99.0),))
    lock = threading.Lock()
    got = witness.read_pages(tmp_path / "a.pdf", {3: ("eng",)}, executor=Recording(), read=read,
                             lock=lock)
    assert submitted == [3] and got[3].words[0].text == "free"
    got = witness.read_pages(tmp_path / "a.pdf", {3: ("eng",), 1: ("eng",)}, read=read, lock=lock)
    assert list(got) == [1, 3] and {w.text for p in got.values() for w in p.words} == {"held"}


@needs_tesseract
def test_the_witness_reads_each_page_once_through_the_stage_cache(tmp_path):
    clean = make_text_pdf(tmp_path / "clean.pdf", PROSE)
    damaged = shifted_pdf(clean, tmp_path / "shifted.pdf")
    root = tmp_path / "root"
    convert(damaged, tmp_path / "a", page_images=False, stage_cache=root, reuse_model=False)
    def entries() -> list[Path]:
        return [p for p in (root / witness.STAGE).glob("*/*") if not p.name.startswith(".")]
    [entry] = entries()
    before = entry.stat().st_mtime_ns
    convert(damaged, tmp_path / "b", page_images=False, stage_cache=root, reuse_model=False)
    assert entries() == [entry]
    assert _page_facts(tmp_path / "a", "text-disputed") == _page_facts(tmp_path / "b", "text-disputed")
    assert entry.stat().st_mtime_ns >= before


def test_tesseract_tsv_becomes_words_in_points():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t150\t300\t75\t30\t96.5\tForm\n"
           "5\t1\t1\t1\t1\t2\t250\t300\t75\t30\t12.0\tnoise\n"
           "4\t1\t1\t1\t1\t0\t150\t300\t175\t30\t-1\t\n")
    [word] = witness.parse_tsv(tsv, 150 / 72)
    assert word.text == "Form" and word.box == (72.0, 144.0, 108.0, 158.4)


def test_the_witness_module_reaches_neither_the_trust_module_nor_replacement_ocr():
    from reflow import stagecache
    files = {p.name for p in stagecache.closure(stagecache.STAGES["witness"])}
    assert "text_trust.py" not in files and "ocr.py" not in files and "ocrwords.py" not in files


def test_the_witness_identity_is_in_the_model_key(monkeypatch):
    from reflow import saved
    from reflow.run import Options
    pdf = Path(__file__).resolve().parent.parent / "samples" / "fw9.pdf"
    before = saved.identify(pdf, Options())
    monkeypatch.setattr(text_trust, "POLICY", text_trust.POLICY + 1)
    after = saved.identify(pdf, Options())
    assert before.model != after.model and after.inputs["witness"]["policy"] == text_trust.POLICY

