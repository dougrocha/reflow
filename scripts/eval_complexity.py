"""Precision and recall of page-complexity routing: the page-vision pass and the
rules' own signals, per class, against the held-out labels in tests/complexity/labels.json.

The labels come from the written definitions in docs/research/complexity-labels.md, never from a
model. Each labelled document is converted with the default options (no assist), and each
labelled page is asked the pass's own question (page_vision.question_for, the same picture and
cache key). The answers are replayed from the committed cache tests/complexity/answers-cache.json
and nothing is called. `--record` asks a local Ollama server for the pages the cache lacks (REAL
calls, under the GPU lease of reflow.budget) and adds them to the cache.

The pass is scored two ways: `vision raw` is every flag the model gave, and `vision gated` drops
handwriting and poor scan on a page that is not scanned or mixed, as page_vision.flag does. The
document-level dense-table item (DENSE_SHARE) is a queue rule over whole documents, not a page
call, so it is not scored. A page with no offered block, or more than MAX_BLOCKS, gets no answer
from the pass and counts as not flagged.

The set is held out: nothing here tunes the pass. It reports counts only.

    .venv/bin/python scripts/eval_complexity.py [--record] [--model ID] [--work DIR] [--pages]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


import checkout  # noqa: E402
from reflow import providers  # noqa: E402
from reflow.analyze import ask  # noqa: E402
from reflow.analyze import page_vision as pv  # noqa: E402
from reflow.analyze.vision import ProviderError  # noqa: E402
from reflow.convert import convert  # noqa: E402
from reflow.identity import file_sha256  # noqa: E402
from reflow.model import Document, Node, Role  # noqa: E402
from reflow.names import DOCUMENT_JSON, PROPOSALS_JSON  # noqa: E402

LABELS = REPO / "tests/complexity/labels.json"
CACHE = REPO / "tests/complexity/answers-cache.json"
CLASSES = ("handwriting", "poor_scan", "dense_table", "diagram")
# The definition of dense_table, applied to the rules' tables (rows x columns, headers included).
DENSE_CELLS = 40
DENSE_COLUMNS = 4


# -- the rules' signals (docs/research/complexity-labels.md, "The rules' own signals") -------------


def _on_page(doc: Document, page: int) -> list[Node]:
    return [n for n in doc.root.walk() if n.page == page]


def _unread(nodes: list[Node], pen: bool) -> bool:
    return any(n.role == Role.FIGURE and n.attrs.get("unread") and
               bool(n.attrs.get("handwritten")) is pen for n in nodes)


def _tables(nodes: list[Node]) -> list[Node]:
    return [n for n in nodes if n.role == Role.TABLE]


def _dense(nodes: list[Node]) -> bool:
    tables = _tables(nodes)
    cells = sum(int(t.attrs.get("rows", 0)) * int(t.attrs.get("columns", 0)) for t in tables)
    return cells >= DENSE_CELLS and any(int(t.attrs.get("columns", 0)) >= DENSE_COLUMNS
                                        for t in tables)


def _figures(nodes: list[Node], vector: bool) -> bool:
    return any(n.role == Role.FIGURE and not n.attrs.get("unread") and
               (not vector or any("vector drawing" in w for w in n.attrs.get("why", [])))
               for n in nodes)


Signal = Callable[[Document, int], bool]
SIGNALS: dict[str, dict[str, Signal]] = {
    "handwriting": {"unread-pen": lambda d, p: _unread(_on_page(d, p), True)},
    "poor_scan": {
        "scan-noisy": lambda d, p: any(f.code == "scan-noisy" for f in d.pages[p - 1].facts),
        "unread-text": lambda d, p: _unread(_on_page(d, p), False)},
    "dense_table": {"table": lambda d, p: bool(_tables(_on_page(d, p))),
                    "table-dense": lambda d, p: _dense(_on_page(d, p))},
    "diagram": {"vector-figure": lambda d, p: _figures(_on_page(d, p), True),
                "figure": lambda d, p: _figures(_on_page(d, p), False)},
}


# -- the pass ----------------------------------------------------------------------------------


@dataclass
class Page:
    """One labelled page: its labels and what each predictor said."""
    doc: str
    page: int
    seen: str
    kind: str
    labels: dict[str, bool | None]
    signature_only: bool
    answered: bool = False     # the pass has an answer for it (replayed or recorded)
    asked: bool = True         # the pass would ask it (it has blocks, and not too many)
    raw: set[str] = field(default_factory=set)
    gated: set[str] = field(default_factory=set)
    rules: dict[str, bool] = field(default_factory=dict)   # "class/signal" -> fired


class SetProvider:
    """The pass's provider during the scorer's conversion: it answers the labelled pages from the
    cache (or, with `real`, from the model, adding to the cache) and refuses every other page
    without a call. So each labelled page is asked exactly the pass's own question and picture."""

    def __init__(self, sha256: str, pages: set[int], cache: dict, model: str,
                 real: pv.PageProvider | None) -> None:
        self.cache_id = model
        self.sha, self.pages, self.cache, self.model, self.real = sha256, pages, cache, model, real
        # page -> (the pass's question, the reply), for the labelled pages answered
        self.asked: dict[int, tuple[pv.PageQuestion, object]] = {}
        self.recorded = 0

    def check(self, question: pv.PageQuestion, image: bytes) -> object:
        if question.page not in self.pages:
            raise ProviderError("not in the labelled set")
        key = pv.cache_key(self.sha, question, self.model)
        entry = self.cache.get(key)
        if not (isinstance(entry, dict) and "reply" in entry):
            if self.real is None:
                raise ProviderError("no cached answer")
            reply = self.real.check(question, image)
            entry = self.cache[key] = {"kind": "page-vision", "reply": reply, "model": self.model,
                                       "prompt_version": pv.PROMPT_VERSION, "page": question.page}
            self.recorded += 1
        self.asked[question.page] = (question, entry["reply"])
        return entry["reply"]


def flags(question: pv.PageQuestion, reply: object) -> set[str]:
    try:
        return {c.value for c in pv.parse_answer(question, reply).complexity}
    except pv.AnswerRejected:
        return set()   # a rejected answer flags nothing, as in the pass


def evaluate(labels_path: Path = LABELS, cache_path: Path = CACHE, model: str = providers.PAGE_VISION_MODEL,
             work: Path | None = None, record: bool = False, only: set[str] | None = None
             ) -> list[Page]:
    """Convert each labelled document (only those whose file stem is in `only`, when given) with
    the pass on, its provider a SetProvider, and score every labelled page."""
    spec = json.loads(labels_path.read_text())["pages"]
    cache = ask.load_cache(cache_path)
    real = pv.make_provider(model) if record else None
    by_doc: dict[str, list[dict]] = {}
    for entry in spec:
        by_doc.setdefault(entry["pdf"], []).append(entry)
    rows: list[Page] = []
    added = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = work or Path(tmp)
        for rel, entries in by_doc.items():
            if only and Path(rel).stem not in only:
                continue
            pdf = checkout.sample(REPO, "samples/" + rel)
            if not pdf.exists():
                print(f"skip {rel}: {pdf} is missing", file=sys.stderr)
                continue
            out = base / Path(rel).stem
            if out.exists():
                shutil.rmtree(out)
            provider = SetProvider(file_sha256(pdf), {e["page"] for e in entries}, cache, model,
                                   real)
            try:
                convert(pdf, out, page_images=False, assist_page_vision=True,
                        assist_page_vision_model=model,
                        assist_page_vision_max_pages=10**6, page_vision_provider=provider)
            finally:
                added += provider.recorded
                if provider.recorded:   # keep what was paid for, even when a later page fails
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
            doc = Document.from_dict(json.loads((out / DOCUMENT_JSON).read_text()))
            assert doc.meta.get("sha256") == provider.sha, "the cache key's PDF hash"
            published = json.loads((out / PROPOSALS_JSON).read_text())["complexity"]
            for e in entries:
                n = e["page"]
                info = doc.pages[n - 1]
                row = Page(Path(rel).stem, n, e.get("seen", ""), info.kind.value,
                           {c: e[c] for c in CLASSES}, bool(e.get("signature_only")))
                row.answered = n in provider.asked
                # the pass asks every page with an offered block and at most MAX_BLOCKS of them
                question = pv.question_for(doc, info)
                row.asked = row.answered or (question is not None
                                             and len(question.blocks) <= pv.MAX_BLOCKS)
                row.raw = flags(*provider.asked[n]) if row.answered else set()
                scan = info.kind in pv.SCAN_KINDS
                row.gated = {c for c in row.raw if scan or c not in pv.SCAN_FLAGS}
                # what page_vision.flag wrote for the page, with its dense tables folded back in
                wrote = set(published["pages"].get(str(n), []))
                if n in published["dense_table_pages"]:
                    wrote.add(pv.Complexity.DENSE_TABLE.value)
                assert wrote == row.gated, f"{rel} p{n}: the pass wrote {wrote}, scored {row.gated}"
                row.rules = {f"{c}/{name}": fire(doc, n)
                             for c, signals in SIGNALS.items() for name, fire in signals.items()}
                rows.append(row)
    if added:
        print(f"recorded {added} new answers into {cache_path}", file=sys.stderr)
    return rows


# -- scoring -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


def score(pairs: Iterable[tuple[bool, bool]]) -> Score:
    """(label, predicted) pairs into counts."""
    tp = fp = fn = tn = 0
    for label, said in pairs:
        if label and said:
            tp += 1
        elif said:
            fp += 1
        elif label:
            fn += 1
        else:
            tn += 1
    return Score(tp, fp, fn, tn)


def label_of(row: Page, cls: str, no_signatures: bool = False) -> bool | None:
    value = row.labels[cls]
    if no_signatures and cls == "handwriting" and value:
        return not row.signature_only
    return value


Predictor = Callable[[Page], bool]


def predictors(cls: str) -> dict[str, Predictor]:
    out: dict[str, Predictor] = {"vision raw": lambda r: cls in r.raw,
                                 "vision gated": lambda r: cls in r.gated}
    names = list(SIGNALS[cls])
    for name in names:
        out[f"rules: {name}"] = lambda r, k=f"{cls}/{name}": r.rules[k]
    if len(names) > 1:
        out["rules: any"] = lambda r: any(r.rules[f"{cls}/{n}"] for n in names)
    return out


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def table(rows: list[Page], title: str) -> list[str]:
    lines = [f"## {title}", "",
             "| Class | Predictor | Precision | Recall | TP | FP | FN | TN |",
             "|---|---|--:|--:|--:|--:|--:|--:|"]
    variants = [(c, c, False) for c in CLASSES]
    variants.insert(1, ("handwriting", "handwriting, signatures not counted", True))
    for cls, name, nosig in variants:
        decided = [r for r in rows if label_of(r, cls, nosig) is not None]
        for pname, predict in predictors(cls).items():
            pool = decided if pname.startswith("rules") else \
                [r for r in decided if r.answered or not r.asked]
            s = score((bool(label_of(r, cls, nosig)), predict(r)) for r in pool)
            lines.append(f"| {name} | {pname} | {_pct(s.precision)} | {_pct(s.recall)} | "
                         f"{s.tp} | {s.fp} | {s.fn} | {s.tn} |")
    return lines + [""]


def report(rows: list[Page], model: str, pages: bool = False) -> str:
    unanswered = [r for r in rows if r.asked and not r.answered]
    unasked = [r for r in rows if not r.asked]
    out = [f"# Complexity routing against {len(rows)} labelled pages ({model}, prompt "
           f"v{pv.PROMPT_VERSION})", ""]
    for cls in CLASSES:
        pos = sum(r.labels[cls] is True for r in rows)
        und = sum(r.labels[cls] is None for r in rows)
        out.append(f"- {cls}: {pos} positive, {len(rows) - pos - und} negative"
                   + (f", {und} undecided (left out)" if und else ""))
    out.append(f"- handwriting positives that are signatures only: "
               f"{sum(bool(r.labels['handwriting']) and r.signature_only for r in rows)}")
    out.append(f"- the pass asks no question of {len(unasked)} pages (no block, or over "
               f"{pv.MAX_BLOCKS}): they count as not flagged")
    if unanswered:
        out.append(f"- **{len(unanswered)} pages have no cached answer and are left out of the "
                   "vision rows** (run with --record)")
    out.append("")
    out += table(rows, "All pages")
    unseen = [r for r in rows if not r.seen]
    out += table(unseen, f"Pages the page-vision pass never saw ({len(unseen)})")
    if pages:
        out += ["## Every page a predictor gets wrong", "",
                "Rules: the signals of the class that fired. n/a: the pass asked nothing.", "",
                "| Page | Class | Label | Vision raw | Gated | Rules |",
                "|---|---|---|---|---|---|"]
        for r in rows:
            for cls in CLASSES:
                rules = [k.split("/")[1] for k, v in r.rules.items() if v and k.startswith(cls)]
                label = r.labels[cls]
                if label is None or label != (cls in r.raw) or label != (cls in r.gated) \
                        or label != bool(rules):
                    out.append(f"| {r.doc} p{r.page} ({r.kind}) | {cls} | {label} | "
                               f"{cls in r.raw if r.answered else 'n/a'} | "
                               f"{cls in r.gated if r.answered else 'n/a'} | "
                               f"{', '.join(rules) or '-'} |")
        out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--model", default=providers.PAGE_VISION_MODEL)
    parser.add_argument("--work", type=Path, help="keep the conversions here and reuse them")
    parser.add_argument("--record", action="store_true",
                        help="ask a local Ollama server for the pages the cache lacks (REAL calls)")
    parser.add_argument("--pages", action="store_true", help="also list every page in error")
    parser.add_argument("--doc", action="append", help="only this document, by file stem "
                        "(repeatable)")
    args = parser.parse_args()
    rows = evaluate(args.labels, args.cache, args.model, args.work, args.record,
                    set(args.doc or []))
    print(report(rows, args.model, args.pages))


if __name__ == "__main__":
    main()
