"""What one conversion runs with: the options, the workers and the lock, the run itself.

`convert` builds a frozen `Run` and hands it to the passes (reflow.prepare, then the ones
reflow.pipeline runs in order, then output.publish). A pass reads any field; the only things it
may change are `run.doc`, `run.seconds` through `step`, and the fields of `run.state`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Executor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reflow import budget
from reflow.device import DEVICES, resolve_device
from reflow.extract.parallel import CHUNK_PAGES, make_pool
from reflow.ingest.ocr import OCR_MODES, OcrResult
from reflow.model import Document
from reflow.pageimages import LARGE_WIDTH_MAX_PAGES, PAGE_WIDTHS, widths_for
from reflow.providers import (
    ALT_TEXT_MODELS,
    ALT_TEXT_PROVIDERS,
    ASSIST_MODELS,
    ASSIST_PROVIDERS,
    CLAUDE_HAIKU,
    CLAUDE_OPUS,
    CLAUDE_SONNET,
    DEFAULT_ASSIST_PROVIDER,
    DIRECT_MAX_DOLLARS,
    DIRECT_MODEL,
    DIRECT_PROMPTS,
    FORM_REPAIR_MAX_CALLS,
    FORM_REPAIR_MODEL,
    LABEL_MAX_CALLS,
    PAGE_VISION_MAX_PAGES,
    PAGE_VISION_MODEL,
    POLISH_MAX_DOLLARS,
    POLISH_MODEL,
    SCAN_FIELDS_MAX_CALLS,
    STRUCTURE_MAX_CALLS,
    TRANSCRIPTION_MODEL,
    provider_of,
    resolve_model,
)

if TYPE_CHECKING:
    from reflow.analyze.decisions import Snapshot
    from reflow.extract.links import Pending
    from reflow.extract.pagecontent import PageReader
    from reflow.extract.tags import TagPage, Walked
    from reflow.ingest.font_map import Repair
    from reflow.ingest.text_trust import MissingReport, TrustReport
    from reflow.render.pages import PageImages

# on_progress(stage, done, total); total is None when unknown.
type Progress = Callable[[str, int, int | None], None]
# tick(k) inside a step: k more units done since the last tick (chunks, pages, figures).
type Tick = Callable[[int], None]

# pdfium keeps process-wide state. convert_many runs documents on threads of one process, so
# every pdfium call in this process (ingest, figure crops, later needs_ocr) holds this lock.
# Pool workers are other processes and need no lock. One lock per process: Workers carries a
# reference to it, never a copy.
PDFIUM_LOCK = threading.RLock()

# The metadata key that marks an Options field as a render setting: read by the renderer only,
# so it is in the render key and not the model key (reflow.saved). An unmarked field is a model
# option, the safe side: changing it builds the model again.
RENDER = "render"


@dataclass(frozen=True)
class Engine:
    """What a layout engine needs and yields; the one place that says (analyze.engine holds its
    builders and asserts it has one for each)."""
    has_lines: bool     # yields pdfium page text and lines, which the scan-field pass reads
    needs_ocr: bool     # the pages are read from the text layer, so image-only ones are OCR'd
    loads_model: bool   # runs a model in this process (device, GPU lease)
    provable: bool      # no weights outside any key (a model's answers are an AI step's: reflow.steps)


ENGINES = {
    "heuristic": Engine(has_lines=True, needs_ocr=True, loads_model=False, provable=True),
    "docling": Engine(has_lines=False, needs_ocr=True, loads_model=True, provable=False),
    "hybrid": Engine(has_lines=True, needs_ocr=True, loads_model=True, provable=False),
    # a model reads each page's picture itself
    "direct": Engine(has_lines=False, needs_ocr=False, loads_model=False, provable=True),
}

FLAG = "flag"   # the metadata key of a field's Flag


@dataclass(frozen=True)
class Flag:
    """How an Options field is a command-line setting: its help, the commands that take it, and
    what typer needs beyond the field's type and default. The flag is the field's name with
    dashes; `pair` gives a switch both forms (--page-images/--no-page-images)."""
    help: str
    commands: tuple[str, ...] = ("convert", "convert-many")
    pair: bool = False
    min: int | None = None
    args: dict[str, Any] = field(default_factory=dict)   # typer.Option keywords (file_okay, ...)


def option(default: Any, *, help: str, render: bool = False, pair: bool = False,
           min: int | None = None, args: dict[str, Any] | None = None,
           commands: tuple[str, ...] = ("convert", "convert-many"), **kwargs: Any) -> Any:
    """An Options field that is a command-line setting. `render` marks it a render setting."""
    flag = Flag(help, commands, pair, min, args or {})
    return field(default=default, metadata={FLAG: flag, **({RENDER: True} if render else {})},
                 **kwargs)


class OptionsError(ValueError):
    """An option is bad: an unknown name, a value out of range, a combination that cannot run, or
    an output folder that is not a conversion's. The command line reports it as exit 2. Any other
    ValueError is a bug in the run, not in what the caller asked for. It is a ValueError, so
    callers that catch that keep working."""


@dataclass(frozen=True)
class Options:
    """Every keyword `convert` accepts about the document. Validated here, once; nowhere else.
    A field declared with `option` is a command-line setting of `convert` and `convert-many`
    (`Flag`): reflow.cli generates both commands' parameters from it. `render=True` marks a
    render setting, read by the renderer only, so it is in the render key and not the model key
    (reflow.saved); an unmarked field is a model option, the safe side: changing it builds the
    model again. A field with `compare=False` is in neither key nor the options record.
    `jobs`, `executor` and `on_progress` are not options: they describe the run (Workers)."""
    password: str = field(default="", repr=False)   # from REFLOW_PDF_PASSWORD or a prompt (reflow.cli); never in a repr
    page_images: bool = option(
        True,
        help='Render page images for "view original".',
        render=True,
        pair=True,
    )
    engine: str = option(
        "heuristic",
        help=(f"Layout engine: {', '.join(ENGINES)}. docling needs the docling extra (ML models); "
            "hybrid runs the heuristics, with docling on hard pages; direct has a model transcribe "
            "each page (--direct-model, docs/direct.md)."),
    )
    device: str = option(
        "auto",
        help=(f"Device for docling and local models: {', '.join(DEVICES)}. "
            "auto picks cuda, then mps, else cpu."),
    )   # as asked for; `with_device` names the concrete device a model will run on
    alt_text: str = option(
        "none",
        help=('none (default): figures without alt text are listed in review.json for you to '
            'fix. Or draft suggestions for you to check, labelled AI-drafted and listed in '
            'review.json: ollama (a local Ollama server, nothing leaves the machine), '
            "anthropic (the Anthropic API with your ANTHROPIC_API_KEY: each figure's crop and"
            ' nearby text leave the machine), or local (an on-device model in this process: '
            'GPU, docling extra). See docs/assist.md.'),
    )   # providers.ALT_TEXT_PROVIDERS: none | local | ollama | anthropic
    alt_text_model: str | None = option(
        None,
        help=("The model --alt-text uses, instead of its default (ollama: "
            f"{ALT_TEXT_MODELS['ollama']}; anthropic: {ALT_TEXT_MODELS['anthropic']})."),
    )
    assist: str = option(
        "none",
        help=('none (default) or local: read what OCR could not (unread crops, hard scans) with '
            f'a local vision model through Ollama ({TRANSCRIPTION_MODEL}). A reading that passes '
            "its checks against the crop is a draft, labelled and queued for review. Nothing "
            'leaves the machine unless OLLAMA_HOST points elsewhere. Figure alt text is '
            '--alt-text. See docs/assist.md.'),
    )
    # Opt-in AI choice of which printed caption labels a form field the rules could not name
    # (analyze.label_assist). Sends page crops to a model: off by default.
    assist_labels: bool = option(
        False,
        help=('Off by default. Ask a model (on this machine by default; see --assist-provider) '
            'which printed caption labels each form field the rules could not name. Sends, '
            "per question, a PNG crop of the page around the field, the candidate captions' "
            "texts and boxes, and the field's PDF name and tooltip: forms can hold personal "
            'data. See docs/assist.md.'),
    )
    assist_provider: str = option(
        DEFAULT_ASSIST_PROVIDER,
        help=("Where --assist-labels, --assist-structure and --assist-scan-fields run the model: "
            "ollama (default; a local Ollama server, nothing leaves the machine: `ollama serve`, "
            f"`ollama pull {ASSIST_MODELS['ollama']}`) or anthropic (the Anthropic API; needs the "
            "anthropic package and ANTHROPIC_API_KEY)."),
    )   # providers.ASSIST_PROVIDERS: ollama (local server, default) | anthropic
    assist_model: str | None = option(
        None,
        help=("Model for --assist-labels, --assist-structure and --assist-scan-fields (default: "
            f"{ASSIST_MODELS['ollama']} for ollama, {ASSIST_MODELS['anthropic']} for anthropic)."),
    )
    assist_labels_max_calls: int = option(
        LABEL_MAX_CALLS,
        help='Most uncached questions asked per document by --assist-labels.',
        min=0,
    )
    # Opt-in AI choice of whether a low-confidence heading on a form is a heading, a label or a
    # value (analyze.structure_assist). Shares assist_model with assist_labels.
    assist_structure: bool = option(
        False,
        help=('Off by default. Ask a model (same provider and model as --assist-labels) whether'
            ' each heading of a form that the rules are unsure of is really a heading, a '
            'label or a value. Sends, per question, a PNG crop of the page around the line, '
            'its text, box and neighbouring lines. See docs/assist.md.'),
    )
    assist_structure_max_calls: int = option(
        STRUCTURE_MAX_CALLS,
        help='Most uncached questions asked per document by --assist-structure.',
        min=0,
    )
    # Fields on scanned forms with no AcroForm (analyze.scan_fields). scan_fields: the rules read
    # the page image and keep the places they are sure of; no model. assist_scan_fields: also asks
    # the model about the doubtful ones (and implies scan_fields). Shares assist_model.
    scan_fields: bool = option(
        False,
        help=('Off by default. Find the form fields of a scanned form that has no fillable '
            'fields: rules read the page image (underlines, blank cells, digit boxes, '
            'checkbox squares) and keep the places they are sure of. No model, nothing leaves'
            ' the machine. Skipped for a PDF with a form of its own. See docs/assist.md.'),
    )
    assist_scan_fields: bool = option(
        False,
        help=('Off by default; implies --scan-fields. Also ask a model (same provider and model'
            ' as --assist-labels) about the places the rules are unsure of, and which printed'
            ' caption names each field it found. Sends, per question, a PNG crop of the '
            'scanned page around the place with the place outlined, its shape and box, and '
            'the nearby text: forms can hold personal data. See docs/assist.md.'),
    )
    assist_scan_fields_max_calls: int = option(
        SCAN_FIELDS_MAX_CALLS,
        help='Most uncached questions asked per document by --assist-scan-fields.',
        min=0,
    )
    # Opt-in page vision (analyze.page_vision): a local vision model (Ollama) reads each
    # page and proposes heading changes, applied only once a reviewer accepts them.
    assist_page_vision: bool = option(
        False,
        help=("Off by default. A local vision model (Ollama) looks at each page with reflow's "
            'blocks outlined and proposes heading changes (demote a heading, promote a '
            'paragraph). A proposal changes nothing until you accept it with `reflow review`;'
            ' the next conversion with this flag applies it, if the text is unchanged. It '
            'never writes text. Sends one page picture per page to the local server.'),
    )
    assist_page_vision_max_pages: int = option(
        PAGE_VISION_MAX_PAGES,
        help='Most uncached pages asked per document by --assist-page-vision.',
        min=0,
    )
    assist_page_vision_model: str = option(
        PAGE_VISION_MODEL,
        help='The Ollama model --assist-page-vision asks.',
    )
    # Also propose unread crops: an OCR paragraph the model calls handwriting, noise or a
    # figure label, and that a rule already doubts. Each still waits for a reviewer's accept.
    assist_page_vision_unread: bool = option(
        False,
        help=('Off by default. With --assist-page-vision, also propose hiding an OCR paragraph '
            'in an unread crop where the model calls it handwriting, noise or a figure label '
            'and a rule already doubts it (an unsure OCR word, no word printed elsewhere in '
            'the document, or beside a figure). Like every proposal it changes nothing until '
            "you accept it; the text stays in the PDF's layer."),
    )
    # Opt-in form repair (analyze.form_repair): where OCR left a form's text unread, a
    # local vision model (Ollama) reads just that region; its checked reading waits for review
    # and never changes the text.
    assist_form_repair: bool = option(
        False,
        help=("Off by default. Where OCR left a form's text unread (a row of field labels, "
            'lines of small print), a local vision model (Ollama) reads just that region, and'
            ' its reading is checked against the page: field numbers, cells, letters and '
            'controls. A reading that passes is a review item beside the OCR text; nothing in'
            ' the transcript changes. Born-digital pages are never read. Sends one PNG crop '
            'per region to the local server. See docs/assist.md.'),
    )
    assist_form_repair_max_calls: int = option(
        FORM_REPAIR_MAX_CALLS,
        help=('Most uncached model calls per document by --assist-form-repair, rereads '
            'included.'),
        min=0,
    )
    assist_form_repair_model: str = option(
        FORM_REPAIR_MODEL,
        help='The Ollama model --assist-form-repair asks.',
    )
    # The prompt-only pipeline (--engine direct, analyze.direct): the model that reads each
    # page (a Claude model on the Anthropic API, else one on the local Ollama server), the most a
    # run may spend on uncached pages, the requests in flight, and the shared answer cache (None:
    # direct.default_cache). The cap and the jobs change no answer, so they are in no key.
    direct_model: str = option(
        DIRECT_MODEL,
        help=("With --engine direct: the model that transcribes each page. A Claude "
            f"model ({CLAUDE_HAIKU}, the default, {CLAUDE_SONNET}, {CLAUDE_OPUS}) runs on the "
            "Anthropic API with your ANTHROPIC_API_KEY: each uncached page's picture and text layer "
            "leave the machine. Any other name is a model on the local Ollama server, such as "
            "glm-ocr. See docs/direct.md."),
    )
    direct_prompt: str = option(
        "base",
        help=('With --engine direct: the prompt variant (docs/direct.md). base (the default); '
            'complete: transcribe every entry, never summarize; forms: stricter form rules; '
            'example: a short example of the HTML wanted.'),
    )
    direct_max_dollars: float = option(
        DIRECT_MAX_DOLLARS,
        help=('With --engine direct: the most a run spends on uncached pages. At the cap it '
            'stops, keeps the pages it finished and flags the rest.'),
        min=0,
        compare=False,
    )
    direct_jobs: int = field(default=4, compare=False)
    direct_cache: Path | None = field(default=None, compare=False, repr=False)
    # Heuristics + AI polish (analyze.polish): after the rules, a Claude model proposes
    # structural edits to each page's nodes, checked against the page's text. Shares the direct
    # engine's answer cache and jobs; its cap is its own.
    polish: bool = option(
        False,
        help=("Off by default. After reflow's own pipeline, a model sees each page's picture "
            'and its transcript and proposes structural edits (heading levels, lists, table '
            "headers, reading order, form groups). An edit that would change the page's text "
            "is rejected; each applied one is marked AI-drafted. Sends each uncached page's "
            'picture and transcript to the Anthropic API. See docs/polish.md.'),
    )
    polish_model: str = option(
        POLISH_MODEL,
        help='With --polish: the Claude model that proposes the edits.',
    )
    polish_max_dollars: float = option(
        POLISH_MAX_DOLLARS,
        help=('With --polish: the most a run spends on uncached pages; the rest stay '
            'unpolished.'),
        min=0,
        compare=False,
    )
    ocr: str = option(
        "auto",
        help=('OCR for image-only pages: auto, none, ocrmypdf. auto (default) runs OCRmyPDF '
            'when it is installed and flags the pages otherwise; none never runs it; ocrmypdf'
            ' requires it (see reflow doctor).'),
    )
    allow_restricted: bool = option(
        False,
        help=('Convert a PDF whose owner forbids text extraction, and flag it in review.json. '
            'Without it such a PDF is refused (exit 3).'),
    )
    chunk_pages: int | None = CHUNK_PAGES
    device_requested: str = field(default="", init=False, repr=False, compare=False)
    page_widths: tuple[int, ...] = option(
        PAGE_WIDTHS,
        help=('Page image widths in px, comma-separated and ascending (default 800,1200). The '
            'first is the default image; the others are srcset candidates for high-DPI '
            'screens (about 2.25x the image pixels with 800,1200). A single width turns '
            'srcset off.'),
        render=True,
    )
    stage_cache: Path | None = option(
        None,
        help=('Off by default. Keep the reading of each PDF (the working copy, OCR, text '
            'extraction and the page reads) in this directory, and reuse it when the same PDF'
            ' is converted again with the same reading code and package versions, so a change'
            ' to analysis or rendering converts faster. Processes can share it. reflow never '
            'removes what it keeps there; the page-check harness prunes its own cache by size'
            ' and age (reflow.stagecache).'),
        args={'file_okay': False},
        compare=False, repr=False,
    )
    srcset_max_pages: int | None = option(
        LARGE_WIDTH_MAX_PAGES,
        help=('Documents with more pages than this get only the first page width, to bound '
            'storage. 0 means no limit.'),
        render=True,
        min=0,
    )
    # With stage_cache: render from a saved model whose identity is proved (reflow.saved); False
    # builds the model again, and keeps it (the page-check harness's timing runs).
    reuse_model: bool = field(default=True, compare=False, repr=False)
    # A reviewer's decisions (analyze.decisions, docs/review-decisions.md): the JSONL store the
    # converter reads once. A missing file is an empty store. compare=False keeps it out of the
    # options record and the model key: a saved model is a base, and reflow.saved replays the
    # store on it (pipeline.redecide). `convert-many` takes a folder of them (--decisions-dir).
    decisions: Path | None = option(
        None,
        help=("Off by default. A reviewer's decisions file (JSONL, docs/review-decisions.md): "
            'corrections, kept readings, alt texts and dismissed warnings. Each is applied '
            'only where it still matches the page exactly; the rest go to review.json. Writes'
            ' decisions.json beside the site. A missing file is an empty store; the converter'
            ' never writes it.'),
        args={'dir_okay': False},
        commands=("convert",),
        compare=False,
    )

    @property
    def assist_model_id(self) -> str:
        """The model the assist steps use: assist_model, else the provider's default."""
        return resolve_model(self.assist_provider, self.assist_model)

    def __post_init__(self) -> None:
        if self.alt_text not in ALT_TEXT_PROVIDERS:
            raise OptionsError(f"unknown alt-text provider: {self.alt_text!r} "
                             f"(use {', '.join(ALT_TEXT_PROVIDERS)})")
        if self.alt_text_model is not None and not self.alt_text_model.strip():
            raise OptionsError("alt_text_model must not be blank")
        if self.alt_text_model is not None and self.alt_text == "none":
            raise OptionsError("--alt-text-model needs --alt-text local, ollama or anthropic")
        if self.decisions is not None:
            path = Path(self.decisions)
            if path.exists() and not path.is_file():
                raise OptionsError(f"the decisions path {path} is not a file")
            object.__setattr__(self, "decisions", path)
        if self.assist not in ("none", "local"):
            raise OptionsError(f"unknown assist provider: {self.assist!r} (use none or local)")
        if self.assist_labels_max_calls < 0:
            raise OptionsError("assist_labels_max_calls must be >= 0")
        if self.assist_provider not in ASSIST_PROVIDERS:
            raise OptionsError(f"unknown assist provider: {self.assist_provider!r} "
                             f"(use {' or '.join(ASSIST_PROVIDERS)})")
        if self.assist_model is not None and not self.assist_model.strip():
            raise OptionsError("assist_model must not be blank")
        if self.assist_structure_max_calls < 0:
            raise OptionsError("assist_structure_max_calls must be >= 0")
        if self.assist_scan_fields_max_calls < 0:
            raise OptionsError("assist_scan_fields_max_calls must be >= 0")
        if self.assist_page_vision_max_pages < 0:
            raise OptionsError("assist_page_vision_max_pages must be >= 0")
        if not self.assist_page_vision_model.strip():
            raise OptionsError("assist_page_vision_model must not be blank")
        if self.assist_form_repair_max_calls < 0:
            raise OptionsError("assist_form_repair_max_calls must be >= 0")
        if not self.assist_form_repair_model.strip():
            raise OptionsError("assist_form_repair_model must not be blank")
        if self.engine not in ENGINES:
            raise OptionsError(f"unknown engine: {self.engine!r} (use {', '.join(ENGINES)})")
        if self.detect_scan_fields and not self.engine_info.has_lines:
            raise OptionsError("--scan-fields and --assist-scan-fields need page text and "
                             f"lines, which --engine {self.engine} does not produce; use "
                             "heuristic or hybrid.")
        if not self.direct_model.strip():
            raise OptionsError("direct_model must not be blank")
        if self.direct_prompt not in DIRECT_PROMPTS:
            raise OptionsError(f"unknown direct prompt: {self.direct_prompt!r} "
                             f"(use {', '.join(DIRECT_PROMPTS)})")
        if self.direct_max_dollars < 0:
            raise OptionsError("direct_max_dollars must be >= 0")
        if provider_of(self.polish_model) != "anthropic":
            raise OptionsError("--polish-model must be a Claude model (claude-haiku-5-5, ...)")
        if self.polish_max_dollars < 0:
            raise OptionsError("polish_max_dollars must be >= 0")
        if self.polish and self.engine == "direct":
            raise OptionsError("--polish edits the nodes reflow's rules make; --engine direct has "
                             "a model write them")
        if self.direct_jobs < 1:
            raise OptionsError("direct_jobs must be >= 1")
        if self.device not in DEVICES:
            raise OptionsError(f"unknown device: {self.device!r} (use {', '.join(DEVICES)})")
        if self.ocr not in OCR_MODES:
            raise OptionsError(f"unknown OCR mode: {self.ocr!r} (use {', '.join(OCR_MODES)})")
        widths = tuple(self.page_widths)
        if not widths or list(widths) != sorted(set(widths)) or min(widths) <= 0:
            raise OptionsError(f"page widths must be distinct, ascending and positive: {widths!r}")
        object.__setattr__(self, "page_widths", widths)
        object.__setattr__(self, "device_requested", self.device)

    def written_widths(self, page_count: int) -> tuple[int, ...]:
        """The page image widths a document of `page_count` pages gets: `page_widths`, less the
        large ones past `srcset_max_pages`."""
        return widths_for(self.page_widths, page_count, self.srcset_max_pages)

    @property
    def detect_scan_fields(self) -> bool:
        """Whether the scanned-form field pass runs: asked for, or implied by its assist."""
        return self.scan_fields or self.assist_scan_fields

    @property
    def engine_info(self) -> Engine:
        return ENGINES[self.engine]

    @property
    def uses_model(self) -> bool:
        """Whether a model runs in this process, on a device."""
        return self.engine_info.loads_model or self.alt_text == "local"


def with_device(options: Options) -> Options:
    """`options` with `device` the concrete device ("cuda", "mps" or "cpu") a model will run on,
    when one runs in this process (Options.uses_model); `options` itself otherwise. The one
    place the machine is probed (reflow.device.resolve_device, which imports torch), so building
    an Options touches nothing. `device_requested` keeps what was asked for. Raises OptionsError
    for a named GPU that is not there."""
    if not options.uses_model:
        return options
    try:
        device = resolve_device(options.device)
    except ValueError as e:   # a named GPU that is not there
        raise OptionsError(str(e)) from None
    resolved = replace(options, device=device)
    object.__setattr__(resolved, "device_requested", options.device_requested)
    return resolved


@dataclass(frozen=True)
class Workers:
    """The process pool a conversion submits page work to, and the pdfium lock.
    `pool` None: every stage runs in this process. `jobs` sizes chunks; 1 when there is no pool.
    Every stage that fans out (extract_document, render_pages, later read_tags) takes
    `executor=workers.pool, jobs=workers.jobs`, so a Run never adds processes beyond its pool."""
    pool: Executor | None
    jobs: int
    pdfium: threading.RLock = field(default=PDFIUM_LOCK)

    @classmethod
    def serial(cls) -> Workers:
        return cls(pool=None, jobs=1)


@contextmanager
def workers_for(executor: Executor | None, jobs: int,
                lease: budget.Lease | None = None) -> Iterator[Workers]:
    """The shared pool as Workers when given (convert_many); else one private spawn pool of
    `jobs` workers for the whole run when jobs > 1 (one pool, not one per stage); else serial.
    A private pool is shut down on exit. With a `lease` (reflow.budget) its tasks hold slots only
    while they run (`Lease.meter`), so the pool shrinks to the lease's fair share when another
    run arrives."""
    if executor is not None:
        yield Workers(pool=executor, jobs=max(1, jobs))
        return
    if jobs <= 1:
        yield Workers.serial()
        return
    pool: Executor = make_pool(jobs)
    if lease is not None:
        pool = lease.meter(pool)
    try:
        yield Workers(pool=pool, jobs=jobs)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


@dataclass
class RunState:
    """What one pass leaves for another in the same conversion. Each field has one writer and
    its readers named here; None (False for the flag) until the writer has run."""
    page_weights: list[int] | None = None       # written by analyze.engine; read by PageReader and extraction
    reader: PageReader | None = None            # the conversion's one PageReader (extract.pagecontent.reader_for)
    walked: Walked | None = None                # analyze.prefetch's early read; extract.tags.shared_tags takes it
    links_scan: Pending | None = None           # analyze.prefetch's early read; evidence.witness takes it
    tags: dict[int, TagPage] | None = None      # extract.tags.shared_tags; evidence and order_evidence read it
    decisions: Snapshot | None = None           # analyze.decisions_apply.snapshot_of: the one read of the store
    early: threading.Thread | None = None       # analyze.prefetch: the tag tree and links read
    parse: threading.Thread | None = None       # analyze.prefetch: the pages they ask the reader for
    page_images: PageImages | None = None       # output.start_page_images; publish collects them
    pages_carried: bool = False                 # saved.Reuse.restore: the stage holds the previous output's page images
    missing: MissingReport | None = None        # ingest.text_trust.flag_missing; saved.Reuse.close reads its failed pages


@dataclass(frozen=True)
class Run:
    """One conversion in flight. Built by reflow.prepare; read by every pass; never shared.
    Invariants: `working` is written only inside prepare and is immutable after it returns;
    every file a pass writes goes under `stage` with a name from reflow.names;
    `seconds` and `gpu_wait` are append-only through `step`."""
    pdf: Path                  # the file the user gave: copied byte for byte, read for meta
    stage: Path                # the staging folder from output.staged
    options: Options
    workers: Workers
    progress: Progress
    doc: Document
    working: Path              # stage / names.WORKING_PDF
    ocr: OcrResult
    seconds: dict[str, float] = field(default_factory=dict)
    # The text-trust preflight's judgments and witness words (ingest.text_trust); None on a run
    # that restored a saved model, whose facts are already in its Document.
    trust: TrustReport | None = None
    # What the font repair decoded (ingest.font_map), with the characters it changed on each page;
    # None on a run that restored a saved model, whose nodes already keep the layer's reading.
    fonts: Repair | None = None
    gpu_wait: dict[str, float] = field(default_factory=dict)   # of seconds[name], waiting for the GPU lease
    state: RunState = field(default_factory=RunState)

    @contextmanager
    def step(self, name: str, label: str | None = None, total: int | None = None
             ) -> Iterator[Tick]:
        """Time a stage into seconds[name], and the part of it spent waiting for the GPU lease
        (reflow.budget.gpu) into gpu_wait[name] when that is 0.1 s or more. The body gets
        tick(k), which adds k to the stage's done count and reports progress(label, done,
        total). A labelled stage with no `total` (an indeterminate one) is announced at 0 before
        the body runs; a counted stage speaks at its first tick; a stage with no label reports
        nothing."""
        stage = label or name
        done = 0

        def tick(k: int) -> None:
            nonlocal done
            done += k
            self.progress(stage, done, total)

        waited = 0.0
        outer = budget.GPU_WAIT.get()

        def note(seconds: float) -> None:   # an enclosing step counts the wait too
            nonlocal waited
            waited += seconds
            if outer is not None:
                outer(seconds)

        if label is not None and total is None:
            self.progress(stage, 0, total)
        token = budget.GPU_WAIT.set(note)
        start = time.monotonic()
        try:
            yield tick
        finally:
            budget.GPU_WAIT.reset(token)
            self.seconds[name] = round(time.monotonic() - start, 2)
            if waited >= 0.1:
                self.gpu_wait[name] = round(waited, 2)
