"""What pdfplumber reads from a page for the tag tree and the link annotations.

Parsing a page with pdfminer costs about 0.07 s of CPU on a page of p17 (142 pages; the densest
takes 0.43 s), and both the tag tree (chars and graphics grouped by marked-content id) and the link annotations
(the words under each rectangle) need it. `PageReader` parses a page once for both, on the
conversion's pool when it has one, and, with the stage cache on (`Options.stage_cache`), keeps
the result on disk keyed by the bytes of the working copy it parsed and this module's code
(reflow.stagecache). This module imports nothing from the analysis, so editing the analysis
leaves the saved pages valid.
"""

from __future__ import annotations

import multiprocessing
import os
from collections import defaultdict
from collections.abc import Collection, Iterable
from concurrent.futures import Executor, ProcessPoolExecutor
from pathlib import Path
from typing import NamedTuple

import pdfplumber
from pdfminer.layout import LTContainer
from pdfplumber.utils import extract_words, resolve_all

from reflow import geometry, identity
from reflow.extract.redraws import Redraws
from reflow.extract.weights import weighted_chunks
from reflow.model import BBox
from reflow.pool import submit_runs

# The saved pages depend on the working copy's bytes, this module and what it imports.
PAGES_PER_WORKER = 12        # a pool made here costs a process start per worker
PAGES_PER_POOL_WORKER = 4    # the conversion's pool is running already
MAX_WORKERS = 8
RUNS_PER_POOL_WORKER = 1     # weighted runs per worker; 2 to 4 waited longer (0.8 to 1.2 s against 0.2 s)

type Word = tuple[str, float, float, float, float]   # text, x0, x1, top, bottom
# Which pages to read the words of: every page asked for, none, or these pages.
type WordsOf = bool | Collection[int]

# The gap between two glyphs of a line, in font sizes, past which pdfplumber's words split (its
# x_tolerance_ratio). A word space is a quarter of the size or more, and the letters of a word
# nearly touch. pdfplumber's own default is 3 pt at any size, which an 8 pt space (2.2 pt)
# written as TJ kerning instead of a space glyph, as pdfTeX writes it, does not reach.
WORD_GAP = 0.15

GRAPHIC_KINDS = ("image", "rect", "curve", "line")
# pdfplumber's object kind for each pdfminer class it lists in Page.objects (Page.process_object
# takes the lowercase class name without "LT"); the others, such as LTAnno, are not objects.
_KINDS = {"LTChar": "char", "LTImage": "image", "LTRect": "rect", "LTCurve": "curve",
          "LTLine": "line"}


def _leaves(items):
    """The layout objects that are not containers, in pdfplumber's order (Page.iter_layout_objects
    without laparams: figures are walked into and are not objects themselves)."""
    for item in items:
        if isinstance(item, LTContainer):
            yield from _leaves(item._objs)
        else:
            yield item


class PlumberPage:
    """The chars and graphics of one page, grouped by marked-content id, as plain tuples
    (so a worker process can return them): chars are (text, x0, top, x1, bottom, size).
    `words` is the page's words when they were asked for, else None."""

    def __init__(self, total: int, chars: dict[int, list[tuple]],
                 graphics: dict[int, list[BBox]], words: list[Word] | None = None):
        self.total = total
        self.chars = chars
        self.graphics = graphics
        self.words = words
        self._memo: dict[tuple[int, ...], tuple[BBox | None, str]] = {}
        self._pieces: dict[int, _Piece] | None = None

    @classmethod
    def from_page(cls, page, *, words: bool = False) -> PlumberPage:
        """What this module keeps of a pdfplumber page, read from pdfminer's layout objects.

        `page.chars` and `page.objects` build a dict of some 25 attributes for every object, and
        that is more than half of the cost of a page; this takes the five numbers it uses from the
        pdfminer objects, with pdfplumber's own arithmetic (Page.process_object), so the values
        are the same bit for bit. The words are pdfplumber's, grouped from the same chars."""
        height, (mb_x0, mb_top) = page.height, page.mediabox[:2]
        shifted = mb_x0 != 0
        chars: dict[int, list[tuple]] = defaultdict(list)
        drawn: dict[str, list[tuple[int, BBox]]] = {kind: [] for kind in GRAPHIC_KINDS}
        plain: list[dict] | None = [] if words else None
        total = 0
        # A glyph drawn again in place is read once (reflow.extract.redraws): once on the page
        # and once among the tagged glyphs, so an artifact copy drawn first keeps the tagged one.
        redraws, tagged = Redraws(), Redraws()
        for obj in _leaves(page.layout._objs):
            kind = _KINDS.get(type(obj).__name__)
            if kind is None:
                continue
            top, bottom = (height - obj.y1) + mb_top, (height - obj.y0) + mb_top
            x0, x1 = (obj.x0 + mb_x0, obj.x1 + mb_x0) if shifted else (obj.x0, obj.x1)
            if kind == "char":
                size = resolve_all(obj.size)
                text = obj.get_text()
                box = (x0, top, x1, bottom)
                if not redraws.seen(text, box, size or 0.0):
                    total += 1
                    if plain is not None:
                        plain.append({"text": text, "x0": x0, "x1": x1, "top": top,
                                      "bottom": bottom, "doctop": top, "upright": obj.upright,
                                      "size": size})
                mcid = resolve_all(getattr(obj, "mcid", None))
                if mcid is not None and not tagged.seen(text, box, size or 0.0):
                    chars[mcid].append((text, x0, top, x1, bottom, size or 0.0))
            else:
                mcid = resolve_all(getattr(obj, "mcid", None))
                if mcid is not None:
                    drawn[kind].append((mcid, (x0, top, x1, bottom)))
        graphics: dict[int, list[BBox]] = defaultdict(list)
        for kind in GRAPHIC_KINDS:   # by kind, as page.objects lists them
            for mcid, box in drawn[kind]:
                graphics[mcid].append(box)
        found = ([(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
                  for w in extract_words(plain, keep_blank_chars=False, x_tolerance_ratio=WORD_GAP)]
                 if plain is not None else None)
        return cls(total, dict(chars), dict(graphics), found)

    def summary(self, mcids: list[int]) -> tuple[BBox | None, str]:
        """The box and text of the content under these MCIDs (chars first, graphics when
        there are no chars). Chains of single-child wrappers share their MCIDs: memoized.

        An element's chars are those of its descendants, so a char would be read once for every
        level of the tree above it. The page's chars are read once into `_pieces` (one per MCID)
        and an element joins the pieces of its MCIDs. The result is the text of the chars in
        order and the union of their boxes."""
        key = tuple(mcids)
        found = self._memo.get(key)
        if found is None:
            if self._pieces is None:
                self._pieces = {m: _piece(chars) for m, chars in self.chars.items()}
            pieces = [p for m in mcids if (p := self._pieces.get(m)) is not None]
            if pieces:
                box = pieces[0].box if len(pieces) == 1 else geometry.union(p.box for p in pieces)
                parts: list[str] = []
                for before, p in zip([None, *pieces], pieces):
                    if before is not None and _spaced(before.last, p.first):
                        parts.append(" ")
                    parts.append(p.raw)
                found = (box, " ".join("".join(parts).split()))
            else:
                found = (geometry.union(b for m in mcids for b in self.graphics.get(m, ())), "")
            self._memo[key] = found
        return found


class _Piece(NamedTuple):
    """The chars of one marked-content id, ready to be joined with those of the next."""
    raw: str       # their text and the spaces _spaced puts between them; whitespace not collapsed
    first: tuple
    last: tuple
    box: BBox


def _spaced(prev: tuple, char: tuple) -> bool:
    """Whether a space goes between two chars in content order: the second is not blank, the
    first does not end in a space, and the second starts more than a third of its size after
    the first ends, or on another line."""
    text, x0, _, _, bottom, size = char
    return bool(text.strip()) and not prev[0].isspace() and (
        abs(bottom - prev[4]) > size / 2 or x0 - prev[3] > size / 3)


def _piece(chars: list[tuple]) -> _Piece:
    """The pieces of a page are made for every element of the tag tree, in the parent: `_spaced`
    and the union of the boxes are written out here, in the one pass over the chars."""
    prev = first = chars[0]
    out = [first[0]]
    x0, top, x1, bottom = first[1], first[2], first[3], first[4]
    for char in chars[1:]:
        text, cx0, ctop, cx1, cbottom, size = char
        if text.strip() and not prev[0].isspace() and (
                abs(cbottom - prev[4]) > size / 2 or cx0 - prev[3] > size / 3):   # _spaced
            out.append(" ")
        out.append(text)
        if cx0 < x0:
            x0 = cx0
        if ctop < top:
            top = ctop
        if cx1 > x1:
            x1 = cx1
        if cbottom > bottom:
            bottom = cbottom
        prev = char
    return _Piece("".join(out), first, prev, (x0, top, x1, bottom))


def read_pages(pdf: Path, numbers: list[int], words: WordsOf = False) -> dict[int, PlumberPage]:
    """Parse these 1-based pages of `pdf` (pages beyond its end are left out)."""
    with pdfplumber.open(pdf) as plumber:
        return {n: PlumberPage.from_page(
                    plumber.pages[n - 1],
                    words=words if isinstance(words, bool) else n in words)
                for n in numbers if 1 <= n <= len(plumber.pages)}


def pool_workers(pages: int, jobs: int | None, executor: Executor | None = None) -> int:
    """The workers `read_contents` parses `pages` pages on: at most `jobs` (None: MAX_WORKERS),
    MAX_WORKERS and the CPU count, and one per PAGES_PER_WORKER pages (PAGES_PER_POOL_WORKER on
    a caller's executor). 1 means serially, in this process."""
    per_worker = min(PAGES_PER_WORKER, PAGES_PER_POOL_WORKER) if executor is not None \
        else PAGES_PER_WORKER
    return min(jobs if jobs else MAX_WORKERS, MAX_WORKERS, os.cpu_count() or 1,
               -(-pages // per_worker))


def read_contents(pdf: Path, numbers: list[int], jobs: int | None,
                  executor: Executor | None = None, words: WordsOf = False,
                  weights: list[int] | None = None):
    """(page, content) for each page in `numbers`, in order: parsed serially, on the caller's
    `executor`, or on a spawn pool made here when there is none. With `weights` (one per page
    of the document, see reflow.extract.weights) and an executor, the runs are cut by weight and
    the heaviest is submitted first."""
    workers = pool_workers(len(numbers), jobs, executor)
    if workers <= 1:
        yield from read_pages(pdf, numbers, words).items()
        return
    if executor is not None and weights is not None and max(numbers) <= len(weights):
        cuts = weighted_chunks([weights[n - 1] for n in numbers], workers * RUNS_PER_POOL_WORKER)
        runs = [numbers[a - 1:b] for a, b in cuts]
        order = sorted(range(len(runs)), key=lambda k: -sum(weights[n - 1] for n in runs[k]))
        for part in submit_runs(executor, read_pages, [(pdf, run, words) for run in runs],
                                order=order):
            yield from part.items()
        return
    size = -(-len(numbers) // workers)
    runs = [numbers[i:i + size] for i in range(0, len(numbers), size)]
    if executor is not None:
        for part in executor.map(read_pages, [pdf] * len(runs), runs, [words] * len(runs)):
            yield from part.items()
        return
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=len(runs), mp_context=context) as pool:
        for part in pool.map(read_pages, [pdf] * len(runs), runs, [words] * len(runs)):
            yield from part.items()


class PageReader:
    """Pages parsed once and kept: `get` parses only the pages not read yet (and, for the pages
    whose words are asked for, those read without them). A page is marked tried only once the
    read has returned, so a parse that raises leaves the pages unread and the next `get` parses
    them and raises again. `stage` is (stage cache root, the source PDF) when the stage cache
    is on; the saved pages are keyed by the bytes of `pdf`, the working copy they parse."""

    def __init__(self, pdf: Path, *, jobs: int | None = None, executor: Executor | None = None,
                 stage: tuple[Path, Path] | None = None,
                 weights: list[int] | None = None):
        self.pdf, self.jobs, self.executor, self.stage = pdf, jobs, executor, stage
        self.weights = weights
        self._pages: dict[int, PlumberPage] = {}
        self._tried: set[tuple[int, bool]] = set()

    def get(self, numbers: Iterable[int], words: WordsOf = False) -> dict[int, PlumberPage]:
        """The contents of these pages. `words` is True for the words of every page, or the
        pages among `numbers` whose words are wanted: one request can ask for the links' pages
        with their words and the tag tree's without, in one parse."""
        wanted = list(dict.fromkeys(numbers))
        wordy = frozenset(wanted if words is True else () if words is False else words)
        missing = [n for n in wanted if (n, n in wordy) not in self._tried
                   and (n not in self._pages or (n in wordy and self._pages[n].words is None))]
        if missing:
            asked = wordy.intersection(missing)
            # True or False when it is the same for every page: what a caller or a cache entry
            # of one kind sees; otherwise the pages, sorted (they key the saved read).
            kinds = True if len(asked) == len(missing) else tuple(sorted(asked))
            self._pages.update(self._load_or_read(missing, kinds or False))
            self._tried.update((n, n in wordy) for n in missing)
        return {n: self._pages[n] for n in wanted if n in self._pages}

    def seed(self, contents: dict[int, PlumberPage]) -> None:
        """Keep pages parsed elsewhere (the table finder's parse of a page), so `get` does not
        parse them again. A page seeded without words is read again if words are asked for."""
        for n, content in contents.items():
            self._pages.setdefault(n, content)

    def _load_or_read(self, numbers: list[int], words: WordsOf) -> dict[int, PlumberPage]:
        if self.stage is None:
            return dict(read_contents(self.pdf, numbers, self.jobs, self.executor, words,
                                      self.weights))
        from reflow import stagecache
        root, source = self.stage
        kind = "pages-words" if words else "pages"
        parts = (identity.file_sha256(self.pdf), tuple(numbers), words)
        found, _ = stagecache.cached(
            root, kind, stagecache.STAGES["pages"], source,
            lambda: dict(read_contents(self.pdf, numbers, self.jobs, self.executor, words,
                                       self.weights)), *parts)
        return found


def reader_for(run) -> PageReader:
    """The conversion's one PageReader (kept in `run.state`), so the tag tree and the links
    share the pages they both need."""
    state = run.state
    if state.reader is None:
        options = run.options
        stage = (options.stage_cache, run.pdf) if options.stage_cache is not None else None
        state.reader = PageReader(run.working, jobs=run.workers.jobs, executor=run.workers.pool,
                                  stage=stage, weights=state.page_weights)
    return state.reader
