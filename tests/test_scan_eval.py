"""The scan-field evaluation (scripts/eval_scan_fields.py) and its hand-labelled set
(tests/scan_fields/fields.json). No conversion and no model: the labels are checked against the
sample PDFs, and the scoring is checked on hand-built fields."""

import importlib.util
import json
import sys
from pathlib import Path

import checkout
import pypdfium2 as pdfium
import pytest

from reflow.analyze.vision import Reply
from reflow.extract.forms import read_widgets
from reflow.model import Document, Node, PageInfo, Role
from reflow.names import ASSIST_CACHE

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("eval_scan_fields", ROOT / "scripts/eval_scan_fields.py")
ev = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ev
spec.loader.exec_module(ev)

SPEC = json.loads(ev.LABELS.read_text())["forms"]
PAGES = {"fw9-scan": 6, "i9-scan": 3, "w4-scan": 3}
REAL = {"census1970-scan": 2, "census1960-scan": 2}   # physical paper scans, labelled by eye only


def label(box=(100, 100, 200, 120), kind="text", caption="Last name", page=1, **extra):
    return ev.Label(page, kind, box, caption, **extra)


def found(box=(100, 100, 200, 120), kind="text", name="Last name", page=1):
    return ev.Found(page, kind, box, name)


def test_the_label_set_covers_the_three_synthetic_forms_and_the_real_scans():
    assert set(SPEC) == set(PAGES) | set(REAL)
    assert "about" in json.loads(ev.LABELS.read_text())
    for name, pages in PAGES.items():
        fields = SPEC[name]["fields"]
        assert len(fields) >= 20
        assert {f["kind"] for f in fields} == {"text", "checkbox"}
        assert len(SPEC[name]["source_pages"]) == pages
        assert not SPEC[name].get("real")
    for name, pages in REAL.items():
        entry = SPEC[name]
        assert entry["real"] is True and entry["source"].startswith("https://www.census.gov/")
        assert len(entry["source_pages"]) == pages and len(entry["fields"]) >= 50


@pytest.mark.parametrize("form", sorted(PAGES) + sorted(REAL))
def test_every_label_is_well_formed_and_inside_its_page(form):
    entry = SPEC[form]
    pdf = pdfium.PdfDocument(checkout.sample(ev.REPO, entry["pdf"]))
    sizes = [pdf[i].get_size() for i in range(len(pdf))]
    assert len(sizes) == len(entry["source_pages"])
    for f in entry["fields"]:
        assert f["kind"] in ("text", "checkbox")
        assert f["caption"].strip() and all(a.strip() for a in f.get("accept", []))
        assert 1 <= f["page"] <= len(sizes)
        w, h = sizes[f["page"] - 1]
        x0, top, x1, bottom = f["box"]
        assert 0 <= x0 < x1 <= w and 0 <= top < bottom <= h, f
        assert form in REAL or f["widget"] is not None or f.get("optional"), \
            "a blank with no widget must be optional"
        assert not (f.get("optional") and f.get("invisible"))
        assert f["widget"] is None or "widget" in f


@pytest.mark.parametrize("form", sorted(PAGES))
def test_the_labels_and_the_source_widgets_list_the_same_fields(form):
    entry = SPEC[form]
    source = read_widgets(checkout.sample(ev.REPO, entry["source"]))
    seen = set()
    expected = {}
    for scan_page, source_page in enumerate(entry["source_pages"], 1):
        for w in source.get(source_page, []):
            key = (scan_page, w.name, round(w.bbox[0], 1), round(w.bbox[1], 1))
            assert key not in expected
            expected[key] = "checkbox" if w.kind == "checkbox" else "text"
    for f in entry["fields"]:
        if f["widget"] is None:
            continue
        key = (f["page"], f["widget"], round(f["box"][0], 1), round(f["box"][1], 1))
        assert key in expected, f"{form}: no source widget for {f}"
        assert key not in seen, f"{form}: widget listed twice: {f}"
        assert expected[key] == f["kind"]
        seen.add(key)
    invisible = {(f["page"], f["widget"], round(f["box"][0], 1), round(f["box"][1], 1))
                 for f in entry["fields"] if f.get("invisible")}
    assert set(expected) - seen == invisible, "every visible source widget must be labelled"


def test_a_detection_over_a_label_is_a_hit_with_its_caption_checked():
    s = ev.score([label()], [found(box=(98, 99, 202, 121))])
    assert len(s.hits) == 1 and not s.misses and not s.false_positives
    assert s.precision == 1.0 and s.recall == 1.0 and s.hits[0].caption_ok
    assert s.hits[0].iou > 0.8


def test_a_label_nothing_covers_is_a_miss_and_a_stray_detection_a_false_positive():
    s = ev.score([label()], [found(box=(300, 400, 400, 420))])
    assert [m.caption for m in s.misses] == ["Last name"]
    assert len(s.false_positives) == 1 and not s.hits
    assert s.precision == 0.0 and s.recall == 0.0


def test_a_detection_of_the_wrong_kind_or_page_does_not_match():
    labels = [label(), label(box=(10, 10, 18, 18), kind="checkbox", caption="Citizen")]
    s = ev.score(labels, [found(kind="checkbox"), found(page=2, box=(10, 10, 18, 18), kind="checkbox")])
    assert not s.hits and len(s.misses) == 2 and len(s.false_positives) == 2


def test_the_centre_rule_allows_a_small_offset_and_no_more():
    box = (100, 100, 110, 110)
    assert ev.score([label(box=box, kind="checkbox")], [found(box=(107, 100, 117, 110), kind="checkbox")]).hits
    assert not ev.score([label(box=box, kind="checkbox")], [found(box=(120, 100, 130, 110), kind="checkbox")]).hits


def test_matching_is_one_to_one_and_prefers_the_closer_box():
    two = [label(box=(100, 100, 200, 120), caption="A"), label(box=(100, 124, 200, 144), caption="B")]
    wide = found(box=(95, 98, 205, 146), name="A")
    s = ev.score(two, [wide])
    assert len(s.hits) == 1 and len(s.misses) == 1 and not s.false_positives
    both = ev.score(two, [wide, found(box=(100, 124, 200, 144), name="B")])
    assert len(both.hits) == 2 and [h.label.caption for h in both.hits if h.found is not wide] == ["B"]


def test_an_optional_label_absorbs_a_detection_and_is_never_a_miss():
    sign = label(box=(100, 300, 300, 320), caption="Signature", optional=True)
    hit = ev.score([label(), sign], [found(), found(box=(100, 300, 300, 320), name="Signature")])
    assert len(hit.hits) == 1 and len(hit.absorbed) == 1 and not hit.false_positives and not hit.misses
    assert hit.precision == 1.0 and hit.recall == 1.0
    skipped = ev.score([label(), sign], [found()])
    assert len(skipped.hits) == 1 and not skipped.misses and not skipped.absorbed


def test_an_invisible_label_is_removed_before_matching():
    ghost = label(box=(100, 300, 200, 320), caption="Ghost", invisible=True)
    s = ev.score([label(), ghost], [found()])
    assert len(s.hits) == 1 and not s.misses and s.recall == 1.0
    stray = ev.score([label(), ghost], [found(), found(box=(100, 300, 200, 320), name="Ghost")])
    assert len(stray.false_positives) == 1, "a detection over an invisible widget is not credited"


def test_caption_accuracy_uses_the_normalized_caption_and_the_accept_alternates():
    cases = [
        (label(caption="Employer’s name"), "EMPLOYERS NAME and address", True),
        (label(caption="Date of birth"), "Birth date", False),
        (label(caption="Date of birth", accept=("Birth date",)), "Birth date", True),
        (label(caption="Last name"), "", False),
    ]
    for lab, name, ok in cases:
        assert ev.score([lab], [found(name=name)]).hits[0].caption_ok is ok
    s = ev.score([label(caption="Date of birth"), label(box=(100, 130, 200, 150), caption="City")],
                 [found(name="Date of birth (mm/dd/yyyy)"), found(box=(100, 130, 200, 150), name="Town")])
    assert s.caption_accuracy == 0.5


def test_scores_split_by_kind_and_merge():
    labels = [label(), label(box=(10, 10, 18, 18), kind="checkbox", caption="Citizen")]
    s = ev.score(labels, [found(), found(box=(300, 300, 308, 308), kind="checkbox", name="")])
    text, box = s.of_kind("text"), s.of_kind("checkbox")
    assert (text.precision, text.recall) == (1.0, 1.0)
    assert (box.precision, box.recall) == (0.0, 0.0)
    both = ev.merge([s, ev.score([label(page=2)], [found(page=2)])])
    assert len(both.hits) == 2 and len(both.misses) == 1 and len(both.false_positives) == 1


def test_a_false_positive_is_reported_beside_the_nearest_caption():
    labels = [label(box=(100, 100, 200, 120), caption="Near"), label(box=(100, 500, 200, 520), caption="Far")]
    assert ev.nearest_caption((110, 130, 190, 150), 1, labels) == "Near"
    assert ev.nearest_caption((110, 130, 190, 150), 3, labels) == ""


def test_found_fields_reads_field_nodes_and_their_accessible_name():
    named = Node(Role.FIELD, page=1, bbox=(10, 10, 100, 30), attrs={"kind": "text", "aria": "Last name"})
    labelled = Node(Role.FIELD, page=1, bbox=(10, 40, 18, 48), text="Citizen", attrs={"kind": "checkbox"})
    odd = Node(Role.FIELD, page=1, bbox=(10, 60, 90, 80), attrs={"kind": "radio"})
    para = Node(Role.P, page=1, bbox=(10, 90, 90, 100), text="Hello")
    doc = Document(Node(Role.DOC, children=[named, labelled, odd, para]), [PageInfo(1, 612, 792)])
    got = ev.found_fields(Document.from_json(doc.to_json()))
    assert [(f.kind, f.name) for f in got] == [("text", "Last name"), ("checkbox", "Citizen")]


def test_percentiles_are_nearest_rank():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert ev.percentile(values, 0.5) == 5.0 and ev.percentile(values, 0.9) == 9.0
    assert ev.percentile([4.2], 0.9) == 4.2


def test_latency_is_summarised_per_model_and_question_kind(tmp_path):
    assert ev.latency(tmp_path / "missing.csv") == {}
    path = tmp_path / "calls.csv"
    rows = [("fw9-scan", "m", "scan-field", 1, 1.0, "field"), ("fw9-scan", "m", "scan-field", 1, 3.0, "none"),
            ("fw9-scan", "m", "label", 1, 2.0, "c1")]
    path.write_text(",".join(ev.CSV_COLUMNS) + "\n" + "\n".join(",".join(map(str, r)) for r in rows) + "\n")
    assert ev.latency(path) == {("m", "label"): (1, 2.0, 2.0), ("m", "scan-field"): (2, 1.0, 3.0)}
    assert "median" in ev.latency_report(path)


def test_the_replay_provider_refuses_every_call():
    with pytest.raises(ev.ProviderError):
        ev.Refuse().choose(None, b"", None)


def test_the_timed_provider_writes_one_row_per_call_even_when_it_fails():
    class Q:
        page = 3

    class Inner:
        cache_id = "m"

        def choose(self, question, image, crop):
            if image == b"bad":
                raise ev.ProviderError("no")
            return Reply("field", confidence=0.5)

    rows: list[dict] = []
    timed = ev.TimedProvider(Inner(), "w4-scan", rows)
    assert timed.choose(Q(), b"", None) == Reply("field", confidence=0.5)
    with pytest.raises(ev.ProviderError):
        timed.choose(Q(), b"bad", None)
    assert [(r["form"], r["model"], r["kind"], r["page"], r["answer"]) for r in rows] == [
        ("w4-scan", "m", "scan-field", 3, "field"), ("w4-scan", "m", "scan-field", 3, "error")]
    assert all(r["seconds"] >= 0 for r in rows) and set(rows[0]) == set(ev.CSV_COLUMNS)


def test_the_report_names_misses_false_positives_and_unmeasured_assist():
    labels = [label(caption="Last name"), label(box=(100, 300, 200, 320), caption="First name")]
    result = ev.FormResult("w4-scan", labels, ev.score(labels, [found(), found(box=(400, 500, 500, 520), name="x")]), None)
    text = ev.report([result], "m")
    assert "precision 1/2 = 50%" in text and "recall 1/2 = 50%" in text
    assert "'First name'" in text and "near 'First name'" in text and "not measured" in text


def test_real_scans_are_reported_in_their_own_row_apart_from_the_synthetic_forms():
    labels = [label()]
    synthetic = ev.FormResult("w4-scan", labels, ev.score(labels, [found()]), None)
    real = ev.FormResult("census-scan", labels, ev.score(labels, []), None, real=True)
    text = ev.report([synthetic, real], "m")
    [real_row] = [line for line in text.splitlines() if line.startswith("REAL")]
    assert "recall 0/1 = 0%" in real_row
    assert not any(line.startswith("ALL") for line in text.splitlines()), "one synthetic form needs no total"


def test_evaluate_converts_twice_and_replays_the_cache_through_a_refusing_provider(tmp_path, monkeypatch):
    calls = []

    def fake_convert(pdf, out, **options):
        calls.append((Path(out).name, options))
        out.mkdir(parents=True, exist_ok=True)
        fields = [Node(Role.FIELD, page=1, bbox=tuple(f["box"]), attrs={"kind": f["kind"], "aria": f["caption"]})
                  for f in SPEC["fw9-scan"]["fields"][:5]]
        doc = Document(Node(Role.DOC, children=fields), [PageInfo(1, 612, 792)], {"scan_fields": {"calls": 0}})
        (out / "document.json").write_text(doc.to_json())
        if "label_provider" in options:
            assert (out / ASSIST_CACHE).read_text() == cache.read_text()

    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"k": {"model": "m", "answer": "field"}}))
    monkeypatch.setattr(ev, "convert", fake_convert)
    [result] = ev.evaluate(cache_path=cache, model="m", forms={"fw9-scan"}, work=tmp_path / "work")
    assert [name for name, _ in calls] == ["rules", "assist"]
    assert calls[0][1]["scan_fields"] is True and "assist_scan_fields" not in calls[0][1]
    assist = calls[1][1]
    assert assist["assist_scan_fields"] is True and assist["assist_model"] == "m"
    with pytest.raises(ev.ProviderError):
        assist["label_provider"].choose(None, b"", None)
    assert len(result.rules.hits) == 5 and result.assist is not None
    assert result.stats["scan_fields"] == {"calls": 0}

    cache.write_text("{}")
    [unmeasured] = ev.evaluate(cache_path=cache, model="m", forms={"fw9-scan"}, work=tmp_path / "work2")
    assert unmeasured.assist is None and len(calls) == 3
