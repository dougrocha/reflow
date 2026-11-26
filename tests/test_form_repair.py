"""Typed proposals for the regions the form repair pass reads: the parse, the checks
against the page's evidence, the bounded reread and the cache, with fake readers only."""

import io
import json

import pytest

from reflow.analyze import form_repair as rp
from reflow.analyze.form_regions import Control, Origin, Region, Task
from reflow.analyze.vision import ProviderError, ProviderUnavailable

HEADER = [("24", "Procedure Date (MM/DD/CCYY)"), ("25", "Area of Oral Cavity"),
          ("26", "Tooth System"), ("27", "Tooth Number(s) or Letter(s)"),
          ("28", "Tooth Surface"), ("29", "Procedure Code"), ("29a", "Diag. Pointer"),
          ("29b", "Qty."), ("30", "Description"), ("31", "Fee")]


def header_region(**kw):
    """The dental form's services header as routing finds it on a scan: OCR read none of it."""
    base = dict(page=1, bbox=(12.0, 325.0, 601.0, 350.0), task=Task.FIELDS, origin=Origin.OCR,
                reason="unread glyphs in a ruled row", unread=125, known=(),
                expected=tuple(str(n) for n in range(24, 32)), columns=11, inked=10)
    return Region(**{**base, **kw})


def fields_reply(rows=HEADER, widget="none"):
    return json.dumps({"fields": [{"id": i, "label": label, "widget": widget}
                                  for i, label in rows]})


class Fake:
    """A reader that answers from a list and records every call."""
    cache_id = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[Task, str]] = []

    def read(self, task, prompt, image):
        self.calls.append((task, prompt))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def run(regions, reader, tmp_path, **kw):
    return rp.repair(regions, reader, tmp_path / "cache.json", sha="abc", model="fake",
                     image_for=lambda r: b"png", **kw)


def problems(region, reply):
    return rp.check(region, rp.parse(region.task, reply))[1]


def test_the_header_keeps_all_ten_labels_with_29a_and_29b(tmp_path):
    reader = Fake(fields_reply())
    [outcome], stats = run([header_region()], reader, tmp_path)
    assert outcome.status is rp.Status.PROPOSED and outcome.problems == ()
    assert [f.id for f in outcome.reading.fields] == [i for i, _ in HEADER]
    assert stats["calls"] == 1 and stats["proposed"] == 1
    assert "29a" in reader.calls[0][1]   # the prompt asks for sub-numbered fields


@pytest.mark.parametrize(("rows", "problem"), [
    (HEADER[:6] + HEADER[8:], rp.Problem.INCOMPLETE),                 # 29a, 29b dropped
    (HEADER[:7] + HEADER[8:], rp.Problem.INCOMPLETE),                 # 29b dropped
    (HEADER[:3], rp.Problem.INCOMPLETE),                               # a collapsed row
    (HEADER + [("29a", "Diag. Pointer")], rp.Problem.DUPLICATE_ID),
    ([(i, "Procedure Date") for i, _ in HEADER], rp.Problem.REPEATED),   # one label, ten numbers
    ([("24", "Procedure Date"), ("26", "Tooth System"), ("25", "Area of Oral Cavity")]
     + HEADER[4:], rp.Problem.OUT_OF_ORDER),
    (HEADER + [("32", "Other Fee(s)")], rp.Problem.INVENTED_ID),       # 32 is read elsewhere
    ([(str(n + 40), label) for n, (_, label) in enumerate(HEADER)], rp.Problem.INVENTED_ID),
])
def test_dropped_duplicated_renumbered_and_invented_fields_are_rejected(rows, problem):
    assert problem in problems(header_region(), fields_reply(rows))


def test_a_number_the_text_layer_read_must_stay():
    region = header_region(known=("29. Procedure Code 29a. Diag. Pointer",))
    found = problems(region, fields_reply([r for r in HEADER if r[0] != "29a"]))
    assert rp.Problem.DROPPED_ID in found
    assert problems(region, fields_reply()) == []


def test_a_sub_field_of_a_known_number_is_allowed_and_a_new_number_is_not():
    region = header_region(expected=(), known=("29. Procedure Code",), columns=3, inked=3,
                           unread=20)
    ok = [("29", "Procedure Code"), ("29a", "Diag. Pointer"), ("29b", "Qty.")]
    assert problems(region, fields_reply(ok)) == []
    assert rp.Problem.INVENTED_ID in problems(region, fields_reply(ok + [("30", "Description")]))


def test_code_boxes_are_not_checkboxes_and_only_the_widget_is_dropped(tmp_path):
    combs = (Control("comb", (40, 340, 100, 350), 4), Control("comb", (120, 340, 180, 350), 4))
    region = header_region(controls=combs, expected=("34",), known=(), columns=2, inked=2,
                           unread=30)
    reply = fields_reply([("34", "Diagnosis Code A"), ("", "B")], widget="checkbox")
    [outcome], _ = run([region], Fake(reply), tmp_path)
    assert outcome.status is rp.Status.PROPOSED
    assert outcome.problems == (rp.Problem.CODE_BOXES_AS_CHECKBOX,)
    assert {f.widget for f in outcome.reading.fields} == {rp.Widget.UNKNOWN}
    square = header_region(controls=(Control("square", (40, 340, 48, 348)),), expected=("34",),
                           columns=2, inked=2, unread=30)
    assert problems(square, reply) == []


def test_text_far_beyond_the_ink_is_invented_and_far_below_is_incomplete():
    region = Region(1, (10, 10, 200, 30), Task.TEXT, Origin.OCR, "unread", unread=40)
    assert problems(region, json.dumps({"lines": ["Call 911 in an emergency now"]})) == []
    assert rp.Problem.INVENTED_TEXT in problems(region, json.dumps({"lines": ["word " * 60]}))
    assert rp.Problem.INCOMPLETE in problems(region, json.dumps({"lines": ["Call"]}))
    assert rp.Problem.REPEATED in problems(region, json.dumps({"lines": ["Call 911"] * 5}))


@pytest.mark.parametrize("raw", [
    "Here are the fields: 24. Procedure Date",                      # prose
    json.dumps({"fields": HEADER}),                                 # wrong item type
    json.dumps({"fields": [], "value": "x"}),                       # an extra key
    json.dumps({"fields": [{"id": "24", "label": "Date", "widget": "dropdown"}]}),
    json.dumps({"fields": [{"id": "24", "label": "Date", "widget": "none", "value": "1/2"}]}),
    '{"fields": [], "fields": []}',                                 # a key given twice
])
def test_malformed_replies_are_rejected(raw):
    with pytest.raises(rp.Rejected):
        rp.parse(Task.FIELDS, raw)


def test_a_rejected_reading_is_read_once_more_told_why(tmp_path):
    reader = Fake(fields_reply(HEADER[:6] + HEADER[8:]), fields_reply())
    [outcome], stats = run([header_region()], reader, tmp_path)
    assert outcome.status is rp.Status.PROPOSED
    assert stats["calls"] == 2 and stats["rejected"] == 1
    assert "rejected because much of the printed text was left out" in reader.calls[1][1]
    assert [a["problems"] for a in outcome.attempts] == [["incomplete"], []]


def test_repeated_failure_leaves_the_region_unresolved_after_two_calls(tmp_path):
    reader = Fake("not json", ProviderError("reply reached the 1500-token limit"))
    [outcome], stats = run([header_region()], reader, tmp_path)
    assert outcome.status is rp.Status.UNRESOLVED and outcome.reading is None
    assert outcome.problems == (rp.Problem.PROVIDER,)
    assert stats == {**stats, "calls": 2, "failed": 1, "rejected": 1, "unresolved": 1}


def test_a_failed_call_then_a_good_reread_replays_without_a_call(tmp_path):
    run([header_region()], Fake(ProviderError("timed out"), fields_reply()), tmp_path)
    again = Fake()
    [outcome], stats = run([header_region()], again, tmp_path)
    assert again.calls == [] and stats["calls"] == 0 and stats["cached"] == 2
    assert outcome.status is rp.Status.PROPOSED


def test_an_unavailable_provider_stops_asking_and_skips_the_rest(tmp_path):
    reader = Fake(ProviderUnavailable("Ollama is not reachable"))
    regions = [header_region(), header_region(bbox=(12.0, 400.0, 601.0, 420.0))]
    outcomes, stats = run(regions, reader, tmp_path)
    assert [o.status for o in outcomes] == [rp.Status.SKIPPED, rp.Status.SKIPPED]
    assert len(reader.calls) == 1 and stats["unavailable"] == 1


def test_the_call_budget_bounds_every_attempt(tmp_path):
    regions = [header_region(bbox=(12.0, y, 601.0, y + 20)) for y in (100.0, 200.0, 300.0)]
    reader = Fake(*[fields_reply()] * 3)
    outcomes, stats = run(regions, reader, tmp_path, max_calls=2)
    assert stats["calls"] == 2 and len(reader.calls) == 2
    assert [o.status for o in outcomes][-1] is rp.Status.SKIPPED


def test_a_replay_hits_the_cache_and_makes_no_call(tmp_path):
    first = Fake(fields_reply(HEADER[:3]), fields_reply())
    run([header_region()], first, tmp_path)
    again = Fake()
    [outcome], stats = run([header_region()], again, tmp_path)
    assert again.calls == [] and stats["calls"] == 0 and stats["cached"] == 2
    assert outcome.status is rp.Status.PROPOSED
    # Replay-only (no reader) reaches the same verdict.
    [outcome], _ = run([header_region()], None, tmp_path)
    assert outcome.status is rp.Status.PROPOSED
    # A region with other evidence is another question: not in the cache.
    [outcome], _ = run([header_region(known=("24. Procedure Date",))], None, tmp_path)
    assert outcome.status is rp.Status.SKIPPED


def test_the_ollama_request_is_local_typed_and_bounded(monkeypatch):
    sent = []

    def urlopen(request, timeout=None, loopback=False):
        sent.append((request.full_url, json.loads(request.data)))
        return io.BytesIO(json.dumps({"message": {"content": '{"lines": []}'}}).encode())

    monkeypatch.setattr("reflow.analyze.vision.send", urlopen)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    reader = rp.OllamaRegionReader("qwen3-vl:8b")
    assert reader.read(Task.FIELDS, rp.PROMPTS[Task.FIELDS], b"png") == '{"lines": []}'
    url, payload = sent[0]
    assert url.startswith("http://localhost:11434")
    assert payload["format"] == rp.SCHEMAS[Task.FIELDS]
    assert payload["options"] == {"temperature": 0, "seed": 0, "num_predict": rp.MAX_TOKENS,
                                  "num_ctx": rp.NUM_CTX}
    assert payload["messages"][-1]["role"] == "assistant"   # the empty think block for qwen3-vl


# -- the pass, end to end ---------------------------------------------------------------------

def _dental():
    from test_form_regions import DENTAL
    return DENTAL


@pytest.fixture(scope="module")
def scanned_dental(tmp_path_factory):
    """Page 1 of the dental claim form as a 200 dpi image-only PDF: OCR reads it, and leaves
    the services header (fields 24 to 31) unread."""
    import shutil

    import pypdfium2 as pdfium
    if shutil.which("ocrmypdf") is None or not _dental().exists():
        pytest.skip("needs ocrmypdf and the gitignored dental claim form sample")
    pdf = pdfium.PdfDocument(_dental())
    try:
        image = pdf[0].render(scale=200 / 72).to_pil().convert("L")
    finally:
        pdf.close()
    path = tmp_path_factory.mktemp("scan") / "dental-p1.pdf"
    image.save(path, "PDF", resolution=200)
    return path


class Header(Fake):
    """Reads every ruled row as the services header, as qwen3-vl:8b read it on the scan, and
    any line of text as nothing: the other regions fail their checks."""

    def read(self, task, prompt, image):
        self.calls.append((task, prompt))
        return fields_reply() if task is Task.FIELDS else json.dumps({"lines": []})


def _text(out):
    from reflow.analyze.proposals import text_digest
    from reflow.model import Document
    return text_digest(Document.from_dict(json.loads((out / "document.json").read_text())))


def test_the_pass_reads_the_scans_header_for_review_and_changes_no_text(scanned_dental,
                                                                         tmp_path, monkeypatch):
    from reflow.convert import convert

    reader = Header()
    monkeypatch.setattr(rp, "make_reader", lambda model=None: reader)
    off, on = tmp_path / "off", tmp_path / "on"
    convert(scanned_dental, off, page_images=False)
    summary = convert(scanned_dental, on, page_images=False, assist_form_repair=True)
    assert _text(on) == _text(off)                 # the transcript is the baseline's
    record = json.loads((on / "form-repair-proposals.json").read_text())
    header = next(r for r in record["regions"] if r["task"] == "fields"
                  and r["bbox"][1] < 340 < r["bbox"][3])
    assert header["status"] == "proposed"
    assert [f["id"] for f in header["fields"]] == [i for i, _ in HEADER]
    review = json.loads((on / "review.json").read_text())
    assert "form-repair-proposal" in json.dumps(review)
    assert summary.form_repair["proposed"] >= 1 and summary.form_repair["calls"] == len(reader.calls)
    # The next conversion replays every reading from the carried cache: no call.
    again = Fake()
    monkeypatch.setattr(rp, "make_reader", lambda model=None: again)
    summary = convert(scanned_dental, on, page_images=False, assist_form_repair=True)
    assert again.calls == [] and summary.form_repair["calls"] == 0
    replayed = json.loads((on / "form-repair-proposals.json").read_text())["regions"]
    assert [(r["id"], r["status"], r.get("fields")) for r in replayed] == \
        [(r["id"], r["status"], r.get("fields")) for r in record["regions"]]


@pytest.mark.skipif(not _dental().exists(), reason="the dental claim form sample is gitignored")
def test_born_digital_text_makes_no_call(tmp_path, monkeypatch):
    from reflow.convert import convert

    reader = Fake()
    monkeypatch.setattr(rp, "make_reader", lambda model=None: reader)
    summary = convert(_dental(), tmp_path / "out", page_images=False, assist_form_repair=True)
    assert reader.calls == [] and summary.form_repair["regions"] == 0
    assert summary.form_repair["pages_native_text"] == 2


def test_one_page_item_shows_every_region_proposed_or_not(tmp_path):
    from reflow.analyze import review
    from reflow.model import Document, Node, PageInfo, Role

    regions = [header_region(bbox=(12.0, 100.0, 601.0, 125.0)), header_region()]
    outcomes, _ = run(regions, Fake("not json", "still not json", fields_reply()), tmp_path)
    assert [o.status for o in outcomes] == [rp.Status.UNRESOLVED, rp.Status.PROPOSED]
    doc = Document(Node(Role.DOC), [PageInfo(1, 612, 792)])
    rp.flag(doc, outcomes, "fake")
    [item] = [i for i in review.queue(doc) if i["role"] == "page"]
    assert "region 1 no reading passed its checks (malformed)" in item["text"]
    assert "region 2 read as: 24. Procedure Date (MM/DD/CCYY);" in item["text"]
    assert "29a. Diag. Pointer; 29b. Qty." in item["text"]
    assert item["boxes"] == [[12.0, 100.0, 601.0, 125.0], [12.0, 325.0, 601.0, 350.0]]


def test_the_page_item_keeps_every_reading_whole_up_to_the_page_budget(tmp_path):
    from reflow.analyze import form_regions as fr
    from reflow.analyze import review
    from reflow.model import Document, Node, PageInfo, Role

    regions = [header_region(bbox=(12.0, 40.0 + 100 * i, 601.0, 65.0 + 100 * i))
               for i in range(fr.MAX_PER_PAGE)]
    outcomes, _ = run(regions, Fake(*[fields_reply()] * fr.MAX_PER_PAGE), tmp_path)
    doc = Document(Node(Role.DOC), [PageInfo(1, 612, 792)])
    rp.flag(doc, outcomes, "fake")
    [item] = [i for i in review.queue(doc) if i["role"] == "page"]
    assert item["text"].count("29b. Qty.") == fr.MAX_PER_PAGE
    assert item["text"].endswith("31. Fee. Check them against the page.")
