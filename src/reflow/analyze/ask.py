"""What the opt-in assist passes share: they put a typed question about a region of the page to a
model, keep every answer in the carried cache (names.ASSIST_CACHE) and apply it only after a check
against the page.

- `Question`: the base of a typed question (the crop box around its boxes, the enum schema).
- `Store`: the cache. One flat JSON file shared by every pass, each entry tagged with the kind
  of pass that wrote it; written atomically.
- `Asker`: one pass's access to its provider: the cached answer, else one call within the budget
  (`max_calls`), recorded with its model and prompt version. A provider that cannot run at all
  (`ProviderUnavailable`) stops the pass with that exception, so the conversion stops with its
  message; a pass that can finish without the model (`degrade`) skips its remaining questions
  instead.
- `ask_choices`: the loop of the passes whose answer is one of a few offered words (label,
  structure, scan fields).
- `CropRenderer`: the PNG the model sees, rendered from the working PDF under the pdfium lock.
- `ChoiceProvider`: the model behind `ask_choices`, local (Ollama) or the user's own Anthropic key.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, Protocol

from reflow import cachedir
from reflow.analyze.vision import (OLLAMA_TIMEOUT, AnthropicVision, OllamaVision, ProviderError,
                                   ProviderUnavailable, Reply, VisionImage, first_json_object)
from reflow.model import BBox, Document
from reflow.providers import ASSIST_PROVIDERS, DEFAULT_ASSIST_PROVIDER, ASSIST_MODELS, resolve_model

NONE = "none"            # the answer of a choice question that changes nothing
SCALE = 2.0              # crop pixels per point
CROP_PAD = 12.0          # points of page around the boxes a question is about


def clip_ends(text: str, limit: int) -> str:
    """Text as sent to the model: a long caption keeps its start and end, so a wrapped caption's
    closing words stay visible. Only the text shortens; the id and box that identify it do not."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head].rstrip()} ... {text[-(limit - head - 5):].lstrip()}"


# -- the question ----------------------------------------------------------------------------


class Cropped(Protocol):
    page: int

    def crop_box(self, page_size: tuple[float, float]) -> BBox: ...


class Question(ABC):
    """A typed question about one region of a page; subclasses are frozen dataclasses with a
    `page` and a `node` (what the answer is about), and say which boxes it concerns."""
    answer_key: ClassVar[str]       # the one property of the structured reply
    system_prompt: ClassVar[str]
    page: int
    node: str

    @property
    @abstractmethod
    def choices(self) -> list[str]: ...

    @property
    @abstractmethod
    def instruction(self) -> str:
        """For a decision model: the one rule its answer follows."""

    @property
    @abstractmethod
    def criteria(self) -> dict[str, str]:
        """What each option means, for a decision model."""

    @abstractmethod
    def boxes(self) -> list[BBox]:
        """The boxes the crop must show."""

    @abstractmethod
    def prompt(self, crop: BBox) -> str:
        """The text part of the request: exactly what the model is told besides the picture."""

    def crop_box(self, page_size: tuple[float, float]) -> BBox:
        boxes = self.boxes()
        return (max(0.0, min(b[0] for b in boxes) - CROP_PAD),
                max(0.0, min(b[1] for b in boxes) - CROP_PAD),
                min(page_size[0], max(b[2] for b in boxes) + CROP_PAD),
                min(page_size[1], max(b[3] for b in boxes) + CROP_PAD))

    def schema(self) -> dict:
        return {"type": "object",
                "properties": {self.answer_key: {"type": "string", "enum": self.choices}},
                "required": [self.answer_key], "additionalProperties": False}


# -- the cache -------------------------------------------------------------------------------


def load_cache(path: Path) -> dict:
    """The cache file's entries; empty when it is missing, unreadable or not an object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_cache(path: Path, cache: dict) -> None:
    """Write the file through a private name, so a reader never sees half of it."""
    tmp = cachedir.temp_path(path)
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class Store:
    """The entries of the shared cache file, read once. Passes run one after another, so each
    reads what the one before it saved; a pass saves only what it changed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries = load_cache(path)
        self.dirty = False

    def get(self, key: str) -> dict | None:
        entry = self.entries.get(key)
        return entry if isinstance(entry, dict) else None

    def put(self, key: str, kind: str, **fields: object) -> dict:
        entry = {"kind": kind, **fields}
        self.entries[key] = entry
        self.dirty = True
        return entry

    def save(self, force: bool = False) -> None:
        if self.dirty or force:
            save_cache(self.path, self.entries)
            self.dirty = False

    @contextmanager
    def saving(self) -> Iterator[Store]:
        """Save on the way out, an exception included: the answers paid for are kept."""
        try:
            yield self
        finally:
            self.save()


# -- asking ----------------------------------------------------------------------------------


class Status(Enum):
    CACHED = "cached"
    ASKED = "asked"
    SKIPPED = "skipped"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Fetched:
    status: Status
    entry: dict | None = None   # the cache entry, for CACHED and ASKED, or a cached error
    error: str = ""             # why the call FAILED or the provider is UNAVAILABLE


class Asker:
    """One pass's cache, provider and call budget. `provider` None replays the cache only (no
    network, no credentials). `stats` gets `cached`, `calls` and `failed`; the pass counts what
    it does with the answers."""

    def __init__(self, store: Store, provider: object | None, *, kind: str, model: str,
                 version: int, max_calls: int, stats: dict[str, int],
                 degrade: bool = False) -> None:
        self.store, self.provider, self.kind, self.model = store, provider, kind, model
        self.version, self.max_calls, self.stats, self.degrade = version, max_calls, stats, degrade
        self.unavailable = False

    def can_call(self) -> bool:
        return (self.provider is not None and not self.unavailable
                and self.stats["calls"] < self.max_calls)

    def why_not(self) -> str:
        if self.unavailable:
            return "unavailable"
        return "replay only" if self.provider is None else "call budget"

    def fetch(self, key: str, *, page: int, valid: Callable[[dict], bool],
              call: Callable[[], Mapping[str, object]], node: str | None = None,
              record_errors: bool = False) -> Fetched:
        """The cached entry for `key` when `valid` accepts it, else one `call` (the fields of the
        entry it makes) while the budget lasts. With `record_errors` a failed call is cached as
        an error and replayed as one."""
        entry = self.store.get(key)
        if entry is not None:
            if record_errors and isinstance(entry.get("error"), str):
                self.stats["cached"] += 1
                return Fetched(Status.CACHED, entry, entry["error"])
            if valid(entry):
                self.stats["cached"] += 1
                return Fetched(Status.CACHED, entry)
        if not self.can_call():
            return Fetched(Status.SKIPPED)
        self.stats["calls"] += 1
        meta = {"model": self.model, "prompt_version": self.version, "page": page,
                **({} if node is None else {"node": node})}
        try:
            fields = call()
        except ProviderUnavailable as e:
            if not self.degrade:
                raise
            self.unavailable = True
            return Fetched(Status.UNAVAILABLE, error=str(e))
        except ProviderError as e:
            self.stats["failed"] += 1
            error = str(e)[:200]
            if record_errors:
                self.store.put(key, self.kind, error=error, **meta)
            return Fetched(Status.FAILED, error=error)
        return Fetched(Status.ASKED, self.store.put(key, self.kind, **fields, **meta))


class ChoiceProvider(Protocol):
    cache_id: str   # the model; part of the cache key

    def choose(self, question: Question, image: bytes, crop: BBox) -> Reply:
        """The id of the offered option the page shows, as the reply's text, with the
        confidence of a decision model. `image` is the PNG of the page region `crop` (points).
        May raise ProviderError for one bad answer, ProviderUnavailable when no question can
        succeed."""
        ...


def ask_choices(questions: list, key_of: Callable[[Question], str],
                apply: Callable[[Question, str, float | None], str], asker: Asker,
                render: Callable[[Question], tuple[bytes, BBox]], *, none: str = NONE) -> None:
    """The loop of every choice pass: replay a cached answer or ask, record it, and hand it to
    `apply(question, answer, confidence)`, which returns the stats key for what became of it
    (`confidence` is a decision model's, else None). `none` is the answer that changes nothing
    (counted as such); a pass applies its answers itself, and may defer them. `render` makes a
    question's crop and is called only before a real call."""
    stats = asker.stats

    def call_for(question: Question) -> Callable[[], dict]:
        def call() -> dict:
            image, crop = render(question)
            reply = asker.provider.choose(question, image, crop)
            if reply.text not in question.choices:
                raise ProviderError(f"reply names {reply.text!r}, which was not offered")
            fields: dict = {"answer": reply.text}
            if reply.confidence is not None:
                fields["confidence"] = round(reply.confidence, 3)
            return fields
        return call

    for question in questions:
        got = asker.fetch(key_of(question), page=question.page, node=question.node,
                          valid=lambda e, q=question: e.get("answer") in q.choices,
                          call=call_for(question))
        if got.status is Status.SKIPPED:
            stats["skipped"] += 1
            continue
        if got.status is Status.FAILED:
            continue
        stats["asked"] += 1
        answer = got.entry["answer"]
        if answer == none:
            stats["none"] += 1
        else:
            stats[apply(question, answer, got.entry.get("confidence"))] += 1


def cache_digest(*parts: object) -> str:
    """The cache key of a question: a hash of its parts, which each pass lists the same way
    every time (its kind, the PDF's hash, the page, what the question is about, the model and
    the prompt version), so a changed page, model or prompt is a new question."""
    return hashlib.sha256(json.dumps(list(parts), ensure_ascii=False).encode()).hexdigest()


def default_model(provider: ChoiceProvider | None) -> str:
    """The model a choice pass records: the provider's, or the default one when it only replays the cache."""
    return provider.cache_id if provider else ASSIST_MODELS[DEFAULT_ASSIST_PROVIDER]


def run_choices(doc: Document, provider: ChoiceProvider | None, cache_path: Path, questions: list,
                cache_key: Callable[[str, Question, str], str],
                apply: Callable[[Question, str, float | None], str],
                render: Callable[[Question], tuple[bytes, BBox]], *, kind: str, version: int,
                max_calls: int, model: str, none: str = NONE) -> dict[str, int]:
    """One choice pass over `questions` (ask_choices) with the cache at `cache_path`, keyed by
    `cache_key(pdf_sha256, question, model)`. `provider` None replays the cache only. Returns
    the counts: the keys of ask_choices, and the stats key `apply` returns."""
    sha = str(doc.meta.get("sha256", ""))
    stats = dict.fromkeys(("asked", "cached", "accepted", "confirmed", "none", "rejected",
                           "failed", "skipped", "calls"), 0)
    with Store(cache_path).saving() as store:
        asker = Asker(store, provider, kind=kind, model=model, version=version,
                      max_calls=max_calls, stats=stats)
        ask_choices(questions, lambda q: cache_key(sha, q, model), apply, asker, render, none=none)
    return stats


# -- the picture -----------------------------------------------------------------------------


class CropRenderer:
    """Renders the PNG crops the model sees from the working PDF, opening it on first use. It
    holds the pdfium `lock` itself around `crop` and `close`: callers do not."""

    scale = SCALE

    def __init__(self, pdf: Path, lock: AbstractContextManager | None = None) -> None:
        self.path = pdf
        self.lock = lock if lock is not None else nullcontext()
        self.pdf: object | None = None   # a pypdfium2.PdfDocument once the first crop is made

    def scale_for(self, page_size: tuple[float, float]) -> float:
        return self.scale

    def mark(self, image, question: Cropped, box: BBox, scale: float) -> None:
        """Draw on the crop before it is saved. Nothing by default."""

    def crop(self, question: Cropped) -> tuple[bytes, BBox]:
        """The PNG of the region the question is about, and its box in page points."""
        import pypdfium2 as pdfium

        with self.lock:
            if self.pdf is None:
                self.pdf = pdfium.PdfDocument(self.path)
            page = self.pdf[question.page - 1]
            try:
                size = (page.get_width(), page.get_height())
                box = question.crop_box(size)
                scale = self.scale_for(size)
                image = page.render(scale=scale).to_pil().convert("RGB")
            finally:
                page.close()
        if box != (0.0, 0.0, *size):   # the whole page is the render as it is
            image = image.crop(tuple(round(v * scale) for v in box))
        self.mark(image, question, box, scale)
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue(), box

    def close(self) -> None:
        with self.lock:
            if self.pdf is not None:
                self.pdf.close()
                self.pdf = None


# -- the model -------------------------------------------------------------------------------


class OllamaChoiceProvider:
    """A choice question through a local Ollama server (analyze.vision.OllamaVision): a chat
    model answers with the offered id under the question's schema; a decision model (clef-flash,
    d1) picks one option on /v1/systemone, and its confidence comes back with the answer. The
    reply is still validated against the offered ids. Nothing leaves the machine."""

    def __init__(self, model: str = ASSIST_MODELS["ollama"], host: str | None = None,
                 timeout: float = OLLAMA_TIMEOUT, keep_alive: str = "10m") -> None:
        self.vision = OllamaVision(model, host, timeout, keep_alive)
        self.cache_id = self.model = model

    def choose(self, question: Question, image: bytes, crop: BBox) -> Reply:
        picture = VisionImage(image, "image/png")
        if self.vision.decision:
            return self.vision.choose(f"{question.system_prompt}\n{question.instruction}",
                                      question.prompt(crop), picture, question.criteria)
        reply = self.vision.ask(question.system_prompt, question.prompt(crop), picture,
                                question.schema())
        # a local model may wrap the object in prose or a code fence despite the schema
        answer = json.dumps(first_json_object(reply.text))
        return Reply(parse_choice(answer, question), reply.truncated, reply.usage)


class AnthropicChoiceProvider:
    """A choice question through the Anthropic Messages API (analyze.vision.AnthropicVision)."""

    def __init__(self, model: str = ASSIST_MODELS["anthropic"], client: object | None = None
                 ) -> None:
        self.vision = AnthropicVision(model, client)
        self.cache_id = self.model = model

    def choose(self, question: Question, image: bytes, crop: BBox) -> Reply:
        reply = self.vision.ask(question.system_prompt, question.prompt(crop),
                                VisionImage(image, "image/png"), question.schema())
        if reply.truncated:
            raise ProviderError("model stopped: max_tokens")
        return Reply(parse_choice(reply.text, question), False, reply.usage)


def parse_choice(text: str, question: Question) -> str:
    """The offered id in a structured reply; anything else is an error."""
    try:
        data = json.loads(text)
        answer = data[question.answer_key]
    except (ValueError, KeyError, TypeError):
        raise ProviderError(f"reply is not the {{{question.answer_key}}} object") from None
    if answer not in question.choices:
        raise ProviderError(f"reply names {answer!r}, which was not offered")
    return answer


def make_provider(name: str = DEFAULT_ASSIST_PROVIDER, model: str | None = None
                  ) -> ChoiceProvider:
    if name not in ASSIST_PROVIDERS:
        raise ValueError(f"unknown assist provider: {name!r} (use {' or '.join(ASSIST_PROVIDERS)})")
    model = resolve_model(name, model)
    return OllamaChoiceProvider(model) if name == "ollama" else AnthropicChoiceProvider(model)
