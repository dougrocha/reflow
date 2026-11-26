"""Page planning on the worker pool.

`layout.analyze` plans every page (figures, tables, blocks, reading order) before it ranks the
heading styles. Planning one page reads that page and a few facts of the whole document (the
body size, the running artifacts, the icon legend, the hanging indents) and changes nothing
outside it, so a run of pages can be planned by a worker process while the parent keeps the
cross-page work: finding those facts, then linking, ranking and stitching.

What crosses the process boundary is small: a task carries its pages (plain dataclasses), their
tables and the shared facts; the answer is one pickle of the planned PageContexts in which every Line,
Widget, Drawing and page of the input is a reference (kind, page, index), resolved to the
parent's own object when the answer is read. The parent's `id()`-keyed sets (artifacts, reasons, marks)
are sent and returned as (page, index) too, because ids mean nothing in another process.
"""

from __future__ import annotations

import io
import pickle
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import Executor

from reflow.analyze.blocks import wrap_offsets
from reflow.analyze.columns import xy_cut
from reflow.analyze.context import PageContext, PlanState
from reflow.analyze.lists import merge_list_markers
from reflow.extract.pages import PageContent
from reflow.extract.parallel import chunk_ranges
from reflow.extract.tables.cells import TableData

MIN_PAGES = 6   # shorter documents are planned in this process: nothing to gain from a hand-off

type Ref = tuple[str, int, int]   # ("L" | "W" | "D" | "P", page number, index in the page's list)
# Plans one page: (page, state, the page's ruled tables, routing info, trace) → its PageContext.
type Planner = Callable[[PageContent, PlanState, list[TableData], dict | None, dict | None], PageContext]


def _lists(page: PageContent):
    return (("L", page.lines), ("W", page.widgets), ("D", page.drawings), ("P", [page]))


def _refs(pages: Sequence[PageContent]) -> dict[int, Ref]:
    """id(object) → its reference, for every line, widget and drawing of the pages."""
    return {id(item): (kind, page.number, i) for page in pages
            for kind, items in _lists(page) for i, item in enumerate(items)}


def _objects(pages: Sequence[PageContent]) -> dict[Ref, object]:
    return {(kind, page.number, i): item for page in pages
            for kind, items in _lists(page) for i, item in enumerate(items)}


class _Out(pickle.Pickler):
    def __init__(self, file, refs: dict[int, Ref]) -> None:
        super().__init__(file, protocol=pickle.HIGHEST_PROTOCOL)
        self._refs = refs

    def persistent_id(self, obj):
        return self._refs.get(id(obj))


class _In(pickle.Unpickler):
    def __init__(self, file, lookup: dict[Ref, object]) -> None:
        super().__init__(file)
        self._lookup = lookup

    def persistent_load(self, pid):
        return self._lookup[pid]


def _dumps(value, refs: dict[int, Ref]) -> bytes:
    buffer = io.BytesIO()
    _Out(buffer, refs).dump(value)
    return buffer.getvalue()


def _loads(data: bytes, pages: Sequence[PageContent]):
    return _In(io.BytesIO(data), _objects(pages)).load()


def _reasons_for(pages: Sequence[PageContent], reasons: dict[int, str]
                 ) -> dict[tuple[int, int], str]:
    """Why each dropped line of the pages was dropped, by (page number, line index)."""
    return {(page.number, i): reasons[id(l)]
            for page in pages for i, l in enumerate(page.lines) if id(l) in reasons}


# -- the hanging indents ----------------------------------------------------------------------

def offsets_chunk(pages: list[PageContent], dropped: dict[tuple[int, int], str],
                  body: float) -> Counter:
    """The hanging indents (see blocks.wrap_offsets) of a run of pages."""
    offsets: Counter = Counter()
    for page in pages:
        lines = [l for i, l in enumerate(page.lines) if (page.number, i) not in dropped
                 and not l.rotated]
        offsets += wrap_offsets(xy_cut(merge_list_markers(lines), body * 1.2)[0])
    return offsets


def offsets_of(pages: list[PageContent], reasons: dict[int, str], body: float,
               executor: Executor, jobs: int) -> Counter:
    ranges = chunk_ranges(len(pages), None, jobs)
    futures = []
    try:
        futures = [executor.submit(offsets_chunk, pages[a - 1:b],
                                   _reasons_for(pages[a - 1:b], reasons), body)
                   for a, b in ranges]
        offsets: Counter = Counter()
        for future in futures:
            offsets += future.result()
        return offsets
    except BaseException:
        for f in futures:
            f.cancel()
        raise


# -- the pages --------------------------------------------------------------------------------

def plan_chunk(planner: Planner, pages: list[PageContent],
               tables: dict[int, list[TableData]], body: float, legend: dict[str, str],
               offsets: Counter, dropped: dict[tuple[int, int], str], want_trace: bool) -> bytes:
    """Plan a run of pages with `planner`; one pickle of the result."""
    objects = _objects(pages)
    reasons = {id(objects[("L", n, i)]): why for (n, i), why in dropped.items()}
    state = PlanState(body, reasons, set(reasons), legend, offsets=offsets)
    base_artifacts, base_reasons = set(state.artifacts), set(state.reasons)
    refs = _refs(pages)
    trace: dict | None = {} if want_trace else None
    planned = []
    for page in pages:
        state.styles = Counter()
        ctx = planner(page, state, tables.get(page.number, []), None, trace)
        turned = [refs[i][2] for i in ctx.label_turned]
        ctx.guarded, ctx.label_turned = set(), set()   # ids mean nothing in the parent: rebuilt there
        planned.append((ctx, turned, state.styles))
    answer = {
        "planned": planned,
        "trace": trace.get("pages", {}) if trace is not None else {},
        "marks": {refs[i][1:]: v for i, v in state.marks.items()},
        "artifacts": [refs[i][1:] for i in state.artifacts - base_artifacts],
        "reasons": {refs[i][1:]: v for i, v in state.reasons.items() if i not in base_reasons},
    }
    return _dumps(answer, refs)


def plan_pages(planner: Planner, pages: list[PageContent], tables: dict[int, list[TableData]],
               state: PlanState, trace: dict | None, executor: Executor, jobs: int,
               submitted: Callable[[], None] | None = None) -> list[PageContext]:
    """`planner` (a module-level function, so a worker can import it) for every page, on the
    pool; `state` and `trace` come out as they would from the pages planned one after another.
    `submitted()` is called once every chunk is on the pool: work queued then runs behind the
    planning."""
    ranges = chunk_ranges(len(pages), None, jobs)
    futures = []
    try:
        for a, b in ranges:
            chunk = pages[a - 1:b]
            futures.append(executor.submit(
                plan_chunk, planner, chunk,
                {p.number: tables[p.number] for p in chunk if p.number in tables},
                state.body, state.legend, state.offsets, _reasons_for(chunk, state.reasons),
                trace is not None))
        if submitted is not None:
            submitted()
        contexts: list[PageContext] = []
        for (a, b), future in zip(ranges, futures):
            chunk = pages[a - 1:b]
            answer = _loads(future.result(), chunk)
            by_number = {p.number: p for p in chunk}
            for ctx, turned, styles in answer["planned"]:
                ctx.guarded = {id(l) for l in ctx.guarded_lines}
                ctx.label_turned = {id(ctx.page.lines[i]) for i in turned}
                contexts.append(ctx)
                state.styles.update(styles)
            line = lambda k: by_number[k[0]].lines[k[1]]  # noqa: E731
            state.marks.update({id(line(k)): v for k, v in answer["marks"].items()})
            state.artifacts.update(id(line(k)) for k in answer["artifacts"])
            state.reasons.update({id(line(k)): v for k, v in answer["reasons"].items()})
            if trace is not None:
                trace.setdefault("pages", {}).update(answer["trace"])
        return contexts
    except BaseException:
        for f in futures:
            f.cancel()
        raise
