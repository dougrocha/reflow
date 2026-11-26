"""The real code of a pooled conversion against the serial one (`--jobs 1`), with Hypothesis.

Two checks, both on the code in src/reflow, not on a model of it:

  * Completion order (test_pooled_*): extract_document and layout.analyze (offsets_of,
    plan_pages) run on real sample PDFs with a stand-in for the process pool. The stand-in
    pickles every task's arguments and result, as the process boundary does, and finishes
    the tasks one at a time, in an order Hypothesis picks, only while the caller is blocked
    waiting on one. The pages, tables, trace and nodes must equal the serial run's.

  * The prefetch contract (test_prefetch_*): prefetch.early/start/finish, the PageReader,
    read_links and tags.shared_tags, called in the order convert and engine call them,
    once with a pool and once without. The pages, the tag tree and the link annotations are
    synthetic (read_contents, walk_tags and scan_links are replaced), and some pages fail to
    parse. What the evidence pass gets must be the same with and without the pool.

An open finding of README.md is a test whose name contains "finding": run.sh runs it apart
from the gate and expects it to fail until the code is fixed. None is open now.
"""

from __future__ import annotations

import json
import pickle
import random
import sys
import threading
import time
from concurrent.futures import Executor, Future
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from reflow.analyze import layout, pageplan, prefetch
from reflow.extract import links as links_mod
from reflow.extract import pagecontent, tags as tags_mod
from reflow.extract.links import read_links
from reflow.extract.pagecontent import PlumberPage, reader_for
from reflow.extract.parallel import extract_document
from reflow.extract.tags import Walked, _Raw, shared_tags
from reflow.extract.weights import page_weights
from reflow.run import RunState

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import checkout  # noqa: E402


def _sample(name: str) -> Path:
    """A sample PDF, from this checkout or the main one. resnet.pdf is not committed:
    scripts/fetch_samples.py downloads it."""
    pdf = checkout.sample(ROOT, f"samples/{name}.pdf")
    if not pdf.exists():
        raise FileNotFoundError(f"samples/{name}.pdf is not here: run scripts/fetch_samples.py")
    return pdf

# --------------------------------------------------------------------------------------------
# Completion order


class AdversarialPool(Executor):
    """A ProcessPoolExecutor stand-in. A task's arguments and result cross a pickle; tasks
    finish one at a time, in the order `rng` picks among those submitted, and only while the
    calling thread is blocked in a wait (future.result(), as_completed, wait)."""

    def __init__(self, seed: int) -> None:
        # A plain Random from a drawn seed: the finisher thread must not draw from Hypothesis
        # (a draw from another thread raises once the test's data is used up, as in shrinking).
        self._rng = random.Random(seed)
        self._pending: list[tuple[Future, object, bytes]] = []
        self._lock = threading.Lock()
        self._caller = threading.get_ident()
        self._stop = False
        self._thread = threading.Thread(target=self._finish_tasks, daemon=True)
        self._thread.start()

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future: Future = Future()
        with self._lock:
            self._pending.append((future, fn, pickle.dumps((args, kwargs))))
        return future

    def _caller_waits(self) -> bool:
        frame = sys._current_frames().get(self._caller)
        return (frame is not None and frame.f_code.co_name == "wait"
                and frame.f_code.co_filename == threading.__file__)

    def _finish_tasks(self) -> None:
        try:
            self._finish_loop()
        except BaseException as error:  # noqa: BLE001 - never leave the caller waiting forever
            with self._lock:
                left, self._pending = self._pending, []
            for future, _, _ in left:
                if future.set_running_or_notify_cancel():
                    future.set_exception(RuntimeError(f"AdversarialPool broke: {error!r}"))
            raise

    def _finish_loop(self) -> None:
        while not self._stop:
            with self._lock:
                ready = bool(self._pending) and self._caller_waits()
                if ready:
                    future, fn, payload = self._pending.pop(self._rng.randrange(len(self._pending)))
            if not ready:
                time.sleep(0.0005)
                continue
            if not future.set_running_or_notify_cancel():
                continue
            try:
                args, kwargs = pickle.loads(payload)
                future.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as error:  # noqa: BLE001 - handed to the caller, as a pool does
                future.set_exception(error)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._stop = True
        self._thread.join()


_SERIAL: dict[str, bytes] = {}


def _strip(pages) -> list[int]:
    """engine._hand_over_parse takes the table finder's parse off the pages."""
    parsed = [p.number for p in pages if p.plumber is not None]
    for p in pages:
        p.plumber = None
    return parsed


def _run(pdf: Path, pages, tables, pool, jobs: int) -> str:
    trace: dict = {}
    nodes = layout.analyze(pages, tables, trace=trace, executor=pool, jobs=jobs)
    return json.dumps({"nodes": [n.to_dict() for n in nodes], "trace": trace}, sort_keys=True,
                      default=repr)


def _serial(name: str):
    if name not in _SERIAL:
        pdf = _sample(name)
        count = _page_count(pdf)
        pages, tables = extract_document(pdf, count, jobs=1)
        parsed = _strip(pages)
        extracted = pickle.dumps((pages, tables, parsed))
        _SERIAL[name] = pickle.dumps((extracted, _run(pdf, pages, tables, None, 1)))
    extracted, result = pickle.loads(_SERIAL[name])
    return pickle.loads(extracted), result


def _page_count(pdf: Path) -> int:
    import pikepdf
    with pikepdf.open(pdf) as doc:
        return len(doc.pages)


@settings(max_examples=12, deadline=None, derandomize=True, database=None,
          phases=[Phase.explicit, Phase.generate],   # no shrinking: an example costs ~1 s
          suppress_health_check=[HealthCheck.too_slow])
@given(name=st.sampled_from(["fw9", "resnet"]), jobs=st.integers(2, 4),
       chunk=st.sampled_from([None, 1, 2, 5]), weighted=st.booleans(),
       seed=st.integers(0, 2**32 - 1))
def test_pooled_extraction_and_planning_match_serial(name, jobs, chunk, weighted, seed):
    (serial_pages, serial_tables, serial_parsed), serial_result = _serial(name)
    pdf = _sample(name)
    pool = AdversarialPool(seed)
    try:
        with mock.patch.object(pageplan, "MIN_PAGES", 1):   # short samples still plan on the pool
            # weighted: engine._read_pages cuts the chunks by page_weights and submits the
            # heaviest first; the pages must still come back in page order.
            weights = page_weights(pdf, len(serial_pages)) if weighted else None
            pages, tables = extract_document(pdf, len(serial_pages), jobs=jobs,
                                             chunk_pages=chunk, executor=pool, weights=weights)
            parsed = _strip(pages)
            assert [p.number for p in pages] == [p.number for p in serial_pages]
            assert pages == serial_pages
            assert tables == serial_tables and list(tables) == list(serial_tables)
            assert parsed == serial_parsed
            assert _run(pdf, pages, tables, pool, jobs) == serial_result
    finally:
        pool.shutdown()


@settings(max_examples=8, deadline=None, derandomize=True, database=None,
          phases=[Phase.explicit, Phase.generate], suppress_health_check=[HealthCheck.too_slow])
@given(name=st.sampled_from(["fw9", "resnet"]), jobs=st.integers(2, 4),
       wanted=st.sets(st.integers(1, 6), min_size=2), seed=st.integers(0, 2**32 - 1))
def test_pooled_page_parse_matches_serial(name, jobs, wanted, seed):
    """pagecontent.read_contents cuts the pages by weight and submits the heaviest run first
    (PageReader's parse on the pool). The pages come back in the order asked, with the content
    a serial parse gives."""
    pdf = _sample(name)
    numbers = sorted(wanted)
    serial = pagecontent.read_pages(pdf, numbers, True)
    pool = AdversarialPool(seed)
    try:
        weights = page_weights(pdf, _page_count(pdf))
        with mock.patch.object(pagecontent, "PAGES_PER_POOL_WORKER", 1):   # a short sample still splits
            found = list(pagecontent.read_contents(pdf, numbers, jobs, pool, True, weights))
    finally:
        pool.shutdown()
    assert [n for n, _ in found] == [n for n in numbers if n in serial]
    for n, content in found:
        assert (content.total, content.chars, content.graphics, content.words) == \
               (serial[n].total, serial[n].chars, serial[n].graphics, serial[n].words)


# --------------------------------------------------------------------------------------------
# The prefetch contract

BOX = (5.0, 5.0, 25.0, 25.0)   # a link rectangle around each page's one word


def _content(n: int, words: bool) -> PlumberPage:
    text = f"p{n}"
    chars = {0: [(c, 10.0 + i, 10.0, 11.0 + i, 20.0, 10.0) for i, c in enumerate(text)]}
    found = [(text, 10.0, 20.0, 10.0, 20.0)] if words else None
    return PlumberPage(len(text), chars, {}, found)


@st.composite
def documents(draw, parse_failures: bool):
    n = draw(st.integers(1, 5))
    pages = st.sets(st.integers(1, n))
    seeded = draw(pages)
    bad = draw(pages.map(lambda s: s - seeded)) if parse_failures else set()
    tag_pages = draw(pages)
    return SimpleNamespace(
        n=n, seeded=seeded, bad=bad, links=draw(pages), tag_pages=tag_pages,
        tagged=bool(tag_pages) or draw(st.booleans()), over_cap=draw(st.booleans()),
        walk_raises=draw(st.booleans()), scan_raises=draw(st.booleans()))


def _evidence(d, pooled: bool) -> dict:
    """What evidence.witness gets from the reader, after the steps convert runs before it."""
    def read_contents(pdf, numbers, jobs, executor=None, words=False, weights=None):
        if set(numbers) & d.bad:
            raise RuntimeError(f"pdfplumber could not parse page {min(set(numbers) & d.bad)}")
        return [(n, _content(n, words if isinstance(words, bool) else n in words))
                for n in numbers if 1 <= n <= d.n]

    def walk_tags(pdf):
        if d.walk_raises:
            raise RuntimeError("walk_tags")
        if not d.tagged:
            return Walked()
        top = _Raw("P", 0, kids=[(p, 0) for p in sorted(d.tag_pages)],
                   by_page={p: [0] for p in sorted(d.tag_pages)})
        return Walked([top], {(p, 0) for p in d.tag_pages})

    def scan_links(pdf, pages=None):
        if d.scan_raises:
            raise RuntimeError("scan_links")
        return {p: [(BOX, f"https://example.com/{p}", "uri", None)] for p in sorted(d.links)}

    cap = d.n - 1 if d.over_cap else d.n
    run = SimpleNamespace(
        pdf=Path("doc.pdf"), working=Path("working.pdf"), state=RunState(),
        doc=SimpleNamespace(pages=[None] * d.n, root=SimpleNamespace(facts=[])),
        workers=SimpleNamespace(pool=object() if pooled else None, jobs=2),
        options=SimpleNamespace(stage_cache=None), ocr=SimpleNamespace(runner="", done=[]))
    with ExitStack() as patches:
        for target, name, value in [
                (pagecontent, "read_contents", read_contents),
                (tags_mod, "walk_tags", walk_tags), (prefetch, "walk_tags", walk_tags),
                (links_mod, "scan_links", scan_links), (prefetch, "scan_links", scan_links),
                (tags_mod, "MAX_TAG_PAGES", cap), (prefetch, "MAX_TAG_PAGES", cap)]:
            patches.enter_context(mock.patch.object(target, name, value))
        try:
            prefetch.early(run)                                   # engine._extract
            reader_for(run).seed({n: _content(n, False) for n in d.seeded})   # _hand_over_parse
            prefetch.start(run)                                   # analyze's on_planning
            prefetch.start(run)                                   # engine._heuristic, after analyze
            prefetch.finish(run)                                  # convert, before evidence.witness
            links = read_links(run.working, reader=reader_for(run),   # evidence.witness
                               scanned=run.state.links_scan)
            found = shared_tags(run)
        except Exception as error:  # noqa: BLE001 - the conversion fails: no output
            prefetch.abandon(run)
            return {"failed": f"{type(error).__name__}: {error}"}
    return {
        "failed": None,
        "links": {p: [(l.target, l.words) for l in ls]
                  for p, ls in links.items()},
        "tags": {p: ([(e.tag, e.text) for e in t.elements], t.tagged_share)
                 for p, t in found.items()},
        "order_unchecked": sum(f.code == "order-unchecked" for f in run.doc.root.facts),
    }


PREFETCH = settings(max_examples=400, deadline=None, derandomize=True, database=None)


@PREFETCH
@given(d=documents(parse_failures=False))
def test_prefetch_gives_the_evidence_pass_what_jobs_1_gives(d):
    assert _evidence(d, pooled=True) == _evidence(d, pooled=False)


@PREFETCH
@given(d=documents(parse_failures=True))
def test_prefetch_failed_link_read_fails_both_runs_and_keeps_the_links(d):
    pooled, serial = _evidence(d, pooled=True), _evidence(d, pooled=False)
    assert (pooled["failed"] is None) == (serial["failed"] is None)
    if serial["failed"] is None:
        assert pooled["links"] == serial["links"]


@PREFETCH
@given(d=documents(parse_failures=True))
def test_prefetch_failed_page_read_gives_the_same_tag_evidence(d):
    pooled, serial = _evidence(d, pooled=True), _evidence(d, pooled=False)
    if pooled["failed"] is None and serial["failed"] is None:
        assert (pooled["tags"], pooled["order_unchecked"]) == \
               (serial["tags"], serial["order_unchecked"])
