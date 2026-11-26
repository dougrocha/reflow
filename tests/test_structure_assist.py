"""Opt-in structure assist (analyze.structure_assist): is a form line a heading, a label or a value.
Nothing here calls the network: the provider is a fake or the cache is replayed. These test the
plumbing (trigger, contract, checks, cache, cap, provenance, tree surgery), not what a model says."""

import json

import pytest

from reflow.analyze import structure_assist as sa
from reflow.analyze.vision import ProviderError, Reply
from reflow.analyze.review import queue
from reflow.analyze.sections import nest_sections
from reflow.model import Document, Node, PageInfo, Role, Source
from reflow.run import Options
from testkit import para


def head(text, box, role=Role.H2, page=1, *facts):
    node = Node(role, page=page, bbox=box, text=text, attrs={"font_size": 12.0})
    for code in facts:
        node.fact(code, Source.TAGS)
    return node


def control(box, page=1):
    return Node(Role.FIELD, page=page, bbox=box, attrs={"kind": "text", "name": "f", "label": "none"})


def document(*nodes):
    return Document(Node(Role.DOC, children=nest_sections(list(nodes))),
                    pages=[PageInfo(1, 612, 792), PageInfo(2, 612, 792)], meta={"sha256": "abc"})


class FakeProvider:
    """Answers from a function of the question; records every call. Not a model."""
    cache_id = "fake-model"
    confidence = None

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def choose(self, question, image, crop):
        self.calls.append((question, image, crop))
        answer = self.answer(question) if callable(self.answer) else self.answer
        return Reply(answer, confidence=self.confidence)


def run(doc, provider, tmp_path, **kw):
    return sa.assist(doc, provider, tmp_path / "cache.json",
                     image_for=lambda q: (b"png", q.crop_box((612, 792))), **kw)


def form():
    """A title, a real section heading, and a "Mail:" line with its address under it."""
    return document(
        head("Appeal form", (36, 20, 300, 40), Role.H1),
        head("Applicant", (36, 100, 200, 112)),
        control((36, 120, 300, 140)),
        head("Mail:", (36, 200, 80, 212), Role.H3),
        para("PO Box 1, Phoenix", (100, 200, 300, 212)))


def heading_roles(doc):
    return [(n.text, n.role.value) for n in doc.root.walk() if n.role.value.startswith("h")]


def test_a_negative_call_cap_is_refused():
    with pytest.raises(ValueError):
        Options(assist_structure_max_calls=-1)


def test_only_unconfirmed_form_like_headings_in_a_document_with_controls_are_asked():
    assert [q.text for q in sa.questions(form())] == ["Applicant", "Mail:"]
    # no control anywhere: a heading ending in a colon is just a heading
    assert sa.questions(document(head("Mail:", (36, 200, 80, 212)))) == []
    # the tags confirm it: never asked; the title (h1) is never asked
    confirmed = document(head("Part I:", (36, 100, 200, 112), Role.H2, 1, "role-agreement"),
                         head("Form:", (36, 20, 300, 40), Role.H1), control((36, 120, 300, 140)))
    assert sa.questions(confirmed) == []
    # far from every control and plain text: a title, not asked
    far = document(head("Instructions", (36, 600, 200, 612)), control((36, 120, 300, 140)))
    assert sa.questions(far) == []
    # a sentence-length heading is asked even far from controls
    sentence = "Sign, save, and email this form to the office within thirty days of the date."
    long = document(head(sentence, (36, 600, 500, 612)), control((36, 120, 300, 140)))
    assert [q.text for q in sa.questions(long)] == [sentence]


def test_the_request_text_is_exactly_the_documented_fields():
    q = sa.questions(form())[1]
    sent = json.loads(q.prompt((24, 188, 312, 224)))
    assert set(sent) == {"line", "image", "around"}
    assert set(sent["line"]) == {"text", "font_size", "box"}
    assert all(set(a) == {"where", "kind", "text", "box"} for a in sent["around"])
    assert {"before", "after"} <= {a["where"] for a in sent["around"]}
    assert q.schema()["properties"]["kind"]["enum"] == ["heading", "label", "value", "unsure"]
    assert sa.clip("x" * 500).endswith("…") and len(sa.clip("x" * 500)) == sa.SENT_CHARS


def test_a_label_answer_turns_the_heading_into_a_paragraph_and_rebuilds_the_sections(tmp_path):
    doc = form()
    provider = FakeProvider(lambda q: "label" if q.text == "Mail:" else "heading")
    stats = run(doc, provider, tmp_path)
    assert stats["calls"] == 2 and stats["accepted"] == 1 and stats["confirmed"] == 1
    mail = next(n for n in doc.root.walk() if n.text == "Mail:")
    assert mail.role == Role.P and mail.attrs["form_text"] == "label"
    fact = mail.facts[-1]
    assert fact.code == "structure-assisted" and fact.source == Source.ASSIST
    assert fact.data == {"model": "fake-model", "kind": "label"}
    assert heading_roles(doc) == [("Appeal form", "h1"), ("Applicant", "h2")]
    # the section "Mail:" opened is gone; its address now follows it inside "Applicant"
    applicant = next(n for n in doc.root.walk() if n.role == Role.SECTION)
    assert [c.text for c in applicant.children[1:] if c.text] == ["Mail:", "PO Box 1, Phoenix"]
    item = next(i for i in queue(doc) if i["id"] == mail.id)
    assert item["severity"] == "warning" and item["warn"] == "structure-assisted"


def test_heading_and_unsure_change_nothing(tmp_path):
    for answer, key in (("heading", "confirmed"), ("unsure", "none")):
        doc = form()
        stats = run(doc, FakeProvider(answer), tmp_path)
        assert stats[key] == 2 and stats["accepted"] == 0
        assert heading_roles(doc) == [("Appeal form", "h1"), ("Applicant", "h2"), ("Mail:", "h3")]
        (tmp_path / "cache.json").unlink()


def test_a_line_with_nothing_beside_it_cannot_be_a_label(tmp_path):
    doc = document(head("Appeal form", (36, 20, 300, 40), Role.H1),
                   head("Applicant", (36, 100, 200, 112)), control((36, 120, 300, 140)),
                   head("Mail:", (36, 700, 80, 712)))
    stats = run(doc, FakeProvider("label"), tmp_path)
    assert stats["rejected"] == 1 and stats["accepted"] == 1   # "Applicant" has the control beside it
    assert ("Mail:", "h2") in heading_roles(doc)


def test_a_heading_with_sub_headings_or_the_last_heading_is_never_demoted(tmp_path):
    parent = document(head("Applicant:", (36, 100, 200, 112)), control((36, 120, 300, 140)),
                      head("Name", (36, 160, 100, 172), Role.H3),
                      head("Home", (36, 200, 100, 212), Role.H3, 1, "role-agreement"))
    stats = run(parent, FakeProvider("value"), tmp_path)
    assert ("Applicant:", "h2") in heading_roles(parent) and stats["rejected"] >= 1
    (tmp_path / "cache.json").unlink()
    only = document(head("Mail:", (36, 120, 80, 132)), control((36, 100, 300, 118)))
    assert run(only, FakeProvider("label"), tmp_path)["rejected"] == 1
    assert heading_roles(only) == [("Mail:", "h2")]


def test_a_tag_confirmed_heading_is_not_demoted_even_from_a_stale_cache(tmp_path):
    doc = form()
    run(doc, FakeProvider("label"), tmp_path)
    fresh = form()
    next(n for n in fresh.root.walk() if n.text == "Mail:").fact("tag-heading", Source.TAGS)
    stats = run(fresh, None, tmp_path, model="fake-model")   # replay only; the line is no longer asked
    assert stats["asked"] == 1 and stats["calls"] == 0 and ("Mail:", "h3") in heading_roles(fresh)


def test_cache_replay_makes_no_call_and_the_cap_bounds_the_calls(tmp_path):
    first = FakeProvider("label")
    run(form(), first, tmp_path)
    assert len(first.calls) == 2
    saved = json.loads((tmp_path / "cache.json").read_text())
    assert {e["kind"] for e in saved.values()} == {"structure"}
    assert {e["prompt_version"] for e in saved.values()} == {sa.STRUCTURE_PROMPT_VERSION}
    again = FakeProvider("heading")
    doc = form()
    stats = run(doc, again, tmp_path)
    assert not again.calls and stats["cached"] == 2 and stats["calls"] == 0
    assert ("Mail:", "h3") not in heading_roles(doc)
    (tmp_path / "cache.json").unlink()
    capped = FakeProvider("heading")
    stats = run(form(), capped, tmp_path, max_calls=1)
    assert len(capped.calls) == 1 and stats["skipped"] == 1


def test_a_failed_question_changes_nothing_and_an_unoffered_answer_is_an_error(tmp_path):
    class Failing(FakeProvider):
        def choose(self, question, image, crop):
            raise ProviderError("down")

    doc = form()
    stats = run(doc, Failing("x"), tmp_path)
    assert stats["failed"] == 2 and ("Mail:", "h3") in heading_roles(doc)
    q = sa.questions(form())[0]
    from reflow.analyze import ask
    assert ask.parse_choice('{"kind": "label"}', q) == "label"
    for bad in ('{"kind": "title"}', '{"candidate": "c1"}', "label"):
        with pytest.raises(ProviderError):
            ask.parse_choice(bad, q)


def test_the_cache_key_changes_with_the_prompt_version_and_the_model(monkeypatch):
    q = sa.questions(form())[0]
    base = sa.cache_key("abc", q, "m")
    assert base != sa.cache_key("abc", q, "other") != sa.cache_key("zzz", q, "m")
    monkeypatch.setattr(sa, "STRUCTURE_PROMPT_VERSION", sa.STRUCTURE_PROMPT_VERSION + 1)
    assert sa.cache_key("abc", q, "m") != base


def test_a_decision_model_confidence_is_recorded_on_the_fact_and_in_the_cache(tmp_path):
    class Confident(FakeProvider):
        confidence = 0.9137

    doc = form()
    run(doc, Confident(lambda q: "label" if q.text == "Mail:" else "heading"), tmp_path)
    mail = next(n for n in doc.root.walk() if n.text == "Mail:")
    assert mail.facts[-1].data == {"model": "fake-model", "kind": "label", "confidence": 0.914}
    stored = json.loads((tmp_path / "cache.json").read_text()).values()
    assert {e["confidence"] for e in stored} == {0.914}
    again = form()
    assert run(again, None, tmp_path, model="fake-model")["cached"] == 2
    replayed = next(n for n in again.root.walk() if n.text == "Mail:")
    assert replayed.facts[-1].data["confidence"] == 0.914
