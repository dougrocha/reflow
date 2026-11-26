"""Opt-in label assist (analyze.label_assist). Nothing here calls the network: the provider is a
fake that answers from a script, or the cache is replayed. These test the plumbing (trigger,
contract, geometry check, cache, cap, provenance), not what any model answers."""

import json
from pathlib import Path

import pytest

from reflow.providers import ASSIST_MODELS
from reflow import providers
from reflow.analyze import ask
from reflow.analyze import label_assist as la
from reflow.analyze.review import queue
from reflow.analyze.vision import ProviderUnavailable, Reply
from reflow.convert import convert
from reflow.model import Document, Node, PageInfo, Role, Source
from reflow.render.site import render_html
from reflow.run import Options
from reflow.names import ASSIST_CACHE
from testkit import para

SAMPLES = Path(__file__).parent.parent / "samples"


def field(box=(100, 200, 200, 214), page=1, **attrs):
    node = Node(Role.FIELD, page=page, bbox=box,
                attrs={"kind": "text", "fid": "f1-1", "name": "f1", "label": "none", **attrs})
    node.fact("label-missing", Source.HEURISTIC, "no tooltip, printed label or cell header")
    return node


def document(*nodes):
    return Document(Node(Role.DOC, children=list(nodes)), pages=[PageInfo(1, 612, 792)],
                    meta={"sha256": "abc"})


class FakeProvider:
    """Answers from a function of the question; records every call. Not a model."""
    cache_id = "fake-model"

    def __init__(self, answer, confidence=None):
        self.answer, self.confidence = answer, confidence
        self.calls = []

    def choose(self, question, image, crop):
        self.calls.append((question, image, crop))
        answer = self.answer(question) if callable(self.answer) else self.answer
        return Reply(answer, confidence=self.confidence)


def png(question):
    return b"png", question.crop_box((612, 792))


def run(doc, provider, tmp_path, **kw):
    return la.assist(doc, provider, tmp_path / "cache.json", image_for=png, **kw)


def test_an_unnamed_field_takes_the_chosen_caption_and_records_who_chose(tmp_path):
    f = field()
    doc = document(para("First name", (100, 180, 160, 192)), para("Notes", (400, 500, 440, 512)), f)
    provider = FakeProvider("c1")
    stats = run(doc, provider, tmp_path)
    assert stats["accepted"] == 1 and stats["calls"] == 1
    assert f.attrs["aria"] == "First name" and f.attrs["label"] == "assist"
    assert [x.code for x in f.facts] == ["label-assisted"]
    fact = f.facts[0]
    assert fact.source == Source.ASSIST and fact.data["model"] == "fake-model"
    assert fact.data["candidate"] == doc.root.children[0].id
    question, image, crop = provider.calls[0]
    assert [c.text for c in question.candidates] == ["First name"]   # the far one is not offered
    assert image == b"png" and crop[0] < 100 <= 100 < crop[2]


def test_the_assisted_answer_is_a_review_warning_and_is_marked_in_the_html(tmp_path):
    f = field()
    doc = document(para("First name", (100, 180, 160, 192)), f)
    run(doc, FakeProvider("c1"), tmp_path)
    items = queue(doc)
    item = next(i for i in items if i["id"] == f.id)
    assert item["severity"] == "warning" and item["warn"] == "label-assisted"
    render_html(doc, "Test", tmp_path, page_widths=(800,))
    html = "".join(p.read_text() for p in tmp_path.rglob("*.html"))
    assert 'aria-label="First name"' in html and 'data-label-source="assist"' in html


def test_none_leaves_the_field_as_the_rules_had_it(tmp_path):
    f = field()
    doc = document(para("First name", (100, 180, 160, 192)), f)
    stats = run(doc, FakeProvider("none"), tmp_path)
    assert stats["none"] == 1 and stats["accepted"] == 0
    assert "aria" not in f.attrs and f.attrs["label"] == "none"
    assert [x.code for x in f.facts] == ["label-missing"]


def test_an_id_the_model_was_not_offered_is_a_failure_not_a_label(tmp_path):
    f = field()
    doc = document(para("First name", (100, 180, 160, 192)), f)
    stats = run(doc, FakeProvider("c9"), tmp_path)
    assert stats["failed"] == 1 and "aria" not in f.attrs
    assert not (tmp_path / "cache.json").exists() or "c9" not in (tmp_path / "cache.json").read_text()


def test_an_answer_the_geometry_contradicts_is_rejected(tmp_path):
    f = field()
    # 80 points right of the field: close enough to be offered, too far to be its caption.
    doc = document(para("Far caption", (280, 200, 340, 212)), f)
    stats = run(doc, FakeProvider("c1"), tmp_path)
    assert stats["rejected"] == 1 and stats["accepted"] == 0
    assert "aria" not in f.attrs and f.facts[0].code == "label-missing"


def test_a_field_with_a_tooltip_or_a_printed_label_is_never_asked(tmp_path):
    named = Node(Role.FIELD, page=1, bbox=(100, 200, 200, 214),
                 attrs={"kind": "text", "label": "tooltip", "aria": "Name"})
    printed = Node(Role.FIELD, page=1, bbox=(100, 300, 200, 314), text="City",
                   attrs={"kind": "text", "label": "printed"})
    doc = document(para("Name", (100, 180, 160, 192)), named, printed)
    provider = FakeProvider("c1")
    assert run(doc, provider, tmp_path)["asked"] == 0 and not provider.calls


def test_a_guessed_name_is_asked_only_with_a_real_choice_and_may_be_kept(tmp_path):
    guessed = Node(Role.FIELD, page=1, bbox=(100, 200, 200, 214),
                   attrs={"kind": "text", "label": "printed-nearby", "aria": "Social security number"})
    lone = document(para("Employer number", (100, 180, 160, 192)), guessed)
    # The guess is offered as a candidate too, so one neighbour is already a choice of two.
    q = la.questions(lone)
    assert [c.text for c in q[0].candidates] == ["Employer number", "Social security number"]
    stats = run(lone, FakeProvider("c2"), tmp_path)
    assert stats["confirmed"] == 1 and guessed.attrs["label"] == "printed-nearby"
    assert not any(f.code == "label-assisted" for f in guessed.facts)
    # With nothing nearby to choose from, there is no question at all.
    empty = Node(Role.FIELD, page=1, bbox=(100, 200, 200, 214),
                 attrs={"kind": "text", "label": "cell-header", "aria": "Header"})
    assert la.questions(document(empty)) == []


def test_answers_are_cached_by_pdf_page_question_model_and_prompt_version(tmp_path, monkeypatch):
    def build():
        f = field()
        return document(para("First name", (100, 180, 160, 192)), f), f

    doc, _ = build()
    provider = FakeProvider("c1")
    run(doc, provider, tmp_path)
    assert len(provider.calls) == 1
    # Replay: no provider at all, same answer applied.
    again, f2 = build()
    stats = run(again, None, tmp_path, model="fake-model")
    assert stats["cached"] == 1 and stats["calls"] == 0 and f2.attrs["aria"] == "First name"
    # Another model, another PDF, another question text or another prompt version is a miss.
    other, f3 = build()
    assert run(other, None, tmp_path, model="other-model")["skipped"] == 1
    assert "aria" not in f3.attrs
    changed, f4 = build()
    changed.meta["sha256"] = "different"
    assert run(changed, None, tmp_path, model="fake-model")["skipped"] == 1
    moved, _ = build()
    moved.root.children[0].set_text("Given name")
    assert run(moved, None, tmp_path, model="fake-model")["skipped"] == 1
    q = la.questions(build()[0])[0]
    keys = {la.cache_key("abc", q, "m"), la.cache_key("abc", q, "n")}
    monkeypatch.setattr(la, "PROMPT_VERSION", la.PROMPT_VERSION + 1)
    keys.add(la.cache_key("abc", q, "m"))
    assert len(keys) == 3
    monkeypatch.undo()
    stored = json.loads((tmp_path / "cache.json").read_text())
    assert next(iter(stored.values()))["prompt_version"] == la.PROMPT_VERSION


def test_calls_per_document_are_capped_and_the_rest_left_to_the_rules(tmp_path):
    fields = [field(box=(100, 200 + 100 * i, 200, 214 + 100 * i), name=f"f{i}") for i in range(3)]
    doc = document(*[para(f"Caption {i}", (100, 180 + 100 * i, 160, 192 + 100 * i))
                     for i in range(3)], *fields)
    provider = FakeProvider("c1")
    stats = run(doc, provider, tmp_path, max_calls=2)
    assert stats["calls"] == 2 and stats["skipped"] == 1 and len(provider.calls) == 2
    assert [("aria" in f.attrs) for f in fields] == [True, True, False]
    assert run(doc, FakeProvider("c1"), tmp_path, max_calls=0)["calls"] == 0


def test_an_unavailable_provider_stops_but_keeps_answers_already_paid_for(tmp_path):
    fields = [field(box=(100, 200 + 100 * i, 200, 214 + 100 * i), name=f"f{i}") for i in range(2)]
    doc = document(*[para(f"Caption {i}", (100, 180 + 100 * i, 160, 192 + 100 * i))
                     for i in range(2)], *fields)

    def answer(question):
        if question.page and len(provider.calls) > 1:
            raise ProviderUnavailable("no key")
        return "c1"

    provider = FakeProvider(answer)
    with pytest.raises(ProviderUnavailable):
        run(doc, provider, tmp_path)
    assert len(json.loads((tmp_path / "cache.json").read_text())) == 1


def test_the_schema_offers_the_candidates_and_none():
    q = la.questions(document(para("A", (100, 180, 160, 192)), field()))[0]
    assert q.schema()["properties"]["candidate"]["enum"] == ["c1", "none"]


def test_the_request_text_is_exactly_the_documented_fields():
    f = field(tooltip="Applicant", name="topmost.name")
    q = la.questions(document(para("Name", (100, 180, 160, 192)), f))[0]
    sent = json.loads(q.prompt((88, 168, 212, 226)))
    assert set(sent) == {"control", "image", "candidates"}
    assert set(sent["control"]) == {"kind", "pdf_name", "pdf_tooltip", "box"}
    assert set(sent["candidates"][0]) == {"id", "text", "box"}
    assert sent["control"]["pdf_name"] == "topmost.name"


def test_options_reject_a_negative_cap():
    with pytest.raises(ValueError):
        Options(assist_labels_max_calls=-1)


@pytest.mark.skipif(not (SAMPLES / "fw9.pdf").exists(), reason="sample missing")
def test_convert_is_off_by_default_and_replays_from_the_cache_when_on(tmp_path, monkeypatch):
    pdf = SAMPLES / "fw9.pdf"
    off = convert(pdf, tmp_path / "off", page_images=False)
    assert off.label_assist == {} and "label_assist" not in off.seconds
    assert "label-assisted" not in off.facts
    assert "label_assist" not in json.loads((tmp_path / "off/document.json").read_text())["meta"]

    provider = FakeProvider("c1")
    first = convert(pdf, tmp_path / "on", page_images=False, assist_labels=True,
                    label_provider=provider)
    assert first.label_assist["calls"] == len(provider.calls) > 0
    assert first.label_assist["model"] == ASSIST_MODELS["ollama"]
    saved = json.loads((tmp_path / "on" / ASSIST_CACHE).read_text())
    assert len(saved) == first.label_assist["calls"]

    # Second run, same output directory: the carried cache answers; no provider may be built.
    def refuse(*a, **k):
        raise AssertionError("the network provider must not be created or asked")

    monkeypatch.setattr(ask.AnthropicChoiceProvider, "choose", refuse)
    second = convert(pdf, tmp_path / "on", page_images=False, assist_labels=True)
    assert second.label_assist["calls"] == 0
    assert second.label_assist["cached"] == first.label_assist["calls"]
    assert second.facts == first.facts


def test_ollama_is_the_default_provider_and_anthropic_the_only_other():
    assert Options().assist_provider == providers.DEFAULT_ASSIST_PROVIDER == "ollama"
    assert Options().assist_model is None and Options().assist_model_id == ASSIST_MODELS["ollama"]
    assert Options(assist_provider="anthropic").assist_model_id == "claude-haiku-5-5"
    assert Options(assist_model="m").assist_model_id == "m"
    with pytest.raises(ValueError):
        Options(assist_provider="claude-cli")
    with pytest.raises(ValueError):
        Options(assist_model=" ")


# -- the wrapped-caption trigger -------------------------------------------------------------


def printed(text, kind="text", **attrs):
    return Node(Role.FIELD, page=1, bbox=(100, 200, 200, 214), text=text,
                attrs={"kind": kind, "name": "f", "label": "printed", **attrs})


def test_a_printed_label_that_is_the_tail_of_a_wrapped_caption_is_asked_about():
    assert la.is_fragment(printed("code (if any)"))
    assert la.is_fragment(printed("entity’s name on line 2.)"))
    assert not la.is_fragment(printed("Social security number"))
    assert not la.is_fragment(printed("Name (as shown)"))
    assert not la.is_fragment(printed("yes", kind="checkbox"))
    assert not la.is_fragment(printed("code (if any)", aria="Exemption code"))
    assert not la.is_fragment(printed("code (if any)", label="tooltip"))
    assert not la.is_fragment(printed("a Qualified tips. If your total income is less than $150,000"))
    assert la.is_fragment(printed("a person who is a spouse"))


def test_a_fragment_label_is_offered_back_as_the_rules_guess_and_can_be_replaced(tmp_path):
    f = printed("code (if any)")
    doc = document(para("Exemption code", (100, 170, 190, 182)),
                   para("Other caption", (100, 150, 190, 162)), f)
    q = la.questions(doc)[0]
    assert [c.text for c in q.candidates][-1] == "code (if any)"
    stats = run(doc, FakeProvider("c1"), tmp_path)
    assert stats["accepted"] == 1 and f.attrs["aria"] == "Exemption code"
    kept = document(para("Exemption code", (100, 170, 190, 182)),
                    para("Other caption", (100, 150, 190, 162)), printed("code (if any)"))
    stats = la.assist(kept, FakeProvider("c3"), tmp_path / "other.json", image_for=png)
    assert stats["confirmed"] == 1


def test_a_long_caption_is_offered_whole_but_sent_clipped_with_exact_ids(tmp_path):
    long = ("Name of entity/individual. For a sole proprietor or disregarded entity, enter the "
            "owner's name on line 1, and enter the business/disregarded entity's name on line 2 "
            "(see instructions for the reasons).")
    assert 160 < len(long) <= la.MAX_CANDIDATE_CHARS
    q = la.questions(document(para(long, (100, 180, 500, 192)), field()))[0]
    cand = q.candidates[0]
    assert cand.text == long and cand.id == "c1"
    sent = json.loads(q.prompt((88, 168, 512, 226)))["candidates"][0]
    assert sent["id"] == "c1" and len(sent["text"]) <= la.SENT_CANDIDATE_CHARS
    assert sent["text"].startswith("Name of entity") and sent["text"].endswith("reasons).")
    doc = document(para(long, (100, 180, 500, 192)), field())
    run(doc, FakeProvider("c1"), tmp_path)
    assert next(n for n in doc.root.walk() if n.role == Role.FIELD).attrs["aria"] == long
    assert ask.clip_ends("short", la.SENT_CANDIDATE_CHARS) == "short"


# -- confidence ---------------------------------------------------------------------------


def test_the_confidence_is_recorded_in_the_cache_and_on_the_fact_and_survives_replay(
        monkeypatch, tmp_path):
    f = field()
    doc = document(para("First name", (100, 180, 160, 192)), f)
    stats = run(doc, FakeProvider("c1", confidence=0.8123), tmp_path)
    assert stats["accepted"] == 1 and f.facts[0].data["confidence"] == 0.812
    cached = list(json.loads((tmp_path / "cache.json").read_text()).values())
    assert cached[0]["confidence"] == 0.812
    g = field()
    again = document(para("First name", (100, 180, 160, 192)), g)
    assert run(again, None, tmp_path, model="fake-model")["cached"] == 1
    assert g.facts[0].data["confidence"] == 0.812


def test_a_provider_without_a_confidence_leaves_the_fact_without_one(tmp_path):
    f = field()
    run(document(para("First name", (100, 180, 160, 192)), f), FakeProvider("c1"), tmp_path)
    assert "confidence" not in f.facts[0].data
