"""The machinery the assist passes share (analyze.ask): the choice providers over a fake Ollama
server, the cache store, the call budget and the provider-unavailable policy, the choice loop
and the crop renderer. What each pass asks and how it applies the answer is tested with the
pass."""

import base64
import io
import json
import urllib.error
from dataclasses import dataclass

import pytest
from PIL import Image

from conftest import make_pdf
from reflow.analyze import ask, vision
from reflow.analyze.vision import ProviderError, ProviderUnavailable, Reply
from testkit import FakeHttp

BOX = (0, 0, 1, 1)


@dataclass(frozen=True)
class Pick(ask.Question):
    page: int
    node: str

    answer_key = "pick"
    system_prompt = "Pick one."

    @property
    def choices(self):
        return ["a", "b", ask.NONE]

    @property
    def instruction(self):
        return "Your answer must be one of a, b, none."

    @property
    def criteria(self):
        return {"a": "the first", "b": "the second", ask.NONE: "neither"}

    def boxes(self):
        return [(100, 200, 140, 220), (300, 400, 340, 420)]

    def prompt(self, crop):
        return json.dumps({"crop": crop})


QUESTION = Pick(1, "n1")


def chat(content, **more):
    return {"message": {"content": content}, **more}


def decision(choice, confidence=0.947, key=vision.DECISION_QUESTION):
    probs = {"a": 0.99, "b": 0.005, "none": 0.005}
    return {"usage": {"input_tokens": 486, "output_tokens": 3},
            "answers": {key: {"type": "choice", "choice": choice, "probabilities": probs,
                              "confidence": confidence}}}


# -- the question ----------------------------------------------------------------------------


def test_the_crop_box_pads_the_union_of_the_boxes_and_stays_on_the_page():
    pad = ask.CROP_PAD
    assert QUESTION.crop_box((612, 792)) == (100 - pad, 200 - pad, 340 + pad, 420 + pad)
    assert QUESTION.crop_box((320, 400)) == (100 - pad, 200 - pad, 320, 400)


def test_the_schema_is_an_enum_of_the_choices():
    assert QUESTION.schema() == {
        "type": "object", "properties": {"pick": {"type": "string", "enum": ["a", "b", "none"]}},
        "required": ["pick"], "additionalProperties": False}


def test_the_structured_reply_must_name_an_offered_id():
    assert ask.parse_choice('{"pick": "a"}', QUESTION) == "a"
    for bad in ("a", '{"pick": "c"}', '{"other": "a"}', "[]", ""):
        with pytest.raises(ProviderError):
            ask.parse_choice(bad, QUESTION)


# -- the providers ---------------------------------------------------------------------------


def test_the_ollama_request_carries_the_image_the_schema_and_a_fixed_seed(monkeypatch):
    fake = FakeHttp(chat('{"pick": "a"}'))
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    monkeypatch.setenv("OLLAMA_HOST", "box:9999")
    reply = ask.OllamaChoiceProvider("m").choose(QUESTION, b"PNG", BOX)
    assert reply.text == "a" and reply.confidence is None
    sent = json.loads(fake.requests[0].data)
    assert fake.requests[0].full_url == "http://box:9999/api/chat"
    assert sent["model"] == "m" and sent["stream"] is False and sent["format"] == QUESTION.schema()
    assert sent["options"] == {"temperature": 0, "seed": 0}
    assert sent["messages"][1]["images"] == [base64.b64encode(b"PNG").decode()]


def test_an_ollama_reply_may_wrap_the_object_in_a_code_fence(monkeypatch):
    monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(chat('```json\n{"pick": "b"}\n```')))
    assert ask.OllamaChoiceProvider("m").choose(QUESTION, b"png", BOX).text == "b"


def test_ollama_replies_outside_the_enum_are_failed_questions(monkeypatch):
    for content in ('{"pick": "c"}', "free text", '{"other": 1}', ""):
        monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(chat(content)))
        with pytest.raises(ProviderError):
            ask.OllamaChoiceProvider("m").choose(QUESTION, b"png", BOX)


def test_ollama_down_or_model_missing_is_unavailable_and_a_timeout_is_one_failed_question(
        monkeypatch):
    refused = FakeHttp(error=urllib.error.URLError(ConnectionRefusedError()))
    monkeypatch.setattr("reflow.analyze.vision.send", refused)
    with pytest.raises(ProviderUnavailable, match="ollama serve"):
        ask.OllamaChoiceProvider("m").choose(QUESTION, b"png", BOX)
    missing = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":"model \'m\' not found"}'))
    monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(error=missing))
    with pytest.raises(ProviderUnavailable, match="ollama pull m"):
        ask.OllamaChoiceProvider("m").choose(QUESTION, b"png", BOX)
    monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(error=TimeoutError()))
    with pytest.raises(ProviderError):
        ask.OllamaChoiceProvider("m").choose(QUESTION, b"png", BOX)


def test_a_decision_model_is_asked_one_choice_question_on_the_systemone_endpoint(monkeypatch):
    fake = FakeHttp(decision("a"))
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    reply = ask.OllamaChoiceProvider("clef-flash:9b").choose(QUESTION, b"PNG", BOX)
    sent = json.loads(fake.requests[0].data)
    assert fake.requests[0].full_url == "http://localhost:11434/v1/systemone"
    assert sent["model"] == "clef-flash:9b" and sent["images"] == [base64.b64encode(b"PNG").decode()]
    assert sent["state"] == QUESTION.prompt(BOX)
    asked = sent["questions"][vision.DECISION_QUESTION]
    assert asked["type"] == "choice" and list(asked["criteria"]) == ["a", "b", "none"]
    assert asked["criteria"]["a"] == "the first" and QUESTION.system_prompt in asked["instructions"]
    assert "messages" not in sent and "format" not in sent
    assert reply.text == "a" and reply.confidence == pytest.approx(0.947)
    assert (reply.usage.tokens_in, reply.usage.tokens_out) == (486, 3)


def test_a_chat_model_keeps_the_chat_endpoint_with_thinking_off_and_reports_its_usage(monkeypatch):
    fake = FakeHttp(chat('{"pick": "none"}', eval_count=4, prompt_eval_count=90, eval_duration=7,
                         model="x"))
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    reply = ask.OllamaChoiceProvider("qwen3-vl:8b").choose(QUESTION, b"PNG", BOX)
    assert reply.text == "none" and reply.confidence is None
    assert json.loads(fake.requests[0].data)["think"] is False
    assert reply.usage.stats == {"eval_count": 4, "prompt_eval_count": 90, "eval_duration": 7}


def test_the_decision_model_table_is_keyed_on_the_name_without_its_tag():
    assert vision.is_decision_model("clef-flash:9b") and vision.is_decision_model("d1:3b")
    assert vision.is_decision_model("D1") and not vision.is_decision_model("qwen3-vl:8b")
    assert not vision.is_decision_model("d10:3b")


def test_a_thinking_only_tag_gets_an_empty_think_turn_so_it_answers(monkeypatch):
    assert vision.needs_empty_think("qwen3-vl:8b") and vision.needs_empty_think("Qwen3-VL:30b")
    assert not vision.needs_empty_think("qwen3-vl:8b-instruct")
    assert not vision.needs_empty_think("gemma4:12b")
    for model, roles in (("qwen3-vl:8b", ["system", "user", "assistant"]),
                         ("gemma4:12b", ["system", "user"])):
        fake = FakeHttp(chat('{"pick": "a"}'))
        monkeypatch.setattr("reflow.analyze.vision.send", fake)
        ask.OllamaChoiceProvider(model).choose(QUESTION, b"PNG", BOX)
        assert [m["role"] for m in json.loads(fake.requests[0].data)["messages"]] == roles


def test_bad_decision_replies_are_failed_questions(monkeypatch):
    bad = [decision("c"), decision("a", key="other"), {"answers": {}}, [],
           {"answers": {vision.DECISION_QUESTION: {"type": "score", "score": 1}}}]
    for reply in bad:
        monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(reply))
        with pytest.raises(ProviderError):
            ask.OllamaChoiceProvider("d1:3b").choose(QUESTION, b"png", BOX)


def test_a_missing_confidence_is_none(monkeypatch):
    reply = decision("none")
    del reply["answers"][vision.DECISION_QUESTION]["confidence"]
    monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(reply))
    answer = ask.OllamaChoiceProvider("d1:3b").choose(QUESTION, b"png", BOX)
    assert (answer.text, answer.confidence) == ("none", None)


def test_the_anthropic_provider_needs_the_sdk_and_the_key_only_when_it_asks(monkeypatch):
    provider = ask.AnthropicChoiceProvider()   # constructing it imports nothing
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        import anthropic  # noqa: F401
    except ImportError:
        with pytest.raises(ProviderUnavailable, match="anthropic package"):
            provider.choose(QUESTION, b"png", BOX)
    else:
        with pytest.raises(ProviderUnavailable, match="ANTHROPIC_API_KEY"):
            provider.choose(QUESTION, b"png", BOX)


def test_make_provider_picks_the_named_provider_and_model():
    provider = ask.make_provider("anthropic", "some-model")
    assert isinstance(provider, ask.AnthropicChoiceProvider) and provider.cache_id == "some-model"
    assert isinstance(ask.make_provider("ollama"), ask.OllamaChoiceProvider)
    with pytest.raises(ValueError):
        ask.make_provider("claude-cli")


# -- the cache -------------------------------------------------------------------------------


def test_a_missing_or_unreadable_cache_is_empty_and_only_an_object_counts(tmp_path):
    path = tmp_path / "cache.json"
    assert ask.load_cache(path) == {}
    path.write_text("not json")
    assert ask.load_cache(path) == {}
    path.write_text("[1]")
    assert ask.load_cache(path) == {}


def test_a_store_saves_only_what_it_changed_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "cache.json"
    store = ask.Store(path)
    store.save()
    assert not path.exists()
    store.put("k", "label", answer="a")
    store.save()
    assert json.loads(path.read_text()) == {"k": {"kind": "label", "answer": "a"}}
    assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]
    assert ask.Store(path).get("k") == {"kind": "label", "answer": "a"}
    assert ask.Store(path).get("other") is None


def test_a_store_keeps_the_other_passes_entries(tmp_path):
    path = tmp_path / "cache.json"
    path.write_text(json.dumps({"theirs": {"kind": "scan-field", "answer": "field"}}))
    with ask.Store(path).saving() as store:
        store.put("mine", "label", answer="a")
    assert set(json.loads(path.read_text())) == {"theirs", "mine"}


def test_a_store_saves_on_the_way_out_of_an_exception(tmp_path):
    path = tmp_path / "cache.json"
    with pytest.raises(ProviderUnavailable):
        with ask.Store(path).saving() as store:
            store.put("k", "label", answer="a")
            raise ProviderUnavailable("gone")
    assert "k" in json.loads(path.read_text())


# -- the asker -------------------------------------------------------------------------------


def asker(tmp_path, provider=object(), *, max_calls=5, degrade=False, version=1):
    stats = {"cached": 0, "calls": 0, "failed": 0}
    store = ask.Store(tmp_path / "cache.json")
    return ask.Asker(store, provider, kind="label", model="m", version=version,
                     max_calls=max_calls, stats=stats, degrade=degrade), stats


def test_a_call_is_recorded_with_its_model_and_prompt_version_and_replayed(tmp_path):
    a, stats = asker(tmp_path)
    got = a.fetch("k", page=3, node="n", valid=lambda e: "answer" in e,
                  call=lambda: {"answer": "a"})
    assert got.status is ask.Status.ASKED and stats["calls"] == 1
    assert got.entry == {"kind": "label", "answer": "a", "model": "m", "prompt_version": 1,
                         "page": 3, "node": "n"}
    again = a.fetch("k", page=3, valid=lambda e: "answer" in e, call=lambda: pytest.fail("call"))
    assert again.status is ask.Status.CACHED and stats["cached"] == 1 and stats["calls"] == 1


def test_an_entry_the_pass_does_not_accept_is_asked_again(tmp_path):
    a, stats = asker(tmp_path)
    a.store.put("k", "label", answer="stale")
    got = a.fetch("k", page=1, valid=lambda e: e["answer"] == "a", call=lambda: {"answer": "a"})
    assert got.status is ask.Status.ASKED and got.entry["answer"] == "a"


def test_the_budget_and_a_missing_provider_skip_the_call(tmp_path):
    a, stats = asker(tmp_path, max_calls=1)
    assert a.fetch("1", page=1, valid=bool, call=lambda: {"x": 1}).status is ask.Status.ASKED
    assert a.fetch("2", page=1, valid=bool, call=lambda: pytest.fail("call")).status is \
        ask.Status.SKIPPED
    assert a.why_not() == "call budget"
    replay, _ = asker(tmp_path, None)
    assert replay.fetch("3", page=1, valid=bool, call=lambda: pytest.fail("call")).status is \
        ask.Status.SKIPPED
    assert replay.why_not() == "replay only"


def test_a_failed_call_counts_and_is_not_cached_unless_errors_are_recorded(tmp_path):
    def fail():
        raise ProviderError("the token limit")

    a, stats = asker(tmp_path)
    got = a.fetch("k", page=1, valid=bool, call=fail)
    assert (got.status, got.error) == (ask.Status.FAILED, "the token limit")
    assert stats["failed"] == 1 and a.store.get("k") is None
    got = a.fetch("k", page=1, valid=bool, call=fail, record_errors=True)
    assert a.store.get("k")["error"] == "the token limit"
    replayed = a.fetch("k", page=1, valid=bool, call=lambda: pytest.fail("call"),
                       record_errors=True)
    assert (replayed.status, replayed.error) == (ask.Status.CACHED, "the token limit")


def test_an_unavailable_provider_stops_the_pass_unless_the_pass_degrades(tmp_path):
    def gone():
        raise ProviderUnavailable("no server")

    a, _ = asker(tmp_path)
    with pytest.raises(ProviderUnavailable):
        a.fetch("k", page=1, valid=bool, call=gone)
    b, _ = asker(tmp_path, degrade=True)
    got = b.fetch("k", page=1, valid=bool, call=gone)
    assert (got.status, got.error) == (ask.Status.UNAVAILABLE, "no server")
    assert b.fetch("j", page=1, valid=bool, call=lambda: pytest.fail("call")).status is \
        ask.Status.SKIPPED
    assert b.why_not() == "unavailable"


# -- the choice loop -------------------------------------------------------------------------


class Chooser:
    cache_id = "chooser"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def choose(self, question, image, crop):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Reply) else Reply(answer)


def loop(tmp_path, provider, questions, *, max_calls=5):
    stats = dict.fromkeys(("asked", "cached", "accepted", "none", "failed", "skipped", "calls"), 0)
    applied = []

    def apply(question, answer, confidence):
        applied.append((question.node, answer, confidence))
        return "accepted"

    with ask.Store(tmp_path / "cache.json").saving() as store:
        asker = ask.Asker(store, provider, kind="label", model="m", version=1,
                          max_calls=max_calls, stats=stats)
        ask.ask_choices(questions, lambda q: q.node, apply, asker,
                        lambda q: (b"png", q.crop_box((612, 792))))
    return stats, applied


def test_answers_are_applied_none_is_counted_and_a_failure_applies_nothing(tmp_path):
    qs = [Pick(1, f"n{i}") for i in range(4)]
    chooser = Chooser(Reply("a", confidence=0.9), "none", ProviderError("bad"), "b")
    stats, applied = loop(tmp_path, chooser, qs)
    assert applied == [("n0", "a", 0.9), ("n3", "b", None)]
    assert stats == {"asked": 3, "cached": 0, "accepted": 2, "none": 1, "failed": 1,
                     "skipped": 0, "calls": 4}
    entries = json.loads((tmp_path / "cache.json").read_text())
    assert entries["n0"] == {"kind": "label", "answer": "a", "confidence": 0.9, "model": "m",
                             "prompt_version": 1, "page": 1, "node": "n0"}
    assert "n2" not in entries


def test_a_reply_that_names_an_unoffered_id_is_a_failure(tmp_path):
    stats, applied = loop(tmp_path, Chooser("zzz"), [Pick(1, "n")])
    assert stats["failed"] == 1 and applied == [] and not (tmp_path / "cache.json").exists()


def test_a_second_run_replays_the_cache_with_its_confidence_and_calls_nothing(tmp_path):
    qs = [Pick(1, "n0"), Pick(1, "n1")]
    loop(tmp_path, Chooser(Reply("a", confidence=0.8123), "b"), qs)
    stats, applied = loop(tmp_path, None, qs)
    assert applied == [("n0", "a", 0.812), ("n1", "b", None)]
    assert stats["cached"] == 2 and stats["calls"] == 0 and stats["asked"] == 2


def test_the_call_cap_leaves_the_rest_unasked(tmp_path):
    chooser = Chooser("a", "a", "a")
    stats, applied = loop(tmp_path, chooser, [Pick(1, f"n{i}") for i in range(3)], max_calls=2)
    assert (stats["calls"], stats["skipped"], len(applied)) == (2, 1, 2)


# -- the picture -----------------------------------------------------------------------------


def test_the_renderer_crops_the_region_at_its_scale_and_opens_the_pdf_once(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", ["text"])
    renderer = ask.CropRenderer(pdf)
    png, box = renderer.crop(QUESTION)
    assert box == QUESTION.crop_box((612, 792))
    image = Image.open(io.BytesIO(png))
    assert image.size == (round((box[2] - box[0]) * ask.SCALE), round((box[3] - box[1]) * ask.SCALE))
    first = renderer.pdf
    renderer.crop(QUESTION)
    assert renderer.pdf is first
    renderer.close()
    assert renderer.pdf is None
    renderer.close()


def test_the_renderer_holds_the_lock_itself_and_marks_what_a_subclass_asks(tmp_path):
    events = []

    class Lock:
        def __enter__(self):
            events.append("in")

        def __exit__(self, *exc):
            events.append("out")

    class Marked(ask.CropRenderer):
        def mark(self, image, question, box, scale):
            events.append(("mark", image.size))

    renderer = Marked(make_pdf(tmp_path / "p.pdf", ["text"]), Lock())
    png, _ = renderer.crop(QUESTION)
    assert events[:2] == ["in", "out"] and events[2][0] == "mark"
    renderer.close()
    assert events[-2:] == ["in", "out"]
    assert Image.open(io.BytesIO(png)).size == events[2][1]
