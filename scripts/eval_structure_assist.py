"""Accuracy of the heading / label / value call on form lines: the rules alone, and the rules plus
the structure assist (`--assist-structure`).

Reads the hand-labelled lines in tests/label_assist/structure.json: each is a heading the rules
produced that the assist would be asked about, with what a person reading the page says it is.
A line is scored correct when the document's treatment agrees: a `heading` is still a heading,
a `label` or a `value` is no longer one. The rules-only figure converts with the assist off; the
assisted figure replays the committed cache tests/label_assist/structure-cache.json (model answers: reference
answers recorded once with Claude (claude-haiku-5-5) during development,
plus local models) and calls nothing. `--record` makes REAL calls through --provider (a local Ollama server by default) and rewrites that cache.

    .venv/bin/python scripts/eval_structure_assist.py [--record] [--form NAME] [--model ID]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from reflow.analyze import ask, structure_assist  # noqa: E402
from reflow.convert import convert  # noqa: E402
from reflow.model import HEADING_ROLES, Document, Node, Role  # noqa: E402
import checkout  # noqa: E402
from reflow import providers  # noqa: E402
from reflow.names import ASSIST_CACHE, DOCUMENT_JSON  # noqa: E402
from reflow.text_quality import squash  # noqa: E402

LABELS = REPO / "tests/label_assist/structure.json"
CACHE = REPO / "tests/label_assist/structure-cache.json"


def find(doc: Document, line: dict) -> Node | None:
    for node in doc.root.walk():
        if (node.page == line["page"] and node.bbox and node.text and node.role != Role.SECTION
                and abs(node.bbox[0] - line["at"][0]) <= 3 and abs(node.bbox[1] - line["at"][1]) <= 3
                and squash(node.text).startswith(line["text"][:30])):
            return node
    return None


def is_heading(node: Node) -> bool:
    return node.role in HEADING_ROLES


def correct(node: Node, line: dict) -> bool:
    return is_heading(node) == (line["kind"] == "heading")


@dataclass
class Row:
    form: str
    text: str
    kind: str
    found: bool
    rules: bool
    assisted: bool
    cached: bool


def evaluate(labels_path: Path = LABELS, cache_path: Path = CACHE,
             model: str = providers.ASSIST_MODELS[providers.DEFAULT_ASSIST_PROVIDER], forms: set[str] | None = None) -> list[Row]:
    spec = json.loads(labels_path.read_text())["forms"]
    rows: list[Row] = []
    with tempfile.TemporaryDirectory() as tmp:
        for form, entry in spec.items():
            if forms and form not in forms:
                continue
            pdf = checkout.sample(REPO, entry["pdf"])
            if not pdf.exists():
                print(f"skip {form}: {pdf} is missing", file=sys.stderr)
                continue
            out = Path(tmp) / form
            convert(pdf, out, page_images=False, jobs=1)
            text = (out / DOCUMENT_JSON).read_text()
            rules_doc, assisted_doc = Document.from_json(text), Document.from_json(text)
            sha = str(rules_doc.meta.get("sha256", ""))
            asked = {q.node for q in structure_assist.questions(rules_doc)
                     if structure_assist.cache_key(sha, q, model) in ask.load_cache(cache_path)}
            # Replay only: provider None never calls out; image_for is never reached.
            structure_assist.assist(assisted_doc, None, cache_path, model=model,
                                    image_for=lambda q: (_ for _ in ()).throw(AssertionError("no call")))
            for line in entry["lines"]:
                a, b = find(rules_doc, line), find(assisted_doc, line)
                rows.append(Row(form, line["text"], line["kind"], a is not None,
                                a is not None and correct(a, line),
                                b is not None and correct(b, line),
                                a is not None and a.id in asked))
    return rows


def record(labels_path: Path = LABELS, cache_path: Path = CACHE, forms: set[str] | None = None,
           **options) -> dict[str, dict]:
    """Convert each labelled form with the assist on and REAL model calls, rewrite the cache from
    the answers and return the per-form stats."""
    spec = json.loads(labels_path.read_text())["forms"]
    cache = {k: v for k, v in ask.load_cache(cache_path).items()
             if v.get("prompt_version") == structure_assist.STRUCTURE_PROMPT_VERSION}
    stats = {}
    with tempfile.TemporaryDirectory() as tmp:
        for form, entry in spec.items():
            if forms and form not in forms:
                continue
            out = Path(tmp) / form
            summary = convert(checkout.sample(REPO, entry["pdf"]), out, page_images=False, jobs=1,
                              assist_structure=True, **options)
            stats[form] = summary.structure_assist
            cache.update(ask.load_cache(out / ASSIST_CACHE))
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


def report(rows: list[Row], model: str) -> str:
    out = [f"structure assist evaluation (model key: {model})"]
    forms = list(dict.fromkeys(r.form for r in rows))
    for form in [*forms, None]:
        sel = [r for r in rows if form is None or r.form == form]
        n = len(sel)
        line = (f"{form or 'ALL':32} {n:3} lines  rules only {sum(r.rules for r in sel)}/{n}"
                f" = {sum(r.rules for r in sel) / max(n, 1):.0%}")
        if any(r.cached for r in sel):
            both = sum(r.assisted for r in sel)
            line += f"  | rules + cached assist {both}/{n} = {both / n:.0%}"
        else:
            line += "  | rules + assist: not measured (no cached answers)"
        out.append(line)
    missing = [r for r in rows if not r.found]
    if missing:
        out.append("lines that matched no node: " + ", ".join(f"{r.form}:{r.text}" for r in missing))
    out.append("headings wrongly demoted by the assist: " + (", ".join(
        f"{r.form}:{r.text}" for r in rows if r.kind == "heading" and r.rules and not r.assisted) or "none"))
    out.append("still wrong with the assist: " + ("; ".join(
        f"{r.form}:{r.text} ({r.kind})" for r in rows if not r.assisted) or "none"))
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, default=LABELS)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--provider", default=providers.DEFAULT_ASSIST_PROVIDER,
                        choices=providers.ASSIST_PROVIDERS, help="provider used by --record")
    parser.add_argument("--model", default=None,
                        help="model key to replay / record (default: the provider's default; "
                             f"the Claude reference is {providers.ASSIST_MODELS["anthropic"]})")
    parser.add_argument("--form", action="append", help="only this form (repeatable)")
    parser.add_argument("--record", action="store_true",
                        help="make REAL calls through --provider (a local Ollama server by default) and rewrite --cache before scoring")
    args = parser.parse_args()
    args.model = providers.resolve_model(args.provider, args.model)
    if args.record:
        t = time.time()
        stats = record(args.labels, args.cache, set(args.form or []), assist_provider=args.provider, assist_model=args.model)
        print(f"recorded in {time.time() - t:.0f}s: " + json.dumps(stats))
    print(report(evaluate(args.labels, args.cache, args.model, forms=set(args.form or [])), args.model))


if __name__ == "__main__":
    main()
