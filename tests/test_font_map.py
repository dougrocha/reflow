"""The font repair (reflow.ingest.font_map, #309): a font whose /ToUnicode reads its letters as
other letters is decoded from the text witness's votes, in the working copy's /ToUnicode, so every
reader sees one decode; the record keeps the layer's reading and a reviewer's reject puts it back.

The conversions here read a deterministic witness, the clean PDF's own words (`clean_witness`),
so they need no Tesseract; the robustness harness runs the real one (tests/robustness)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pdfplumber
import pikepdf
import pytest
from conftest import make_pdf, make_text_pdf
from decisions_kit import append, mint_id, new_store
from robustness.kinds import _cmap, save, shift_letter, unicode_map
from testkit import no_build, ocr_available, walk_json

from reflow.analyze import decisions_apply
from reflow.analyze.confidence import CODES
from reflow.analyze.decisions import read as read_store
from reflow.convert import convert
from reflow.identity import file_sha256
from reflow.ingest import font_map, text_trust, witness
from reflow.ingest.font_map import Ballots, decide
from reflow.ingest.pages import read_source
from reflow.ingest.text_trust import FACT_DISPUTED, Judgment, TrustReport
from reflow.ingest.witness import WitnessPage, WitnessWord
from reflow.model import Document, Fact, Source
from reflow.names import DOCUMENT_JSON, REVIEW_JSON

PROSE = [
    (72, 72, 11, "The staff filed five official files after the final fit check of the year."),
    (72, 90, 11, "Our office flew the flag of the field team in the fall, as it does often."),
    (72, 108, 11, "A brief note of thanks for the effort of everyone who offered to help."),
    (72, 126, 11, "The board met in the spring and voted to keep the hours the same for now."),
    (72, 144, 11, "Each member of the public may ask for a copy of the minutes at the desk."),
    (72, 162, 11, "This report was printed on time and mailed to every house on the road."),
    (72, 180, 11, "They paid the old bills and moved the extra funds into a quiet savings jar."),
    (72, 198, 11, "Then the clerk wrote a short summary of the work done during the season."),
]
PAGE_TWO = [(72, 72, 11, "the office report was filed on time")]


# --------------------------------------------------------------------------- the vote rules

FONT = (7, 0)


def ballots(*votes: tuple[int, str, str]) -> Ballots:
    """(code, reading, word) per vote."""
    b = Ballots()
    for code, reading, word in votes:
        b.cast((FONT, code), reading, word)
    return b


def test_a_code_needs_three_votes_from_two_distinct_words():
    reading = {(FONT, 0x47): "G"}
    assert decide(ballots((0x47, "F", "Gpsn"), (0x47, "F", "Gpsn"), (0x47, "F", "Gfft")),
                  reading)[0][0].after == "F"
    assert decide(ballots((0x47, "F", "Gpsn"), (0x47, "F", "Gfft")), reading)[0] == []
    assert decide(ballots(*[(0x47, "F", "Gpsn")] * 9), reading)[0] == []   # one word only


def test_a_repeated_word_casts_at_most_two_votes():
    # nine runs of a header and one other word: 2 + 1 votes after the cap, agreement 3 of 3
    b = ballots(*[(0x47, "F", "Gpsn")] * 9, (0x47, "F", "Gfft"))
    [d], _ = decide(b, {(FONT, 0x47): "G"})
    assert (d.votes, d.total) == (3, 3)


def test_a_digit_or_symbol_never_changes():
    for before, after in (("1", "l"), ("l", "1"), ("$", "S")):
        b = ballots(*[(0x31, after, w) for w in ("a1", "b1", "c1", "d1")])
        decided, doubtful = decide(b, {(FONT, 0x31): before})
        assert decided == [] and doubtful == [(FONT, 0x31)]


def test_a_private_use_reading_is_decided_like_an_unknown_letter():
    # A /ToUnicode into the Private Use Area (#342): the code reads as U+E047 and the witness
    # reads "F" over it. A digit is still never decided.
    b = ballots(*[(0x47, "F", w) for w in ("Form", "For", "Fee")])
    [d], _ = decide(b, {(FONT, 0x47): ""})
    assert (d.before, d.after, d.votes) == ("", "F", 3)
    b = ballots(*[(0x39, "9", w) for w in ("W-9", "2019", "1099")])
    assert decide(b, {(FONT, 0x39): ""}) == ([], [(FONT, 0x39)])


def test_a_split_vote_under_80_percent_decides_nothing():
    b = ballots(*[(0x47, "F", w) for w in ("Ga", "Gb", "Gc")], (0x47, "E", "Gd"))
    assert decide(b, {(FONT, 0x47): "G"})[0] == []          # 3 of 4: 75%
    b = ballots(*[(0x47, "F", w) for w in ("Ga", "Gb", "Gc", "Ge")], (0x47, "E", "Gd"))
    assert decide(b, {(FONT, 0x47): "G"})[0][0].after == "F"   # 4 of 5


def test_a_reading_the_witness_confirms_is_not_changed():
    b = ballots(*[(0x47, "G", w) for w in ("Ga", "Gb", "Gc")])
    assert decide(b, {(FONT, 0x47): "G"}) == ([], [])


def test_the_codes_are_document_warnings_of_weight_zero():
    for code in font_map.CODES:
        assert CODES[code].weight == 0.0 and CODES[code].scope == "doc"
        assert CODES[code].severity == "warning"


def test_type0_type3_and_unmapped_fonts_are_not_decoded():
    with pikepdf.new() as pdf:
        for subtype, why in (("/Type0", "Type0"), ("/Type3", "Type3"), ("/Type1", "no /ToUnicode")):
            font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                                                        Subtype=pikepdf.Name(subtype),
                                                        BaseFont=pikepdf.Name.Helvetica))
            assert why in font_map._font(font.objgen, "/F1", font).unsupported


# --------------------------------------------------------------------------- the documents

def shared_shift(src: Path, dst: Path, twin: tuple[str, str] | None = None) -> Path:
    """`src` with its regular font, which every page shares, given a /ToUnicode that reads each
    letter as the next one. `twin` (a, b) also reads the code of `a` as the layer reads `b`."""
    with pikepdf.open(src) as pdf:
        font = pdf.pages[0].Resources.Font["/FR"]
        umap = unicode_map(font)
        shifted = {c: "".join(map(shift_letter, t)) for c, t in umap.items()}
        if twin is not None:
            shifted[ord(twin[0])] = shifted[ord(twin[1])]
        font.ToUnicode = _cmap(pdf, shifted, 1)
        save(pdf, dst)
    return dst


def clean_witness(monkeypatch, clean: Path, unread: tuple[int, ...] = ()) -> None:
    """The witness reads each page as the clean PDF's words (pdfplumber), and fails on `unread`."""
    with pdfplumber.open(clean) as pdf:
        words = {i: tuple(WitnessWord(w["text"], (w["x0"], w["top"], w["x1"], w["bottom"]), 96.0)
                          for w in page.extract_words())
                 for i, page in enumerate(pdf.pages, start=1)}

    def read(working, number, langs, read=None):
        if number in unread or number not in words:
            return WitnessPage(number, error="RuntimeError: not read")
        return WitnessPage(number, words[number])
    monkeypatch.setattr(witness, "witness_page", read)
    monkeypatch.setattr(witness, "available", lambda: True)
    monkeypatch.setattr(witness, "languages", lambda lang: ("eng",))


def _doc(out: Path) -> dict:
    return json.loads((out / DOCUMENT_JSON).read_text(encoding="utf-8"))


def _texts(out: Path, page: int) -> list[str]:
    return [n["text"] for n in walk_json(_doc(out)["root"])
            if n.get("page") == page and n.get("text") and not n.get("children")]


def _root_facts(out: Path, code: str) -> list[dict]:
    return [f for f in _doc(out)["root"].get("facts", []) if f["code"] == code]


def _chunks(out: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((out / "chunks").glob("*.html")))


@pytest.fixture
def shifted(tmp_path, monkeypatch) -> tuple[Path, Path]:
    clean = make_text_pdf(tmp_path / "clean.pdf", [PROSE, PAGE_TWO])
    clean_witness(monkeypatch, clean, unread=(2,))   # page 2 is never witnessed
    return clean, shared_shift(clean, tmp_path / "shifted.pdf")


def test_a_shared_font_is_decoded_on_every_page_even_one_not_witnessed(shifted, tmp_path):
    clean, pdf = shifted
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    assert _texts(out, 2) == ["the office report was filed on time"]
    assert "The staff filed five official files" in " ".join(_texts(out, 1))
    [fact] = _root_facts(out, "font-map")
    assert fact["data"]["map"].startswith("fm-") and fact["data"]["pages"] == [1, 2]
    assert any("‘U’ → ‘T’" in line for line in fact["data"]["codes"])
    assert not _root_facts(out, "font-ambiguous")
    # the page 1 the witness read agrees now: no text-disputed left on it
    assert not any(f["code"] == FACT_DISPUTED for p in _doc(out)["pages"] for f in p.get("facts", []))
    # every changed node keeps the layer's reading, letter for letter the shifted text
    changed = [n for n in walk_json(_doc(out)["root"]) if "layer_text" in n.get("attrs", {})]
    assert changed and all(n["attrs"]["layer_text"] == "".join(map(shift_letter, n["text"]))
                           for n in changed if n["page"] == 2)
    html = _chunks(out)
    assert "Text on this page was decoded from a font whose text layer was wrong. The PDF read" in html
    assert "‘uif’" in html and "data-generated" in html
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["decoded"] == {"fonts": 1, "codes": fact["data"]["count"]}
    review = json.loads((out / REVIEW_JSON).read_text())
    assert any(i.get("warn") == "font-map" for i in review)


def test_the_repair_is_the_same_bytes_twice(shifted, tmp_path):
    clean, pdf = shifted
    outs = []
    for name in ("a", "b"):
        working = tmp_path / name / "working.pdf"
        working.parent.mkdir()
        lock = threading.RLock()
        doc = read_source(pdf, working, "", lock=lock)
        report = text_trust.preflight(doc, working, lock=lock)
        repair = font_map.repair(doc, working, report, lock=lock)
        assert repair.applied
        outs.append((file_sha256(working), doc.to_json(indent=None)))
    assert outs[0] == outs[1]
    convert(pdf, tmp_path / "c", page_images=False)
    convert(pdf, tmp_path / "d", page_images=False)
    assert (tmp_path / "c" / DOCUMENT_JSON).read_bytes() == (tmp_path / "d" / DOCUMENT_JSON).read_bytes()


def test_a_doubt_page_with_a_few_wrong_letters_is_not_changed(tmp_path):
    """Two letters read wrong (not the remapped font the census names) on a page judged doubt:
    the witness disputes it, but such a page does not vote, so nothing is decoded."""
    clean = make_text_pdf(tmp_path / "clean.pdf", [PROSE])
    pdf = tmp_path / "few.pdf"
    with pikepdf.open(clean) as src:
        font = src.pages[0].Resources.Font["/FR"]
        umap = dict(unicode_map(font))   # a copy: pdfminer caches the encoding's dict
        umap[ord("e")], umap[ord("o")] = "x", "q"
        font.ToUnicode = _cmap(src, umap, 1)
        save(src, pdf)
    lock = threading.RLock()
    working = tmp_path / "working.pdf"
    doc = read_source(pdf, working, "", lock=lock)
    before = file_sha256(working)
    with pdfplumber.open(clean) as plumb:
        words = tuple(WitnessWord(w["text"], (w["x0"], w["top"], w["x1"], w["bottom"]), 96.0)
                      for w in plumb.pages[0].extract_words())
    doc.pages[0].facts.append(Fact(FACT_DISPUTED, Source.OCR, "", {}))
    report = TrustReport(judged={1: Judgment("doubt", ("3 of 300 characters unmapped",))},
                         witnessed={1: words})
    repair = font_map.repair(doc, working, report, lock=lock)
    assert not repair.applied and not repair.abstained
    assert file_sha256(working) == before and not doc.root.facts


@pytest.mark.parametrize("twin, reading", [
    (("z", "e"), "f"),   # z's code reads 'f' too, a letter the page votes on often
    (("z", "x"), "y"),   # z's code reads 'y' too, a letter too rare on the page to be decided
])
def test_a_map_that_is_not_one_to_one_is_ambiguous_and_nothing_is_applied(
        tmp_path, monkeypatch, twin, reading):
    clean = make_text_pdf(tmp_path / "clean.pdf", [PROSE])
    clean_witness(monkeypatch, clean)
    pdf = shared_shift(clean, tmp_path / "twin.pdf", twin=twin)
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    [fact] = _root_facts(out, "font-ambiguous")
    assert "not one-to-one" in fact["detail"] and f"‘{reading}’" in fact["detail"]
    assert not _root_facts(out, "font-map")
    assert "Uif tubgg gjmfe" in " ".join(_texts(out, 1))   # the layer's text, as it was
    assert "layer_text" not in json.dumps(_doc(out))


def test_actual_text_in_a_form_the_page_draws_is_found():
    """An /ActualText inside a Form XObject, or in a form's /Properties, as on the page itself;
    a form that draws itself is walked once."""
    with pikepdf.new() as pdf:
        def page_with(form_stream: bytes, props=None) -> None:
            form = pdf.make_stream(form_stream, Type=pikepdf.Name.XObject,
                                   Subtype=pikepdf.Name.Form, BBox=[0, 0, 10, 10])
            form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))  # a cycle
            if props is not None:
                form.Resources.Properties = pikepdf.Dictionary(MC0=props)
            page = pikepdf.Page(pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 10, 10]))
            page.obj.Contents = pdf.make_stream(b"/Fm0 Do")
            page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))
            pdf.pages.append(page)
        page_with(b"BT ET")
        page_with(b"/Span <</ActualText (fi)>> BDC BT ET EMC")
        page_with(b"/Span /MC0 BDC BT ET EMC", pikepdf.Dictionary(ActualText=pikepdf.String("fi")))
        assert font_map._has_actual_text(pdf, [1, 2, 3]) == [2, 3]


# --------------------------------------------------------------------------- a reviewer's answer

def _store(tmp_path: Path, out: Path, verdict: str, *undo: str) -> tuple[Path, str]:
    store = tmp_path / "store.jsonl"
    if not store.exists():
        new_store(store, "shifted")
    [fact] = _root_facts(out, "font-map")
    rid = mint_id()
    append(store, {"v": 1, "id": rid, "at": "2026-11-12T09:00:00Z",
                   "pdf": _doc(out)["meta"]["sha256"], "kind": "font-map",
                   "map": fact["data"]["map"], "verdict": verdict})
    for of in undo:
        append(store, {"v": 1, "id": mint_id(), "at": "2026-11-12T09:00:01Z",
                       "pdf": _doc(out)["meta"]["sha256"], "kind": "undo", "of": of})
    return store, rid


def test_a_reject_restores_the_layers_reading_on_a_saved_model_without_converting(
        shifted, tmp_path, monkeypatch):
    clean, pdf = shifted
    root = tmp_path / "root"
    out = tmp_path / "out"
    # page 2's witness failed, so this model is not kept: witness page 2 too for this test
    clean_witness(monkeypatch, clean)
    assert convert(pdf, out, page_images=False, stage_cache=root).model == "built, kept"
    decoded = {n["id"]: n for n in walk_json(_doc(out)["root"]) if "layer_text" in n.get("attrs", {})}
    store, rid = _store(tmp_path, out, "reject")
    no_build(monkeypatch)
    convert(pdf, out, page_images=False, stage_cache=root, decisions=store)
    after = {n["id"]: n for n in walk_json(_doc(out)["root"])}
    assert decoded and all(after[i]["text"] == n["attrs"]["layer_text"] for i, n in decoded.items())
    assert _texts(out, 2) == ["uif pggjdf sfqpsu xbt gjmfe po ujnf"]
    ledger = json.loads((out / "decisions.json").read_text())
    assert ledger["fonts"] == [{"id": rid, "map": _root_facts(out, "font-map")[0]["data"]["map"],
                                "verdict": "reject", "nodes": len(decoded)}]
    review = json.loads((out / REVIEW_JSON).read_text())
    assert not any(i.get("warn") == "font-map" for i in review)   # answered
    assert "decoded from a font" not in _chunks(out)
    # undo the reject: the decoded text and its item come back
    store, _ = _store(tmp_path, out, "accept")   # accept replaces the reject: text stays decoded
    convert(pdf, out, page_images=False, stage_cache=root, decisions=store)
    assert _texts(out, 2) == ["the office report was filed on time"]
    assert not any(i.get("warn") == "font-map" for i in json.loads((out / REVIEW_JSON).read_text()))


def test_revert_of_a_rejected_map_is_the_model_it_started_from(shifted, tmp_path, monkeypatch):
    clean, pdf = shifted
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    store, _ = _store(tmp_path, out, "reject")
    body = (out / DOCUMENT_JSON).read_text(encoding="utf-8")
    doc = Document.from_json(body)
    counts = decisions_apply.apply(doc, read_store(store))
    assert counts["applied"] == 0 and doc.to_json(indent=None) != body
    assert any(n.text == n.attrs.get("layer_text") for n in doc.root.walk() if n.text)
    decisions_apply.revert(doc)
    assert doc.to_json(indent=None) == body


@pytest.mark.skipif(not ocr_available(), reason="OCRmyPDF is not installed")
def test_ocrmypdf_keeps_the_patched_font_on_the_pages_it_does_not_read(tmp_path, monkeypatch):
    clean = make_text_pdf(tmp_path / "clean.pdf", [PROSE, PAGE_TWO])
    scan = make_pdf(tmp_path / "scan.pdf", ["scan"])
    with pikepdf.open(clean) as pdf, pikepdf.open(scan) as image:
        pdf.pages.extend(image.pages)
        save(pdf, tmp_path / "mixed-clean.pdf")
    clean_witness(monkeypatch, tmp_path / "mixed-clean.pdf")
    pdf = shared_shift(tmp_path / "mixed-clean.pdf", tmp_path / "mixed.pdf")
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    pages = {p["number"]: [f["code"] for f in p.get("facts", [])] for p in _doc(out)["pages"]}
    assert "ocr-page" in pages[3] and "ocr-page" not in pages[1] + pages[2]
    assert _texts(out, 2) == ["the office report was filed on time"]
    assert _root_facts(out, "font-map")
