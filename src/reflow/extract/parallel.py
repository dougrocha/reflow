"""Page-level parallel extraction: text, drawings and ruled tables on chunks of pages.

Extraction and table finding read each page on its own (no state crosses pages: the font
cache is built per page, table finding only needs the page's own drawings), so a document
can be cut into runs of pages and each run handled by a worker process. pdfium objects can't
be pickled and pdfium isn't thread-safe, so a worker opens the PDF itself and returns plain
dataclasses. The parent concatenates the chunks in page order; every cross-page step stays
serial in the parent.

Workers are started with "spawn": pdfium keeps process-wide state (library init, font
caches) that is not safe to inherit across fork.
"""

from __future__ import annotations

import math
import multiprocessing
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor
from pathlib import Path

from reflow.extract.forms import read_widgets
from reflow.extract.pagecontent import PlumberPage
from reflow.extract.pages import PageContent, extract_pages
from reflow.pool import submit_runs
from reflow.extract.rulings import ruled_pages
from reflow.extract.tables.cells import TableData
from reflow.extract.tables.ruled import candidate_pages, find_tables
from reflow.extract.weights import weighted_chunks

CHUNK_PAGES: int | None = None  # pages per task; None sizes the chunks from the job count
FALLBACK_CHUNK_PAGES = 20  # with no size and no job count: big enough to amortize opening the PDF
CHUNKS_PER_JOB = 4  # enough tasks per worker to even out pages that cost more than others
WEIGHTED_CHUNKS_PER_JOB = 8  # the same, when the chunks are cut by what the pages weigh


def chunk_ranges(page_count: int, size: int | None = None, jobs: int | None = None
                 ) -> list[tuple[int, int]]:
    """1-based inclusive (first, last) runs covering 1..page_count in order.

    Without a `size`, the document is cut into about CHUNKS_PER_JOB chunks per job:
    max(1, ceil(page_count / (jobs * 4))) pages each. A fixed 20 pages leaves a 142-page
    document with 8 chunks for 8 workers, so the last pages of the slowest chunk set the time;
    with `jobs` unknown the size is FALLBACK_CHUNK_PAGES."""
    if size is None:
        size = (max(1, math.ceil(page_count / (max(jobs, 1) * CHUNKS_PER_JOB)))
                if jobs else FALLBACK_CHUNK_PAGES)
    return [(first, min(first + size - 1, page_count)) for first in range(1, page_count + 1, size)]


def extract_chunk(path: Path, first: int, last: int
                  ) -> tuple[list[PageContent], dict[int, list[TableData]]]:
    """Extract pages first..last and find the ruled tables on those that have rules."""
    pages = list(extract_pages(path, first, last))
    # Form widgets are read in a pass of their own, so a document with no form is untouched.
    widgets = read_widgets(path, first, last)
    for p in pages:
        p.widgets = widgets.get(p.number, [])
    candidates = ruled_pages(path, candidate_pages({p.number: p.drawings for p in pages}))
    # find_tables opens the whole PDF in pdfplumber: skip that for a run with no ruled pages.
    contents: dict[int, PlumberPage] = {}
    tables = find_tables(path, candidates, contents) if candidates else {}
    for p in pages:
        p.plumber = contents.get(p.number)
    return pages, tables


def make_pool(jobs: int) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context("spawn"))


def extract_document(path: Path, page_count: int, *, jobs: int = 1,
                     chunk_pages: int | None = CHUNK_PAGES,
                     executor: Executor | None = None,
                     weights: list[int] | None = None,
                     on_pages: Callable[[int], None] | None = None
                     ) -> tuple[list[PageContent], dict[int, list[TableData]]]:
    """All pages' content and ruled tables, in page order.

    jobs == 1, or a document of a single chunk (with no executor), is the serial path: one run
    over the whole document, in this process. Otherwise chunks go to `executor`, or to a pool
    made here for the call (no larger than the number of chunks). With `chunk_pages` None the
    chunk size follows `jobs` (see chunk_ranges); with an executor, `jobs` is its worker count.
    With `weights` (page_weights) and no `chunk_pages`, the chunks are cut by weight and the
    heaviest is submitted first; the caller computes them, since reading them takes the GIL
    from any thread that is running. `on_pages(n)` is called with the page count of each chunk as it is collected."""
    ranges = chunk_ranges(page_count, chunk_pages, jobs)
    if executor is None and (jobs <= 1 or len(ranges) <= 1):
        pages, tables = extract_chunk(path, 1, page_count)
        if on_pages:
            on_pages(len(pages))
        return pages, tables

    # Cut by weight and start the heaviest chunk first: with equal page counts, the three chunks
    # of tax tables in p17 take 8 of its 18 CPU-seconds, and when they start last they set the time.
    order = list(range(len(ranges)))
    if chunk_pages is None and weights is not None and len(weights) == page_count:
        ranges = weighted_chunks(weights, max(jobs, 1) * WEIGHTED_CHUNKS_PER_JOB)
        order = sorted(range(len(ranges)),
                       key=lambda k: -sum(weights[ranges[k][0] - 1:ranges[k][1]]))

    own = executor is None
    pool = executor or make_pool(min(jobs, len(ranges)))
    try:
        pages: list[PageContent] = []
        tables: dict[int, list[TableData]] = {}
        for chunk_pages, chunk_tables in submit_runs(
                pool, extract_chunk, [(path, *r) for r in ranges], order=order):
            pages.extend(chunk_pages)
            tables.update(chunk_tables)
            if on_pages:
                on_pages(len(chunk_pages))
        return pages, tables
    finally:
        if own:
            pool.shutdown(wait=True, cancel_futures=True)
