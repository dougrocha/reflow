"""Read the working copy's document facts and per-page info into a Document (no content yet).

Kept apart from `reflow.prepare` so that the stage cache (reflow.stagecache) can key the
result on this reading code alone: `read_source` is the whole of the cached ingest stage."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import Executor
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pypdfium2 as pdfium

from reflow.ingest.classify import page_info
from reflow.identity import file_sha256
from reflow.ingest.load import normalize, open_pdf, read_meta
from reflow.model import Document, Node, PageInfo, Role
from reflow.pool import submit_runs

PARALLEL_MIN_PAGES = 24   # shorter documents are read here: a hand-off costs more than it saves


def read_source(pdf: Path, working: Path, password: str = "", *,
                on_page: Callable[[int, int], None] | None = None,
                executor: Executor | None = None, jobs: int = 1,
                lock: AbstractContextManager | None = None) -> Document:
    """Write the working copy of `pdf` to `working` (normalize) and read it (ingest). A long
    document is read on the pool; otherwise ingest holds pdfium's `lock` for the read."""
    normalize(pdf, working, password, lock=lock)
    return ingest(working, password, on_page, original=pdf, executor=executor, jobs=jobs,
                  lock=lock)


def ingest(
    path: Path,
    password: str = "",
    on_page: Callable[[int, int], None] | None = None,
    *,
    original: Path | None = None,
    executor: Executor | None = None,
    jobs: int = 1,
    lock: AbstractContextManager | None = None,
) -> Document:
    """Build a Document with metadata and per-page info; the content tree is still empty.

    `path` is the copy whose pages are read (the normalized working copy). The document facts
    (metadata, security, sha256, filename) come from `original`, the file the user gave, when
    it is set, since the working copy is decrypted and rewritten. Each page records the label
    the file declares for it (PageInfo.label; "" when it declares none).
    `on_page(done, total)` is called after each page, for progress reporting.
    With an `executor` (a process pool of `jobs` workers) a long document's pages are read there,
    in runs, and `on_page` is called after each run; the pages are the same. The caller then
    does not touch pdfium here; a document read here holds `lock` (pdfium's) while it does.
    """
    source = original or path
    with open_pdf(source, password) as pdf:
        meta = read_meta(pdf)
    meta["sha256"] = file_sha256(source)
    meta["filename"] = source.name

    pages = None
    if executor is not None:
        with open_pdf(path, password) as counted:
            total = len(counted.pages)
        if total >= PARALLEL_MIN_PAGES:
            pages = _read_pages_on(executor, jobs, path, password, total, on_page)
    if pages is None:
        with lock or nullcontext():
            doc = pdfium.PdfDocument(path, password=password or None)
            try:
                pages = page_infos(doc, range(1, len(doc) + 1), on_page)
            finally:
                doc.close()
    root = Node(Role.DOC, attrs={k: meta[k] for k in ("title", "lang") if meta.get(k)})
    return Document(root=root, pages=pages, meta=meta)


def page_infos(doc: pdfium.PdfDocument, numbers: Iterable[int],
               on_page: Callable[[int, int], None] | None = None) -> list[PageInfo]:
    """PageInfo (with its label) of the pages `numbers` (1-based) of an open document.
    `on_page(done, total)` is called after each."""
    numbers = list(numbers)
    infos = []
    for done, n in enumerate(numbers, 1):
        page = doc[n - 1]
        try:
            info = page_info(page, n)
        finally:
            page.close()
        info.label = doc.get_page_label(n - 1) or ""
        infos.append(info)
        if on_page:
            on_page(done, len(numbers))
    return infos


def read_page_infos(path: Path, password: str, first: int, last: int) -> list[PageInfo]:
    """`page_infos` of pages first..last of the file, 1-based inclusive."""
    doc = pdfium.PdfDocument(path, password=password or None)
    try:
        return page_infos(doc, range(first, last + 1))
    finally:
        doc.close()


def _read_pages_on(executor: Executor, jobs: int, path: Path, password: str, total: int,
                   on_page: Callable[[int, int], None] | None) -> list[PageInfo]:
    # About four runs per worker (not reflow.extract.parallel.chunk_ranges: the stage cache keys
    # this module on the code it imports, and ingest must not pull in the analysis).
    size = max(1, -(-total // (max(jobs, 1) * 4)))
    calls = [(path, password, a, min(a + size - 1, total)) for a in range(1, total + 1, size)]
    pages: list[PageInfo] = []
    for run in submit_runs(executor, read_page_infos, calls):
        pages.extend(run)
        if on_page:
            on_page(len(pages), total)
    return pages
