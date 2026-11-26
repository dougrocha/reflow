"""The complexity-routing scorer (scripts/eval_complexity.py), its held-out labels and the
committed cache of real page-vision answers. Nothing here calls a model."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import checkout
from reflow.providers import PAGE_VISION_MODEL
from reflow.analyze import page_vision as pv
from reflow.analyze.vision import ProviderError

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("eval_complexity",
                                              ROOT / "scripts/eval_complexity.py")
ev = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ev
spec.loader.exec_module(ev)

LABELS = json.loads(ev.LABELS.read_text())["pages"]
CENSUS = checkout.sample(ev.REPO, "samples/scan-census-1960.pdf")


def test_every_page_is_labelled_on_every_class():
    assert len(LABELS) >= 90
    assert len({(p["pdf"], p["page"]) for p in LABELS}) == len(LABELS)
    for p in LABELS:
        assert all(p[c] in (True, False, None) for c in ev.CLASSES), p
        assert not p["signature_only"] or p["handwriting"], p   # only on a handwriting page
        assert p["note"] is not None and isinstance(p["seen"], str)


def test_each_class_has_positives_and_negatives_and_unseen_pages():
    for c in ev.CLASSES:
        assert sum(p[c] is True for p in LABELS) >= 5, c
        assert sum(p[c] is False for p in LABELS) >= 50, c
    assert sum(not p["seen"] for p in LABELS) >= 30


def test_the_hard_cases_of_the_issue_are_in_the_set():
    have = {(Path(p["pdf"]).stem, p["page"]) for p in LABELS}
    for doc, pages in {"scan-census-1960": [2], "09-scanned-budget-handwriting": [5],
                       "02-building-permit-applications": [2, 3],
                       "07-financial-audit-comprehensive": [9, 15, 18]}.items():
        assert {(doc, n) for n in pages} <= have


def test_score_counts_each_cell():
    s = ev.score([(True, True), (True, False), (False, True), (False, False), (False, False)])
    assert (s.tp, s.fn, s.fp, s.tn) == (1, 1, 1, 2)
    assert s.precision == 0.5 and s.recall == 0.5
    assert ev.score([(False, False)]).precision is None


def test_signature_only_pages_count_as_negatives_in_the_second_variant():
    row = ev.Page("d", 1, "", "mixed", {"handwriting": True, "poor_scan": False,
                                        "dense_table": False, "diagram": False}, True)
    assert ev.label_of(row, "handwriting") is True
    assert ev.label_of(row, "handwriting", no_signatures=True) is False


def test_the_set_provider_refuses_other_pages_and_never_calls_without_record():
    q = pv.PageQuestion(3, (100, 100), (pv.Block(pv.BlockId("b1"), "n", pv.Word.PARAGRAPH,
                                                 ev.Role.P, (0, 0, 1, 1), "x"),))
    provider = ev.SetProvider("sha", {3}, {}, "m", None)
    with pytest.raises(ProviderError):
        provider.check(pv.PageQuestion(4, q.size, q.blocks), b"")
    with pytest.raises(ProviderError):
        provider.check(q, b"")   # labelled, but no answer cached and no real provider
    reply = {"blocks": [], "complexity": {c.value: c is pv.Complexity.POOR_SCAN
                                          for c in pv.Complexity}}
    cached = ev.SetProvider("sha", {3}, {pv.cache_key("sha", q, "m"): {"reply": reply}}, "m", None)
    assert cached.check(q, b"") == reply and ev.flags(*cached.asked[3]) == {"poor_scan"}


def test_the_committed_cache_holds_only_page_vision_answers():
    cache = json.loads(ev.CACHE.read_text())
    assert len(cache) >= 80
    for entry in cache.values():
        assert entry["kind"] == "page-vision" and entry["model"] == PAGE_VISION_MODEL
        assert entry["prompt_version"] == pv.PROMPT_VERSION and entry["page"] >= 1


@pytest.mark.skipif(not CENSUS.exists(), reason="sample missing")
def test_replaying_the_cache_scores_the_census_scan_and_calls_nothing(tmp_path):
    rows = ev.evaluate(work=tmp_path, only={"scan-census-1960"})
    assert [r.page for r in rows] == [1, 2]
    assert all(r.asked and r.answered for r in rows)   # from the cache: there is no provider
    assert all(r.labels["poor_scan"] for r in rows)
    out = ev.report(rows, PAGE_VISION_MODEL, pages=True)
    assert "| poor_scan | vision raw |" in out and "a predictor gets wrong" in out
