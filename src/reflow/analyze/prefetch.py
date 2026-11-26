"""Work the evidence pass needs, begun while the pool is busy with something else.

Two things sit in the middle of the evidence pass (evidence.witness), each of which waits on
nothing the layout decides:

  1. Reading the tag tree and the link annotations from the file (pikepdf: `walk_tags`,
     `scan_links`). They need only the working PDF, so `early` runs them on a thread while the
     parent waits for extraction, when it has nothing else to do.
  2. Parsing the pages they then ask the conversion's PageReader for (pdfplumber, on the pool):
     the pages with a link and the pages the tag tree has content on, in one request. `start` asks
     as soon as the page planning has been handed to the pool, so the pool parses them behind
     the planning and the evidence pass finds them read. One request, because the pool runs its
     tasks in the order they were submitted: a second request made after the first returned
     would queue behind the page images, which `convert` starts in between.

Both leave their results in `run.state` for the evidence pass (`walked`, `links_scan`) or in the
PageReader. The reader is not thread-safe: while the parse thread runs it is the only caller, and
`finish` (before the evidence pass) joins it. A failure in either thread is not raised from it:
the evidence pass does the same work itself and fails (or records "order-unchecked") as it always
did.
"""

from __future__ import annotations

import threading

from reflow.extract.links import scan_links
from reflow.extract.pagecontent import reader_for
from reflow.extract.tags import MAX_TAG_PAGES, Walked, walk_tags
from reflow.run import Run


class _Early(threading.Thread):
    def __init__(self, run: Run) -> None:
        super().__init__(name="reflow-prefetch-early", daemon=True)
        self.run_ = run
        self.walked: Walked | None = None
        self.scan = None

    def run(self) -> None:
        try:
            if len(self.run_.doc.pages) <= MAX_TAG_PAGES:
                self.walked = walk_tags(self.run_.working)
            self.scan = scan_links(self.run_.working)
        except Exception:  # noqa: BLE001 - the evidence pass will do this itself
            pass


class _Parse(threading.Thread):
    def __init__(self, run: Run, links: list[int], tagged: list[int]) -> None:
        super().__init__(name="reflow-prefetch-parse", daemon=True)
        self.run_, self.links, self.tagged = run, links, tagged

    def run(self) -> None:
        try:
            reader_for(self.run_).get(sorted({*self.links, *self.tagged}), words=self.links)
        except Exception:  # noqa: BLE001 - the evidence pass reads what is missing, and fails as it did
            pass


def early(run: Run) -> None:
    """Begin reading the tag tree and the links from the file, if the run has a pool."""
    if run.workers.pool is not None and run.state.early is None:
        run.state.early = _Early(run)
        run.state.early.start()


def _collect(run: Run) -> None:
    """Join `early` and hand its results to the evidence pass."""
    thread, run.state.early = run.state.early, None
    if thread is None:
        return
    thread.join()
    if thread.walked is not None:
        run.state.walked = thread.walked
    if thread.scan is not None:
        run.state.links_scan = thread.scan


def start(run: Run) -> None:
    """Begin parsing the pages the evidence pass will want, if there is a pool and a reason."""
    if run.workers.pool is None or run.state.parse is not None:
        return
    _collect(run)
    walked, scan = run.state.walked, run.state.links_scan
    if walked is None or scan is None:   # not read early (or it failed): the evidence pass reads
        return
    links, tagged = sorted(scan), walked.pages
    if links or tagged:
        run.state.parse = _Parse(run, links, tagged)
        run.state.parse.start()


def finish(run: Run) -> None:
    """Wait for the parse to end, before the evidence pass uses the reader."""
    _collect(run)
    thread, run.state.parse = run.state.parse, None
    if thread is not None:
        thread.join()


def abandon(run: Run) -> None:
    """Wait for both threads after a failure elsewhere, so nothing is left running."""
    for thread in (run.state.early, run.state.parse):
        if thread is not None:
            thread.join()
    run.state.early = run.state.parse = None
