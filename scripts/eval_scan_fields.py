"""Accuracy of field detection on scanned forms (no AcroForm): the rules alone, and the rules plus
the scan-field assist replayed from its committed answers.

Reads the hand-labelled fields in tests/scan_fields/fields.json, converts each scanned form twice
(`scan_fields`: certain candidates only; `assist_scan_fields`: also the model's answers, replayed
from tests/scan_fields/answers-cache.json, with a provider that refuses every call so nothing
reaches a model), reads the FIELD nodes from document.json and scores them against the labels:

- A detected field matches a label on the same page and of the same kind when the label's centre
  lies inside the detected box grown by PAD points. Pairs are taken greedily, highest IoU first,
  each label and each detected field at most once.
- Labels marked "invisible" (a widget with nothing printed to write in) are dropped before
  matching. A detection that matches an "optional" label (a blank a person could fill in, such
  as a signature line) is neither a hit nor a false positive, and the label is not a miss.
- Precision = hits / (hits + false positives), recall = hits / (hits + misses), overall and per
  kind. Caption accuracy is over the hits: the normalized label caption, or an "accept"
  alternate, is contained in the field's accessible name (aria, else label text).

A negative check follows the scores (see `prose_circles`): scanned prose has round letters, bullets and
degree signs, and none of them may become a certain answer circle. Its counts are printed per page, for
the NACA report in `samples/naca-ocr.pdf` and for rendered prose pages in real fonts.

`--record` makes REAL calls through --provider (a local Ollama server by default) for every
labelled form, writes each call's wall time, question kind ("scan-field" or "label"), answer and
page as a row of --calls-csv, and merges the run's ai-assist-cache.json entries into --cache.
Median and p90 per-call latency are printed from that CSV when it exists.

    .venv/bin/python scripts/eval_scan_fields.py [--record] [--form NAME] [--model ID]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO / "tests"))

from eval_label_assist import norm  # noqa: E402
from prosepage import PROSE_EDGES, list_page, prose_page  # noqa: E402

from reflow.analyze import ask, label_assist, scan_rules  # noqa: E402
from reflow.analyze.vision import ProviderError  # noqa: E402
from reflow.convert import convert  # noqa: E402
from reflow.model import BBox, Document, Role  # noqa: E402
import checkout  # noqa: E402
from reflow import providers  # noqa: E402
from reflow.geometry import iou  # noqa: E402
from reflow.names import ASSIST_CACHE, DOCUMENT_JSON  # noqa: E402

LABELS = REPO / "tests/scan_fields/fields.json"
CACHE = REPO / "tests/scan_fields/answers-cache.json"
CALLS_CSV = REPO / "docs/assist-scan-fields-calls.csv"
CSV_COLUMNS = ("form", "model", "kind", "page", "seconds", "answer")
PAD = 3.0            # points a detected box is grown by before it must contain a label's centre
KINDS = ("text", "checkbox")
RECORD_MAX_CALLS = 200
# Pages of the real scans kept out of tuning. The rules were tuned on the other pages,
# so these are the check on whether a rule generalizes. Their misses and false fields are not
# listed unless --show-holdout is given; only the counts are.
PROSE_PDFS = ("samples/naca-ocr.pdf",)          # a real scanned report: text, tables, charts, no form
PROSE_FONTS = ("sans", "bold", "serif", "dejavu")   # rendered prose pages: these fonts at these sizes
PROSE_SIZES = (9, 11, 12, 14)
HOLDOUT: dict[str, frozenset[int]] = {"census1960-scan": frozenset({1}), "census1970-scan": frozenset({2})}


@dataclass(frozen=True)
class Label:
    page: int
    kind: str
    box: BBox
    caption: str
    accept: tuple[str, ...] = ()
    invisible: bool = False
    optional: bool = False
    widget: str | None = None


@dataclass(frozen=True)
class Found:
    page: int
    kind: str
    box: BBox
    name: str = ""   # the accessible name: aria, else label text


@dataclass(frozen=True)
class Hit:
    label: Label
    found: Found
    iou: float
    caption_ok: bool


@dataclass
class Score:
    hits: list[Hit] = field(default_factory=list)
    misses: list[Label] = field(default_factory=list)
    false_positives: list[Found] = field(default_factory=list)
    absorbed: list[Hit] = field(default_factory=list)   # matched an optional label: not scored

    @property
    def precision(self) -> float | None:
        n = len(self.hits) + len(self.false_positives)
        return len(self.hits) / n if n else None

    @property
    def recall(self) -> float | None:
        n = len(self.hits) + len(self.misses)
        return len(self.hits) / n if n else None

    @property
    def caption_accuracy(self) -> float | None:
        return sum(h.caption_ok for h in self.hits) / len(self.hits) if self.hits else None

    def of_kind(self, kind: str) -> Score:
        return Score([h for h in self.hits if h.label.kind == kind],
                     [m for m in self.misses if m.kind == kind],
                     [f for f in self.false_positives if f.kind == kind],
                     [h for h in self.absorbed if h.label.kind == kind])


def restrict(s: Score, pages: frozenset[int], keep: bool) -> Score:
    """The part of a score on `pages` (keep) or off them (not keep)."""
    on = lambda page: (page in pages) == keep  # noqa: E731
    return Score([h for h in s.hits if on(h.label.page)], [m for m in s.misses if on(m.page)],
                 [f for f in s.false_positives if on(f.page)], [h for h in s.absorbed if on(h.label.page)])


def merge(scores: list[Score]) -> Score:
    return Score([h for s in scores for h in s.hits], [m for s in scores for m in s.misses],
                 [f for s in scores for f in s.false_positives], [h for s in scores for h in s.absorbed])


def contains_centre(found: BBox, label: BBox, pad: float = PAD) -> bool:
    cx, cy = (label[0] + label[2]) / 2, (label[1] + label[3]) / 2
    return found[0] - pad <= cx <= found[2] + pad and found[1] - pad <= cy <= found[3] + pad


def caption_ok(label: Label, name: str) -> bool:
    got = norm(name)
    return bool(got) and any(w and w in got for w in (norm(c) for c in (label.caption, *label.accept)))


def score(labels: list[Label], found: list[Found], pad: float = PAD) -> Score:
    """Match detected fields to labels (the rule is in the module docstring) and tally them."""
    labels = [label for label in labels if not label.invisible]
    pairs = sorted(
        (-iou(label.box, f.box), li, fi)
        for li, label in enumerate(labels) for fi, f in enumerate(found)
        if label.page == f.page and label.kind == f.kind and contains_centre(f.box, label.box, pad))
    label_of: dict[int, int] = {}
    found_of: dict[int, int] = {}
    for _, li, fi in pairs:
        if li not in label_of and fi not in found_of:
            label_of[li], found_of[fi] = fi, li
    result = Score()
    for li, label in enumerate(labels):
        if li in label_of:
            f = found[label_of[li]]
            hit = Hit(label, f, iou(label.box, f.box), caption_ok(label, f.name))
            (result.absorbed if label.optional else result.hits).append(hit)
        elif not label.optional:
            result.misses.append(label)
    result.false_positives = [f for fi, f in enumerate(found) if fi not in found_of]
    return result


def nearest_caption(box: BBox, page: int, labels: list[Label]) -> str:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2

    def distance(label: Label) -> float:
        return math.hypot((label.box[0] + label.box[2]) / 2 - cx, (label.box[1] + label.box[3]) / 2 - cy)

    on_page = [label for label in labels if label.page == page]
    return min(on_page, key=distance).caption if on_page else ""


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile, q in (0, 1]."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def load_labels(path: Path = LABELS) -> dict[str, tuple[dict, list[Label]]]:
    spec = json.loads(path.read_text())["forms"]
    return {name: (entry, [Label(f["page"], f["kind"], tuple(f["box"]), f["caption"], tuple(f.get("accept", [])),
                                 bool(f.get("invisible")), bool(f.get("optional")), f.get("widget"))
                           for f in entry["fields"]])
            for name, entry in spec.items()}


def found_fields(doc: Document) -> list[Found]:
    return [Found(n.page, str(n.attrs.get("kind")), tuple(n.bbox), str(n.attrs.get("aria") or n.text or ""))
            for n in doc.root.walk()
            if n.role == Role.FIELD and n.page and n.bbox and n.attrs.get("kind") in KINDS]


class Refuse:
    """The provider of a replay: a question the cache cannot answer fails and nothing is sent."""

    cache_id = ""

    def choose(self, question, image, crop):
        raise ProviderError("replay only: a call would reach a model")


@dataclass
class FormResult:
    form: str
    labels: list[Label]
    rules: Score
    assist: Score | None   # None: no cached answers for this model
    stats: dict = field(default_factory=dict)
    real: bool = False     # a physical scan of paper, not a render of a born-digital PDF


def _read(out: Path) -> Document:
    return Document.from_json((out / DOCUMENT_JSON).read_text())


def cached_for(cache_path: Path, model: str) -> int:
    return sum(1 for e in ask.load_cache(cache_path).values()
               if isinstance(e, dict) and e.get("model") == model)


def evaluate(labels_path: Path = LABELS, cache_path: Path = CACHE, model: str = providers.ASSIST_MODELS[providers.DEFAULT_ASSIST_PROVIDER],
             forms: set[str] | None = None, work: Path | None = None, rules_only: bool = False) -> list[FormResult]:
    results: list[FormResult] = []
    replay = not rules_only and cached_for(cache_path, model) > 0
    with tempfile.TemporaryDirectory() as tmp:
        for form, (entry, labels) in load_labels(labels_path).items():
            if forms and form not in forms:
                continue
            pdf = checkout.sample(REPO, entry["pdf"])
            if not pdf.exists():
                print(f"skip {form}: {pdf} is missing", file=sys.stderr)
                continue
            root = (work or Path(tmp)) / form
            root.mkdir(parents=True, exist_ok=True)
            convert(pdf, root / "rules", page_images=False, jobs=1, scan_fields=True)
            rules = score(labels, found_fields(_read(root / "rules")))
            assist, stats = None, {}
            if replay:
                out = root / "assist"
                out.mkdir()
                (out / ASSIST_CACHE).write_text(cache_path.read_text())
                convert(pdf, out, page_images=False, jobs=1, assist_scan_fields=True,
                        assist_provider=providers.DEFAULT_ASSIST_PROVIDER, assist_model=model, label_provider=Refuse())
                doc = _read(out)
                assist = score(labels, found_fields(doc))
                stats = {k: doc.meta[k] for k in ("scan_fields", "label_assist") if k in doc.meta}
            results.append(FormResult(form, labels, rules, assist, stats, bool(entry.get("real"))))
    return results


class TimedProvider:
    """Wraps the real provider: every call's wall time, question kind, answer and page become a CSV row."""

    def __init__(self, inner, form: str, rows: list[dict]) -> None:
        self.inner, self.form, self.rows = inner, form, rows
        self.cache_id = inner.cache_id

    def choose(self, question, image, crop):
        kind = "label" if isinstance(question, label_assist.LabelQuestion) else "scan-field"
        start, answer = time.perf_counter(), "error"
        try:
            reply = self.inner.choose(question, image, crop)
            answer = reply.text
            return reply
        finally:
            self.rows.append({"form": self.form, "model": self.cache_id, "kind": kind, "page": question.page,
                              "seconds": round(time.perf_counter() - start, 3), "answer": answer})


def record(labels_path: Path = LABELS, cache_path: Path = CACHE, calls_csv: Path = CALLS_CSV,
           forms: set[str] | None = None, provider: str = providers.DEFAULT_ASSIST_PROVIDER,
           model: str | None = None, max_calls: int = RECORD_MAX_CALLS) -> dict[str, dict]:
    """Convert each labelled form with the assist on and REAL model calls; append the calls to the CSV and
    merge the answers into the cache file. Returns the per-form scan-field stats."""
    model = providers.resolve_model(provider, model)
    cache = ask.load_cache(cache_path)
    rows: list[dict] = []
    stats = {}
    with tempfile.TemporaryDirectory() as tmp:
        for form, (entry, _) in load_labels(labels_path).items():
            if forms and form not in forms:
                continue
            out = Path(tmp) / form
            summary = convert(checkout.sample(REPO, entry["pdf"]), out, page_images=False, jobs=1, assist_scan_fields=True,
                              assist_provider=provider, assist_model=model, assist_scan_fields_max_calls=max_calls,
                              label_provider=TimedProvider(ask.make_provider(provider, model), form, rows))
            stats[form] = getattr(summary, "scan_fields", {})
            cache.update(ask.load_cache(out / ASSIST_CACHE))
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    fresh = not calls_csv.exists()
    calls_csv.parent.mkdir(parents=True, exist_ok=True)
    with calls_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, CSV_COLUMNS)
        if fresh:
            writer.writeheader()
        writer.writerows(rows)
    return stats


def latency(calls_csv: Path = CALLS_CSV) -> dict[tuple[str, str], tuple[int, float, float]]:
    """(model, kind) -> (calls, median seconds, p90 seconds) from the CSV; empty when it does not exist."""
    if not calls_csv.exists():
        return {}
    grouped: dict[tuple[str, str], list[float]] = {}
    with calls_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            grouped.setdefault((row["model"], row["kind"]), []).append(float(row["seconds"]))
    return {key: (len(v), percentile(v, 0.5), percentile(v, 0.9)) for key, v in sorted(grouped.items())}


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} = {part / whole:.0%}" if whole else f"{part}/0"


def summary_line(name: str, mode: str, s: Score) -> str:
    kinds = "  ".join(
        f"{k} P {pct(len(sk.hits), len(sk.hits) + len(sk.false_positives))} R {pct(len(sk.hits), len(sk.hits) + len(sk.misses))}"
        for k in KINDS for sk in [s.of_kind(k)])
    return (f"{name:10} {mode:7} precision {pct(len(s.hits), len(s.hits) + len(s.false_positives))}"
            f"  recall {pct(len(s.hits), len(s.hits) + len(s.misses))}"
            f"  | {kinds}  | caption {pct(sum(h.caption_ok for h in s.hits), len(s.hits))}")


def box_text(box: BBox) -> str:
    return "[" + ", ".join(f"{v:.0f}" for v in box) + "]"


def problems(r: FormResult, mode: str, s: Score, show_holdout: bool = False) -> list[str]:
    if r.form in HOLDOUT and not show_holdout:
        s = restrict(s, HOLDOUT[r.form], keep=False)
    out = [f"{r.form} {mode} misses ({len(s.misses)}):"]
    out += [f"  p{m.page} {m.kind} {box_text(m.box)} {m.caption!r}" for m in s.misses]
    out.append(f"{r.form} {mode} false positives ({len(s.false_positives)}):")
    out += [f"  p{f.page} {f.kind} {box_text(f.box)} near {nearest_caption(f.box, f.page, r.labels)!r}"
            f"{f' named {f.name!r}' if f.name else ''}" for f in s.false_positives]
    wrong = [h for h in s.hits if not h.caption_ok]
    out.append(f"{r.form} {mode} wrong captions ({len(wrong)}):")
    out += [f"  p{h.label.page} {h.label.kind} {box_text(h.found.box)} wanted {h.label.caption!r} got {h.found.name!r}"
            for h in wrong]
    return out


def report(results: list[FormResult], model: str, rules_only: bool = False, show_holdout: bool = False) -> str:
    out = [f"scan field evaluation (model key: {model})" if not rules_only else "scan field evaluation (rules only)"]
    modes = [("rules", lambda r: r.rules)] + ([] if rules_only else [("assist", lambda r: r.assist)])
    for name, pick in modes:
        sel = [(r, pick(r)) for r in results]
        for r, s in sel:
            out.append(summary_line(r.form, name, s) if s else
                       f"{r.form:10} {name:7} not measured (no cached answers for {model})")
        for label, real in (("ALL", False), ("REAL", True)):
            have = [s for r, s in sel if s and r.real == real]
            if have and (real or sum(not r.real for r in results) > 1):
                out.append(summary_line(label, name, merge(have)))
        held = [(r, s) for r, s in sel if s and r.form in HOLDOUT]
        if held:
            for label, keep in (("TUNE", False), ("HOLD", True)):
                out.append(summary_line(label, name, merge([restrict(s, HOLDOUT[r.form], keep) for r, s in held])))
    for r in results:
        for name, pick in modes:
            if pick(r):
                out.extend(problems(r, name, pick(r), show_holdout))
        if r.stats:
            out.append(f"{r.form} assist stats: " + json.dumps(r.stats))
    return "\n".join(out)


def latency_report(calls_csv: Path) -> str:
    rows = latency(calls_csv)
    if not rows:
        return ""
    return "\n".join(["per-call latency (" + str(calls_csv) + "):"] + [
        f"  {model} {kind}: {n} calls, median {med:.1f}s, p90 {p90:.1f}s" for (model, kind), (n, med, p90) in rows.items()])


def certain_circles(found: list[scan_rules.Candidate]) -> int:
    return sum(c.shape is scan_rules.Shape.CIRCLE and c.certain for c in found)


def prose_circles(work: Path) -> list[str]:
    """The negative check: the certain answer circles the rules propose on scanned prose, per page.
    Every count must be 0. Pages of `PROSE_PDFS` are read by a real `--scan-fields` conversion (a
    page is listed only when the converter takes it for a scan with OCR text); the rendered pages
    are the glyph lines of tests/prosepage.py in each of `PROSE_FONTS` at each of `PROSE_SIZES`."""
    out = ["scanned prose pages, certain circles (every count must be 0):"]
    total = 0
    for name in PROSE_PDFS:
        pdf = REPO / name
        if not pdf.exists():
            out.append(f"  {name}: missing, not measured")
            continue
        counts: dict[int, int] = {}
        real = scan_rules.detect

        def spy(pdf_path, number, lines, real=real, counts=counts):
            found = real(pdf_path, number, lines)
            counts[number] = certain_circles(found)
            return found

        scan_rules.detect = spy
        try:
            convert(pdf, work / "prose-" / pdf.stem, page_images=False, jobs=1, scan_fields=True)
        finally:
            scan_rules.detect = real
        total += sum(counts.values())
        out.append(f"  {name}: " + (", ".join(f"p{n} {c}" for n, c in sorted(counts.items()))
                                    or "no page read as a scan"))
    rendered = []
    for font in PROSE_FONTS:
        for size in PROSE_SIZES:
            page = prose_page(font, size, marks=font != "bold")
            if page is None:
                continue
            image, lines = page
            count = certain_circles(scan_rules.propose(image, lines, 1))
            total += count
            rendered.append(f"{font} {size}pt {count}")
    out.append("  rendered pages (lines of lone o, O, 0, degree signs and bullets; bold has no degree sign or bullet): "
               + (", ".join(rendered) or "no font found"))
    # Lone round letters at a line's edge, and lists opened by a hollow bullet or Word's "o".
    shaped = []
    for font, size in (("sans", 12), ("serif", 12), ("dejavu", 8), ("dejavu", 12)):
        page = prose_page(font, size, texts=PROSE_EDGES)
        if page is not None:
            count = certain_circles(scan_rules.propose(*page, 1))
            total += count
            shaped.append(f"edges {font} {size}pt {count}")
    for font, size, marker in (("sans", 12, "○"), ("serif", 12, "○"), ("dejavu", 8, "○"), ("mono", 12, "o")):
        page = list_page(font, size, ["Option one", "Option two", "Option three", "Option four"], marker)
        if page is not None:
            count = certain_circles(scan_rules.propose(*page, 1))
            total += count
            shaped.append(f"list {marker} {font} {size}pt {count}")
    out.append("  edges and lists: " + (", ".join(shaped) or "no font found"))
    out.append(f"  total {total}" + ("" if total == 0 else "  <-- FAIL"))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--calls-csv", type=Path, default=CALLS_CSV)
    parser.add_argument("--provider", default=providers.DEFAULT_ASSIST_PROVIDER, choices=providers.ASSIST_PROVIDERS,
                        help="provider used by --record")
    parser.add_argument("--model", default=None, help="model key to replay / record (default: the provider's default)")
    parser.add_argument("--form", action="append", help="only this form (repeatable)")
    parser.add_argument("--max-calls", type=int, default=RECORD_MAX_CALLS,
                        help="--record: uncached questions asked per form (assist_scan_fields_max_calls)")
    parser.add_argument("--show-holdout", action="store_true",
                        help="also list the misses and false fields on the hold-out pages (don't, while tuning)")
    parser.add_argument("--rules-only", action="store_true",
                        help="score the rules alone: skip the assist replay (its cached answers are keyed to the "
                             "candidates the rules drew, so they go stale when the rules change)")
    parser.add_argument("--record", action="store_true",
                        help="make REAL calls through --provider for every labelled form, append them to --calls-csv "
                             "and merge the answers into --cache before scoring")
    args = parser.parse_args()
    model = providers.resolve_model(args.provider, args.model)
    forms = set(args.form or [])
    if args.record:
        started = time.time()
        stats = record(args.labels, args.cache, args.calls_csv, forms, args.provider, model, args.max_calls)
        print(f"recorded in {time.time() - started:.0f}s: " + json.dumps(stats))
    print(report(evaluate(args.labels, args.cache, model, forms=forms, rules_only=args.rules_only), model,
                 args.rules_only, args.show_holdout))
    if not forms:
        with tempfile.TemporaryDirectory() as tmp:
            print("\n".join(prose_circles(Path(tmp))))
    text = latency_report(args.calls_csv)
    if text:
        print(text)


if __name__ == "__main__":
    main()
