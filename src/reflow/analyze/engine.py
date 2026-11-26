"""The engine pass: run the layout engine named in the options and put its tree under root.

Four engines produce `root.children`: heuristic (pypdfium2 text + pdfplumber tables +
reflow.analyze.layout), docling (ML models over the whole document), hybrid (heuristic,
with Docling on the pages the router calls hard) and direct (a model transcribes each page,
analyze.direct). What each one needs and yields besides the
tree is the `Backbone`. The one invariant every engine must meet is applied here: no U+FFFD in
the tree's text (reflow.leaders.normalize_document)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from reflow.analyze import prefetch
from reflow.analyze.layout import analyze
from reflow.extract.pages import PageContent
from reflow.extract.parallel import extract_document
from reflow.extract.weights import page_weights
from reflow.extract.tables.cells import TableData
from reflow.leaders import normalize_document
from reflow.model import Node
from reflow.run import ENGINES, Run

if TYPE_CHECKING:
    from reflow.analyze.hybrid import Routing
    from reflow.analyze.direct import Reader as DirectReader
    from reflow.analyze.ask import ChoiceProvider


@dataclass(frozen=True)
class Backbone:
    """What the engine knows besides the tree it put under root.children. `pages` is None for
    --engine docling: no pdfium lines, so passes that read lines (notes, label inference) skip."""
    pages: list[PageContent] | None
    tables: dict[int, list[TableData]]
    trace: dict                         # heuristic per-page record; {} otherwise
    routing: Routing | None             # hybrid only: hard pages, decisions; None otherwise


def _extract(run: Run, provider: ChoiceProvider | None = None
             ) -> tuple[list[PageContent], dict[int, list[TableData]]]:
    """The pages of the document, with the check boxes a form with no fields draws as squares
    (analyze.drawn_boxes), the fields that the printed blanks of its text layer make
    (analyze.blanks) and, when asked for, those the scanned pages show (analyze.scan_fields).
    The passes run after the stage cache was read or written, so the cache never holds a
    synthetic widget and stays valid for every option."""
    pages, tables = _read_pages(run)
    if run.options.detect_scan_fields:
        from reflow.analyze import scan_fields
        scan_fields.run_pass(run, pages, provider)
    # The check boxes a form with no AcroForm draws as squares (analyze.drawn_boxes), before the blanks:
    # a blank that a box labels is the box's own.
    from reflow.analyze import drawn_boxes
    drawn_boxes.inject(pages)
    from reflow.analyze import blanks
    blanks.run_pass(pages, tables)
    return pages, tables


def _read_pages(run: Run) -> tuple[list[PageContent], dict[int, list[TableData]]]:
    """Text, drawings and ruled tables, page by page, on the run's workers."""
    # What each page weighs, to cut the chunks by. Read before `early` starts its thread: with the
    # thread running, the parent loop that reads them waits for the GIL on every call (0.7 s on p17).
    weights = page_weights(run.working, len(run.doc.pages)) if run.workers.pool is not None else None
    run.state.page_weights = weights   # the reader cuts its parse runs by them too
    prefetch.early(run)   # the tag tree and the links, read from the file while the pool extracts
    with run.step("extract", "Extracting text and tables", total=len(run.doc.pages)) as tick:
        # A partial, not a closure here: the stage cache keys extraction on the code of
        # reflow.extract.parallel, and these arguments only schedule the work.
        extract = partial(extract_document, run.working, len(run.doc.pages),
                          jobs=run.workers.jobs, chunk_pages=run.options.chunk_pages,
                          executor=run.workers.pool, weights=weights, on_pages=tick)
        root = run.options.stage_cache
        if root is None:
            return _hand_over_parse(run, extract())
        # Opt-in (reflow.stagecache, --stage-cache).
        from reflow import stagecache
        result, hit = stagecache.cached_extract(root, run.pdf, run.working, extract)
        if hit:
            tick(len(run.doc.pages))
        return _hand_over_parse(run, result)


def _hand_over_parse(run: Run, result: tuple[list[PageContent], dict[int, list[TableData]]]
                     ) -> tuple[list[PageContent], dict[int, list[TableData]]]:
    """The pages pdfplumber parsed to find ruled tables go to the conversion's PageReader, for
    the tag tree and the links, and come off the pages."""
    from reflow.extract.pagecontent import reader_for
    pages, _ = result
    reader_for(run).seed({p.number: p.plumber for p in pages if p.plumber is not None})
    for p in pages:
        p.plumber = None
    return result


def _heuristic(run: Run, provider: ChoiceProvider | None, reader: DirectReader | None
               ) -> tuple[list[Node], Backbone]:
    pages, tables = _extract(run, provider)
    trace: dict = {}  # what the layout saw, line by line (output.publish writes one file per page)
    with run.step("layout", "Analyzing layout"):
        nodes = analyze(pages, tables, trace=trace, executor=run.workers.pool,
                        jobs=run.workers.jobs, on_planning=lambda: prefetch.start(run))
    prefetch.start(run)   # a document too short for the pool: reads begin now
    return nodes, Backbone(pages, tables, trace, None)


def _docling(run: Run, provider: ChoiceProvider | None, reader: DirectReader | None
             ) -> tuple[list[Node], Backbone]:
    from reflow.analyze.docling_layout import analyze_docling
    with run.step("docling", f"Docling ({run.options.device})"):
        nodes = analyze_docling(run.working, device=run.options.device)
    return nodes, Backbone(None, {}, {}, None)


def _direct(run: Run, provider: ChoiceProvider | None, reader: DirectReader | None
            ) -> tuple[list[Node], Backbone]:
    from reflow.analyze import direct
    nodes = direct.transcribe(run, reader or direct.make_reader(run.options.direct_model,
                                                                      run.options.direct_prompt))
    return nodes, Backbone(None, {}, {}, None)


def _hybrid(run: Run, provider: ChoiceProvider | None, reader: DirectReader | None
            ) -> tuple[list[Node], Backbone]:
    from reflow.analyze.hybrid import analyze_hybrid
    pages, tables = _extract(run, provider)
    with run.step("layout", f"Layout + Docling on hard pages ({run.options.device})"):
        nodes, routing = analyze_hybrid(run.working, pages, tables, run.doc.pages,
                                        device=run.options.device)
    return nodes, Backbone(pages, tables, {}, routing)


_BUILDERS: dict[str, Callable[[Run, ChoiceProvider | None, DirectReader | None],
                              tuple[list[Node], Backbone]]] = {
    "heuristic": _heuristic, "docling": _docling, "hybrid": _hybrid, "direct": _direct,
}
if set(_BUILDERS) != set(ENGINES):
    raise RuntimeError("run.ENGINES and engine._BUILDERS disagree")


def build(run: Run, provider: ChoiceProvider | None = None,
          reader: DirectReader | None = None) -> Backbone:
    """Run the engine named in run.options, set run.doc.root.children, and apply the one text
    invariant every engine must meet: no U+FFFD in the tree (its counts stay on the nodes as
    `unicode_errors`, which confidence.apply reads as unicode-errors facts). `provider` is the
    model the scan-field pass asks (--assist-scan-fields); None replays the cache only."""
    nodes, backbone = _BUILDERS[run.options.engine](run, provider, reader)
    run.doc.root.children = nodes
    normalize_document(run.doc)
    return backbone
