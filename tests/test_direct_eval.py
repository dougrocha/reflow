"""scripts/eval_direct.py: the spec sample, the scoring by check family, and the table."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "pages"))
_spec = importlib.util.spec_from_file_location("eval_direct", REPO / "scripts" / "eval_direct.py")
ev = sys.modules["eval_direct"] = importlib.util.module_from_spec(_spec)  # for its dataclasses
_spec.loader.exec_module(ev)

from checks import Assertion  # noqa: E402
from specs import PageSpec  # noqa: E402


def spec(i: int, checks=("contains",)) -> PageSpec:
    return PageSpec(f"doc-{i:03d}", "fw9", 1, "",
                    [Assertion(check=c, value=["x"], index=k) for k, c in enumerate(checks)])


def test_a_sample_is_spread_evenly_and_the_same_each_run():
    specs = [spec(i) for i in range(10)]
    assert [s.id for s in ev.sample(specs, 4)] == ["doc-000", "doc-002", "doc-005", "doc-007"]
    assert ev.sample(specs, None) == specs and ev.sample(specs, 50) == specs


def test_a_page_with_no_output_fails_every_check():
    score = ev.Score("m")
    ev.score_page(score, spec(0, ("contains", "heading", "calendar_month")), None)
    assert dict(score.total) == {"contains": 1, "heading": 1, "calendar_month": 1}
    assert sum(score.passed.values()) == 0
    fam = score.by_family()
    assert (fam["text"], fam["headings"], fam["other"], fam["all"], fam["tables"]) == (0, 0, 0, 0, None)


def test_a_page_the_model_left_unread_fails_even_its_negative_checks():
    class Doc:   # an unread page still has its (empty) page wrapper
        page_map = {1: 1}   # the eval converts excerpts

        def page(self, number):
            from checks import parse_html
            return parse_html('<div class="pg" data-page="1"></div>')

        def shell(self):
            return None

        def pdf_text(self, number):
            return ""
    score = ev.Score("m")
    ev.score_page(score, spec(0, ("no_text",)), Doc())
    assert score.passed["no_text"] == 1
    ev.score_page(score, spec(0, ("no_text",)), Doc(), unread=True)
    assert (score.passed["no_text"], score.total["no_text"]) == (1, 2)


def test_the_table_has_a_row_per_model_and_the_check_counts():
    a, b = ev.Score("default", pages=2, seconds=1.0), ev.Score("claude-haiku-5-5", pages=2,
                                                             seconds=10.0, dollars=0.0036)
    for s, ok in ((a, True), (b, False)):
        s.add("contains", ok)
        s.add("table", True)
    rows = ev.table([a, b]).splitlines()
    assert rows[0].endswith("| s/page | $/1,000 pages |")
    assert rows[2] == "| default | 100% | — | — | 100% | — | — | — | — | 100% | 0.5 | — |"
    assert rows[3] == "| claude-haiku-5-5 | 0% | — | — | 100% | — | — | — | — | 50% | 5.0 | $1.80 |"
    assert rows[4] == "| checks | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 2 |  |  |"


def test_the_dollars_per_1000_pages_keep_two_digits_when_small():
    assert ev.per_thousand(ev.Score("polish:claude-haiku-5-5", pages=64, dollars=0.004)) == "$0.062"
    assert ev.per_thousand(ev.Score("glm-ocr", pages=64)) == "$0"
    assert ev.per_thousand(ev.Score("default", pages=64)) == "—"


def test_a_row_names_its_pipeline_model_and_prompt():
    assert ev.Row.parse("default").kind == "default"
    assist = ev.Row.parse("assist:claude-haiku-5-5")
    assert (assist.kind, assist.model) == ("assist", "claude-haiku-5-5")
    assert assist.options()["assist_provider"] == "anthropic" and assist.options()["alt_text"] == "anthropic"
    assert ev.Row.parse("polish:claude-haiku-5-5").options() == {"polish": True, "polish_model": "claude-haiku-5-5"}
    forms = ev.Row.parse("claude-haiku-5-5/forms")
    assert (forms.kind, forms.model, forms.prompt) == ("direct", "claude-haiku-5-5", "forms")
    assert ev.Row.parse("claude-haiku-5-5").prompt == "base"
    import pytest
    with pytest.raises(SystemExit):
        ev.Row.parse("claude-haiku-5-5/nope")


def test_the_assist_tally_counts_each_request_and_refuses_one_past_the_cap(monkeypatch):
    import pytest
    from reflow.analyze import vision

    def fake(self, system, prompt, image, schema=None, max_tokens=2048):
        return vision.Reply("{}", False, vision.Usage(1_000_000, 0))   # $0.10 on Haiku
    monkeypatch.setattr(vision.AnthropicVision, "ask", fake)
    tally = ev.Tally()
    tally.cap, tally.on = 0.201, True   # room for two $0.10 answers, not a third request
    client = vision.AnthropicVision("claude-haiku-5-5", client=object())
    image = vision.VisionImage(b"x", "image/png")
    client.ask("s", "p", image, {})
    client.ask("s", "p", image, {})
    assert tally.dollars == pytest.approx(0.2) and tally.reserved == 0
    with pytest.raises(vision.ProviderError):
        client.ask("s", "p", image, {})
    tally.on = False
    client.ask("s", "p", image, {})          # not counted, not capped
    assert tally.dollars == pytest.approx(0.2)


def test_a_failed_assist_request_counts_the_most_it_could_cost(monkeypatch):
    import pytest
    from reflow.analyze import direct, vision

    def refused(self, system, prompt, image, schema=None, max_tokens=2048):
        raise vision.ProviderError("model stopped: refusal")
    monkeypatch.setattr(vision.AnthropicVision, "ask", refused)
    tally = ev.Tally()
    tally.cap, tally.on = 1.0, True
    client = vision.AnthropicVision("claude-haiku-5-5", client=object())
    with pytest.raises(vision.ProviderError):
        client.ask("s", "p", vision.VisionImage(b"x", "image/png"), {})
    assert tally.dollars == pytest.approx(direct.dollars("claude-haiku-5-5", direct.FIXED_INPUT_TOKENS + 2, 2048))
    assert tally.reserved == 0
