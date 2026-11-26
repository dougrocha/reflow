"""The structure-assist evaluation (scripts/eval_structure_assist.py), its hand-labelled lines and
the committed cache of real model answers. Nothing here calls a model."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import checkout
from reflow.providers import ASSIST_MODELS
from reflow.analyze import structure_assist as sa
from reflow.analyze.vision import Reply
from reflow.convert import convert
from reflow.names import ASSIST_CACHE

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("eval_structure_assist", ROOT / "scripts/eval_structure_assist.py")
ev = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ev
spec.loader.exec_module(ev)

FORM = "az-ma-appeals-form-2026"
PDF = checkout.sample(ev.REPO, json.loads(ev.LABELS.read_text())["forms"][FORM]["pdf"])
needs_pdf = pytest.mark.skipif(not PDF.exists(), reason="sample missing")


def test_the_labelled_lines_are_well_formed():
    forms = json.loads(ev.LABELS.read_text())["forms"]
    assert len(forms) >= 6 and any(name.startswith("az-") for name in forms)
    lines = [line for entry in forms.values() for line in entry["lines"]]
    assert len(lines) >= 30
    assert {line["kind"] for line in lines} == {"heading", "label", "value"}
    for line in lines:
        assert line["text"] and line["page"] >= 1 and len(line["at"]) == 2


def test_the_committed_cache_holds_only_recorded_structure_answers():
    cache = json.loads(ev.CACHE.read_text())
    assert cache and any(e["model"] == ASSIST_MODELS["anthropic"] for e in cache.values())
    for entry in cache.values():
        assert entry["kind"] == "structure" and entry["model"]
        assert entry["prompt_version"] == sa.STRUCTURE_PROMPT_VERSION
        assert entry["answer"] in sa.KINDS


@needs_pdf
def test_replaying_the_committed_cache_beats_the_rules_and_calls_nothing():
    rows = ev.evaluate(model=ASSIST_MODELS["anthropic"], forms={FORM})
    assert len(rows) == 7 and all(r.found for r in rows)
    assert sum(r.assisted for r in rows) > sum(r.rules for r in rows)
    assert all(r.cached for r in rows if not r.rules)  # each line the rules miss, from the cache
    assert "rules + cached assist" in ev.report(rows, ASSIST_MODELS["anthropic"])


@needs_pdf
def test_convert_is_off_by_default_and_replays_the_cache_when_on(tmp_path):
    off = convert(PDF, tmp_path / "off", page_images=False)
    assert off.structure_assist == {} and "structure_assist" not in off.seconds
    assert "structure-assisted" not in off.facts
    out = tmp_path / "on"
    out.mkdir()
    (out / ASSIST_CACHE).write_text(ev.CACHE.read_text())

    class Refuse:
        """Replays the cache. The three section headings it does not hold were taken as the
        label of the first box under them before each box took its own caption, so the
        committed answers predate them."""
        cache_id = ASSIST_MODELS["anthropic"]
        NEW = {"Member Information", "Information about your plan", "Information about your request"}

        def choose(self, question, *a):
            assert question.text in self.NEW, "a replayed answer must not call the provider"
            return Reply("heading")

    on = convert(PDF, out, page_images=False, assist_structure=True,
                  assist_provider="anthropic", label_provider=Refuse())
    assert on.structure_assist["calls"] == len(Refuse.NEW) and on.structure_assist["accepted"] > 0
    assert on.facts["structure-assisted"] == on.structure_assist["accepted"]
