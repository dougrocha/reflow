"""The opt-in AI steps of a conversion, declared once: what each one is called, when it is on,
where its pass leaves its stats, and what it says about itself to a build record, a reader and a
check of freshness.

`convert.Summary` takes each step's stats from `doc.meta`, `cli.report` prints them,
`uncovered` names what no record can pin down about them and `provenance.build_info`
publishes them. Adding a step is an entry here, a report line in `cli.REPORTS` and the pass.

A step reads the options as a record (`options_record`: the Options fields by name), which is
what a published build keeps. A record from an older build may lack a newer field, so every read
is a `.get`.

The converter reads these (`saved` keys a model on the options record and on what `uncovered`
names), and the docs:build record (`reflow.provenance`) reads them from here.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reflow.providers import ALT_TEXT_MODELS, resolve_model

if TYPE_CHECKING:
    from reflow.run import Options

type Record = Mapping[str, Any]


@dataclass(frozen=True)
class AiStep:
    key: str                              # the Summary attribute, and the report line
    meta: str | None                      # the doc.meta key its pass writes its stats under
    on: Callable[[Record], bool]
    uncovered: Callable[[Record], str] | None = None   # what a record cannot pin down
    asks: str = ""        # what the assist model chooses, for the one sentence that joins these steps
    info: Callable[[Record, Mapping[str, Any]], dict[str, Any]] | None = None   # its build_info entry
    run: bool = False     # reads page by page and keeps per-page costs (provenance.model_run)


def _asked(what: str, cap: str) -> Callable[[Record, Mapping[str, Any]], dict[str, Any]]:
    """The build_info entry of a step that puts questions to the assist model: its stats count the
    answers (fresh or cached), the questions sent and the ones the cap left unasked."""
    def info(opts: Record, stats: Mapping[str, Any]) -> dict[str, Any]:
        provider = opts["assist_provider"]
        return {"step": what, "provider": provider,
                "model": stats.get("model") or resolve_model(provider, opts.get("assist_model")),
                "answers": stats.get("asked", 0), "sent": stats.get("calls", 0),
                "cap": opts[cap], "unasked": stats.get("skipped", 0)}
    return info


def _transcription(opts: Record, stats: Mapping[str, Any]) -> dict[str, Any]:
    return {"step": "Transcription", "provider": "local", "model": ALT_TEXT_MODELS["local"]}


def _page_vision(opts: Record, stats: Mapping[str, Any]) -> dict[str, Any]:
    return {"step": "Page vision", "provider": "ollama", "model": opts["assist_page_vision_model"],
            "cap": opts["assist_page_vision_max_pages"]}


def _form_repair(opts: Record, stats: Mapping[str, Any]) -> dict[str, Any]:
    """Its answers are the regions the model read, whether or not the reading passed its checks."""
    return {"step": "Form repair", "provider": "ollama", "model": opts["assist_form_repair_model"],
            "answers": stats.get("proposed", 0) + stats.get("unresolved", 0),
            "sent": stats.get("calls", 0), "cap": opts["assist_form_repair_max_calls"],
            "unasked": stats.get("skipped", 0)}


def _page_vision_uncovered(opts: Record) -> str:
    what = ("heading changes and unread crops" if opts.get("assist_page_vision_unread")
            else "heading changes")
    return (f"{what} proposed by ollama ({opts['assist_page_vision_model']}) and accepted by a "
            "reviewer, with the assist cache and the decisions carried over from the previous "
            "output")


# In this order: the sentences of `uncovered` and the entries of build_info's "ai".
STEPS = (
    AiStep("assist", "ai_assist", lambda o: o.get("assist", "none") != "none",
           uncovered=lambda o: f"transcriptions by the {o['assist']} assist model",
           info=_transcription),
    AiStep("label_assist", "label_assist", lambda o: bool(o.get("assist_labels")),
           asks="form-field labels", info=_asked("Form-field labels", "assist_labels_max_calls")),
    AiStep("structure_assist", "structure_assist", lambda o: bool(o.get("assist_structure")),
           asks="headings", info=_asked("Headings", "assist_structure_max_calls")),
    AiStep("scan_fields", "scan_fields", lambda o: bool(o.get("assist_scan_fields")),
           asks="scanned-form fields",
           info=_asked("Scanned-form fields", "assist_scan_fields_max_calls")),
    AiStep("direct", "direct", lambda o: o.get("engine") == "direct", run=True,
           uncovered=lambda o: (f"page transcripts written by {o['direct_model']} (prompt "
                                f"{o['direct_prompt']}), and the shared cache of its answers")),
    AiStep("polish", "polish", lambda o: bool(o.get("polish")), run=True,
           uncovered=lambda o: (f"structural edits proposed by {o['polish_model']}, and the "
                                "shared cache of its answers")),
    AiStep("page_vision", None, lambda o: bool(o.get("assist_page_vision")),
           uncovered=_page_vision_uncovered, info=_page_vision),
    AiStep("form_repair", "form_repair", lambda o: bool(o.get("assist_form_repair")),
           uncovered=lambda o: ("form text OCR left unread, read by ollama "
                                f"({o['assist_form_repair_model']}) for review, with the assist "
                                "cache carried over from the previous output"),
           info=_form_repair),
)


def options_record(options: Options) -> dict[str, Any]:
    """Every field of `options` that Options compares: the ones that can change the output. The
    password is kept only as whether one was given; the stage-cache folder and the device as
    asked for are not compared (the resolved device is)."""
    out: dict[str, Any] = {}
    for f in dataclasses.fields(options):
        if not f.compare:
            continue
        value = getattr(options, f.name)
        if f.name == "password":
            value = bool(value)
        elif isinstance(value, tuple):
            value = list(value)
        out[f.name] = value
    return out


def uncovered(options: Options) -> list[str]:
    """What a conversion with `options` reads that no record can pin down: a model's answers,
    and the caches of them carried over from the previous output."""
    record = options_record(options)
    out = []
    if options.alt_text != "none":
        out.append(f"figure descriptions drafted by {options.alt_text} "
                   f"({options.alt_text_model or 'its default model'}), and the alt-text cache "
                   "carried over from the previous output")
    asked = [step.asks for step in STEPS if step.asks and step.on(record)]
    for step in STEPS:
        if step.asks and asked:
            out.append(f"{', '.join(asked)} chosen by {options.assist_provider} "
                       f"({options.assist_model or 'its default model'}), and the assist cache "
                       "carried over from the previous output")
            asked = []
        elif step.uncovered and step.on(record):
            out.append(step.uncovered(record))
    return out


def read_json(path: Path) -> Any:
    """The JSON in `path`, or None when it is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
