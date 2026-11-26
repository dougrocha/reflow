"""Accuracy of form-field naming: the rules alone, and the rules plus the label assist.

Reads the hand-labelled pairs in tests/label_assist/labels.json, converts each form with the
rules only, and scores every labelled field: is its accessible name (aria, else its label text)
the printed caption the human wrote down? Then it replays the assist's cached answers over the
same documents and scores again. Nothing here calls a model: it replays the committed cache (model answers, recorded once with claude-haiku-5-5 by running
`reflow convert --assist-labels` and copying the carried ai-assist-cache.json in); with no
cached answers the second figure is reported as not measured.

    .venv/bin/python scripts/eval_label_assist.py [--cache FILE] [--model ID] [--labels FILE]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from reflow.analyze import ask, label_assist  # noqa: E402
from reflow.convert import convert  # noqa: E402
from reflow.model import Document, Node, Role  # noqa: E402
import checkout  # noqa: E402
from reflow import providers  # noqa: E402
from reflow.names import ASSIST_CACHE, DOCUMENT_JSON  # noqa: E402
from reflow.text_quality import squash  # noqa: E402

LABELS = REPO / "tests/label_assist/labels.json"
CACHE = REPO / "tests/label_assist/answers-cache.json"


def norm(text: str) -> str:
    return squash(re.sub(r"[^0-9a-z]+", " ", text.lower().replace("'", "").replace("’", "")))


def accessible_name(node: Node) -> str:
    return str(node.attrs.get("aria") or node.text or "")


def find(doc: Document, label: dict) -> Node | None:
    for node in doc.root.walk():
        if node.role not in (Role.FIELD, Role.GROUP) or node.page != label["page"] or not node.bbox:
            continue
        if "at" in label:
            if abs(node.bbox[0] - label["at"][0]) <= 3 and abs(node.bbox[1] - label["at"][1]) <= 3 \
                    and node.role == Role.GROUP:
                return node
        else:
            name = str(node.attrs.get("name") or "")
            if name == label["name"] or name.endswith("." + label["name"]):
                return node
    return None


def correct(node: Node, label: dict) -> bool:
    got = norm(accessible_name(node))
    wants = [norm(w) for w in [label["caption"], *label.get("accept", [])]]
    return got in wants if label.get("exact") else any(w in got for w in wants)


@dataclass
class Row:
    form: str
    caption: str
    found: bool
    rules: bool
    assisted: bool
    asked: bool
    cached: bool


def evaluate(labels_path: Path = LABELS, cache_path: Path = CACHE,
             model: str = providers.ASSIST_MODELS[providers.DEFAULT_ASSIST_PROVIDER], work: Path | None = None,
             forms: set[str] | None = None) -> list[Row]:
    spec = json.loads(labels_path.read_text())["forms"]
    cache = ask.load_cache(cache_path)
    rows: list[Row] = []
    with tempfile.TemporaryDirectory() as tmp:
        for form, entry in spec.items():
            if forms and form not in forms:
                continue
            pdf = checkout.sample(REPO, entry["pdf"])
            if not pdf.exists():
                print(f"skip {form}: {pdf} is missing", file=sys.stderr)
                continue
            out = (work or Path(tmp)) / form
            convert(pdf, out, page_images=False, jobs=1)
            rules_doc = Document.from_json((out / DOCUMENT_JSON).read_text())
            assisted_doc = Document.from_json((out / DOCUMENT_JSON).read_text())
            sha = str(rules_doc.meta.get("sha256", ""))
            asked = {q.node: label_assist.cache_key(sha, q, model) in cache
                     for q in label_assist.questions(rules_doc)}
            # Replay only: provider None never calls out; image_for is never reached.
            label_assist.assist(assisted_doc, None, cache_path, model=model,
                                image_for=lambda q: (_ for _ in ()).throw(AssertionError("no call")))
            for label in entry["labels"]:
                a, b = find(rules_doc, label), find(assisted_doc, label)
                rows.append(Row(form, label["caption"], a is not None,
                                a is not None and correct(a, label),
                                b is not None and correct(b, label),
                                a is not None and a.id in asked,
                                a is not None and asked.get(a.id, False)))
    return rows


def record(labels_path: Path = LABELS, cache_path: Path = CACHE, forms: set[str] | None = None,
           **assist_options) -> dict[str, dict]:
    """Convert each labelled form with the assist on and REAL model calls (through `assist_provider`, a local
    Ollama server by default), merge the answers into the cache file and return the
    per-form assist stats. Entries recorded under an older prompt version are dropped."""
    spec = json.loads(labels_path.read_text())["forms"]
    cache = {k: v for k, v in ask.load_cache(cache_path).items()
             if v.get("prompt_version") == label_assist.PROMPT_VERSION}
    stats = {}
    with tempfile.TemporaryDirectory() as tmp:
        for form, entry in spec.items():
            if forms and form not in forms:
                continue
            pdf = checkout.sample(REPO, entry["pdf"])
            out = Path(tmp) / form
            summary = convert(pdf, out, page_images=False, jobs=1, assist_labels=True, **assist_options)
            stats[form] = summary.label_assist
            cache.update(ask.load_cache(out / ASSIST_CACHE))
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


def report(rows: list[Row], model: str) -> str:
    out = [f"label assist evaluation (model key: {model})"]
    forms = list(dict.fromkeys(r.form for r in rows))
    for form in [*forms, None]:
        sel = [r for r in rows if form is None or r.form == form]
        n = len(sel)
        rules = sum(r.rules for r in sel)
        asked = sum(r.asked for r in sel)
        cached = sum(r.cached for r in sel)
        line = (f"{form or 'ALL':30} {n:3} labelled  rules only {rules}/{n} = {rules / max(n, 1):.0%}"
                f"  | labelled fields the assist would be asked about: {asked}, with a cached answer: {cached}")
        if cached:
            both = sum(r.assisted for r in sel)
            line += f"  | rules + cached assist {both}/{n} = {both / n:.0%}"
        else:
            line += "  | rules + assist: not measured (no cached answers)"
        out.append(line)
    missing = [r for r in rows if not r.found]
    if missing:
        out.append("labels that matched no field: " + ", ".join(f"{r.form}:{r.caption}" for r in missing))
    wrong = [r for r in rows if not r.rules]
    out.append("rules get wrong: " + "; ".join(
        f"{r.form}:{r.caption}{' (asked)' if r.asked else ' (never asked)'}" for r in wrong))
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
                        help="make REAL calls through --provider (a local Ollama server by default) for every labelled form and merge "
                             "the answers into --cache before scoring")
    args = parser.parse_args()
    args.model = providers.resolve_model(args.provider, args.model)
    if args.record:
        import time
        t = time.time()
        stats = record(args.labels, args.cache, set(args.form or []), assist_provider=args.provider, assist_model=args.model)
        print(f"recorded in {time.time() - t:.0f}s: " + json.dumps(stats))
    print(report(evaluate(args.labels, args.cache, args.model, forms=set(args.form or [])), args.model))


if __name__ == "__main__":
    main()
