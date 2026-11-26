"""How well the form repair pass reads what OCR left unread, against two controls on the
same local model: the current whole-page transcription prompt, and the targeted prompt on the
whole page.

The pages are born-digital pages turned into 200 dpi image-only scans, so their own text layer
is the truth and OCR has to read them again. Reflow converts each scan (OCR included, no pass),
and form_regions routes the gaps OCR left. The truth of a page is the native words inside its
regions; nothing about the truth reaches the router, the prompts or the checks. Two pages are
kept as they are: an old OCR layer (maternal-risk-assessment p1) and a blank page (its p2).

Conditions. Every one that asks a model asks the same model, at temperature 0, seed 0 and
num_ctx 8192:

- ocr: no model; what a conversion without the pass holds in the regions (main's output).

- page/current: assist.TRANSCRIPTION_PROMPT on the whole page (one call per page with a gap).
- page/targeted: form_repair's prompt and schema for each task the page's regions have, on
  the whole page (one call per task per page).
- region/targeted: form_repair.repair on the regions, with its checks and one reread; scored on
  the readings that passed (`checked`) and on every first reading (`raw`).

Scores per condition, summed over pages (a page with no truth adds only cost):

- complete: the share of the regions' native words the output holds (multisets, case and
  punctuation folded).
- added: output words found nowhere in the page's native text (invented words).
- field errors: the page's numbered fields inside its regions (`24. Procedure Date`) the output
  misses or labels wrongly (word F1 under 0.6), and the field numbers it gives that are printed
  nowhere on the page.
- unresolved: regions no reading passed (region/targeted only).
- calls, seconds and generated tokens.

Usage: eval_form_repair.py [--model qwen3-vl:8b] [--pages slug:page ...] [--out DIR]
Check `nvidia-smi` and `ollama ps` before a run: the seconds include any model load.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import pypdfium2 as pdfium  # noqa: E402

import checkout  # noqa: E402
from reflow import geometry, providers  # noqa: E402
from reflow.analyze import assist  # noqa: E402
from reflow.analyze import form_regions as fr  # noqa: E402
from reflow.analyze import form_repair as rp  # noqa: E402
from reflow.analyze.form_regions import Task  # noqa: E402
from reflow.analyze.vision import OllamaVision, ProviderError, VisionImage  # noqa: E402
from reflow.convert import convert  # noqa: E402
from reflow.model import Document  # noqa: E402
from reflow.names import DOCUMENT_JSON, WORKING_PDF  # noqa: E402
from reflow.text_quality import squash  # noqa: E402

SITE = "samples/azblue/site"
# (slug, page, scanned): the development page first, then the held-out ones.
PAGES = [
    ("dental-claim-form", 1, True),               # development: the issue's repro
    ("nurse-on-call-flyer", 1, True),             # small print
    ("tier-1a-rx-list", 1, True),                 # full-width text over a drug table
    ("employee-application-spanish", 1, True),    # a Spanish form
    ("ma-vision-flyer-bilingual", 2, True),       # English and Spanish
    ("deceased-member-affidavit", 1, True),       # a short form
    ("maternal-risk-assessment", 1, False),       # an old OCR layer, kept
    ("maternal-risk-assessment", 2, False),       # a blank page
]
DPI = 200
SCALE = 2.0
NUM_CTX = 8192
PAGE_TOKENS = 4096
WORD = re.compile(r"[\w$%#/&'’-]+", re.U)
FIELD = re.compile(r"(?<![\w.])(\d{1,3}[a-z]?)\.\s+(.*?)(?=(?<![\w.])\d{1,3}[a-z]?\.\s|$)")


def words(text: str) -> list[str]:
    return [w.strip("'’-").casefold() for w in WORD.findall(text) if w.strip("'’-")]


def fields_of(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        for m in FIELD.finditer(squash(line)):
            out.setdefault(m.group(1), words(m.group(2)))
    return out


def f1(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    common = sum((Counter(a) & Counter(b)).values())
    return 2 * common / (len(a) + len(b))


def native_lines(pdf: Path, number: int, boxes: list) -> tuple[str, str]:
    """The page's native text inside the boxes (as lines) and the whole page's."""
    doc = pdfium.PdfDocument(pdf)
    try:
        texts = fr.page_texts(doc[number - 1])
    finally:
        doc.close()
    inside = [t for t in texts if any(geometry.centre_in(t.bbox, b) for b in boxes)]
    return "\n".join(fr.text_lines(inside)), "\n".join(fr.text_lines(texts))


def scan(src: Path, number: int, dest: Path) -> Path:
    if dest.exists():
        return dest
    doc = pdfium.PdfDocument(src)
    try:
        image = doc[number - 1].render(scale=DPI / 72).to_pil().convert("L")
    finally:
        doc.close()
    image.save(dest, "PDF", resolution=DPI)
    return dest


def page_png(pdf: Path, number: int, box=None) -> bytes:
    doc = pdfium.PdfDocument(pdf)
    try:
        image = doc[number - 1].render(scale=SCALE).to_pil()
    finally:
        doc.close()
    if box is not None:
        image = image.crop(tuple(round(v * SCALE) for v in box))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


class Timed:
    def __init__(self, model: str) -> None:
        self.vision = OllamaVision(model)
        self.calls, self.seconds, self.tokens = 0, 0.0, 0

    def ask(self, system: str, prompt: str, image: bytes, schema: dict, tokens: int) -> str:
        start = time.monotonic()
        self.calls += 1
        try:
            reply = self.vision.ask(system, prompt, VisionImage(image, "image/png"), schema,
                                    tokens, NUM_CTX)
        finally:
            self.seconds += time.monotonic() - start
        self.tokens += reply.usage.tokens_out
        if reply.truncated:
            raise ProviderError("token limit")
        return reply.text


class CountingReader(rp.OllamaRegionReader):
    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.calls, self.seconds, self.tokens = 0, 0.0, 0
        ask = self.vision.ask

        def counted(*args, **kwargs):
            reply = ask(*args, **kwargs)
            self.tokens += reply.usage.tokens_out
            return reply

        self.vision.ask = counted

    def read(self, task, prompt, image):
        self.calls += 1
        start = time.monotonic()
        try:
            raw = super().read(task, prompt, image)
        finally:
            self.seconds += time.monotonic() - start
        return raw


def as_text(task: Task, raw: str) -> str:
    try:
        reading = rp.parse(task, raw)
    except rp.Rejected:
        return ""
    if reading.fields:
        return "\n".join(f"{f.id}. {f.label}" if f.id else f.label for f in reading.fields)
    return "\n".join(reading.lines)


def score(out: str, truth: str, page_text: str) -> dict:
    got, want, page = words(out), words(truth), Counter(words(page_text))
    hit = sum((Counter(got) & Counter(want)).values())
    added = sum(1 for w in got if w not in page)
    errors = 0
    truth_fields, got_fields = fields_of(truth), fields_of(out)
    for fid, label in truth_fields.items():
        if fid not in got_fields or f1(label, got_fields[fid]) < 0.6:
            errors += 1
    page_fields = fields_of(page_text)
    numbers = [fid for fid in got_fields if fid not in page_fields]   # printed nowhere
    return {"truth": len(want), "hit": hit, "added": added, "fields": len(truth_fields),
            "field_errors": errors + len(numbers) * bool(truth_fields)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", default=providers.FORM_REPAIR_MODEL)
    ap.add_argument("--out", type=Path, default=REPO / ".worktree/eval-form-repair")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "cache.json").unlink(missing_ok=True)   # every run asks: the cost is measured
    site = checkout.main_checkout(REPO) / SITE
    totals: dict[str, Counter] = {c: Counter() for c in ("ocr", "page/current", "page/targeted",
                                                         "region/raw", "region/checked")}
    page_ctl, page_tgt = Timed(args.model), Timed(args.model)
    reader = CountingReader(args.model)
    rows = []
    for slug, number, scanned in PAGES:
        original = site / f"{slug}.pdf"
        name = f"{slug}-p{number}" + ("-scan" if scanned else "")
        pdf = scan(original, number, args.out / f"{name}.pdf") if scanned else original
        out = args.out / name
        if not (out / DOCUMENT_JSON).exists():
            convert(pdf, out, page_images=False)
        doc = Document.from_dict(json.loads((out / DOCUMENT_JSON).read_text()))
        working = out / WORKING_PDF
        page_no = 1 if scanned else number
        routing = fr.route(doc, lambda n, w=working: fr.read_page(w, n))
        regions = [r for r in routing.regions if r.page == page_no]
        boxes = [r.bbox for r in regions]
        truth, page_text = native_lines(original, number, boxes) if scanned else ("", "")
        row = {"page": name, "regions": len(regions),
               "tasks": sorted({r.task.value for r in regions}),
               "skipped": Counter(s.reason for s in routing.skipped if s.page == page_no)}
        # What a conversion without the pass holds there: the text layer's words in the regions.
        outputs: dict[str, str] = {"ocr": "\n".join(line for r in regions for line in r.known)}
        if regions:
            whole = page_png(working, page_no)
            try:
                reply = page_ctl.ask(assist.TRANSCRIPTION_PROMPT, "Transcribe this image.", whole,
                                     assist.TRANSCRIPTION_SCHEMA, PAGE_TOKENS)
                outputs["page/current"] = assist.parse_transcription(reply).text
            except ProviderError as e:
                outputs["page/current"] = ""
                row["page/current error"] = str(e)
            parts = []
            for task in sorted({r.task for r in regions}):
                try:
                    parts.append(as_text(task, page_tgt.ask(rp.SYSTEM, rp.PROMPTS[task], whole,
                                                            rp.SCHEMAS[task], PAGE_TOKENS)))
                except ProviderError as e:
                    row[f"page/targeted {task.value} error"] = str(e)
            outputs["page/targeted"] = "\n".join(parts)
            outcomes, stats = rp.repair(
                regions, reader, args.out / "cache.json", sha=f"eval:{name}", model=args.model,
                image_for=lambda r, w=working: page_png(w, r.page, r.bbox))
            cache = json.loads((args.out / "cache.json").read_text())
            first = [as_text(o.region.task, cache.get(rp.cache_key(
                f"eval:{name}", o.region, args.model, 0), {}).get("reply", ""))
                for o in outcomes]
            outputs["region/raw"] = "\n".join(first)
            outputs["region/checked"] = "\n".join(
                "\n".join(f"{f.id}. {f.label}" if f.id else f.label for f in o.reading.fields)
                or "\n".join(o.reading.lines) for o in outcomes if o.reading is not None)
            row["unresolved"] = stats["unresolved"] + stats["skipped"]
            row["outcomes"] = [(o.region.task.value, [round(v) for v in o.region.bbox],
                                o.status.value, [p.value for p in o.problems]) for o in outcomes]
            totals["region/checked"]["unresolved"] += row["unresolved"]
        for cond, text in outputs.items():
            s = score(text, truth, page_text) if scanned else {
                "added": 0, "truth": 0, "hit": 0, "fields": 0, "field_errors": 0}
            row[cond] = s
            totals[cond].update(s)
        (args.out / f"{name}.outputs.json").write_text(
            json.dumps({"truth": truth, **outputs}, ensure_ascii=False, indent=1))
        rows.append(row)
        print(json.dumps(row, default=dict, ensure_ascii=False))
    for cond, ctl in (("ocr", None), ("page/current", page_ctl), ("page/targeted", page_tgt),
                      ("region/raw", reader), ("region/checked", reader)):
        t = totals[cond]
        if ctl is not None:
            t.update({"calls": ctl.calls, "seconds": round(ctl.seconds), "tokens": ctl.tokens})
    print(f"\nmodel {args.model}, temperature 0, seed 0, num_ctx {NUM_CTX}")
    print(f"{'condition':16} {'complete':>14} {'added':>6} {'field errors':>13} "
          f"{'unresolved':>10} {'calls':>5} {'seconds':>7} {'tokens':>6}")
    for cond, t in totals.items():
        complete = f"{t['hit']}/{t['truth']} = {t['hit'] / max(t['truth'], 1):.0%}"
        fields = f"{t['field_errors']}/{t['fields']}"
        unresolved = str(t["unresolved"]) if cond == "region/checked" else "-"
        print(f"{cond:16} {complete:>14} {t['added']:>6} {fields:>13} {unresolved:>10} "
              f"{t['calls']:>5} {t['seconds']:>7} {t['tokens']:>6}")


if __name__ == "__main__":
    main()
