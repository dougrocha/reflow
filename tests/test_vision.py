import base64
import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from reflow.analyze.vision import (
    EMPTY_THINK,
    AnthropicVision,
    OllamaVision,
    ProviderError,
    ProviderUnavailable,
    VisionImage,
)
from testkit import FakeHttp, resolving

WEBP = VisionImage(b"RIFF....WEBP", "image/webp")
SCHEMA = {"type": "object"}


def test_the_ollama_chat_request_has_the_image_the_schema_and_thinking_off(monkeypatch):
    fake = FakeHttp({"message": {"content": '{"a": 1}'}, "eval_count": 3, "done_reason": "stop"})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    vision = OllamaVision("gemma4:e4b", host="h:1")
    reply = vision.ask("sys", "prompt", WEBP, SCHEMA, max_tokens=1024)
    assert reply.text == '{"a": 1}' and not reply.truncated
    sent = json.loads(fake.requests[0].data)
    assert fake.requests[0].full_url == "http://h:1/api/chat"
    assert sent["think"] is False and sent["format"] == SCHEMA
    assert sent["messages"][1]["images"] == [base64.b64encode(WEBP.data).decode()]
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["options"] == {"temperature": 0, "seed": 0, "num_predict": 1024}
    assert reply.usage.tokens_out == 3 and reply.usage.stats == {"eval_count": 3}


def test_raw_text_mode_has_no_schema_no_empty_system_turn_and_the_context_it_is_given(monkeypatch):
    fake = FakeHttp({"message": {"content": "<p>hi</p>"}, "prompt_eval_count": 7, "eval_count": 2})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    reply = OllamaVision("glm-ocr").ask("", "Text Recognition:", WEBP, None, 8192, 16384)
    sent = json.loads(fake.requests[0].data)
    assert "format" not in sent and [m["role"] for m in sent["messages"]] == ["user"]
    assert sent["options"] == {"temperature": 0, "seed": 0, "num_predict": 8192, "num_ctx": 16384}
    assert (reply.text, reply.usage.tokens_in, reply.usage.tokens_out) == ("<p>hi</p>", 7, 2)


def test_a_thinking_only_qwen3_vl_gets_the_empty_think_turn_and_a_cut_answer_is_flagged(monkeypatch):
    fake = FakeHttp({"message": {"content": '{"alt": "A'}, "done_reason": "length"})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    reply = OllamaVision("qwen3-vl:8b").ask("sys", "p", WEBP, SCHEMA)
    assert reply.truncated
    assert json.loads(fake.requests[0].data)["messages"][-1] == {"role": "assistant",
                                                                  "content": EMPTY_THINK}


def test_a_decision_model_goes_to_systemone_and_keeps_its_confidence(monkeypatch):
    reply = {"answers": {"answer": {"type": "choice", "choice": "yes", "confidence": 0.75,
                                    "probabilities": {"yes": 0.75, "no": 0.25}}}, "usage": {}}
    fake = FakeHttp(reply)
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    vision = OllamaVision("d1:3b")
    reply = vision.choose("decide", "state", WEBP, {"yes": "it is", "no": "it is not"})
    assert (reply.text, reply.confidence) == ("yes", 0.75)
    assert fake.requests[0].full_url == "http://localhost:11434/v1/systemone"


def test_a_missing_model_is_unavailable(monkeypatch):
    missing = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":"model not found"}'))
    monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(error=missing))
    with pytest.raises(ProviderUnavailable, match="ollama pull m"):
        OllamaVision("m").ask("s", "p", WEBP, SCHEMA)


# -- nothing leaves the machine by a route the host does not name (#345) ------------------------

PROXY = "http://proxy.invalid:3128"


@pytest.fixture
def connections(monkeypatch):
    """socket.create_connection, refused: the addresses urllib tried, so no test needs a
    listener or the network."""
    tried = []

    def refuse(address, *_, **__):
        tried.append(tuple(address[:2]))
        raise ConnectionRefusedError(f"refused {address}")

    monkeypatch.setattr("socket.create_connection", refuse)
    return tried


@pytest.mark.parametrize("host, address", [
    ("localhost:18799", ("127.0.0.1", 18799)),     # this machine
    ("gpu.example:11434", ("gpu.example", 11434)),  # another, which OLLAMA_HOST names
])
def test_an_ollama_request_never_goes_through_a_proxy(monkeypatch, connections, host, address):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY",
                 "all_proxy"):
        monkeypatch.setenv(name, PROXY)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    resolving(monkeypatch, {"localhost": ["127.0.0.1"], "gpu.example": ["10.0.0.5"]})
    assert urllib.request.getproxies()["http"] == PROXY   # urllib's own default would use it
    with pytest.raises(ProviderUnavailable):
        OllamaVision("m", host=host).ask("", "p", WEBP)
    assert connections == [address]


@pytest.mark.parametrize("host, addresses, local", [
    ("127.a.8.8.8.8.nip.io", ["8.8.8.8"], False),               # a name that starts with "127."
    ("127.a.192.168.0.223.nip.io", ["192.168.0.223"], False),   # a machine on the LAN
    ("127.0.0.1.attacker.example", ["203.0.113.9"], False),
    ("both.example", ["127.0.0.1", "10.0.0.5"], False),        # loopback and another address
    ("nowhere.invalid", None, False),                           # no address: never this machine
    ("localhost", ["127.0.0.1", "::1"], True),
    ("127.0.0.1", ["127.0.0.1"], True),
    ("[::1]", ["::1"], True),
    ("127.0.1.1", ["127.0.1.1"], True),
])
def test_only_a_host_whose_every_address_is_loopback_is_this_machine(monkeypatch, host,
                                                                      addresses, local):
    looked = resolving(monkeypatch, {host.strip("[]"): addresses} if addresses else {})
    assert OllamaVision("m", host=f"{host}:11434").local() is local
    assert looked == [host.strip("[]")]


def test_a_host_that_turns_remote_after_its_check_is_never_connected(monkeypatch, connections):
    import socket
    answers = iter([["127.0.0.1"]])   # loopback when local() looks, then another machine

    def getaddrinfo(host, port, *_, **__):
        ip = next(answers, ["8.8.8.8"])[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr("socket.getaddrinfo", getaddrinfo)
    vision = OllamaVision("m", host="rebind.example:11434")
    with pytest.raises((ProviderUnavailable, ProviderError)):
        vision.ask("", "p", WEBP, local_only=True)
    assert connections == []


def test_a_request_for_this_machine_only_is_never_sent_elsewhere(monkeypatch):
    fake = FakeHttp({"message": {"content": "x"}})
    monkeypatch.setattr("reflow.analyze.vision.send", fake)
    resolving(monkeypatch, {"localhost": ["127.0.0.1"], "10.0.0.5": ["10.0.0.5"]})
    with pytest.raises(ProviderUnavailable, match="this machine"):
        OllamaVision("m", host="10.0.0.5:11434").ask("", "p", WEBP, local_only=True)
    assert fake.requests == []
    assert OllamaVision("m", host="localhost:11434").ask("", "p", WEBP, local_only=True).text == "x"
    assert fake.loopback == [True]


class FakeAnthropic:
    def __init__(self, stop_reason="end_turn"):
        self.stop_reason, self.calls = stop_reason, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason, usage=None,
                               content=[SimpleNamespace(type="text", text='{"a": 1}')])


@pytest.fixture
def sdk(monkeypatch):
    """A stand-in for the optional anthropic package: only its error classes are read."""
    import sys
    import types
    fake = types.ModuleType("anthropic")
    fake.AnthropicError = type("AnthropicError", (Exception,), {})
    fake.AuthenticationError = type("AuthenticationError", (fake.AnthropicError,), {})
    fake.PermissionDeniedError = type("PermissionDeniedError", (fake.AnthropicError,), {})
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def test_the_anthropic_request_carries_the_images_own_media_type(sdk):
    client = FakeAnthropic()
    assert AnthropicVision("m", client).ask("s", "p", WEBP, SCHEMA).text == '{"a": 1}'
    source = client.calls[0]["messages"][0]["content"][0]["source"]
    assert source["media_type"] == "image/webp"


def test_an_anthropic_refusal_is_one_failed_request(sdk):
    with pytest.raises(ProviderError, match="refusal"):
        AnthropicVision("m", FakeAnthropic("refusal")).ask("s", "p", WEBP, SCHEMA)


def test_without_a_schema_the_anthropic_request_asks_for_raw_text(sdk):
    client = FakeAnthropic()
    AnthropicVision("m", client).ask("s", "p", WEBP, None, 16000)
    assert client.calls[0]["output_config"] == {"effort": "low"}
    assert client.calls[0]["max_tokens"] == 16000


def test_nothing_is_touched_when_a_client_is_made(monkeypatch):
    monkeypatch.setattr("reflow.analyze.vision.send", FakeHttp(error=AssertionError("no I/O")))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    OllamaVision("m"), AnthropicVision("m")
