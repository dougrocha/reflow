"""Per-page pass / xfail / fail table for the page-check suite, for the QA loop.

    uv run python tests/pages/report.py            # every page (converts every document)
    uv run python tests/pages/report.py fw9        # pages whose id contains "fw9"
    uv run python tests/pages/report.py -v maricopa-directory-508
    uv run python tests/pages/report.py --quick --scorecard      # plus a per-document scorecard
    uv run python tests/pages/report.py --quick --write docs/corpus-scorecard.md

Status per assertion: pass (holds), xfail (known open defect, fails as expected), FAIL (a
regression, or a new check that does not hold), XPASS (an xfail that now holds: remove
its "xfail" from the spec so it guards the fix). Exit status 1 on FAIL or XPASS.

The scorecard has one row per document: assertion outcomes, mean measured recall and mean
min_recall floor over its page checks, review items from review.json, and conversion seconds
and pages per second from the timing the harness records when it converts a document.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from checks import CheckFailed, page_text, recall  # noqa: E402
from corpus import REPO, TIMING_FILE, Corpus  # noqa: E402
from reflow.names import REVIEW_JSON  # noqa: E402
from specs import load_specs, run_assertion  # noqa: E402

DEFAULT_SCORECARD = "docs/corpus-scorecard.md"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("filter", nargs="?", default=None, help="substring of the page id")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print the message for every xfail too")
    ap.add_argument("--quick", action="store_true",
                    help="convert only the tested pages of long documents (see README.md)")
    ap.add_argument("--scorecard", action="store_true",
                    help="after the page table, print a per-document scorecard")
    ap.add_argument("--write", nargs="?", const=DEFAULT_SCORECARD, default=None, metavar="PATH",
                    help=f"write the scorecard as markdown (default {DEFAULT_SCORECARD})")
    ap.add_argument("--reconvert", action="store_true",
                    help="convert the selected documents again even if cached, to record "
                         "their conversion timing for the scorecard")
    args = ap.parse_args(argv)
    if args.quick:
        os.environ["REFLOW_PAGE_CHECKS_QUICK"] = "1"

    specs = load_specs(args.filter)
    if not specs:
        print(f"no page checks match {args.filter!r}")
        return 1
    corpus = Corpus()
    needed: dict[str, set[int]] = {}
    for spec in specs:
        needed.setdefault(spec.doc, set()).add(spec.page)
    corpus.prepare(needed, pages=needed, fresh=args.reconvert)
    rows, details = [], []
    cards: dict[str, DocCard] = {}
    totals: Counter = Counter()
    for spec in specs:
        counts: Counter = Counter()
        card = cards.setdefault(spec.doc, DocCard(spec.doc))
        card.pages += 1
        try:
            doc = corpus.doc(spec.doc)
            card.attach(doc)
            pg = doc.page(spec.page)
            pdf = doc.pdf_text(spec.page)
            try:
                value = recall(pdf, page_text(pg))
                measured = f"{value:.2f}"
                if any(a.check == "min_recall" for a in spec.assertions):
                    card.recalls.append(value)
            except CheckFailed:
                measured = "-"
        except Exception as e:  # conversion or lookup failed: report, keep going
            rows.append((spec.id, "-", "ERROR", str(e).splitlines()[0][:70], counts))
            totals["error"] += 1
            card.errors += 1
            continue
        for a in spec.assertions:
            try:
                run_assertion(spec, a, doc, pg, pdf)
                ok, msg = True, ""
            except CheckFailed as e:
                ok, msg = False, str(e)
            if ok and a.xfail:
                status = "XPASS"
            elif ok:
                status = "pass"
            elif a.xfail:
                status = "xfail"
            else:
                status = "FAIL"
            counts[status] += 1
            totals[status] += 1
            card.counts[status] += 1
            if a.check == "min_recall":
                floor = a.value["floor"] if isinstance(a.value, dict) else a.value
                card.floors.append(float(floor))
            if status in ("FAIL", "XPASS") or (args.verbose and status == "xfail"):
                reason = f" | known: {a.xfail}" if a.xfail else ""
                details.append(f"  {spec.id:<24} {status:<5} {a.check}[{a.index}]: "
                               f"{msg if not ok else 'holds, but marked xfail'}{reason}")
        rows.append((spec.id, measured, _status_word(counts), "", counts))

    header = f"{'page':<24}{'recall':>8}  {'pass':>4} {'xfail':>5} {'fail':>4} {'xpass':>5}  status"
    print(header)
    print("-" * len(header))
    for pid, rec, word, note, counts in rows:
        if word == "ERROR":
            print(f"{pid:<24}{'-':>8}  ERROR {note}")
            continue
        print(f"{pid:<24}{rec:>8}  {counts['pass']:>4} {counts['xfail']:>5} "
              f"{counts['FAIL']:>4} {counts['XPASS']:>5}  {word}")
    print("-" * len(header))
    print(f"{'total':<24}{'':>8}  {totals['pass']:>4} {totals['xfail']:>5} "
          f"{totals['FAIL']:>4} {totals['XPASS']:>5}  "
          f"{len(specs)} pages, {sum(totals.values())} assertions")
    if details:
        print()
        print("\n".join(details))
    if args.scorecard or args.write:
        print()
        print(scorecard(cards, quick=corpus.quick))
        if args.write:
            out = Path(args.write)
            out = out if out.is_absolute() else REPO / out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(scorecard_markdown(cards, quick=corpus.quick, filter=args.filter),
                           encoding="utf-8")
            print(f"\nwrote {out}")
    bad = totals["FAIL"] + totals["XPASS"] + totals["error"]
    return 1 if bad else 0


def _status_word(c: Counter) -> str:
    if c["FAIL"] or c["ERROR"]:
        return "FAIL"
    if c["XPASS"]:
        return "XPASS"
    return "ok" if not c["xfail"] else "ok (known open)"


class DocCard:
    """One document's row in the scorecard: assertion outcomes, recall, review items, speed."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.pages = 0                   # page checks tested
        self.errors = 0                  # page checks that did not convert or were not found
        self.counts: Counter = Counter()
        self.recalls: list[float] = []   # measured recall of each tested page
        self.floors: list[float] = []    # each min_recall floor in the specs
        self.excerpt = False             # quick mode converted an excerpt, not the whole PDF
        self.review: Counter = Counter()
        self.pages_converted = 0
        self.seconds: float | None = None
        self._attached = False

    def attach(self, doc) -> None:
        """Read the review items and the conversion timing from the document's output."""
        if self._attached:
            return
        self._attached = True
        self.excerpt = doc.page_map is not None
        review = doc.out_dir / REVIEW_JSON
        if review.exists():
            self.review.update(item.get("severity", "?")
                               for item in json.loads(review.read_text(encoding="utf-8")))
        timing = doc.out_dir / TIMING_FILE
        if timing.exists():
            data = json.loads(timing.read_text(encoding="utf-8"))
            self.pages_converted = int(data["pages"])
            self.seconds = sum(data["seconds"].values())

    @property
    def pages_per_second(self) -> float | None:
        return self.pages_converted / self.seconds if self.seconds else None


def _mean(values: list[float]) -> str:
    return f"{sum(values) / len(values):.1f}" if values else "-"


SCORECARD_HEAD = ["document", "pages", "pass", "xfail", "fail", "xpass", "mean recall",
                  "mean floor", "review err", "review warn", "convert s", "pages/s"]


def _scorecard_rows(cards: dict[str, DocCard]) -> list[list[str]]:
    rows = []
    for slug in sorted(cards):
        c = cards[slug]
        pps = c.pages_per_second
        rows.append([
            slug + ("*" if c.excerpt else ""), str(c.pages),
            str(c.counts["pass"]), str(c.counts["xfail"]),
            str(c.counts["FAIL"] + c.errors), str(c.counts["XPASS"]),
            _mean(c.recalls), _mean(c.floors),
            str(c.review["error"]), str(c.review["warning"]),
            f"{c.seconds:.1f}" if c.seconds is not None else "-",
            f"{pps:.1f}" if pps is not None else "-",
        ])
    return rows


def _scorecard_total(cards: dict[str, DocCard]) -> str:
    t: Counter = Counter()
    for c in cards.values():
        t.update(c.counts)
        t["FAIL"] += c.errors
    return (f"{len(cards)} documents, {sum(c.pages for c in cards.values())} page checks: "
            f"{t['pass']} pass, {t['xfail']} xfail, {t['FAIL']} fail, {t['XPASS']} xpass")


def _scorecard_notes(quick: bool) -> list[str]:
    notes = ["pages = page checks tested. mean recall = measured word recall over those pages; "
             "mean floor = their min_recall floors. Only pages with a min_recall check count.",
             "review err / warn = items in the conversion's review.json.",
             "convert s = summed stage seconds of the conversion; pages/s = pages converted "
             "divided by that. \"-\" for a cache entry made before timing was recorded: "
             "run with --reconvert to record it."]
    if quick:
        notes.append("* quick mode: converted from an excerpt, so review counts and speed "
                     "describe the excerpt, not the whole PDF. Run without --quick for the "
                     "whole-document figures.")
    return notes


def scorecard(cards: dict[str, DocCard], *, quick: bool) -> str:
    table = [SCORECARD_HEAD, *_scorecard_rows(cards)]
    widths = [max(len(r[i]) for r in table) for i in range(len(SCORECARD_HEAD))]
    lines = ["  ".join(v.ljust(w) if i == 0 else v.rjust(w)
                       for i, (v, w) in enumerate(zip(r, widths))) for r in table]
    rule = "-" * len(lines[0])
    lines.insert(1, rule)
    lines.append(rule)
    lines.append(_scorecard_total(cards))
    lines.extend(_scorecard_notes(quick))
    return "\n".join(lines)


def scorecard_markdown(cards: dict[str, DocCard], *, quick: bool, filter: str | None) -> str:
    scope = f"pages matching `{filter}`" if filter else "all page checks"
    lines = [
        "# Corpus fidelity scorecard", "",
        f"Generated {datetime.date.today().isoformat()} by `tests/pages/report.py` ({scope}, "
        f"{'quick mode' if quick else 'whole documents'}). Regenerate with:", "",
        "    .venv/bin/python tests/pages/report.py " + ("--quick " if quick else "")
        + "--write docs/corpus-scorecard.md", "",
        "| " + " | ".join(SCORECARD_HEAD) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(SCORECARD_HEAD) - 1)) + "|",
        *("| " + " | ".join(r) + " |" for r in _scorecard_rows(cards)),
        "", _scorecard_total(cards), "",
        *(f"- {n}" for n in _scorecard_notes(quick)),
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
