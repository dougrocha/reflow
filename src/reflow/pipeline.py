"""The model half of a conversion: from a prepared Run to the finished `Model`, the passes in
order, and the files that hold it (names.MODEL_NAMES).

Everything this module and reflow.prepare import is in the model's key (reflow.saved, the
"model" stage of reflow.stagecache), so nothing here may import reflow.output, reflow.render or
reflow.convert: an edit to the renderer must leave the key, and the saved model, as they are
(tests/test_stagecache_keys.py fails if model code reaches render code). `convert` runs
`read_pages`, starts the page images on the pool, then runs `finish`; a render-only change reads
the saved model back with `Model.read` instead."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from reflow.analyze import (
    ask,
    assist,
    confidence,
    decisions_apply,
    direct,
    engine,
    enrich,
    evidence,
    label_assist,
    order_evidence,
    page_integrity,
    form_repair,
    page_vision,
    polish,
    prefetch,
    review,
    structure,
    structure_assist,
    text_evidence,
)
from reflow.analyze.engine import Backbone
from reflow.analyze.enrich import Figures, make_provider
from reflow.providers import TRANSCRIPTION_MODEL
from reflow.analyze.review import ReviewItem
from reflow.ingest import font_map, text_trust
from reflow.ingest.ocr import OcrPage, OcrPlan, OcrResult
from reflow.model import Document
from reflow.names import DOCUMENT_JSON, MODEL_JSON, REVIEW_JSON, TRACE_DIR
from reflow.run import Options, Run

SCHEMA = 1   # of MODEL_JSON


@dataclass(frozen=True)
class Providers:
    """The models a conversion may ask, made before anything is written, so ProviderUnavailable
    comes first. Transcription (assist local) asks TRANSCRIPTION_MODEL through Ollama, under the
    GPU lease. `label` and `page_vision` replace the assist provider and the local page-vision
    model (tests)."""
    transcriber: enrich.Provider | None
    alt: enrich.Provider
    options: Options
    label: ask.ChoiceProvider | None = None
    page_vision: page_vision.PageProvider | None = None
    direct: direct.Reader | None = None       # replaces the direct engine's model (tests)
    polish: direct.Reader | None = None       # replaces the polish model (tests)

    @classmethod
    def make(cls, opts: Options, **replacements) -> Providers:
        """The providers `opts` asks for; `replacements` are the optional fields above."""
        transcriber = (make_provider("ollama", model=TRANSCRIPTION_MODEL)
                       if opts.assist == "local" else None)
        model = {"model": opts.alt_text_model} if opts.alt_text_model else {}
        if opts.alt_text == "local":
            alt = make_provider("local", device=opts.device, **model)
        else:
            alt = make_provider(opts.alt_text, **model)
        return cls(transcriber, alt, opts, **replacements)

    def ask(self) -> ask.ChoiceProvider:
        return self.label or ask.make_provider(self.options.assist_provider,
                                                        self.options.assist_model_id)

    def vision(self) -> page_vision.PageProvider:
        return self.page_vision or page_vision.make_provider(self.options.assist_page_vision_model)

    def close(self) -> None:
        """Unload the local models and give back their GPU lease (analyze.local_model.LocalModel.close).
        Idempotent: `finish` closes each once it is done with it, and convert again at the end."""
        for provider in (self.transcriber, self.alt):
            if provider is not None:
                provider.close()


@dataclass(frozen=True)
class ModelFacts:
    """What the model passes knew besides the Document: the Summary's inputs from outside it.
    Saved in MODEL_JSON with the figures map; nothing here is a time or a path, so a conversion
    at --jobs 1 and one at --jobs N write the same bytes."""
    ocr: OcrResult                              # without its seconds
    alt_text: dict[str, int | str]              # provider, described, cached, decorative, failed, skipped
    alt_text_errors: list[str] = field(default_factory=list)
    docling_pages: dict[int, list[str]] = field(default_factory=dict)   # hybrid: hard pages
    page_vision: dict[str, int] = field(default_factory=dict)          # page_vision.run_pass


@dataclass(frozen=True)
class Model:
    """The finished model, built by `finish` or read back by `Model.read`; the render half reads
    only this. `body` is the text of DOCUMENT_JSON, and `doc` is what it parses to."""
    doc: Document
    body: str
    items: list[ReviewItem]
    figures: Figures          # node id -> (crop name under FIGURES_DIR, width, height)
    facts: ModelFacts

    def write_document(self, folder: Path) -> None:
        """DOCUMENT_JSON and REVIEW_JSON: the two files a reviewer's decisions change."""
        (folder / DOCUMENT_JSON).write_text(self.body, encoding="utf-8")
        (folder / REVIEW_JSON).write_text(json.dumps(self.items, ensure_ascii=False, indent=1),
                                          encoding="utf-8")

    def write(self, folder: Path, trace: dict) -> None:
        """DOCUMENT_JSON, REVIEW_JSON, TRACE_DIR/NNNN.json from the engine's `trace` (when it
        kept one), and MODEL_JSON, into `folder`. The figure crops are already there."""
        self.write_document(folder)
        if trace:
            (folder / TRACE_DIR).mkdir()
            for number, page_trace in trace.get("pages", {}).items():
                (folder / TRACE_DIR / f"{number:04d}.json").write_text(
                    json.dumps(page_trace, ensure_ascii=False, indent=None), encoding="utf-8")
        (folder / MODEL_JSON).write_text(_facts_json(self.figures, self.facts), encoding="utf-8")

    @classmethod
    def read(cls, folder: Path) -> Model:
        """The model `write` left in `folder`. Raises ValueError when DOCUMENT_JSON does not
        parse back to the same text: the renderer would read another model than the one saved."""
        body = (folder / DOCUMENT_JSON).read_text(encoding="utf-8")
        doc = Document.from_json(body)
        if doc.to_json(indent=None) != body:
            raise ValueError(f"{DOCUMENT_JSON} does not round-trip through the model")
        items = json.loads((folder / REVIEW_JSON).read_text(encoding="utf-8"))
        figures, facts = _facts_from((folder / MODEL_JSON).read_text(encoding="utf-8"))
        return cls(doc, body, items, figures, facts)


def decide(run: Run) -> list[ReviewItem]:
    """The tail of the model that a reviewer's decisions change, and the one place it is written:
    undo what a saved build applied (a model with no decisions is left as it is), then, when the
    run has a decisions file, score the model from the converter's own state and apply the
    store, then build the review queue. `finish` runs it on a fresh model; `redecide` runs it on
    a saved one, so a full build and a replay cannot drift."""
    decisions_apply.revert(run.doc)
    if (snapshot := decisions_apply.snapshot_of(run)) is not None:   # a reviewer's decisions file
        with run.step("decisions"):
            confidence.apply(run.doc)       # scored from the converter's own state, before any decision
            decisions_apply.apply(run, snapshot)
    with run.step("review"):
        return review.queue(run.doc)        # confidence.apply, then the review items


def redecide(run: Run, model: Model) -> Model:
    """A saved `model` (read back by `Model.read` into run.stage) with this run's decisions in
    place of the ones it was saved with: `decide` on its Document, then DOCUMENT_JSON and
    REVIEW_JSON again. Nothing to do, and nothing written, when the run has no decisions file
    and the saved model holds none: it is then the model `finish` would make."""
    if run.options.decisions is None and "decisions" not in run.doc.meta:
        return model
    items = decide(run)
    replayed = replace(model, body=run.doc.to_json(indent=None), items=items)
    replayed.write_document(run.stage)
    return replayed


def read_pages(run: Run, providers: Providers) -> Backbone:
    """The engine (root.children; lines, tables, trace, routing; the scan-field pass asks its
    model), then the OCR word grades onto the tree, before the title."""
    backbone = engine.build(run, providers.ask() if run.options.assist_scan_fields else None,
                            providers.direct)
    # Every pass runs in a timed step, so the stage seconds add up to the run.
    with run.step("ocr_marks"):
        text_evidence.flag_ocr_marks(run.doc, text_evidence.ocr_evidence(run, backbone.pages))
    return backbone


def finish(run: Run, backbone: Backbone, providers: Providers) -> Model:
    """The passes after the engine, in order, then the model's files in run.stage (Model.write).
    The page images may be rendering on the pool meanwhile (output.start_page_images)."""
    opts = run.options
    with run.step("prefetch"):
        prefetch.finish(run)                # the pages the evidence reads, parsed on the pool
    with run.step("evidence"):
        evidence.witness(run, backbone)     # the title; witnesses → facts and fills
    with run.step("order"):                 # tag-tree or Docling order disagrees
        order_evidence.apply(run, docling=backbone.routing.docling if backbone.routing else ())
    with run.step("structure"):
        structure.finish(run, backbone)     # structure facts, row headers, labels, ids
    if opts.assist_structure:                   # opt-in, remote: heading, label or value
        structure_assist.run_pass(run, providers.ask())
    detected = bool(run.doc.meta.get("scan_fields", {}).get("injected"))
    if opts.assist_labels or (opts.assist_scan_fields and detected):
        # opt-in, remote: which caption labels a field; for --assist-scan-fields alone, only
        # the fields it found on scanned pages
        label_assist.run_pass(run, providers.ask(), only_detected=not opts.assist_labels)
    vision: dict[str, int] = {}
    if opts.assist_page_vision:                 # opt-in, local: proposals; applies only the accepted
        vision = page_vision.run_pass(run, providers.vision())
    if opts.assist_form_repair:                 # opt-in, local: readings for review; no text changes
        form_repair.run_pass(run, form_repair.make_reader(opts.assist_form_repair_model))
    if opts.polish:                             # opt-in, remote: structural edits, text unchanged
        polish.run_pass(run, providers.polish)
    with run.step("scan_noisy"):
        page_integrity.flag_scan_noisy(run.doc, backbone.pages)   # assist sends these whole
    if providers.transcriber is not None:
        assist.transcribe(run, providers.transcriber)
    described = enrich.describe(run, providers.alt)   # figure crops + alt text
    providers.alt.close()
    with run.step("empty_pages"), run.workers.pdfium:
        page_integrity.flag_empty(run.doc, run.working)
    if run.fonts is not None and run.fonts.changes:   # the layer's reading of each decoded node
        with run.step("layer_text"):
            font_map.mark_nodes(run.doc, run.fonts.changes)
    with run.step("missing_words"):         # takes the pdfium lock itself, not around OCR
        run.state.missing = text_trust.flag_missing(
            run.doc, run.working, run.trust, lock=run.workers.pdfium,
            covers=page_integrity.covers_words, skip=frozenset(run.ocr.plan.pages),
            enabled=opts.engine_info.needs_ocr and opts.ocr != "none")
    items = decide(run)                     # a reviewer's decisions, then the review queue
    facts = ModelFacts(
        ocr=run.ocr, alt_text={"provider": described.provider, **described.stats},
        alt_text_errors=list(described.errors),
        docling_pages=dict(backbone.routing.hard) if backbone.routing else {},
        page_vision=dict(vision))
    with run.step("model"):
        model = Model(run.doc, run.doc.to_json(indent=None), items, described.files, facts)
        model.write(run.stage, backbone.trace)
    return model


# --------------------------------------------------------------------------- MODEL_JSON

def _facts_json(figures: Figures, facts: ModelFacts) -> str:
    ocr = facts.ocr
    value = {
        "schema": SCHEMA,
        "figures": {node: list(entry) for node, entry in figures.items()},
        "ocr": {"runner": ocr.runner, "graded": ocr.graded, "done": list(ocr.done),
                "failed": {str(n): e for n, e in ocr.failed.items()},
                "plan": {str(n): {"reason": p.reason, "mode": p.mode,
                                  "region_boxes": [list(b) for b in p.region_boxes]}
                         for n, p in ocr.plan.pages.items()}},
        "alt_text": facts.alt_text, "alt_text_errors": facts.alt_text_errors,
        "docling_pages": {str(n): list(r) for n, r in facts.docling_pages.items()},
    }   # not page_vision: a model it asked is never reused (saved.unprovable), so never read back
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)


def _facts_from(text: str) -> tuple[Figures, ModelFacts]:
    value = json.loads(text)
    if value.get("schema") != SCHEMA:
        raise ValueError(f"{MODEL_JSON}: schema {value.get('schema')!r}, not {SCHEMA}")
    figures: Figures = {node: (name, int(w), int(h)) for node, (name, w, h) in value["figures"].items()}
    o = value["ocr"]
    plan = OcrPlan({int(n): OcrPage(p["reason"], p["mode"],
                                    tuple(tuple(b) for b in p["region_boxes"]))
                    for n, p in o["plan"].items()})
    ocr = OcrResult(plan=plan, done=list(o["done"]),
                    failed={int(n): e for n, e in o["failed"].items()}, runner=o["runner"],
                    graded=o["graded"])
    return figures, ModelFacts(
        ocr=ocr, alt_text=value["alt_text"], alt_text_errors=list(value["alt_text_errors"]),
        docling_pages={int(n): list(r) for n, r in value["docling_pages"].items()})

