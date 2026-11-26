"""Measure the unread-crop rule on real documents: how many OCR paragraphs the page-vision model
calls junk (handwriting, noise, figure label), how many of those a rule also doubts (the
eligible ones, each an `unread-crop` proposal), and a crop picture of each eligible paragraph to
judge by eye whether hiding it would hide real text.

    .venv/bin/python scripts/measure_vision_unread.py OUT [--doc NAME=PDF ...] [--ref SLUG=DOC_JSON ...]

Each document is converted with the CLI defaults (what docs:build uses) plus
--assist-page-vision --assist-page-vision-unread, every page asked (no page cap), into OUT/<name>.
A document with no OCR paragraph in its reference conversion (--ref, the published build's
document.json) is not converted: the rule needs an OCR paragraph, so it has nothing eligible.
OUT/summary.json holds the counts; OUT/crops/<name>-<n>.png each eligible paragraph on its page.
The model runs on the local Ollama server, under reflow's GPU lease.

--ocr-pages-only converts in this process and asks the model only about the pages that hold an
OCR paragraph in the reference build; every other page is answered "as reflow reads it", which
makes no proposal. The count is the same (a crop needs an OCR paragraph on its page), and a
2,600-page directory with OCR on 2 pages takes minutes instead of hours.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"   # this checkout's converter, not the venv's
sys.path.insert(0, str(SRC))

from reflow.names import DOCUMENT_JSON, PROPOSALS_JSON, WORKING_PDF  # noqa: E402

JUNK = {"handwriting", "noise", "figure_label"}
PAD = 36   # pt of page around a crop picture


def walk(node: dict):
    yield node
    for child in node.get("children", ()):
        yield from walk(child)


def is_ocr_para(node: dict) -> bool:
    return node.get("role") == "p" and ("ocr" in node.get("attrs", {}) or node.get("source") == "ocr")


def ocr_paras(doc_json: Path) -> int:
    return sum(map(is_ocr_para, walk(json.loads(doc_json.read_text())["root"])))


def crop(pdf: Path, page: int, bbox: list[float], out: Path) -> None:
    import pypdfium2 as pdfium
    from PIL import ImageDraw

    doc = pdfium.PdfDocument(pdf)
    try:
        p = doc[page - 1]
        w, h = p.get_width(), p.get_height()
        scale = 2.0
        image = p.render(scale=scale).to_pil().convert("RGB")
    finally:
        doc.close()
    x0, y0, x1, y1 = bbox
    box = (max(0, x0 - PAD), max(0, y0 - PAD), min(w, x1 + PAD), min(h, y1 + PAD))
    draw = ImageDraw.Draw(image)
    draw.rectangle([v * scale for v in bbox], outline=(220, 30, 30), width=3)
    image.crop(tuple(round(v * scale) for v in box)).save(out)


class OcrPagesOnly:
    """The real provider for `pages`; elsewhere every block read as reflow reads it."""

    def __init__(self, inner, pages: set[int]) -> None:
        self.inner, self.pages, self.cache_id, self.asked = inner, pages, inner.cache_id, 0

    def check(self, question, image):
        if question.page in self.pages:
            self.asked += 1
            return self.inner.check(question, image)
        return {"blocks": [{"id": b.id, "role": b.role.value} for b in question.blocks],
                "complexity": {"dense_table": False, "diagram": False, "poor_scan": False,
                               "handwriting": False}}


def convert_in_process(pdf: str, out: Path, model: str, stage_cache: str | None,
                       pages: set[int]) -> int:
    from reflow.analyze import page_vision as pv
    from reflow.convert import convert

    provider = OcrPagesOnly(pv.make_provider(model), pages)
    convert(Path(pdf), out, page_images=False, assist_page_vision=True,
            assist_page_vision_unread=True, assist_page_vision_model=model,
            assist_page_vision_max_pages=100000, page_vision_provider=provider,
            stage_cache=Path(stage_cache) if stage_cache else None)
    return provider.asked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--doc", action="append", default=[], help="NAME=PDF")
    ap.add_argument("--ref", action="append", default=[], help="NAME=document.json of the build")
    ap.add_argument("--stage-cache", default=None)
    ap.add_argument("--model", default="qwen3-vl:8b")
    ap.add_argument("--ocr-pages-only", action="store_true")
    args = ap.parse_args()
    refs = dict(r.split("=", 1) for r in args.ref)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "crops").mkdir(exist_ok=True)
    summary: dict[str, dict] = {}
    for spec in args.doc:
        name, pdf = spec.split("=", 1)
        if name in refs and ocr_paras(Path(refs[name])) == 0:
            summary[name] = {"ocr_paragraphs": 0, "converted": False}
            print(f"{name}: no OCR paragraph, nothing eligible", flush=True)
            continue
        out = args.out / name
        cmd = [sys.executable, "-c", "import sys; from reflow.cli import app; sys.argv[0] = "
               "'reflow'; app()", "convert", pdf, "-o", str(out),
               "--assist-page-vision", "--assist-page-vision-unread",
               "--assist-page-vision-model", args.model,
               "--assist-page-vision-max-pages", "100000"]
        if args.stage_cache:
            cmd += ["--stage-cache", args.stage_cache]
        asked = None
        start = time.monotonic()
        if args.ocr_pages_only:
            ocr_pages = {n.get("page") for n in walk(json.loads(Path(refs[name]).read_text())["root"])
                         if is_ocr_para(n)}
            asked = convert_in_process(pdf, out, args.model, args.stage_cache, ocr_pages)
            done = subprocess.CompletedProcess([], 0)
        else:
            env = {**os.environ, "PYTHONPATH": os.pathsep.join(
                filter(None, [str(SRC), os.environ.get("PYTHONPATH")]))}
            done = subprocess.run(cmd, capture_output=True, text=True, env=env)
        took = time.monotonic() - start
        if done.returncode:
            summary[name] = {"error": done.stderr[-2000:]}
            print(f"{name}: FAILED\n{done.stderr[-2000:]}", flush=True)
            continue
        doc = json.loads((out / DOCUMENT_JSON).read_text())
        record = json.loads((out / PROPOSALS_JSON).read_text())
        nodes = {n.get("id"): n for n in walk(doc["root"])}
        paras = [n for n in nodes.values() if is_ocr_para(n)]
        junk = [n for n in paras if any(f.get("code") == "vision-block"
                                        and f.get("data", {}).get("word") in JUNK
                                        for f in n.get("facts", ()))]
        crops = [p for p in record["proposals"] if p["change"] == "unread-crop"]
        for i, p in enumerate(crops, 1):
            node = nodes[p["node"]]
            p["full_text"] = node.get("text")
            p["bbox"] = node.get("bbox")
            p["picture"] = f"crops/{name}-{i:03d}.png"
            crop(out / WORKING_PDF, p["page"], node["bbox"], args.out / p["picture"])
        summary[name] = {"ocr_paragraphs": len(paras),
                         "ocr_paragraphs_called_junk": len(junk),
                         "eligible": len(crops), "seconds": round(took),
                         "pages_asked_of_the_model": asked if asked is not None
                         else record["stats"]["calls"],
                         "stats": record["stats"], "crops": crops}
        print(f"{name}: {len(paras)} OCR paragraphs, {len(junk)} called junk, {len(crops)} "
              f"eligible; {record['stats']} in {took:.0f}s", flush=True)
        (args.out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
