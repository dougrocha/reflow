"""Fields on scanned forms (analyze.scan_fields). Nothing here calls a model or needs an OCR
engine: the scan is a small image-only PDF with an invisible text layer, the raster rules
(analyze.scan_rules) are replaced by fixed candidates, and the provider is a fake or the cache
is replayed. These test the plumbing (eligibility, the question, the checks, the cache, the cap,
provenance, rendering, the flags), not what a model says or what the rules find."""

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from conftest import make_pdf
from pikepdf import Dictionary, Name
from PIL import Image

from reflow.providers import ASSIST_MODELS
from reflow.prepare import prepare
from reflow.analyze import ask
from reflow.analyze import label_assist as la
from reflow.analyze import scan_fields as sf
from reflow.analyze.vision import Reply
from reflow.analyze import scan_rules as sr
from reflow.analyze.confidence import CODES
from reflow.convert import convert
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.forms import Detection, DetectionSource, Widget
from reflow.model import Document, Fact, Node, PageInfo, PageKind, Role, Source
from reflow.render.chunks import FORM_NOTE, SCAN_FORM_NOTE
from reflow.run import Options, Workers
from reflow.names import ASSIST_CACHE

Shape, Doubt = sr.Shape, sr.Doubt


def scan_pdf(path: Path, lines=None) -> Path:
    """One image-only page with an invisible text layer, as OCRmyPDF leaves it: (x, top, text)."""
    lines = lines or [(72, 100, "First name"), (72, 160, "Last name"),
                      (72, 220, "I agree to the terms")]
    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1,
                                        BaseFont=Name.Helvetica))
    image = pikepdf.Stream(pdf, b"\xff" * 10000, Type=Name.XObject, Subtype=Name.Image,
                           Width=100, Height=100, ColorSpace=Name.DeviceGray,
                           BitsPerComponent=8)
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Resources = Dictionary(Font=Dictionary(F1=font), XObject=Dictionary(Im0=image))
    ops = "q 612 0 0 792 0 0 cm /Im0 Do Q\n"
    for x, top, text in lines:
        ops += f"BT 3 Tr /F1 12 Tf {x} {792 - top - 10} Td ({text}) Tj ET\n"
    page.Contents = pdf.make_stream(ops.encode())
    pdf.save(path, static_id=True)  # same bytes each call, so the assist cache key holds
    return path


FIRST = sr.Candidate(1, Shape.UNDERLINE, (72, 113, 300, 124))
LAST = sr.Candidate(1, Shape.UNDERLINE, (72, 173, 300, 184), doubts=frozenset({Doubt.LONG_RULE}))
AGREE = sr.Candidate(1, Shape.SQUARE, (56, 220, 68, 232), doubts=frozenset({Doubt.INK_INSIDE}))


def found(monkeypatch, *candidates):
    """The rules find `candidates` on every page they are asked about."""
    asked = []

    def detect(pdf, number, lines):
        asked.append(number)
        return [c for c in candidates if c.page == number]

    monkeypatch.setattr(sr, "detect", detect)
    monkeypatch.setattr(sr, "scrub", lambda lines, kept: list(lines))
    return asked


class FakeProvider:
    """Answers scan-field questions from `answer` (a string or a function of the question) and
    every other question with "none"; records every call. Not a model."""
    cache_id = "fake-model"

    def __init__(self, answer="field"):
        self.answer = answer
        self.calls = []
        self.images = []

    def choose(self, question, image, crop):
        self.calls.append(question)
        self.images.append(image)
        if not isinstance(question, sf.ScanFieldQuestion):
            return Reply("none")
        return Reply(self.answer(question) if callable(self.answer) else self.answer)

    def scan_calls(self):
        return [q for q in self.calls if isinstance(q, sf.ScanFieldQuestion)]


def convert_scan(tmp_path, **options):
    pdf = scan_pdf(tmp_path / "scan.pdf")
    return convert(pdf, tmp_path / "out", ocr="none", page_images=False, **options)


def document_json(tmp_path):
    return json.loads((tmp_path / "out/document.json").read_text())


def fields(doc_json):
    out = []

    def walk(node):
        if node["role"] == "field":
            out.append(node)
        for child in node.get("children", []):
            walk(child)

    walk(doc_json["root"])
    return out


def codes(node):
    return [f["code"] for f in node.get("facts", [])]


# -- options and the flags -------------------------------------------------------------------


def test_options_default_off_and_validated():
    o = Options()
    assert (o.scan_fields, o.assist_scan_fields, o.assist_scan_fields_max_calls) == (False, False, 25)
    assert o.detect_scan_fields is False
    assert Options(scan_fields=True).detect_scan_fields
    assert Options(assist_scan_fields=True).detect_scan_fields   # the assist implies the rules
    assert Options(assist_scan_fields=True).scan_fields is False   # without changing the option
    with pytest.raises(ValueError, match="assist_scan_fields_max_calls"):
        Options(assist_scan_fields_max_calls=-1)
    Options(assist_scan_fields_max_calls=0)
    for flag in ("scan_fields", "assist_scan_fields"):
        with pytest.raises(ValueError, match="docling"):
            Options(engine="docling", **{flag: True})
    Options(engine="hybrid", scan_fields=True)
    assert Options(engine="docling").detect_scan_fields is False


def test_cli_flags_reach_the_options(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from reflow import cli
    from reflow.run import OptionsError

    seen = {}

    def fake_convert(pdf, output, **options):
        seen.update(options)
        raise OptionsError("stop here")

    monkeypatch.setattr(cli, "run_convert", fake_convert)
    pdf = scan_pdf(tmp_path / "scan.pdf")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["convert", str(pdf), "-o", str(tmp_path / "o"), "--scan-fields",
                                     "--assist-scan-fields", "--assist-scan-fields-max-calls", "3"])
    assert result.exit_code == 2
    assert seen["scan_fields"] and seen["assist_scan_fields"]
    assert seen["assist_scan_fields_max_calls"] == 3
    runner.invoke(cli.app, ["convert", str(pdf), "-o", str(tmp_path / "o")])
    assert not seen["scan_fields"] and not seen["assist_scan_fields"]
    assert seen["assist_scan_fields_max_calls"] == 25
    bad = runner.invoke(cli.app, ["convert", str(pdf), "--assist-scan-fields-max-calls", "-1"])
    assert bad.exit_code != 0


# -- flag off: nothing runs, nothing changes ---------------------------------------------------


def test_flags_off_never_import_or_run_the_pass(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the scan-field pass must not run with the flags off")

    monkeypatch.setattr(sf, "run_pass", refuse)
    monkeypatch.setattr(sr, "detect", refuse)
    summary = convert_scan(tmp_path)
    assert summary.scan_fields == {} and "scan_fields" not in summary.seconds
    doc = document_json(tmp_path)
    assert "scan_fields" not in doc["meta"] and fields(doc) == []
    assert not any("scan-fields" in codes(p) for p in doc["pages"])
    assert "data-field-source" not in (tmp_path / "out/index.html").read_text()


def test_the_module_is_not_imported_by_a_conversion_with_the_flags_off(tmp_path):
    src = Path(sf.__file__).parents[2]
    pdf = scan_pdf(tmp_path / "scan.pdf")
    code = (f"import sys; sys.path.insert(0, {str(src)!r})\n"
            "from pathlib import Path\nfrom reflow.convert import convert\n"
            f"convert(Path({str(pdf)!r}), Path({str(tmp_path / 'o')!r}), ocr='none', page_images=False)\n"
            "assert 'reflow.analyze.scan_fields' not in sys.modules\n")
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-800:]


def test_a_document_without_scans_reads_the_same_with_the_flag_on(tmp_path, monkeypatch):
    found(monkeypatch, FIRST)
    pdf = make_pdf(tmp_path / "text.pdf", ["text", "text"])
    convert(pdf, tmp_path / "off", ocr="none", page_images=False)
    on = convert(pdf, tmp_path / "on", ocr="none", page_images=False, scan_fields=True)
    assert on.scan_fields["pages"] == 0 and on.scan_fields["injected"] == 0
    for name in ["index.html", "chunks/0001.html", "toc.json", "review.json"]:
        read = [re.sub(rb"\?v=[0-9a-f]+", b"", (tmp_path / d / name).read_bytes())
                for d in ("off", "on")]   # the asset version hashes the document, meta included
        assert read[0] == read[1], name


# -- eligibility -------------------------------------------------------------------------------


def line(text="Name", ocr=True, top=100.0):
    return Line(1, (72, top, 150, top + 12), text, 12.0, False, False, "Helvetica", ocr=ocr)


def content(*lines, widgets=()):
    return PageContent(1, 612, 792, list(lines), [], widgets=list(widgets))


def info(coverage=1.0, *facts):
    return PageInfo(1, 612, 792, image_coverage=coverage, facts=list(facts))


def test_a_page_is_eligible_when_it_is_an_image_with_a_text_layer_and_lines():
    ocr_page = Fact("ocr-page", Source.OCR, "text recognized by tesseract")
    assert sf.eligible(info(1.0), content(line(), line(top=200)))      # an OCR layer already there
    assert sf.eligible(info(0.5, ocr_page), content(line()))           # OCR'd in this run
    assert not sf.eligible(info(0.5), content(line()))                 # a picture beside text
    assert not sf.eligible(info(0.1, ocr_page), content(line()))       # OCR'd, but not an image page
    assert not sf.eligible(info(1.0), content(line(ocr=False)))        # visible text: born digital
    assert not sf.eligible(info(1.0), content())                       # nothing to bind captions to
    assert not sf.eligible(info(0.0), content(line(ocr=False)))


def prepared(tmp_path, **extra):
    stage = tmp_path / "stage"
    stage.mkdir()
    return prepare(scan_pdf(tmp_path / "scan.pdf"), stage,
                          Options(ocr="none", **extra), Workers.serial())


def test_a_document_with_any_widget_is_skipped_whole(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no page of a document with a form is read for fields")

    monkeypatch.setattr(sr, "detect", refuse)
    run = prepared(tmp_path, scan_fields=True)
    acro = Widget(1, 0, "text", (0, 0, 10, 10), name="a")
    pages = [content(line(), widgets=[acro]), content(line())]
    pages[1].number = 2
    sf.run_pass(run, pages, None)
    assert run.doc.meta["scan_fields"]["pages"] == 0 and run.doc.meta["scan_fields"]["injected"] == 0
    assert pages[1].widgets == []


# -- rules only --------------------------------------------------------------------------------


def test_certain_candidates_become_fields_and_are_never_asked(tmp_path, monkeypatch):
    certain = sr.Candidate(1, Shape.UNDERLINE, (72, 173, 300, 184))
    asked = found(monkeypatch, FIRST, certain, AGREE)
    provider = FakeProvider()
    summary = convert_scan(tmp_path, scan_fields=True, label_provider=provider)
    assert asked == [1] and provider.calls == []
    stats = summary.scan_fields
    assert (stats["mode"], stats["model"], stats["pages"]) == ("rules", "", 1)
    assert (stats["certain"], stats["doubtful"], stats["injected"]) == (2, 1, 2)
    assert (stats["asked"], stats["calls"], stats["skipped"]) == (0, 0, 0)

    doc = document_json(tmp_path)
    nodes = fields(doc)
    assert [n["attrs"]["kind"] for n in nodes] == ["text", "text"]   # the doubtful square is left out
    assert [n["text"] for n in nodes] == ["First name", "Last name"]
    for n in nodes:
        assert n["attrs"]["detected"] == "rules" and n["attrs"]["name"] == ""
        assert "field-assisted" not in codes(n)
    assert doc["meta"]["scan_fields"] == stats
    assert not (tmp_path / "out" / ASSIST_CACHE).exists()


def test_a_page_with_detected_fields_carries_the_page_fact_and_a_review_item(tmp_path, monkeypatch):
    found(monkeypatch, FIRST)
    convert_scan(tmp_path, scan_fields=True)
    page = document_json(tmp_path)["pages"][0]
    fact = next(f for f in page["facts"] if f["code"] == "scan-fields")
    assert fact["data"] == {"fields": 1, "assisted": 0}
    review = json.loads((tmp_path / "out/review.json").read_text())
    item = next(i for i in review if i["id"] == "page:1")
    assert item["severity"] == "warning" and item["label"] == CODES["scan-fields"].label


def test_a_page_the_rules_fail_on_is_skipped_and_the_conversion_goes_on(tmp_path, monkeypatch):
    def broken(pdf, number, lines):
        raise ZeroDivisionError("integer modulo by zero")

    monkeypatch.setattr(sr, "detect", broken)
    summary = convert_scan(tmp_path, scan_fields=True)
    assert summary.scan_fields["errors"] == 1 and summary.scan_fields["pages"] == 0
    assert summary.scan_fields["injected"] == 0
    assert fields(document_json(tmp_path)) == []


def test_a_skipped_page_logs_its_traceback_and_gets_a_review_item(tmp_path, monkeypatch, caplog):
    def broken(pdf, number, lines):
        raise ZeroDivisionError("integer modulo by zero")

    monkeypatch.setattr(sr, "detect", broken)
    with caplog.at_level("WARNING", logger="reflow.analyze.scan_fields"):
        convert_scan(tmp_path, scan_fields=True)
    record = next(r for r in caplog.records if "page 1" in r.getMessage())
    assert record.exc_info and record.exc_info[0] is ZeroDivisionError
    page = document_json(tmp_path)["pages"][0]
    fact = next(f for f in page["facts"] if f["code"] == "scan-fields-skipped")
    assert "ZeroDivisionError" in fact["detail"]
    review = json.loads((tmp_path / "out/review.json").read_text())
    item = next(i for i in review if i["id"] == "page:1")
    assert item["severity"] == "warning" and item["label"] == CODES["scan-fields-skipped"].label


def test_a_page_with_nothing_detected_gets_no_fact_and_no_note(tmp_path, monkeypatch):
    found(monkeypatch)
    summary = convert_scan(tmp_path, scan_fields=True)
    assert summary.scan_fields["pages"] == 1 and summary.scan_fields["injected"] == 0
    assert "scan-fields" not in summary.facts
    html = (tmp_path / "out/index.html").read_text()
    assert "form-note" not in html and "data-field-source" not in html


def test_rendering_marks_scan_fields_and_offers_no_pdf_fill(tmp_path, monkeypatch):
    found(monkeypatch, FIRST, sr.Candidate(1, Shape.SQUARE, (56, 220, 68, 232)))
    convert_scan(tmp_path, scan_fields=True)
    html = (tmp_path / "out/index.html").read_text()
    assert html.count('data-field-source="scan"') == 2
    assert SCAN_FORM_NOTE in html and FORM_NOTE not in html
    assert "data-pdf-field" not in html and "data-pdf-value" not in html
    assert '<label for="f1-0">First name</label>' in html
    manifest = json.loads((tmp_path / "out/manifest.json").read_text())
    assert not manifest.get("fillable")


def test_comb_and_multiline_candidates_keep_their_shape_on_the_widget():
    comb = sr.Candidate(1, Shape.COMB, (72, 100, 172, 118), cells=9)
    cell = sr.Candidate(1, Shape.CELL, (72, 200, 300, 260), multiline=True)
    w = sf.to_widget(comb, 3, Detection(DetectionSource.RULES, "comb"))
    assert (w.kind, w.index, w.name, w.tooltip, w.maxlen) == ("text", 3, "", "", 9)
    assert w.comb and not w.multiline and w.detected == Detection(DetectionSource.RULES, "comb")
    w = sf.to_widget(cell, 0, Detection(DetectionSource.RULES, "cell"))
    assert w.multiline and not w.comb and w.maxlen is None
    box = sf.to_widget(AGREE, 1, Detection(DetectionSource.RULES, "square"))
    assert box.kind == "checkbox"


def test_the_stage_cache_never_holds_a_synthetic_widget(tmp_path, monkeypatch):
    found(monkeypatch, FIRST)
    root = tmp_path / "cache"
    pdf = scan_pdf(tmp_path / "scan.pdf")
    on = convert(pdf, tmp_path / "on", ocr="none", page_images=False, scan_fields=True,
                 stage_cache=root)
    assert on.scan_fields["injected"] == 1
    off = convert(pdf, tmp_path / "off", ocr="none", page_images=False, stage_cache=root)
    assert off.scan_fields == {}
    out = json.loads((tmp_path / "off/document.json").read_text())
    assert fields(out) == []


# -- the model ---------------------------------------------------------------------------------


def test_doubtful_candidates_are_asked_only_in_assist_mode(tmp_path, monkeypatch):
    found(monkeypatch, FIRST, LAST, AGREE)
    provider = FakeProvider(lambda q: "checkbox" if q.candidate.shape is Shape.SQUARE else "field")
    summary = convert_scan(tmp_path, assist_scan_fields=True, label_provider=provider)
    stats = summary.scan_fields
    assert stats["mode"] == "assist" and stats["model"] == ASSIST_MODELS["ollama"]
    assert (stats["certain"], stats["doubtful"], stats["asked"], stats["calls"]) == (1, 2, 2, 2)
    assert (stats["accepted"], stats["injected"]) == (2, 3)
    assert {q.candidate for q in provider.scan_calls()} == {LAST, AGREE}   # never the certain one

    doc = document_json(tmp_path)
    by_source = {n["attrs"]["fid"]: n["attrs"]["detected"] for n in fields(doc)}
    assert by_source == {"f1-0": "rules", "f1-1": "assist", "f1-2": "assist"}
    assisted = [n for n in fields(doc) if n["attrs"]["detected"] == "assist"]
    for n in assisted:
        fact = next(f for f in n["facts"] if f["code"] == "field-assisted")
        assert fact["source"] == "assist" and fact["data"]["model"] == ASSIST_MODELS["ollama"]
    last = next(n for n in assisted if n["attrs"]["kind"] == "text")
    fact = next(f for f in last["facts"] if f["code"] == "field-assisted")
    assert fact["data"]["shape"] == "underline" and fact["data"]["doubts"] == ["long-rule"]
    assert "field-assisted" not in codes(next(n for n in fields(doc) if n["attrs"]["fid"] == "f1-0"))
    page_fact = next(f for f in doc["pages"][0]["facts"] if f["code"] == "scan-fields")
    assert page_fact["data"] == {"fields": 3, "assisted": 2}
    cache = json.loads((tmp_path / "out" / ASSIST_CACHE).read_text())
    entries = [e for e in cache.values() if e["kind"] == "scan-field"]
    assert len(entries) == 2 and {e["answer"] for e in entries} == {"field", "checkbox"}
    assert all(e["prompt_version"] == sf.SCAN_PROMPT_VERSION and e["model"] == ASSIST_MODELS["ollama"]
               for e in entries)


def test_a_page_not_taken_for_a_form_is_never_asked_about(tmp_path, monkeypatch):
    lone = sr.Candidate(1, Shape.CELL, (72, 300, 300, 320),
                        doubts=frozenset({Doubt.FEW_FIELDS, Doubt.GRID_CELL}))
    found(monkeypatch, lone)
    provider = FakeProvider("field")
    summary = convert_scan(tmp_path, assist_scan_fields=True, label_provider=provider)
    assert provider.calls == []
    assert (summary.scan_fields["doubtful"], summary.scan_fields["unasked"],
            summary.scan_fields["injected"]) == (1, 1, 0)


def test_rules_only_never_asks_even_when_a_provider_is_given(tmp_path, monkeypatch):
    found(monkeypatch, LAST)
    provider = FakeProvider()
    summary = convert_scan(tmp_path, scan_fields=True, label_provider=provider)
    assert provider.calls == [] and summary.scan_fields["injected"] == 0
    assert summary.scan_fields["doubtful"] == 1 and summary.scan_fields["skipped"] == 0


def test_the_cap_leaves_the_rest_with_the_rules(tmp_path, monkeypatch):
    found(monkeypatch, FIRST, LAST, AGREE)
    provider = FakeProvider()
    summary = convert_scan(tmp_path, assist_scan_fields=True, assist_scan_fields_max_calls=1,
                           label_provider=provider)
    stats = summary.scan_fields
    assert (stats["calls"], stats["skipped"], stats["asked"]) == (1, 1, 1)
    assert stats["injected"] == 2 and len(provider.scan_calls()) == 1
    (tmp_path / "out" / ASSIST_CACHE).unlink()
    zero = convert_scan(tmp_path, assist_scan_fields=True, assist_scan_fields_max_calls=0,
                        label_provider=FakeProvider())
    assert (zero.scan_fields["calls"], zero.scan_fields["skipped"]) == (0, 2)
    assert zero.scan_fields["injected"] == 1


def test_a_rerun_replays_the_cache_and_makes_no_call(tmp_path, monkeypatch):
    found(monkeypatch, FIRST, LAST)
    first = convert_scan(tmp_path, assist_scan_fields=True, label_provider=FakeProvider())
    assert first.scan_fields["calls"] == 1

    def refuse(self, *a, **k):
        raise AssertionError("the network provider must not be asked")

    monkeypatch.setattr(ask.OllamaChoiceProvider, "choose", refuse)
    second = convert_scan(tmp_path, assist_scan_fields=True)   # the output folder's cache answers
    assert second.scan_fields["calls"] == 0 and second.scan_fields["cached"] == 1
    assert second.scan_fields["injected"] == first.scan_fields["injected"] == 2
    assert second.facts == first.facts


def test_replay_with_no_provider_and_no_cache_asks_nothing(tmp_path, monkeypatch):
    found(monkeypatch, FIRST, LAST)
    run = prepared(tmp_path, assist_scan_fields=True)
    page = PageContent(1, 612, 792, [line("First name", top=100), line("Last name", top=160)], [])
    sf.run_pass(run, [page], None)
    stats = run.doc.meta["scan_fields"]
    assert (stats["calls"], stats["skipped"], stats["injected"]) == (0, 1, 1)


def test_a_failed_question_leaves_the_candidate_out(tmp_path, monkeypatch):
    from reflow.analyze.vision import ProviderError

    class Failing(FakeProvider):
        def choose(self, question, image, crop):
            if isinstance(question, sf.ScanFieldQuestion):
                raise ProviderError("bad reply")
            return Reply("none")

    found(monkeypatch, LAST)
    summary = convert_scan(tmp_path, assist_scan_fields=True, label_provider=Failing())
    assert (summary.scan_fields["failed"], summary.scan_fields["injected"]) == (1, 0)
    assert not (tmp_path / "out" / ASSIST_CACHE).exists() or \
        json.loads((tmp_path / "out" / ASSIST_CACHE).read_text()) == {}


def test_judge_checks_the_answer_against_the_shape_the_rules_measured():
    assert sf.judge(LAST, "field") == "accepted"
    assert sf.judge(AGREE, "checkbox") == "accepted"
    assert sf.judge(LAST, "checkbox") == "rejected"   # a checkbox on a non-square
    assert sf.judge(AGREE, "field") == "rejected"     # a text field on a square
    comb = sr.Candidate(1, Shape.COMB, (0, 0, 100, 18), cells=5)
    assert sf.judge(comb, "field") == "accepted" and sf.judge(comb, "checkbox") == "rejected"
    ring = sr.Candidate(1, Shape.CIRCLE, (56, 220, 62, 226), doubts=frozenset({Doubt.INK_INSIDE}))
    assert ring.kind == "checkbox"
    assert sf.judge(ring, "checkbox") == "accepted" and sf.judge(ring, "field") == "rejected"
    for candidate in (LAST, AGREE):
        assert sf.judge(candidate, "label") == "dropped"


@pytest.mark.parametrize("answer, key, injected", [
    ("checkbox", "rejected", 0),    # the model calls an underline a checkbox
    ("label", "dropped", 0),        # printed text
    ("none", "none", 0),            # neither
    ("field", "accepted", 1),
])
def test_each_answer_has_its_count_and_effect(tmp_path, monkeypatch, answer, key, injected):
    found(monkeypatch, LAST)
    summary = convert_scan(tmp_path, assist_scan_fields=True,
                           label_provider=FakeProvider(answer))
    stats = summary.scan_fields
    assert stats[key] == 1 and stats["injected"] == injected and stats["asked"] == 1
    assert len(fields(document_json(tmp_path))) == injected


def test_a_cached_answer_the_page_contradicts_is_rejected(tmp_path, monkeypatch):
    found(monkeypatch, LAST)
    convert_scan(tmp_path, assist_scan_fields=True, label_provider=FakeProvider("field"))
    cache_path = tmp_path / "out" / ASSIST_CACHE
    cache = json.loads(cache_path.read_text())
    for entry in cache.values():
        entry["answer"] = "checkbox"
    cache_path.write_text(json.dumps(cache))
    again = convert_scan(tmp_path, assist_scan_fields=True, label_provider=FakeProvider("field"))
    assert (again.scan_fields["cached"], again.scan_fields["rejected"]) == (1, 1)
    assert again.scan_fields["calls"] == 0 and again.scan_fields["injected"] == 0


def test_the_model_sees_the_page_around_the_candidate_with_it_outlined(tmp_path, monkeypatch):
    found(monkeypatch, LAST)
    provider = FakeProvider("field")
    convert_scan(tmp_path, assist_scan_fields=True, label_provider=provider)
    question = provider.scan_calls()[0]
    image = Image.open(io.BytesIO(provider.images[0])).convert("RGB")
    colours = dict((colour, n) for n, colour in image.getcolors(maxcolors=1 << 24))
    assert colours.get(sf.OUTLINE, 0) > 100
    prompt = json.loads(question.prompt((0, 0, 100, 100)))
    assert prompt["candidate"]["shape"] == "underline" and "proposed_as" not in prompt["candidate"]
    assert prompt["candidate"]["doubts"] == [sr.DOUBT_TEXT[Doubt.LONG_RULE]]
    assert [n["text"] for n in prompt["nearby"]] == ["Last name", "I agree to the terms", "First name"]
    assert "pdf" not in json.dumps(prompt).lower() and "scan.pdf" not in json.dumps(prompt)
    assert question.schema()["properties"]["kind"]["enum"] == ["field", "checkbox", "label", "none"]
    assert question.answer_key == "kind" and question.choices == list(sf.CHOICES)


def test_the_label_pass_names_a_detected_field_the_rules_could_not(tmp_path, monkeypatch):
    lone = sr.Candidate(1, Shape.UNDERLINE, (72, 400, 300, 411))
    found(monkeypatch, FIRST, lone)
    pdf = scan_pdf(tmp_path / "scan.pdf", [(72, 100, "First name"), (72, 340, "Telephone number")])

    class Labelling(FakeProvider):
        def choose(self, question, image, crop):
            if isinstance(question, la.LabelQuestion):
                self.calls.append(question)
                return Reply(question.candidates[0].id)
            return super().choose(question, image, crop)

    provider = Labelling()
    summary = convert(pdf, tmp_path / "out", ocr="none", page_images=False,
                      assist_scan_fields=True, label_provider=provider)
    asked = [q for q in provider.calls if isinstance(q, la.LabelQuestion)]
    assert summary.label_assist["calls"] == len(asked) == 1
    node = next(n for n in fields(document_json(tmp_path)) if n["attrs"]["fid"] == "f1-1")
    assert node["attrs"]["label"] == "assist" and node["attrs"]["aria"] == "Telephone number"


def test_label_pass_filter_keeps_only_detected_controls_and_groups():
    def control(box, **attrs):
        n = Node(Role.FIELD, page=1, bbox=box, attrs={"kind": "text", "label": "none", **attrs})
        if attrs.get("label", "none") == "none":
            n.fact("label-missing", Source.HEURISTIC, "no label")
        return n

    near = Node(Role.P, page=1, bbox=(100, 180, 290, 192), text="Name")
    pdf_field = control((100, 200, 200, 214), name="a")
    scanned = control((100, 230, 200, 244), detected="rules")
    members = [control((300, 200, 312, 212), kind="checkbox", detected="assist", label="printed"),
               control((300, 230, 312, 242), kind="checkbox", detected="assist", label="printed")]
    group = Node(Role.GROUP, page=1, bbox=(300, 200, 312, 242), attrs={"label": "none"},
                 children=members)
    group.fact("label-missing", Source.HEURISTIC, "no legend")
    doc = Document(Node(Role.DOC, children=[near, pdf_field, scanned, group]),
                   pages=[PageInfo(1, 612, 792)], meta={"sha256": "abc"})
    everything = {q.node for q in la.questions(doc)}
    only = {q.node for q in la.questions(doc, only_detected=True)}
    assert pdf_field.id in everything and pdf_field.id not in only
    assert only == {scanned.id, group.id} <= everything
    assert la.detected(group) and la.detected(scanned) and not la.detected(pdf_field)


# -- the cache key -----------------------------------------------------------------------------


def question(candidate=LAST, near=(sf.Near("Last name", (72, 160, 150, 172)),)):
    return sf.ScanFieldQuestion(1, "s1-0", candidate, tuple(near))


def test_the_cache_key_changes_with_what_the_model_was_shown(monkeypatch):
    key = sf.cache_key("abc", question(), "m")
    assert key == sf.cache_key("abc", question(), "m")
    assert key != sf.cache_key("abd", question(), "m")             # another file
    assert key != sf.cache_key("abc", question(), "other")         # another model
    monkeypatch.setattr(sf, "SCAN_PROMPT_VERSION", sf.SCAN_PROMPT_VERSION + 1)
    assert key != sf.cache_key("abc", question(), "m")             # another prompt
    monkeypatch.undo()
    assert key != sf.cache_key("abc", question(near=(sf.Near("Surname", (72, 160, 150, 172)),)), "m")
    assert key != sf.cache_key("abc", question(near=()), "m")
    moved = sr.Candidate(1, Shape.UNDERLINE, (72, 193, 300, 204), doubts=LAST.doubts)
    assert key != sf.cache_key("abc", question(moved), "m")
    fewer = sr.Candidate(1, Shape.UNDERLINE, LAST.bbox)
    assert key != sf.cache_key("abc", question(fewer), "m")        # no longer in doubt about it
    assert key == sf.cache_key("abc", question(sr.Candidate(
        1, Shape.UNDERLINE, (72.2, 172.8, 299.7, 184.1), doubts=LAST.doubts)), "m")   # whole points


def test_nearby_text_is_the_ocr_lines_within_reach_nearest_first():
    lines = [line("Far away", top=700), line("Last name", top=160), line("First name", top=100),
             line("   ", top=175)]
    near = sf.nearby(LAST, lines)
    assert [n.text for n in near] == ["Last name", "First name"]
    many = [line(f"row {i}", top=170 + i) for i in range(20)]
    assert len(sf.nearby(LAST, many)) == sf.MAX_NEAR


def test_the_widget_provenance_reaches_the_node_through_the_forms_planner():
    from reflow.analyze.forms import plan_forms

    caption = Line(1, (72, 100, 150, 112), "Name", 12.0, False, False, "Helvetica", ocr=True)
    assisted = Widget(1, 0, "text", (72, 114, 300, 126),
                      detected=Detection(DetectionSource.ASSIST, "underline", ("long-rule",), "m", 0.9))
    ruled = Widget(1, 1, "text", (72, 214, 300, 226), detected=Detection(DetectionSource.RULES, "underline"))
    page = PageContent(1, 612, 792, [caption], [], widgets=[assisted, ruled])
    plan = plan_forms(page, page.lines, page.lines, [])
    first, second = plan.flow
    assert first.attrs["detected"] == "assist" and second.attrs["detected"] == "rules"
    fact = next(f for f in first.facts if f.code == "field-assisted")
    assert fact.source == Source.ASSIST and fact.data["confidence"] == 0.9
    assert fact.data["doubts"] == ["long-rule"] and fact.data["model"] == "m"
    assert [f.code for f in second.facts] == ["label-missing"]   # a rules field has no assist fact


def test_hybrid_keeps_a_page_with_detected_fields_off_docling():
    from reflow.analyze.hybrid import route

    info = {"pages": {n: {"split": 0, "figures": 0, "tables": 1, "rules": 0, "blocks": 5,
                          "short_blocks": 0} for n in (1, 2)}}
    pages = [PageInfo(1, 612, 792, kind=PageKind.MIXED,
                      facts=[Fact("scan-fields", Source.HEURISTIC, "found")]),
             PageInfo(2, 612, 792, kind=PageKind.MIXED)]
    assert route(info, pages).hard == {2: ["tables"]}
