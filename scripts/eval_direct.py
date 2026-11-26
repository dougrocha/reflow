"""Scored eval of --engine direct (docs/direct.md): each model transcribes the spec pages of
the page-check suite, and the suite's own checks score the result, with reflow's heuristic
pipeline (`default`) on the same pages as the baseline row.

For each document with a chosen spec page, an excerpt PDF holds only those pages (as the quick
page checks cut one). Each model converts every excerpt with --engine direct, and `default`
converts them with reflow's defaults. Then every assertion of every chosen spec runs on the
converted page, as tests/pages/test_pages.py runs it (a heading's level is not checked, since an
excerpt ranks fonts over its own pages). An assertion passes or fails; its `xfail` mark is
ignored, so `default` is scored on its known defects too. A page a model left unread fails all
of its checks.

A row (`--models`) is one way to make the transcript:

- `default`: reflow's heuristics;
- `assist:<model>`: the heuristics with every assist step and the alt text on a Claude model (the
  site's "haiku" variant);
- `polish:<model>`: the heuristics, then the polish pass (reflow.analyze.polish) on that model;
- `<model>` or `<model>/<prompt>`: --engine direct with that model and prompt variant
  (reflow.analyze.direct.PROMPTS; "base" when none is named).

The table gives, per row, the pass rate of each family of checks (FAMILIES) and in all, the
seconds per page, and the dollars per 1,000 pages: the row's dollars for the sample's pages,
divided by its pages and scaled to 1,000 (Anthropic list prices; a cached direct or polish page
counts what its request cost when it was made). A direct row's seconds are model seconds; the
other rows' are their conversion's wall time per page. An assist row counts the tokens of the
requests it sends, so it needs a fresh output folder (the default, a temporary one) to count
them all.

Requests: first every page of every excerpt is read through the shared answer cache
(reflow.analyze.direct), `--jobs` at a time for a Claude model, under one spending cap for the
whole run (`--max-dollars`, default 5). Then each excerpt is converted with a cap of 0, so every
page comes from the cache. Run again, and the cached answers cost nothing: the table is rebuilt
for free. A Claude model needs ANTHROPIC_API_KEY in this process only:

    env $(cat .env) .venv/bin/python scripts/eval_direct.py --sample 64 --models default \
        assist:claude-haiku-5-5 polish:claude-haiku-5-5 claude-haiku-5-5 claude-haiku-5-5/forms
    .venv/bin/python scripts/eval_direct.py --models default glm-ocr --sample 48

`--sample N` keeps N specs spread evenly over the sorted spec ids (the same N specs each run);
without it every spec is scored. `--json PATH` writes the scores per check kind too.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests" / "pages"))

from checks import CheckFailed  # noqa: E402
from corpus import Corpus, DocOutput, source  # noqa: E402
from specs import PageSpec, load_specs, run_assertion  # noqa: E402

from reflow import providers  # noqa: E402
from reflow.analyze import direct, vision  # noqa: E402
from reflow.convert import convert  # noqa: E402

BASELINE = "default"

# Check kinds by what they test; a kind not listed counts under "other".
FAMILIES: dict[str, tuple[str, ...]] = {
    "text": ("min_recall", "contains", "contains_in_order", "no_text"),
    "headings": ("heading", "not_heading", "max_headings", "doc_title"),
    "blocks": ("same_block", "same_item", "separate_blocks", "same_entry", "no_block",
               "max_blocks"),
    "tables": ("table", "tables"),
    "lists": ("list_items", "nested_list"),
    "forms": ("fields", "field_group", "ruled_box", "labelled_once", "field_labels",
              "control_names", "no_control_names", "field_rows", "no_empty_field_cells",
              "signature_items"),
    "figures": ("figures", "min_figures", "in_figure", "unread_figures", "alt_has", "alt_not",
                "max_figure_share", "figure_order"),
}
FAMILY_OF = {kind: fam for fam, kinds in FAMILIES.items() for kind in kinds}
COLUMNS = (*FAMILIES, "other")


@dataclass(frozen=True)
class Row:
    """One way to make the transcript: `kind` is "default", "assist", "polish" or "direct"."""
    name: str
    kind: str
    model: str = ""
    prompt: str = direct.DEFAULT_PROMPT

    @classmethod
    def parse(cls, name: str) -> Row:
        if name == BASELINE:
            return cls(name, BASELINE)
        for kind in ("assist", "polish"):
            if name.startswith(f"{kind}:"):
                return cls(name, kind, name.split(":", 1)[1])
        model, _, prompt = name.partition("/")
        if prompt and prompt not in direct.PROMPTS:
            raise SystemExit(f"unknown prompt variant {prompt!r} (use {', '.join(direct.PROMPTS)})")
        return cls(name, "direct", model, prompt or direct.DEFAULT_PROMPT)

    def options(self) -> dict:
        """The convert options of a non-direct row."""
        if self.kind == "assist":
            return {"alt_text": "anthropic", "alt_text_model": self.model, "assist_labels": True,
                    "assist_structure": True, "assist_scan_fields": True,
                    "assist_provider": "anthropic", "assist_model": self.model}
        if self.kind == "polish":
            return {"polish": True, "polish_model": self.model}
        return {}


class Tally:
    """The dollars of every AnthropicVision request this process sends (the assist steps and the
    alt text) while `on`, under a cap: a request starts only while the dollars spent, plus the
    most each request in flight and this one can cost, stay within `cap` (as direct.Spend). A
    request past it fails as a provider error, which the assist steps count and go on without. A
    request that fails is counted at the most it could cost, since its bill is not known."""

    def __init__(self) -> None:
        self.dollars = 0.0
        self.reserved = 0.0
        self.cap = 0.0
        self.on = False
        self._lock = threading.Lock()
        original = vision.AnthropicVision.ask
        tally = self

        def ask(self, system, prompt, image, schema=None, max_tokens=2048):
            if not tally.on:
                return original(self, system, prompt, image, schema, max_tokens)
            most = direct.dollars(self.model, direct.FIXED_INPUT_TOKENS
                                  + len((system + prompt).encode()), max_tokens)
            with tally._lock:
                if tally.dollars + tally.reserved + most > tally.cap:
                    raise vision.ProviderError("the eval run reached its spending cap")
                tally.reserved += most
            cost = most   # a failed request (a refusal) may be billed: count the most it could cost
            try:
                reply = original(self, system, prompt, image, schema, max_tokens)
                cost = direct.dollars(self.model, reply.usage.tokens_in, reply.usage.tokens_out)
                return reply
            finally:
                with tally._lock:
                    tally.reserved -= most
                    tally.dollars += cost
        vision.AnthropicVision.ask = ask


def family(kind: str) -> str:
    return FAMILY_OF.get(kind, "other")


def sample(specs: list[PageSpec], n: int | None) -> list[PageSpec]:
    """`n` specs spread evenly over `specs` (sorted by id), or all of them."""
    if not n or n >= len(specs):
        return specs
    return [specs[i * len(specs) // n] for i in range(n)]


@dataclass
class Score:
    """One model's results: passes and checks per kind, and what the pages took."""

    model: str
    passed: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    pages: int = 0
    unread: int = 0
    seconds: float = 0.0
    dollars: float = 0.0
    spent: float = 0.0
    stopped: bool = False
    failures: list[str] = field(default_factory=list)

    def add(self, kind: str, ok: bool) -> None:
        self.total[kind] += 1
        self.passed[kind] += ok

    def rate(self, kinds) -> float | None:
        total = sum(self.total[k] for k in kinds)
        return None if not total else 100 * sum(self.passed[k] for k in kinds) / total

    def by_family(self) -> dict[str, float | None]:
        out = {fam: self.rate(kinds) for fam, kinds in FAMILIES.items()}
        out["other"] = self.rate([k for k in self.total if family(k) == "other"])
        out["all"] = self.rate(list(self.total))
        return out


def score_page(score: Score, spec: PageSpec, doc: DocOutput | None, unread: bool = False) -> None:
    """Run every assertion of `spec` on its converted page; with no page, or a page the model
    left unread (its empty page would pass a negative check), each one fails."""
    try:
        pg = doc.page(spec.page) if doc and not unread else None
    except KeyError:
        pg = None
    for a in spec.assertions:
        ok = False
        if pg is not None:
            try:
                run_assertion(spec, replace(a, xfail=None), doc, pg)   # the eval knows no open defects
                ok = True
            except (CheckFailed, KeyError) as e:
                score.failures.append(f"{spec.id} {a.index} {a.check}: {e}")
        else:
            score.failures.append(f"{spec.id} {a.index} {a.check}: the page was not converted")
        score.add(a.check, ok)


def excerpts(specs: list[PageSpec], root: Path) -> dict[str, tuple[Path, dict[int, int]]]:
    """Per document, an excerpt PDF of its chosen pages and their source → excerpt numbers."""
    pages: dict[str, set[int]] = defaultdict(set)
    for s in specs:
        pages[s.doc].add(s.page)
    out = {}
    for slug, wanted in sorted(pages.items()):
        keep = tuple(sorted(wanted))
        folder = root / slug
        folder.mkdir(parents=True, exist_ok=True)
        pdf = Corpus._write_excerpt(slug, keep, folder / source(slug).name)
        out[slug] = (pdf, {p: i + 1 for i, p in enumerate(keep)})
    return out


def read_all(docs, reader: direct.Reader, cache: direct.Cache, spend: direct.Spend,
             jobs: int) -> None:
    """Read every page of every excerpt into the cache, `jobs` at a time, under one cap."""
    pdfium = threading.Lock()   # pdfium is not thread-safe, across documents too
    sources = {slug: direct.PageSource(pdf, pdfium, len(pages), reader.reads_text)
               for slug, (pdf, pages) in docs.items()}
    work = [(slug, n) for slug, (_, pages) in docs.items() for n in pages.values()]
    done = [0]
    lock = threading.Lock()

    def one(item) -> None:
        slug, number = item
        result = direct.read_pages(sources[slug], [number], reader, cache, spend, 1)[0]
        with lock:
            done[0] += 1
            state = "cached" if result.cached else ("ok" if result.reading else result.error)
            print(f"  [{done[0]}/{len(work)}] {slug} p{number}: {state}  "
                  f"(${spend.spent:.4f} spent)", file=sys.stderr, flush=True)

    try:
        with ThreadPoolExecutor(max_workers=jobs if reader.provider == "anthropic" else 1) as pool:
            list(pool.map(one, work))
    finally:
        for s in sources.values():
            s.close()


def run_model(name: str, specs: list[PageSpec], docs, root: Path, args,
              tally: Tally | None = None) -> Score:
    row = Row.parse(name)
    score = Score(name)
    out_root = root / "out" / name.replace("/", "-").replace(":", "-")
    reader = None
    if row.kind == "direct":
        reader = direct.make_reader(row.model, row.prompt)
        cache = direct.Cache(args.cache or direct.default_cache())
        spend = direct.Spend(row.model, args.max_dollars)
        read_all(docs, reader, cache, spend, args.jobs)
        score.spent, score.stopped = spend.spent, spend.stopped
    if tally is not None:
        tally.dollars, tally.cap, tally.on = 0.0, args.max_dollars, row.kind == "assist"
    outputs: dict[str, DocOutput | None] = {}
    unread: set[tuple[str, int]] = set()   # (document, source page)
    wall = 0.0
    for slug, (pdf, pages) in docs.items():
        out = out_root / slug
        spent = score.spent + (tally.dollars if tally and tally.on else 0.0)
        if row.kind in ("assist", "polish") and spent >= args.max_dollars:
            print(f"  {name} {slug}: not converted, the run reached its ${args.max_dollars} cap",
                  file=sys.stderr)
            score.stopped = True
            outputs[slug] = None
            continue
        t0 = time.monotonic()
        try:
            if reader is not None:
                convert(pdf, out, direct_reader=reader, engine="direct", direct_model=row.model,
                        direct_prompt=row.prompt, direct_max_dollars=0, direct_cache=args.cache)
            elif row.kind == "polish":
                convert(pdf, out, direct_cache=args.cache,
                        polish_max_dollars=args.max_dollars - score.spent, **row.options())
            else:
                convert(pdf, out, **row.options())
        except Exception as e:  # a failed conversion fails its pages' checks
            print(f"  {name} {slug}: conversion failed: {e}", file=sys.stderr)
            outputs[slug] = None
            continue
        wall += time.monotonic() - t0
        doc = DocOutput(slug, out, pages)
        outputs[slug] = doc
        score.pages += len(pages)
        meta = doc.document().get("meta", {})
        if reader is not None:
            run = meta.get("direct", {})
            score.unread += len(run.get("unread", {}))
            source_of = {n: p for p, n in pages.items()}
            unread |= {(slug, source_of[int(n)]) for n in run.get("unread", {})}
            score.seconds += sum(p["s"] for p in run.get("pages", {}).values())
            score.dollars += sum(p["usd"] for p in run.get("pages", {}).values())
        elif row.kind == "polish":
            run = meta.get("polish", {})
            score.dollars += sum(p["usd"] for p in run.get("pages", {}).values())
            score.spent += float(run.get("spent", 0.0))
            score.stopped |= bool(run.get("stopped"))
    if reader is None:
        score.seconds = wall
    if tally is not None and tally.on:
        score.dollars = score.spent = tally.dollars
        tally.on = False
    for spec in specs:
        score_page(score, spec, outputs.get(spec.doc), (spec.doc, spec.page) in unread)
    return score


def per_thousand(s: Score) -> str:
    """The row's dollars per 1,000 pages, as the site's "How this transcript was made" gives it:
    two places, or two significant digits under 10 cents."""
    if s.model == BASELINE:
        return "—"
    n = s.dollars / s.pages * 1000 if s.pages else 0.0
    return "$0" if n == 0 else f"${n:.2f}" if n >= 0.1 else f"${n:.2g}"


def table(scores: list[Score]) -> str:
    head = ["model", *COLUMNS, "all", "s/page", "$/1,000 pages"]
    rows = [head, ["---"] * len(head)]
    for s in scores:
        fam = s.by_family()
        cells = [s.model] + [("—" if fam[c] is None else f"{fam[c]:.0f}%") for c in (*COLUMNS, "all")]
        per_page = s.seconds / s.pages if s.pages else 0.0
        cells += [f"{per_page:.1f}", per_thousand(s)]
        rows.append(cells)
    counts = scores[0].total if scores else {}
    rows.append(["checks", *[str(sum(counts[k] for k in counts if family(k) == c)) for c in COLUMNS],
                 str(sum(counts.values())), "", ""])
    return "\n".join("| " + " | ".join(r) + " |" for r in rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", nargs="+", default=[BASELINE, providers.DIRECT_MODEL],
                    help="the rows: default, assist:<model>, polish:<model>, <model>[/<prompt>]")
    ap.add_argument("--sample", type=int, default=None, help="score N specs spread evenly")
    ap.add_argument("--only", default=None, help="keep the spec ids that contain this")
    ap.add_argument("--max-dollars", type=float, default=providers.DIRECT_MAX_DOLLARS,
                    help="the spending cap of each model's run (default 5)")
    ap.add_argument("--jobs", type=int, default=direct.DEFAULT_JOBS)
    ap.add_argument("--cache", type=Path, default=None, help="the answer cache (default: shared)")
    ap.add_argument("--json", type=Path, default=None, help="write the scores per check kind")
    ap.add_argument("--failures", type=Path, default=None,
                    help="write each failed check, one per line, per model (<path>.<model>.txt)")
    ap.add_argument("--out", type=Path, default=None,
                    help="keep the excerpts and conversions here (default: a temporary folder)")
    args = ap.parse_args(argv)

    specs = sample(load_specs(args.only), args.sample)
    print(f"{len(specs)} spec pages, {sum(len(s.assertions) for s in specs)} checks, "
          f"{len({s.doc for s in specs})} documents", file=sys.stderr)
    for name in args.models:
        Row.parse(name)   # a bad row name stops the run before any request
    tally = Tally() if any(Row.parse(m).kind == "assist" for m in args.models) else None
    scores = []
    with tempfile.TemporaryDirectory(prefix="eval-direct-") as tmp:
        root = args.out or Path(tmp)
        docs = excerpts(specs, root / "pdf")
        for model in args.models:
            print(f"{model}:", file=sys.stderr, flush=True)
            s = run_model(model, specs, docs, root, args, tally)
            print(f"  {s.pages} pages, {s.unread} unread, spent ${s.spent:.4f}"
                  + (" (stopped at the cap)" if s.stopped else ""), file=sys.stderr)
            scores.append(s)
            if args.failures:
                safe = model.replace("/", "-").replace(":", "-")
                path = args.failures.with_name(f"{args.failures.name}.{safe}.txt")
                path.write_text("".join(f + "\n" for f in s.failures), encoding="utf-8")
    print(table(scores))
    if args.json:
        args.json.write_text(json.dumps([
            {"model": s.model, "pages": s.pages, "unread": s.unread, "seconds": round(s.seconds, 2),
             "dollars": round(s.dollars, 4), "spent": round(s.spent, 4), "stopped": s.stopped,
             "kinds": {k: [s.passed[k], s.total[k]] for k in sorted(s.total)}}
            for s in scores], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
