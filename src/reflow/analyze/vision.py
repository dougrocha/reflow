"""Asking a vision model one question about one picture: the transport, and nothing about forms
or figures.

Every opt-in AI route that sends a picture uses it: the assists (label, structure, page vision,
form repair, transcription), figure alt text, polish and the direct engine. OllamaVision talks to a local Ollama server over HTTP (stdlib only), so nothing
leaves the machine unless OLLAMA_HOST names another: never through a proxy (`send`), and a host
is this machine only when every address it resolves to is loopback (`loopback`). AnthropicVision uses the Anthropic Messages API with the user's own key; the
`anthropic` SDK is imported only on the first request, so the package is optional, and a fully
cached run needs neither the SDK nor the key. Neither does any I/O when it is constructed.

The image goes as sent, with its own media type (figure crops are WebP, form crops PNG): both
services accept either, so nothing is transcoded.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field

from reflow import budget

# Ollama "decision" models answer through POST /v1/systemone (a state, images and typed questions
# in; the chosen option with per-option probabilities and a confidence out), not /api/chat. The
# table is keyed on the model name without its tag.
DECISION_MODELS = frozenset({"clef-flash", "d1"})
DECISION_QUESTION = "answer"   # the name of the one question sent to a decision model
# Ollama 0.40 ignores `think: false` for the thinking-only qwen3-vl tags (their renderer is
# qwen3-vl-thinking): the model reasons for thousands of tokens and, with a `format`, never
# answers. An assistant turn that closes an empty think block makes it answer at once.
EMPTY_THINK_MODELS = frozenset({"qwen3-vl"})
EMPTY_THINK = "<think>\n\n</think>\n\n"
# The task prompt of the OCR models (glm-ocr, MiniCPM-OCR, per OpenBMB's docs): a plain-text or
# markdown answer, no system turn.
OCR_PROMPT = "Text Recognition:"
OLLAMA_TIMEOUT = 180.0   # seconds per question; a cold model load is the slow part


class ProviderUnavailable(Exception):
    """The provider can't run at all (missing packages): stop the conversion."""


class ProviderError(Exception):
    """One question to a model went unanswered (a figure keeps its placeholder, a form label
    stays the rules' choice). `usage` is what a request that was paid for but refused used."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


def first_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ProviderError("no JSON in answer")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ProviderError(f"invalid JSON: {e}") from e


def loads_unique(text: str) -> object:
    """json.loads, but a key given twice (which json.loads would resolve to its last value) is
    an error. Raises ValueError."""
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"a key given twice in {keys}")
        return dict(pairs)

    return json.loads(text, object_pairs_hook=unique)


@dataclass(frozen=True)
class VisionImage:
    data: bytes
    media_type: str   # "image/webp", "image/png", ...

    def b64(self) -> str:
        return base64.standard_b64encode(self.data).decode("ascii")


@dataclass(frozen=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    stats: Mapping[str, object] = field(default_factory=dict)   # what the server reported, whole


@dataclass(frozen=True)
class Reply:
    text: str                       # the model's answer: the schema's JSON, or raw text
    truncated: bool = False         # the answer stopped at the token limit
    usage: Usage = Usage()
    confidence: float | None = None   # a decision model's confidence in its choice


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def is_decision_model(model: str) -> bool:
    """Whether Ollama serves this model through /v1/systemone (see DECISION_MODELS)."""
    return model.split(":", 1)[0].strip().lower() in DECISION_MODELS


def needs_empty_think(model: str) -> bool:
    """Whether `think: false` does not stop this model reasoning (see EMPTY_THINK_MODELS); the
    instruct tags of the same family do not think and are left alone."""
    name, _, tag = model.strip().lower().partition(":")
    return name in EMPTY_THINK_MODELS and "instruct" not in tag


def loopback(host: str, port: int | None = None) -> list[tuple]:
    """The addresses `host` resolves to (getaddrinfo's), when every one is this machine's
    loopback; else none. A name that resolves nowhere, or partly elsewhere, is not this machine,
    whatever it looks like ("127.a.8.8.8.8.nip.io" is 8.8.8.8)."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return []
    try:
        mine = all(ipaddress.ip_address(str(a[4][0])).is_loopback for a in found)
    except ValueError:
        return []
    return found if mine else []


def _loopback_only(address: tuple, timeout: object = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
                   source_address: tuple | None = None) -> socket.socket:
    """socket.create_connection for this machine only: the host is resolved here, as it is
    connected, so a name that changed since `loopback` checked it is refused, not followed."""
    host, port = address[:2]
    found = loopback(host, port)
    if not found:
        raise ConnectionRefusedError(f"{host} is not this machine")
    error: OSError | None = None
    for *_, sockaddr in found:
        try:
            return socket.create_connection(sockaddr[:2], timeout, source_address)  # type: ignore[arg-type]
        except OSError as e:
            error = e
    raise error or ConnectionRefusedError(f"{host} did not answer")


class _LoopbackHTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        def connection(host, **kw):
            conn = http.client.HTTPConnection(host, **kw)
            conn._create_connection = _loopback_only
            return conn
        return self.do_open(connection, req)


class _LoopbackHTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        def connection(host, **kw):
            conn = http.client.HTTPSConnection(host, **kw)
            conn._create_connection = _loopback_only
            return conn
        return self.do_open(connection, req, context=self._context)


# No proxy, ever: urllib's default sends even a localhost request through HTTP_PROXY unless
# no_proxy lists it. The loopback opener also connects only to this machine's addresses.
_OPENERS = {False: urllib.request.build_opener(urllib.request.ProxyHandler({})),
            True: urllib.request.build_opener(urllib.request.ProxyHandler({}), _LoopbackHTTP(),
                                              _LoopbackHTTPS())}


def send(request: urllib.request.Request, timeout: float, loopback: bool = False):
    """Open `request` with no proxy; with `loopback`, only to an address of this machine. The
    one way an Ollama request leaves this module (tests replace it)."""
    return _OPENERS[loopback].open(request, timeout=timeout)


class OllamaVision:
    """A local Ollama server (stdlib urllib). Chat models use `POST {OLLAMA_HOST}/api/chat`: the
    picture goes in the message's `images` and the answer schema in `format`; thinking is off.
    Decision models (DECISION_MODELS) use `POST /v1/systemone`: one `choice` question, answered
    with the chosen option and a confidence. Temperature 0 and a fixed seed make a run
    repeatable. Each answer is a Reply that carries the tokens used and the numbers Ollama
    reported (counts and durations), for benchmarks."""

    def __init__(self, model: str, host: str | None = None, timeout: float = OLLAMA_TIMEOUT,
                 keep_alive: str = "10m") -> None:
        self.cache_id = self.model = model
        self.host, self.timeout, self.keep_alive = host, timeout, keep_alive
        self.decision = is_decision_model(model)

    def url(self) -> str:
        host = (self.host or os.environ.get("OLLAMA_HOST") or "").strip() or "localhost:11434"
        if "://" not in host:
            host = "http://" + host
        host = host.rstrip("/").replace("//0.0.0.0", "//localhost")
        if host.count(":") < 2:
            host += ":11434"
        return host + ("/v1/systemone" if self.decision else "/api/chat")

    def local(self) -> bool:
        """Whether the server runs on this machine, and so on its GPU (reflow.budget.gpu): every
        address its host resolves to is loopback (`loopback`). The lease covers reflow's requests
        only: Ollama keeps a model loaded for `keep_alive` after one, and serves other clients
        without asking."""
        return bool(loopback(urllib.parse.urlsplit(self.url()).hostname or ""))

    def chat_payload(self, system: str, prompt: str, image: VisionImage, schema: dict | None = None,
                     max_tokens: int | None = None, num_ctx: int | None = None) -> dict:
        """The /api/chat body. `schema` None asks for raw text; an empty `system` sends no system
        turn (an OCR model's own task prompt is the user turn). `num_ctx` raises Ollama's
        context past its 4,096-token default, which cuts a longer prompt without a word."""
        messages = [{"role": "system", "content": system}] if system else []
        messages.append({"role": "user", "content": prompt, "images": [image.b64()]})
        if needs_empty_think(self.model):
            messages.append({"role": "assistant", "content": EMPTY_THINK})
        options: dict = {"temperature": 0, "seed": 0}
        if max_tokens:
            options["num_predict"] = max_tokens
        if num_ctx:
            options["num_ctx"] = num_ctx
        payload = {"model": self.model, "stream": False, "think": False,
                   "keep_alive": self.keep_alive, "options": options, "messages": messages}
        if schema is not None:
            payload["format"] = schema
        return payload

    def decision_payload(self, instructions: str, state: str, image: VisionImage,
                         criteria: Mapping[str, str]) -> dict:
        return {"model": self.model, "keep_alive": self.keep_alive, "state": state,
                "images": [image.b64()],
                "questions": {DECISION_QUESTION: {"type": "choice", "instructions": instructions,
                                                  "criteria": dict(criteria)}}}

    def post(self, payload: dict, local_only: bool = False) -> object:
        """Send one request; the decoded JSON reply. A request to this machine connects only to
        its loopback addresses (`send`). With `local_only` (personal data), a server elsewhere
        is not sent it: ProviderUnavailable. Raises ProviderUnavailable when no request can
        succeed (no server, no model) and ProviderError for this one."""
        import urllib.error

        local = self.local()
        if local_only and not local:
            raise ProviderUnavailable(
                f"Ollama at {self.url()} is not on this machine, and this is sent only to a model "
                "on this machine")
        request = urllib.request.Request(self.url(), json.dumps(payload).encode(),
                                         {"Content-Type": "application/json"})
        try:
            with budget.gpu("cuda" if local else "remote"), \
                    send(request, self.timeout, local) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code == 404 and "not found" in detail.lower():
                raise ProviderUnavailable(
                    f"Ollama does not have the model {self.model!r}: start it with "
                    f"`ollama serve`, then `ollama pull {self.model}`.") from None
            raise ProviderError(f"Ollama answered {e.code}: {detail}") from e
        except TimeoutError:
            raise ProviderError(f"Ollama timed out after {self.timeout:.0f}s") from None
        except (urllib.error.URLError, ConnectionError) as e:
            if isinstance(getattr(e, "reason", None), TimeoutError):
                raise ProviderError(f"Ollama timed out after {self.timeout:.0f}s") from None
            raise ProviderUnavailable(
                f"Ollama is not reachable at {self.url()} ({e}): start it with `ollama serve`, "
                f"then `ollama pull {self.model}`.") from None
        except ValueError as e:
            raise ProviderError(f"Ollama sent a reply that is not JSON: {e}") from e

    def ask(self, system: str, prompt: str, image: VisionImage, schema: dict | None = None,
            max_tokens: int | None = None, num_ctx: int | None = None,
            local_only: bool = False) -> Reply:
        """A chat model's answer, constrained to `schema` (raw text when it is None). With
        `local_only`, it is asked only on this machine (`post`)."""
        data = self.post(self.chat_payload(system, prompt, image, schema, max_tokens, num_ctx),
                         local_only)
        if not isinstance(data, dict):
            raise ProviderError("Ollama's reply is not a JSON object")
        message = data.get("message")
        text = message.get("content", "") if isinstance(message, dict) else ""
        stats = {k: v for k, v in data.items() if k.endswith(("_count", "_duration"))}
        usage = Usage(_count(data.get("prompt_eval_count")), _count(data.get("eval_count")), stats)
        return Reply(text or "", data.get("done_reason") == "length", usage)

    def choose(self, instructions: str, state: str, image: VisionImage,
               criteria: Mapping[str, str]) -> Reply:
        """A decision model's chosen option, one of `criteria` (the reply's text), with its
        confidence."""
        data = self.post(self.decision_payload(instructions, state, image, criteria))
        answers = data.get("answers") if isinstance(data, dict) else None
        answer = answers.get(DECISION_QUESTION) if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ProviderError("Ollama's decision reply has no choice answer")
        used = data.get("usage") if isinstance(data, dict) else None
        used = used if isinstance(used, dict) else {}
        usage = Usage(_count(used.get("input_tokens")), _count(used.get("output_tokens")),
                      {"usage": used, "probabilities": answer.get("probabilities")})
        choice = answer.get("choice")
        if choice not in criteria:
            raise ProviderError(f"reply names {choice!r}, which was not offered")
        confidence = answer.get("confidence")
        known = isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        return Reply(choice, False, usage, float(confidence) if known else None)


def unload_ollama(host: str | None = None) -> None:
    """Ask the local Ollama server to unload every model it holds (`keep_alive` 0), so a model
    that needs the whole GPU can load. Does nothing for a remote server, or when none answers."""
    probe = OllamaVision("", host)
    if not probe.local():
        return
    base = probe.url().removesuffix("/api/chat")
    try:
        with send(urllib.request.Request(base + "/api/ps"), 5, loopback=True) as response:
            models = [m["name"] for m in json.loads(response.read()).get("models", [])]
        for name in models:
            body = json.dumps({"model": name, "keep_alive": 0}).encode()
            request = urllib.request.Request(base + "/api/generate", body,
                                             {"Content-Type": "application/json"})
            with send(request, 30, loopback=True) as response:
                response.read()
    except (OSError, ValueError, KeyError, TypeError):
        return   # no server, or an answer we don't read: the load reports its own error


class AnthropicVision:
    """Answers from the Anthropic Messages API (official SDK, imported lazily), with the user's
    own ANTHROPIC_API_KEY. `flag` names the option that chose it, for the messages a missing
    package or key raises."""

    def __init__(self, model: str, client: object | None = None, *,
                 flag: str = "--assist-provider anthropic") -> None:
        self.cache_id = self.model = model
        self._client = client
        self.flag = flag
        self._lock = threading.Lock()

    def _sdk(self):
        try:
            import anthropic
        except ImportError:
            raise ProviderUnavailable(
                f"{self.flag} needs the anthropic package, which is not installed. Install it "
                "(paru -S python-anthropic) and set ANTHROPIC_API_KEY.") from None
        with self._lock:
            if not self._client:
                if not os.environ.get("ANTHROPIC_API_KEY"):
                    raise ProviderUnavailable(
                        f"{self.flag} needs ANTHROPIC_API_KEY, which is not set. Export your "
                        "Anthropic API key.")
                self._client = anthropic.Anthropic()
        return anthropic, self._client

    def ask(self, system: str, prompt: str, image: VisionImage, schema: dict | None = None,
            max_tokens: int = 2048) -> Reply:
        """The model's answer, constrained to `schema` (raw text when it is None). A refusal
        raises ProviderError carrying the usage it was billed."""
        anthropic, client = self._sdk()
        output = {"effort": "low"}
        if schema is not None:
            output["format"] = {"type": "json_schema", "schema": schema}
        try:
            response = client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system, output_config=output,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": image.media_type,
                                                 "data": image.b64()}},
                    {"type": "text", "text": prompt}]}])
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise ProviderUnavailable(f"Anthropic rejected the credentials: {e}") from e
        except anthropic.AnthropicError as e:
            raise ProviderError(f"Anthropic request failed: {e}") from e
        used = getattr(response, "usage", None)
        tokens_in = _count(getattr(used, "input_tokens", None))
        tokens_out = _count(getattr(used, "output_tokens", None))
        usage = Usage(tokens_in, tokens_out,
                      {"input_tokens": tokens_in, "output_tokens": tokens_out})
        if response.stop_reason == "refusal":
            raise ProviderError("model stopped: refusal", usage)
        text = "".join(b.text for b in response.content if b.type == "text")
        return Reply(text, response.stop_reason == "max_tokens", usage)
