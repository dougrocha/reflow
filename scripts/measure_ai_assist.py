"""Run the three opt-in assist examples on one GPU, then verify cached reconversion.

Run from the repo root with PYTHONPATH=src and the project's Python. Nothing is downloaded
or installed. The source samples are read only; page subsets and output live under --output.
Timings are single-run wall times, with model loading measured separately. The report keeps
the complete drafts for checking against the selected original pages.
"""

import argparse
import json
import time
from pathlib import Path

import pikepdf

from reflow.analyze.local_model import LocalModel
from reflow.convert import convert
from reflow.model import Document
from reflow.names import DOCUMENT_JSON

CASES = [("01-chem-math", 1), ("09-scanned-budget-handwriting", 4),
         ("11-timelines-difficult-graphics", 4)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if any((args.output / (name + "-assist")).exists() for name, _ in CASES):
        parser.error("choose a new --output directory so the first conversions have no cache")
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"runs_per_case": 1, "model": LocalModel().cache_id, "cases": []}
    start = time.monotonic()
    try:
        LocalModel().load()
        report["model_load_seconds"] = round(time.monotonic() - start, 3)
        print("Model loaded", report["model_load_seconds"], "seconds", flush=True)
        for name, number in CASES:
            selected = args.output / (name + ".pdf")
            with pikepdf.open(args.samples / (name + ".pdf")) as original:
                pdf = pikepdf.new()
                pdf.pages.append(original.pages[number - 1])
                pdf.save(selected)
            out = args.output / (name + "-assist")
            start = time.monotonic()
            summary = convert(selected, out, assist="local", page_images=False, jobs=1)
            wall = round(time.monotonic() - start, 3)
            doc = Document.from_json((out / DOCUMENT_JSON).read_text())
            drafts = [n for n in doc.root.walk() if n.attrs.get("provenance") == "ai-drafted"]
            case = {"sample": name, "source_page": number, "output": str(out),
                    "wall_seconds": wall, "seconds": summary.seconds,
                    "assist": doc.meta.get("ai_assist"), "alt_text": summary.alt_text,
                    "alt_text_errors": summary.alt_text_errors,
                    "drafts": [n.to_dict() for n in drafts]}
            report["cases"].append(case)
            print(name, "wall", wall, "seconds", summary.seconds,
                  "assist", case["assist"], "alt", summary.alt_text, flush=True)
            print("\n".join(n.text or n.attrs.get("alt", "") for n in drafts), flush=True)
            start = time.monotonic()
            convert(selected, out, assist="local", page_images=False, jobs=1)
            cached = Document.from_json((out / DOCUMENT_JSON).read_text())
            case["reconversion_wall_seconds"] = round(time.monotonic() - start, 3)
            case["reconversion_assist"] = cached.meta.get("ai_assist")
            print("Reconversion", case["reconversion_wall_seconds"],
                  case["reconversion_assist"], flush=True)
            (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
        raise


if __name__ == "__main__":
    main()
