"""Reading the regions analyze.form_regions routes, as typed proposals checked against the page.
Nothing here changes the document: a checked proposal waits for review, beside the
baseline text it would add to, which stays as the PDF and OCR have it.

The question is narrow and typed by the region's task:

- FIELDS (a ruled row of a form): `{"fields": [{"id", "label", "widget"}, ...]}`. `id` is the
  printed field number ("29a"), or "" when the label has none; `label` the printed words after
  it; `widget` what is drawn to fill the field in, one of Widget, `unknown` when unsure. There is
  no slot for a value, so the model can't fill a blank in.
- TEXT (lines OCR did not read): `{"lines": [...]}`, each a printed line, top to bottom.

A reply is parsed once (`parse`): any other key or type rejects it. Then `check` tests it
against the region's evidence (form_regions.Region), and each failure is a Problem:

- a field number given twice, out of order, or one the page can't have here (neither read in
  the region nor missing from the page's run of numbers, nor a sub-field of one of those): the
  model invented or renumbered a field. A field number the text layer read in the region and
  the reply drops: the reply is incomplete.
- fewer fields than the row has cells with ink in them (each holds a label: the dental
  header's 10 inked cells are its 10 labels, 29a and 29b among them), or fewer letters than a
  third of the unread glyphs: materially incomplete (a table collapsed into a few words is not a success). More than
  LETTERS_PER_GLYPH letters per piece of ink the page holds there: invented text. The same
  line or label over and over: a repetition loop.
- a checkbox or radio where the rules found no square or circle: the widget is not supported
  by the page. Where they found comb boxes and nothing else, it is code boxes read as
  checkboxes (the dental form's diagnosis code boxes). Either way only that widget is dropped
  to `unknown`; the field stays.

A reply with a problem other than a dropped widget is rejected. The region is read once more,
told which check failed (`MAX_ATTEMPTS`), and if that fails too it is left unresolved. A
provider error (a timeout, the token limit, a reply that is not JSON) counts as an attempt.
An unavailable provider stops the pass; every region left is unresolved, and the baseline
stands.

Cache: each attempt's raw reply, or its provider error, is cached by (PDF sha256, page, region box, task, the
region's evidence, model, PROMPT_VERSION, attempt) in the carried assist cache, so a replay
makes no call and reaches the same verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from reflow.providers import FORM_REPAIR_MAX_CALLS, FORM_REPAIR_MODEL
from reflow import geometry
from reflow.analyze import ask
from reflow.analyze import form_regions as fr
from reflow.analyze.form_regions import Region, Task
from reflow.analyze.vision import OllamaVision, ProviderError, VisionImage, loads_unique
from reflow.model import Document, Fact, Source
from reflow.text_quality import squash

if TYPE_CHECKING:
    from reflow.run import Run

PROMPT_VERSION = 1
MAX_ATTEMPTS = 2           # a first reading and one targeted reread
MAX_TOKENS = 1500
NUM_CTX = 8192
LETTERS_PER_GLYPH = 2.5    # a reply with more letters than this per unread glyph invents text
MIN_LETTER_SHARE = 0.33    # a reply with fewer letters than this per unread glyph is incomplete
MAX_REPEATS = 3            # the same label or line more often than this is a repetition loop
MAX_LABEL = 120            # characters: a longer label is not one field's caption


class Widget(StrEnum):
    NONE = "none"               # a label or a heading over a column: nothing to fill in
    TEXT = "text"               # a blank cell or line to write in
    CODE_BOXES = "code_boxes"   # a row of boxes for one character each
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"


SYSTEM = ("You read one region cut from a form page and answer only with the structured "
          "output. Copy printed words exactly; never fill in a blank, guess a value, or add a "
          "field you cannot see.")

PROMPTS = {
    Task.FIELDS: (
        "Transcribe every printed field number and label in this image exactly, from left to "
        "right. Preserve all sub-numbered fields (29a, 29b). Do not fill blank cells.\n"
        "For each field give: id, its printed number with any letter (\"29a\"), or \"\" if it "
        "has none; label, the printed words after the number; widget, what is drawn to fill it "
        "in: none (a column heading or a label only), text (a blank to write in), code_boxes "
        "(a row of boxes for one character each), checkbox, radio, signature, or unknown if "
        "you are not sure."),
    Task.TEXT: (
        "Transcribe every printed line in this image exactly, top to bottom, one entry per "
        "line. Do not add anything that is not printed."),
}

SCHEMAS = {
    Task.FIELDS: {
        "type": "object", "required": ["fields"], "additionalProperties": False,
        "properties": {"fields": {"type": "array", "items": {
            "type": "object", "required": ["id", "label", "widget"],
            "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "label": {"type": "string"},
                           "widget": {"type": "string", "enum": [w.value for w in Widget]}}}}}},
    Task.TEXT: {
        "type": "object", "required": ["lines"], "additionalProperties": False,
        "properties": {"lines": {"type": "array", "items": {"type": "string"}}}},
}


class Problem(StrEnum):
    MALFORMED = "malformed"               # not the schema's JSON
    DUPLICATE_ID = "duplicate-id"
    OUT_OF_ORDER = "out-of-order"         # numbers not left to right ascending: renumbered
    INVENTED_ID = "invented-id"           # a number the page can't have here
    DROPPED_ID = "dropped-id"             # a number the text layer read here is missing
    INCOMPLETE = "incomplete"             # far fewer fields or letters than the page shows
    INVENTED_TEXT = "invented-text"       # far more letters than the page has ink for
    REPEATED = "repeated"                 # a repetition loop
    UNSUPPORTED_WIDGET = "unsupported-widget"   # a checkbox or radio with no square or circle
    CODE_BOXES_AS_CHECKBOX = "code-boxes-as-checkbox"
    PROVIDER = "provider"                 # the model's call failed: timeout, token limit, ...


# The words a reread is told, for each problem that rejects a reply.
TOLD = {
    Problem.MALFORMED: "the answer did not follow the structure",
    Problem.DUPLICATE_ID: "a field number was given twice",
    Problem.OUT_OF_ORDER: "the field numbers were not in the order printed",
    Problem.INVENTED_ID: "a field number was given that is not printed in the image",
    Problem.DROPPED_ID: "a printed field number was left out",
    Problem.INCOMPLETE: "much of the printed text was left out",
    Problem.INVENTED_TEXT: "the answer had more text than the image holds",
    Problem.REPEATED: "the answer repeated itself",
    Problem.PROVIDER: "the answer was cut off or failed",
}
WIDGET_ONLY = frozenset({Problem.UNSUPPORTED_WIDGET, Problem.CODE_BOXES_AS_CHECKBOX})


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    widget: Widget


@dataclass(frozen=True)
class Reading:
    """A reply, parsed: fields for a FIELDS region, lines for a TEXT one."""
    fields: tuple[Field, ...] = ()
    lines: tuple[str, ...] = ()


class Rejected(ValueError):
    pass


def parse(task: Task, raw: str) -> Reading:
    """The reply's JSON as a Reading, or Rejected for any other key, type or widget."""
    try:
        data = loads_unique(raw)
    except ValueError as e:
        raise Rejected(f"not JSON: {e}") from None
    if not isinstance(data, dict):
        raise Rejected("not an object")
    if task is Task.FIELDS:
        items = data.get("fields")
        if set(data) != {"fields"} or not isinstance(items, list):
            raise Rejected("needs exactly {fields: [...]}")
        out = []
        for item in items:
            if not (isinstance(item, dict) and set(item) == {"id", "label", "widget"}
                    and all(isinstance(item[k], str) for k in item)):
                raise Rejected(f"a field is not {{id, label, widget}}: {item!r}"[:200])
            try:
                widget = Widget(item["widget"])
            except ValueError:
                raise Rejected(f"no such widget: {item['widget']!r}") from None
            out.append(Field(item["id"].strip().rstrip("."), squash(item["label"]),
                             widget))
        return Reading(fields=tuple(out))
    lines = data.get("lines")
    if set(data) != {"lines"} or not isinstance(lines, list) or \
            not all(isinstance(line, str) for line in lines):
        raise Rejected("needs exactly {lines: [strings]}")
    return Reading(lines=tuple(squash(line) for line in lines if line.strip()))


# -- the checks -------------------------------------------------------------------------------

FIELD_ID = re.compile(r"^(\d{1,3})([a-z]?)$")
KNOWN_ID = re.compile(r"(?<![\w.])(\d{1,3}[a-z]?)\.(?=\s|$)")


def _key(fid: str) -> tuple[int, str]:
    m = FIELD_ID.match(fid)
    return (int(m.group(1)), m.group(2)) if m else (-1, fid)


def _letters(text: str) -> int:
    return sum(c.isalnum() for c in text)


def known_ids(region: Region) -> list[str]:
    return [m.group(1) for line in region.known for m in KNOWN_ID.finditer(line)]


def check(region: Region, reading: Reading) -> tuple[Reading, list[Problem]]:
    """The reading's problems against the region's evidence, and the reading with any
    unsupported widget made `unknown`."""
    problems: list[Problem] = []
    texts = [f"{f.id} {f.label}" for f in reading.fields] or list(reading.lines)
    letters = sum(_letters(t) for t in texts)
    ink = region.unread + sum(_letters(k) for k in region.known)
    if letters > LETTERS_PER_GLYPH * ink + 10:
        problems.append(Problem.INVENTED_TEXT)
    if letters < MIN_LETTER_SHARE * region.unread:
        problems.append(Problem.INCOMPLETE)
    seen: dict[str, int] = {}
    for t in [f.label for f in reading.fields] or texts:   # a label repeats whatever its number
        seen[t.casefold()] = seen.get(t.casefold(), 0) + 1
    if any(n > MAX_REPEATS for n in seen.values()) or any(len(f.label) > MAX_LABEL
                                                          for f in reading.fields):
        problems.append(Problem.REPEATED)
    fields = list(reading.fields)
    if region.task is Task.FIELDS:
        ids = [f.id for f in fields if f.id]
        if len(set(ids)) != len(ids):
            problems.append(Problem.DUPLICATE_ID)
        if any(not FIELD_ID.match(i) for i in ids) or \
                [_key(i) for i in ids] != sorted(_key(i) for i in ids):
            problems.append(Problem.OUT_OF_ORDER)
        known = known_ids(region)
        allowed = set(known) | set(region.expected)
        bases = {_key(i)[0] for i in allowed}
        if any(i not in allowed and not (_key(i)[1] and _key(i)[0] in bases) for i in ids):
            problems.append(Problem.INVENTED_ID)
        if set(known) - set(ids):
            problems.append(Problem.DROPPED_ID)
        if len(fields) < region.inked:    # each ruled cell with ink holds a field
            problems.append(Problem.INCOMPLETE)
        shapes = {c.shape for c in region.controls}
        for i, f in enumerate(fields):
            if f.widget in (Widget.CHECKBOX, Widget.RADIO) and not shapes & {"square", "circle"}:
                problems.append(Problem.CODE_BOXES_AS_CHECKBOX if "comb" in shapes
                                else Problem.UNSUPPORTED_WIDGET)
                fields[i] = Field(f.id, f.label, Widget.UNKNOWN)
    return Reading(tuple(fields), reading.lines), list(dict.fromkeys(problems))


# -- asking -----------------------------------------------------------------------------------


class RegionReader(Protocol):
    cache_id: str

    def read(self, task: Task, prompt: str, image: bytes) -> str:
        """The model's raw JSON reply for the PNG `image`. Raises ProviderError for one bad
        answer (the token limit included), ProviderUnavailable when none can succeed."""
        ...


class OllamaRegionReader:
    """The region question through a local Ollama server (analyze.vision.OllamaVision): the
    thinking-only qwen3-vl tags get the empty think block there, and the GPU lease."""

    def __init__(self, model: str = FORM_REPAIR_MODEL, host: str | None = None) -> None:
        self.vision = OllamaVision(model, host)
        self.cache_id = self.model = model

    def read(self, task: Task, prompt: str, image: bytes) -> str:
        reply = self.vision.ask(SYSTEM, prompt, VisionImage(image, "image/png"), SCHEMAS[task],
                                MAX_TOKENS, NUM_CTX)
        if reply.truncated:
            raise ProviderError(f"reply reached the {MAX_TOKENS}-token limit")
        return reply.text


def prompt_for(region: Region, problems: list[Problem]) -> str:
    """The task's prompt; for a reread, with the checks the last reading failed."""
    text = PROMPTS[region.task]
    if told := [TOLD[p] for p in problems if p in TOLD]:
        text += ("\nA previous reading of this image was rejected because "
                 + "; ".join(told) + ". Read it again carefully.")
    return text


def cache_key(sha: str, region: Region, model: str, attempt: int) -> str:
    evidence = [region.known, region.expected, region.columns, region.unread,
                [[c.shape, [round(v) for v in c.bbox], c.cells] for c in region.controls]]
    return ask.cache_digest("form-repair", sha, region.page, geometry.rounded(region.bbox, 1),
                            region.task.value, evidence, model, PROMPT_VERSION, attempt)


class Status(StrEnum):
    PROPOSED = "proposed"       # checked: waits for review
    UNRESOLVED = "unresolved"   # every attempt was rejected or failed
    SKIPPED = "skipped"         # the call budget ran out, or the provider is unavailable


@dataclass(frozen=True)
class Outcome:
    region: Region
    status: Status
    reading: Reading | None = None
    problems: tuple[Problem, ...] = ()        # the last attempt's (a PROPOSED one: widgets only)
    attempts: list[dict] = field(default_factory=list)   # per attempt: problems, cached, seconds

    @property
    def id(self) -> str:
        r = self.region
        return "fr-" + hashlib.sha256(json.dumps(
            [r.page, [round(v) for v in r.bbox], r.task.value]).encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        r = self.region
        out = {"id": self.id, "page": r.page, "bbox": geometry.rounded(r.bbox, 1),
               "task": r.task.value, "origin": r.origin.value, "reason": r.reason,
               "status": self.status.value, "known": list(r.known),
               "problems": [p.value for p in self.problems], "attempts": self.attempts}
        if self.reading is not None:
            out["fields"] = [asdict(f) | {"widget": f.widget.value} for f in self.reading.fields]
            out["lines"] = list(self.reading.lines)
        return out


def repair(regions: tuple[Region, ...] | list[Region], reader: RegionReader | None,
           cache_path: Path, *, sha: str, model: str,
           image_for: Callable[[Region], bytes],
           max_calls: int = FORM_REPAIR_MAX_CALLS) -> tuple[list[Outcome], dict[str, int]]:
    """Read each region (or replay the cache), check each reading, and return one Outcome per
    region with the counts. `reader` None replays only. `image_for` renders a region's crop
    and is called only before a real call. Nothing in the document changes."""
    stats = {"regions": len(regions), "calls": 0, "cached": 0, "failed": 0, "rejected": 0,
             "proposed": 0, "unresolved": 0, "skipped": 0, "unavailable": 0}
    outcomes: list[Outcome] = []
    with ask.Store(cache_path).saving() as store:
        asker = ask.Asker(store, reader, kind="form-repair", model=model,
                          version=PROMPT_VERSION, max_calls=max_calls, stats=stats, degrade=True)
        for region in regions:
            problems: list[Problem] = []
            attempts: list[dict] = []
            images: list[bytes] = []
            status, reading = Status.UNRESOLVED, None
            for attempt in range(MAX_ATTEMPTS):
                record: dict = {"attempt": attempt}
                took: list[float] = []

                def read() -> dict:
                    if not images:
                        images.append(image_for(region))
                    start = time.monotonic()
                    try:
                        return {"reply": reader.read(region.task, prompt_for(region, problems),
                                                     images[0])}
                    finally:
                        took.append(round(time.monotonic() - start, 2))

                got = asker.fetch(cache_key(sha, region, model, attempt), page=region.page,
                                  valid=lambda e: isinstance(e.get("reply"), str), call=read,
                                  record_errors=True)
                if got.status is ask.Status.SKIPPED:
                    status = Status.SKIPPED
                    record["skipped"] = asker.why_not()
                    attempts.append(record)
                    break
                if got.status is ask.Status.UNAVAILABLE:
                    stats["unavailable"] += 1
                    status = Status.SKIPPED
                    record["skipped"] = f"unavailable: {got.error}"[:200]
                    attempts.append(record)
                    break
                if got.error:
                    problems = [Problem.PROVIDER]
                    if got.status is ask.Status.CACHED:   # a failed call, replayed as it failed
                        record["cached"] = True
                    record.update(problems=[Problem.PROVIDER.value], error=got.error)
                    if got.status is not ask.Status.CACHED:
                        record["seconds"] = took[0]
                    attempts.append(record)
                    continue
                raw = got.entry["reply"]
                if got.status is ask.Status.CACHED:
                    record["cached"] = True
                else:
                    record["seconds"] = took[0]
                try:
                    reading, problems = check(region, parse(region.task, raw))
                except Rejected as e:
                    reading, problems = None, [Problem.MALFORMED]
                    record["error"] = str(e)[:200]
                record["problems"] = [p.value for p in problems]
                attempts.append(record)
                if reading is not None and not set(problems) - WIDGET_ONLY:
                    status = Status.PROPOSED
                    break
                stats["rejected"] += 1
                reading = None
            stats[status.value] += 1
            outcomes.append(Outcome(region, status, reading if status is Status.PROPOSED
                                    else None, tuple(problems), attempts))
    return outcomes, stats


# -- the pass ---------------------------------------------------------------------------------

def _shown(outcome: Outcome) -> str:
    if outcome.reading is None:
        return ""
    texts = [f"{f.id}. {f.label}" if f.id else f.label for f in outcome.reading.fields]
    return "; ".join(texts or outcome.reading.lines)


def _said(outcome: Outcome) -> str:
    if outcome.status is Status.PROPOSED:
        return f"read as: {_shown(outcome)}"
    why = f" ({', '.join(p.value for p in outcome.problems)})" if outcome.problems else ""
    return f"no reading passed its checks{why}"


def flag(doc: Document, outcomes: list[Outcome], model: str) -> None:
    """One review fact per page with regions read, since the review queue makes one item of a
    page's facts and shows one text: `form-repair-proposal` when a reading passed, else
    `form-repair-unresolved`. Its text gives every region's outcome in page order and its
    targets every region's box. Facts only; no node and no text changes."""
    pages = {p.number: p for p in doc.pages}
    by_page: dict[int, list[Outcome]] = {}
    for o in outcomes:
        by_page.setdefault(o.region.page, []).append(o)
    for number, found in by_page.items():
        proposed = any(o.status is Status.PROPOSED for o in found)
        said = "; ".join(f"region {i} {_said(o)}" for i, o in enumerate(found, 1))
        pages[number].facts.append(Fact(
            "form-repair-proposal" if proposed else "form-repair-unresolved", Source.ASSIST,
            f"OCR left text unread in {len(found)} region(s) of this page, which {model} "
            f"read for review. {said}. Check them against the page.",
            {"model": model, "proposals": [o.id for o in found],
             "region_boxes": [round(v, 1) for o in found for v in o.region.bbox]}))


def run_pass(run: Run, reader: RegionReader | None) -> dict[str, int]:
    """The pipeline pass (after structure and the other assists, before review): route, read,
    check, flag, and write FORM_REPAIR_JSON. Returns the counts. The document's text and
    structure are those of a run without the pass."""
    from reflow.names import ASSIST_CACHE, FORM_REPAIR_JSON

    opts = run.options
    model = opts.assist_form_repair_model
    renderer = ask.CropRenderer(run.working, run.workers.pdfium)

    def read(number: int) -> fr.PageRead:
        with run.workers.pdfium:
            return fr.read_page(run.working, number)

    with run.step("form_repair", "AI form repair"):
        routing = fr.route(run.doc, read)
        try:
            outcomes, stats = repair(routing.regions, reader, run.stage / ASSIST_CACHE,
                                     sha=str(run.doc.meta.get("sha256", "")), model=model,
                                     image_for=lambda r: renderer.crop(r)[0],
                                     max_calls=opts.assist_form_repair_max_calls)
        finally:
            renderer.close()
        flag(run.doc, outcomes, model)
    skipped: dict[str, int] = {}
    for skip in routing.skipped:
        skipped[skip.reason] = skipped.get(skip.reason, 0) + 1
    stats = {**stats, **{f"pages_{k}" if k == "native-text" else f"regions_{k}": v
                         for k, v in skipped.items()}}
    stats = {k.replace("-", "_"): v for k, v in stats.items()}
    record = {"model": model, "prompt_version": PROMPT_VERSION, "stats": stats,
              "regions": [o.to_dict() for o in outcomes],
              "skipped": [{"page": s.page, "reason": s.reason,
                           "bbox": geometry.rounded(s.bbox, 1) if s.bbox else None}
                          for s in routing.skipped]}
    run.doc.meta["form_repair"] = {"model": model, **stats}
    (run.stage / FORM_REPAIR_JSON).write_text(json.dumps(record, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    return stats


def make_reader(model: str | None = None) -> RegionReader:
    return OllamaRegionReader(model or FORM_REPAIR_MODEL)


__all__ = ["Field", "OllamaRegionReader", "Outcome",
           "flag", "make_reader", "run_pass",
           "Problem", "Reading", "Rejected", "RegionReader", "Status", "Widget", "cache_key",
           "check", "parse", "prompt_for", "repair"]
