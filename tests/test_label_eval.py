"""The label-assist evaluation harness (scripts/eval_label_assist.py) and its hand-labelled set.

The scripted provider below answers from the labels themselves, so the "assisted" score it
produces proves the replay plumbing works and nothing about any model. The committed
answers-cache.json holds reference answers recorded once with Claude (claude-haiku-5-5) during
development, plus answers recorded with local Ollama models."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from reflow.providers import ASSIST_MODELS
from reflow.analyze import label_assist as la
from reflow.analyze.vision import Reply
from reflow.convert import convert
from reflow.model import Document

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("eval_label_assist", ROOT / "scripts/eval_label_assist.py")
ev = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ev   # dataclasses look the module up by name
spec.loader.exec_module(ev)

needs_fw9 = pytest.mark.skipif(not (ROOT / "samples/fw9.pdf").exists(), reason="sample missing")


def test_the_label_set_covers_the_three_forms_and_is_well_formed():
    forms = json.loads(ev.LABELS.read_text())["forms"]
    assert set(forms) == {"fw9", "uscis-i9", "docaccess-assessment-appeal"}
    for entry in forms.values():
        assert len(entry["labels"]) >= 15
        for label in entry["labels"]:
            assert label["caption"] and label["page"] >= 1 and ("name" in label) != ("at" in label)


def test_the_committed_cache_holds_only_recorded_model_answers():
    """Answers from `reflow convert --assist-labels` (carried ai-assist-cache.json)."""
    cache = json.loads(ev.CACHE.read_text())
    assert cache and any(e["model"] == ASSIST_MODELS["anthropic"] for e in cache.values())
    for entry in cache.values():
        assert entry["kind"] == "label" and entry["model"]
        assert entry["prompt_version"] == la.PROMPT_VERSION
        assert entry["answer"] == "none" or entry["answer"].startswith("c")


def test_normalization_and_matching():
    assert ev.norm("Requester’s name,  and address") == "requesters name and address"


@needs_fw9
def test_rules_only_scores_and_empty_cache_reports_assist_as_not_measured(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    rows = ev.evaluate(cache_path=empty, forms={"fw9"}, work=tmp_path)
    assert len(rows) == 18 and all(r.found for r in rows)
    assert 0 < sum(r.rules for r in rows) < len(rows)
    assert not any(r.cached for r in rows)
    assert [r.rules for r in rows] == [r.assisted for r in rows]   # nothing replayed, nothing changed
    text = ev.report(rows, ASSIST_MODELS["ollama"])
    assert "not measured (no cached answers)" in text


@needs_fw9
def test_replaying_scripted_answers_changes_only_the_asked_fields(tmp_path):
    """Plumbing only: a scripted oracle fills a cache, evaluate() replays it."""
    labels = {(label["page"], label["name"]): label for label in
              json.loads(ev.LABELS.read_text())["forms"]["fw9"]["labels"] if "name" in label}

    class Oracle:
        cache_id = ASSIST_MODELS["ollama"]

        def choose(self, question, image, crop):
            for (page, name), label in labels.items():
                if page == question.page and question.name.endswith(name):
                    for c in question.candidates:
                        if ev.norm(label["caption"]) in ev.norm(c.text):
                            return Reply(c.id)
            return Reply("none")

    out = tmp_path / "fw9"
    convert(ROOT / "samples/fw9.pdf", out, page_images=False, jobs=1)
    doc = Document.from_json((out / "document.json").read_text())
    cache = tmp_path / "cache.json"
    stats = la.assist(doc, Oracle(), cache, image_for=lambda q: (b"", (0, 0, 1, 1)))
    assert stats["calls"] > 0
    rows = ev.evaluate(cache_path=cache, model=ASSIST_MODELS["ollama"], forms={"fw9"},
                       work=tmp_path / "again")
    asked = [r for r in rows if r.cached]
    assert asked and all(r.assisted >= r.rules for r in rows)
    assert all(r.assisted == r.rules for r in rows if not r.cached)
