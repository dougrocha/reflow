"""The conversion pipeline: PDF → accessible HTML output directory. `convert` checks the
options, builds a frozen Run (reflow.run) and hands it to the passes, each named for what it
owns; the Document and its fact ledger are all they change, and `Summary` is derived at the end."""

from __future__ import annotations

import multiprocessing
import pickle
import threading
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Executor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool, ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field, fields
from pathlib import Path

from reflow import budget, output, pipeline, saved
from reflow.analyze import ask, direct, page_vision, prefetch, review
from reflow.extract.parallel import make_pool
from reflow.ingest.ocr import OcrResult
from reflow.model import Document, Role
from reflow.pageimages import PAGE_WIDTH
from reflow.prepare import prepare
from reflow.run import Options, Progress, Run, with_device, workers_for
from reflow.steps import STEPS

__all__ = ["DocResult", "Summary", "convert", "convert_many"]


def convert(pdf: Path, out_dir: Path, *, jobs: int = 1, executor: Executor | None = None,
            lease: budget.Lease | None = None, on_progress: Progress | None = None,
            label_provider: ask.ChoiceProvider | None = None,
            page_vision_provider: page_vision.PageProvider | None = None,
            direct_reader: direct.Reader | None = None,
            polish_reader: direct.Reader | None = None,
            must_reuse: bool = False, **options) -> Summary:
    """Convert `pdf` into `out_dir`; returns what was produced.

    `options` are the fields of reflow.run.Options. `jobs` > 1 runs the page work (text,
    tables, page images) in that many worker processes, with the same output as jobs=1; with
    `executor` (a process pool, see convert_many) that pool is used and `jobs` is its size.
    With a `lease` (reflow.budget) and no `executor`, the pool made here is metered: its tasks
    hold slots only while they run, within the lease's fair share.
    The output is built beside `out_dir` and replaces it only once the conversion succeeds
    (reflow.output.staged). With the stage cache on, a saved model whose identity is proved
    (reflow.saved: the previous output, or the stage root) is rendered instead of built again.
    Raises OptionsError (a ValueError: bad option, foreign files in out_dir), ProviderUnavailable,
    OcrUnavailable, SourceUnreadable (the PDF needs a password, or is not one), RestrictedPdf.
    Nothing is written before the checks.
    `label_provider` replaces the provider (`assist_provider`) of `assist_labels`, `assist_structure` and `assist_scan_fields` (tests).
    `page_vision_provider` replaces the local Ollama model of `assist_page_vision` (tests).
    `direct_reader` replaces the model of `engine="direct"` (tests), `polish_reader` that of
    `polish` (tests).
    `must_reuse` (reflow rerender) never builds the model: with no saved model to prove it raises
    saved.NotReusable before anything runs, and the output is left as it was."""
    opts = with_device(Options(**options))          # ValueError before any work
    providers = pipeline.Providers.make(opts, label=label_provider, page_vision=page_vision_provider,
                                        direct=direct_reader, polish=polish_reader)
    output.recover(out_dir)                          # what a killed run left beside it
    output.check_replaceable(out_dir)
    reuse = saved.Reuse.open(pdf, out_dir, opts)     # reads only; inert without the stage cache

    with output.staged(out_dir) as stage, workers_for(executor, jobs, lease) as workers:
        restored = reuse.restore(stage, workers, on_progress)   # the saved model, proved, or None
        if restored is None and must_reuse:
            raise saved.NotReusable("the saved model was not found or does not match its record")
        run = restored[0] if restored else prepare(pdf, stage, opts, workers, progress=on_progress)
        try:
            if restored:
                model = restored[1]
                output.start_page_images(run)       # on the pool; nothing else is running
            else:
                backbone = pipeline.read_pages(run, providers)
                output.start_page_images(run)       # on the pool, while the serial passes run
                model = pipeline.finish(run, backbone, providers)
            site = output.publish(run, model)       # every file the renderer derives
            reuse.close(run, model)                 # build.json; the model into the stage root
        finally:                                        # a failed run leaves nothing writing into the stage
            providers.close()                           # local models and their GPU lease
            if run.state.page_images is not None:
                run.state.page_images.cancel()
            prefetch.abandon(run)
    return Summary.of(run, model, site, reuse.note)


def device_report(opts: Options) -> dict:
    if not opts.uses_model:
        return {}
    notes = []
    if opts.device_requested == "auto" and opts.device == "cpu":
        notes.append("no GPU found (auto); ran on the CPU")
    if opts.device == "cpu" and opts.alt_text == "local":
        notes.append("the local vision model on the CPU is slow (minutes per figure) and needs "
                     "about 17 GB of memory; use a GPU for long documents")
    return {"device": opts.device, "device_notes": notes}


@dataclass(frozen=True)
class Summary:
    """What a conversion produced, for the CLI and DocResult. Every field is computed from the
    Document, its ledger or the publisher's measurements; nothing is carried by hand."""
    title: str                     # root.attrs["title"]
    title_origin: str; title_suspect: bool   # "meta"/"heading"/"filename" (the "title" fact); a title-suspect fact
    engine: str; pages: int; figures: int; tables: int   # FIGURE nodes with a bbox; TABLE nodes
    chunks: int; headings: int; index_bytes: int; largest_chunk_bytes: int   # SiteStats
    review: dict[str, int]         # review.summary(items): errors, warnings, pages
    facts: dict[str, int]          # count per fact code over the document, pages and nodes
    ocr: OcrResult
    ocr_missing: int
    docling_pages: dict[int, list[str]]
    alt_text: dict[str, int | str]
    alt_text_errors: list[str]
    # The AI steps' stats (reflow.steps.STEPS: the Summary attribute is the step's key; {} when
    # it did not run). `page_vision` comes from the model facts, the others from doc.meta.
    assist: dict[str, int | str] = field(default_factory=dict)
    label_assist: dict[str, int | str] = field(default_factory=dict)
    structure_assist: dict[str, int | str] = field(default_factory=dict)
    scan_fields: dict[str, int | str] = field(default_factory=dict)
    page_vision: dict[str, int] = field(default_factory=dict)
    form_repair: dict[str, int | str] = field(default_factory=dict)
    direct: dict = field(default_factory=dict)   # the direct engine's model, pages, tokens, dollars
    polish: dict = field(default_factory=dict)   # the polish model, edits, tokens, dollars
    decisions: dict[str, int] = field(default_factory=dict)   # applied, dismissed, orphaned; {} without a decisions file
    seconds: dict[str, float] = field(default_factory=dict)
    gpu_wait: dict[str, float] = field(default_factory=dict)   # of seconds, waiting for the GPU lease
    device: str = ""               # the device models ran on; "" when no model ran
    device_notes: list[str] = field(default_factory=list)   # fallbacks and slow-on-CPU warnings
    srcset_cutoff: int | None = None   # the page limit that dropped the large page image width, if it did
    base_width: int = PAGE_WIDTH       # the page image width written when the large one is dropped
    model: str = ""   # with the stage cache: "built, kept", "previous output", "stage cache", or why not kept

    @classmethod
    def of(cls, run: Run, model: pipeline.Model, site: output.SiteStats, reused: str = ""
           ) -> Summary:
        """The one place that knows each field's origin."""
        doc, known, items = model.doc, model.facts, model.items
        facts = Counter(f.code for f in doc.root.facts)
        facts.update(f.code for p in doc.pages for f in p.facts)
        facts.update(f.code for n in doc.root.walk() if n is not doc.root for f in n.facts)
        title = doc.root.attrs["title"]
        title_fact = next((f for f in doc.root.facts if f.code == "title"), None)
        if title_fact is not None:
            origin = str(title_fact.data.get("origin", "meta"))
        else:
            origin = ("meta" if title == doc.meta.get("title") else
                      "filename" if title == run.pdf.stem else "heading")
        nodes = [n for n in doc.root.walk() if n is not doc.root]
        return cls(
            title=title, title_origin=origin, title_suspect=facts["title-suspect"] > 0,
            engine=run.options.engine, pages=len(doc.pages),
            figures=sum(1 for n in nodes if n.role == Role.FIGURE and n.bbox),
            tables=sum(1 for n in nodes if n.role == Role.TABLE),
            chunks=site.chunks, headings=site.headings, index_bytes=site.index_bytes,
            largest_chunk_bytes=site.largest_chunk_bytes,
            review=review.summary(items), facts=dict(facts), ocr=run.ocr,
            ocr_missing=facts["ocr-missing"], docling_pages=dict(known.docling_pages),
            alt_text=dict(known.alt_text), alt_text_errors=list(known.alt_text_errors),
            **{step.key: dict(doc.meta.get(step.meta, {})) for step in STEPS if step.meta},
            page_vision=dict(known.page_vision), decisions=_decisions_counts(doc),
            seconds=dict(run.seconds), gpu_wait=dict(run.gpu_wait),
            srcset_cutoff=(run.options.srcset_max_pages
                           if run.options.page_images
                           and len(run.options.written_widths(len(doc.pages))) < len(run.options.page_widths)
                           else None),
            base_width=run.options.page_widths[0], model=reused, **device_report(run.options))


def _decisions_counts(doc: Document) -> dict[str, int]:
    """How many of a reviewer's decisions applied, were dismissed or no longer match, from the
    ledger (analyze.decisions); empty when the conversion was given no decisions file."""
    ledger = doc.decisions_ledger
    if ledger is None:
        return {}
    return {"applied": len(ledger["applied"]), "dismissed": len(ledger["dismissed"]),
            "orphaned": len(ledger["orphaned"])}


class _PoolHolder:
    """The one shared process pool, replaced if a worker crash breaks it. With a `lease`
    (reflow.budget) its tasks hold slots only while they run (`Lease.meter`)."""

    def __init__(self, jobs: int, lease: budget.Lease | None = None) -> None:
        self._jobs, self._lease = jobs, lease
        self._lock = threading.Lock()
        self._pool: Executor | None = self._make()
        self._retired: list[Executor] = []

    def _make(self) -> Executor:
        pool = make_pool(self._jobs)
        return self._lease.meter(pool) if self._lease else pool

    def get(self) -> Executor:
        with self._lock:
            if self._pool is None:
                self._pool = self._make()
            return self._pool

    def replace(self, broken: Executor | None) -> None:
        with self._lock:
            if self._pool is broken:  # another document may have replaced it already
                self._retired.append(self._pool)
                self._pool = None

    def close(self) -> None:
        with self._lock:
            for pool in [self._pool, *self._retired]:
                if pool is not None:
                    pool.shutdown(wait=True, cancel_futures=True)


@dataclass
class DocResult:
    """The outcome of one document in convert_many: `summary` on success, else `error` and
    `traceback_text`, its formatted text (an exception's traceback object cannot cross the process
    boundary of isolate=True, so the text is what every mode keeps)."""
    pdf: Path
    out_dir: Path
    summary: Summary | None = None
    error: BaseException | None = None
    traceback_text: str = ""

    @classmethod
    def failed(cls, pdf: Path, out_dir: Path, error: BaseException, text: str | None = None
               ) -> DocResult:
        """The result of a document that raised `error`: `text` is its traceback, formatted
        from `error` when not given (give it when `error` is a stand-in for the one raised)."""
        text = "".join(traceback.format_exception(error)) if text is None else text
        return cls(pdf, out_dir, error=error.with_traceback(None), traceback_text=text)

    @property
    def ok(self) -> bool:
        return self.error is None


def _convert_in_process(pdf: Path, out_dir: Path, jobs: int, options: dict,
                        budget_at: tuple[Path, int] | None = None) -> DocResult:
    """One document of convert_many(isolate=True), run in its own worker process. With
    `budget_at` (Lease.where) it joins that budget, so its page workers hold slots only while
    they run."""
    try:
        with budget.lease(*budget_at) if budget_at else nullcontext() as lease:
            return DocResult(pdf, out_dir,
                             summary=convert(pdf, out_dir, jobs=jobs, lease=lease, **options))
    except Exception as e:  # noqa: BLE001 - reported per document
        text = "".join(traceback.format_exception(e))
        try:
            pickle.dumps(e)
        except Exception:  # noqa: BLE001 - an exception that cannot cross the process boundary
            e = RuntimeError(f"{type(e).__name__}: {e}")
        return DocResult.failed(pdf, out_dir, e, text)


def _one_at_a_time(options: dict) -> bool:
    """Whether the documents run one at a time in this process: a model loads here
    (Options.uses_model), or every page asks a model server (a non-heuristic engine, alt text,
    assist), whose requests two documents at once would double. An option that does not validate
    is left for `convert` to report, document by document."""
    if (options.get("engine", "heuristic") != "heuristic" or options.get("alt_text", "none") != "none"
            or options.get("assist") == "local"):
        return True
    names = {f.name for f in fields(Options)}
    try:
        return Options(**{k: v for k, v in options.items() if k in names}).uses_model
    except (ValueError, TypeError):
        return False


def _options_of(options: dict, decisions_dir: Path | None, out_dir: Path) -> dict:
    """The options of one document: with a decisions directory, its store is
    <directory>/<output folder name>.jsonl (a missing file is an empty store)."""
    if decisions_dir is None:
        return options
    return {**options, "decisions": Path(decisions_dir) / f"{out_dir.name}.jsonl"}


def _convert_many_isolated(docs: Sequence[tuple[Path, Path]], *, jobs: int, max_docs: int,
                           big_pages: int, lease: budget.Lease | None, on_start, on_done,
                           options: dict, decisions_dir: Path | None = None
                           ) -> list[DocResult]:
    """convert_many with every document in its own process. The serial steps (layout, review,
    publishing) are plain Python, so documents on threads of one process take turns on the GIL;
    separate processes run them side by side. A document of more than `big_pages` pages also
    gets a pool of `jobs` page workers of its own; shorter ones convert serially in their process.

    With a `lease` (reflow.budget), a short document starts only with a slot for its process (it
    is the worker), and gives it back when it ends. A big one holds no slot for its process, which
    only asks for work: it joins the budget as a member of its own (`Lease.where`) and its page
    workers hold slots only while they run. With none free, a lone document still runs, so the
    run never waits on another's. The output is the same."""
    import pikepdf

    def pages(pdf: Path) -> int:
        try:
            with pikepdf.open(pdf) as doc:
                return len(doc.pages)
        except Exception:  # noqa: BLE001 - convert reports the real error
            return 0

    big: dict[int, bool] = {}

    def is_big(i: int) -> bool:
        if i not in big:
            big[i] = pages(docs[i][0]) > big_pages
        return big[i]

    results: list[DocResult | None] = [None] * len(docs)
    waiting = list(range(len(docs)))
    running: dict = {}   # future -> (document, slots it holds)
    with ProcessPoolExecutor(max_workers=max(1, max_docs),
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        while waiting or running:
            while waiting and len(running) < max(1, max_docs):
                pdf, out_dir = docs[waiting[0]]
                page_jobs = (lease.cap(jobs) if lease else jobs) if jobs > 1 else 1
                if page_jobs > 1 and not is_big(waiting[0]):
                    page_jobs = 1
                # A short document is a worker and takes a slot. A big one asks for workers.
                slots = lease.take(1) if lease and page_jobs == 1 else 0
                if lease and page_jobs == 1 and not slots and running:
                    break
                i = waiting.pop(0)
                budget_at = lease.where() if lease and page_jobs > 1 else None
                if on_start:
                    on_start(i)
                try:
                    running[pool.submit(_convert_in_process, pdf, out_dir, page_jobs,
                                        _options_of(options, decisions_dir, out_dir),
                                        budget_at)] = (i, slots)
                except BrokenProcessPool as e:   # a worker crashed: this one fails as it would have
                    if lease:
                        lease.give(slots)
                    results[i] = DocResult.failed(pdf, out_dir, e)
                    if on_done:
                        on_done(i, results[i])
            if not running:
                continue
            done, _ = wait(running, timeout=0.25 if waiting else None,
                           return_when=FIRST_COMPLETED)
            for future in done:
                i, slots = running.pop(future)
                if lease:
                    lease.give(slots)
                try:
                    results[i] = future.result()
                except Exception as e:  # noqa: BLE001 - a crashed worker fails the documents on it
                    results[i] = DocResult.failed(docs[i][0], docs[i][1], e)
                if on_done:
                    on_done(i, results[i])
    return results  # type: ignore[return-value]


def convert_many(docs: Sequence[tuple[Path, Path]], *, jobs: int, max_docs: int = 2,
                 isolate: bool = False, big_pages: int = 100, lease: budget.Lease | None = None,
                 on_start: Callable[[int], None] | None = None,
                 on_done: Callable[[int, DocResult], None] | None = None,
                 on_progress: Callable[[int, str, int, int | None], None] | None = None,
                 decisions_dir: Path | None = None, **options) -> list[DocResult]:
    """Convert several (pdf, out_dir) pairs, up to `max_docs` at a time, on ONE process pool.

    The pool has up to `jobs` worker processes (spawn); every running document submits its page
    chunks and page images to it, so the process count stays at about `jobs` however many
    documents run. With a `lease` (reflow.budget), a task holds one of its slots while it runs, so
    the pool's concurrency follows the lease's fair share as other runs come and go. Each document runs `convert` in a thread of this process (the serial steps
    are plain Python and share the GIL; the pool is what scales); pdfium in this process is
    guarded by PDFIUM_LOCK. `options` are the keyword options of `convert`. One document
    failing never stops the others: it comes back as a DocResult with `error`. Results are in
    input order and each output is byte-identical to `convert(..., jobs=1)` (except
    working.pdf, which qpdf rewrites with a fresh /ID). `on_start(i)` / `on_done(i, result)` /
    `on_progress(i, stage, done, total)` are called from the document threads.

    `isolate=True` instead gives every document a process of its own, `max_docs` at a time
    (documents over `big_pages` pages also get a pool of `jobs` page workers of their own; no
    shared pool, and `on_progress` is not called). Output is the same. With a `lease`
    (reflow.budget) the documents and their page workers hold slots only while they run.

    `decisions_dir` gives each document the decisions file <decisions_dir>/<output folder
    name>.jsonl (reflow.analyze.decisions); a document with no file there reads an empty store."""
    in_parent = _one_at_a_time(options)
    if isolate and not in_parent:
        return _convert_many_isolated(docs, jobs=jobs, max_docs=max_docs, big_pages=big_pages,
                                      lease=lease, on_start=on_start, on_done=on_done, options=options,
                                      decisions_dir=decisions_dir)
    results: list[DocResult | None] = [None] * len(docs)
    if in_parent:
        max_docs = 1  # models (GPU memory, pdfium, a server's request limit) take one document
    holder = _PoolHolder(jobs, lease) if jobs > 1 else None

    def run(i: int) -> None:
        pdf, out_dir = docs[i]
        if on_start:
            on_start(i)
        pool = holder.get() if holder else None
        try:
            progress = (lambda *a: on_progress(i, *a)) if on_progress else None
            summary = convert(pdf, out_dir, jobs=jobs, executor=pool, on_progress=progress,
                              **_options_of(options, decisions_dir, out_dir))
            result = DocResult(pdf, out_dir, summary=summary)
        except Exception as e:  # noqa: BLE001 - reported per document
            text = "".join(traceback.format_exception(e))
            if isinstance(e, BrokenProcessPool) and holder:
                # A worker died (crash, out of memory). The pool is unusable for everyone who
                # was on it; the documents that start later get a fresh one.
                holder.replace(pool)
                e = BrokenProcessPool(f"a worker process crashed while converting {pdf.name}: {e}")
            result = DocResult.failed(pdf, out_dir, e, text)
        results[i] = result
        if on_done:
            on_done(i, result)

    if holder is None:
        for i in range(len(docs)):
            run(i)
    else:
        try:
            with ThreadPoolExecutor(max_workers=max(1, max_docs)) as threads:
                for f in [threads.submit(run, i) for i in range(len(docs))]:
                    f.result()
        finally:
            holder.close()
    return results  # type: ignore[return-value]
