"""Benchmark the transcription routes for hard scans and handwriting.

Each page of PAGES is cut out of its sample into a one-page PDF, then converted once per route
through `reflow.convert.convert`, into a fresh output folder:

- `none`: the default conversion, OCRmyPDF with Tesseract (`--ocr auto`). Its text is OCR's.
- `local`: `--assist local` with the transcriber swapped for Qwen3-VL-8B in-process.
- `ollama:TAG`: `--assist local` with the transcriber swapped for TAG through Ollama, asked with
  the same TRANSCRIPTION_PROMPT and the same JSON answer (assist.OllamaTranscriber).
  `ollama:qwen3-vl:8b` is what `--assist local` asks (providers.TRANSCRIPTION_MODEL).
- `ocr:TAG`: the same, but asked as OpenBMB documents MiniCPM-OCR: the image and the prompt
  `Text Recognition:`, with a plain-text answer (assist.OllamaTranscriber, `ocr_prompt`).

The conversion sends what `--assist local` sends: assist.regions picks the crops. After it, the
whole page (rendered at assist.SCALE, as regions renders a scan) goes to the same transcriber
once more, unless the pipeline already sent it, so every route reads every page whole. The score
uses the whole-page text.

    scripts/bench_transcription.py run ROUTE [--work DIR] [--host URL]
    scripts/bench_transcription.py score [--work DIR] [--truth DIR] [--json PATH]
    scripts/bench_transcription.py compare [--work DIR] [--json PATH]

Run one route per process, so the in-process model is freed when the process ends. nvidia-smi
memory.used is sampled through the run; the video memory is the peak above the idle reading
taken before the model loads. The harness stops only the Ollama model it loaded.

`score` compares each route's whole-page text with the hand-checked transcriptions in --truth
(`<page>.printed.txt`, one printed line per line; `<page>.hand.txt`, the handwriting, and
`<page>.notes.txt`, which the score does not read). It reports the character error rate on the
printed lines, and the words that match nothing on the page, which are the junk or made-up
text a reader would see. Handwriting is graded by hand from the same outputs.

`compare` grades the pages of #291 by their page-check specs instead: for each reading (reflow's
transcript as a reader gets it, by default and with each route's drafts, each route's whole-page
text and the site's Haiku prompt-only build, where it exists), the #291 words
found and the known junk left.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from benchlib import (  # noqa: E402
    GpuSampler,
    fresh_route,
    gpu_processes,
    idle_mib,
    ollama_host,
    ollama_load,
    ollama_loaded,
    ollama_stop,
    route_dir,
    track_usage,
)

import checkout  # noqa: E402
from reflow.names import DOCUMENT_JSON  # noqa: E402

MAIN = checkout.main_checkout(REPO)   # the gitignored samples and research live there
SAMPLES = MAIN / "samples"
SPECS = REPO / "tests" / "pages" / "specs"

# page id -> (sample under samples/, 1-based page). The hard pages, then the other pages #291 names.
PAGES = {
    "chem-math-p1": ("docaccess/01-chem-math.pdf", 1),
    "permit-p1": ("docaccess/02-building-permit-applications.pdf", 1),
    "permit-p3": ("docaccess/02-building-permit-applications.pdf", 3),
    "legislation-p5": ("docaccess/05-legislation-tracked-changes.pdf", 5),
    "budget-p4": ("docaccess/09-scanned-budget-handwriting.pdf", 4),
    "budget-p5": ("docaccess/09-scanned-budget-handwriting.pdf", 5),
    "carmel-p4": ("docaccess/11-timelines-difficult-graphics.pdf", 4),
    "brochure-p2": ("docaccess/08-multi-column-brochure.pdf", 2),
    "legislation-p1": ("docaccess/05-legislation-tracked-changes.pdf", 1),
    "brochure-p1": ("docaccess/08-multi-column-brochure.pdf", 1),
    "remit-p1": ("azblue/site/remittance-advice-medical.pdf", 1),
    "claim-p1": ("azblue/site/claim-form-instruction-booklet.pdf", 1),
    "claim-p4": ("azblue/site/claim-form-instruction-booklet.pdf", 4),
    "maternal-p1": ("azblue/site/maternal-risk-assessment.pdf", 1),
}

# page id -> (page-check spec, site slug).
# `compare` grades each reading with the spec's #291 words.
COMPARED = {
    "chem-math-p1": ("docaccess-chem-math-1", "da-chem-math"),
    "permit-p1": ("docaccess-permit-1", "da-building-permit-applications"),
    "legislation-p1": ("docaccess-legislation-1", "da-legislation-tracked-changes"),
    "legislation-p5": ("docaccess-legislation-5", "da-legislation-tracked-changes"),
    "budget-p4": ("docaccess-budget-4", "da-scanned-budget-handwriting"),
    "brochure-p1": ("docaccess-brochure-1", "da-multi-column-brochure"),
    "remit-p1": ("az-remittance-advice-scan-1", "az-remittance-advice-medical"),
    "claim-p1": ("az-claim-form-instruction-booklet-1", "az-claim-form-instruction-booklet"),
    "claim-p4": ("az-claim-form-instruction-booklet-4", "az-claim-form-instruction-booklet"),
    "maternal-p1": ("az-maternal-risk-assessment-1", "az-maternal-risk-assessment"),
}


def page_pdf(work: Path, page: str) -> Path:
    import pikepdf

    out = work / "pages" / f"{page}.pdf"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        sample, number = PAGES[page]
        with pikepdf.open(SAMPLES / sample) as original:
            pdf = pikepdf.new()
            pdf.pages.append(original.pages[number - 1])
            pdf.save(out)
    return out


def whole_page(pdf: Path) -> bytes:
    """The page as assist.regions renders a scan for the model: SCALE, PNG, uncropped."""
    import io

    import pypdfium2 as pdfium

    from reflow.analyze.assist import SCALE

    doc = pdfium.PdfDocument(pdf)
    try:
        buf = io.BytesIO()
        doc[0].render(scale=SCALE).to_pil().save(buf, "PNG")
        return buf.getvalue()
    finally:
        doc.close()


def forced_tesseract(pdf: Path, out: Path) -> str:
    """The page's text after `ocrmypdf --force-ocr -l eng` (the languages of LANGS_DEFAULT)."""
    import subprocess

    import pypdfium2 as pdfium

    subprocess.run(["ocrmypdf", "--force-ocr", "-q", "-l", "eng", str(pdf), str(out)],
                   check=True, capture_output=True)
    doc = pdfium.PdfDocument(out)
    try:
        textpage = doc[0].get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            textpage.close()
    finally:
        doc.close()


def make_transcriber(route: str, host: str):
    from reflow.analyze.assist import OllamaTranscriber
    from reflow.analyze.enrich import make_provider

    kind, _, tag = route.partition(":")
    if kind == "local" and not tag:
        return make_provider("local")
    if kind in ("ollama", "ocr") and tag:
        transcriber = OllamaTranscriber(tag, host=host, ocr_prompt=kind == "ocr")
        # The conversion closes its transcriber after the assist step; the harness unloads
        # the Ollama model itself, once every page is read.
        transcriber.close = lambda: None
        return transcriber
    raise SystemExit("ROUTE is none, local, ollama:TAG or ocr:TAG")


def run(route: str, work: Path, host: str) -> None:
    import reflow.convert as rc
    import reflow.pipeline as rp
    from reflow.analyze.vision import ProviderError
    from reflow.model import Document, Source

    base, record = fresh_route(work, route)
    kind, _, tag = route.partition(":")
    transcriber = None if route == "none" else make_transcriber(route, host)
    calls: list[dict] = []
    current = {"page": "", "phase": "pipeline"}

    if transcriber is not None:
        from reflow.analyze.assist import ink

        transcribe = transcriber.transcribe
        usage = track_usage(transcriber.client)

        def timed(image: bytes, media_type: str, told: str = ""):
            t0 = time.perf_counter()
            entry = {"page": current["page"], "phase": current["phase"],
                     "sha1": hashlib.sha1(image).hexdigest(), "bytes": len(image),
                     "ink": ink(image), "told": told}
            try:
                result = transcribe(image, media_type, told)
                entry.update(status="ok", text=result.text, handwritten=result.handwritten)
                return result
            except ProviderError as e:
                entry.update(status="failed", error=str(e)[:300])
                raise
            finally:
                entry["seconds"] = time.perf_counter() - t0
                entry["stats"] = usage["last"]
                calls.append(entry)

        transcriber.transcribe = timed

    real = rp.make_provider

    def make_provider(name, *a, **kw):
        # convert makes an "ollama" provider only for --assist local (alt text stays off)
        return transcriber if name == "ollama" and transcriber is not None else real(name, *a, **kw)

    rp.make_provider = make_provider
    sampler = GpuSampler("memory.used", 250)
    try:
        record["idle_mib"] = idle_mib(sampler, host, tag if kind in ("ollama", "ocr") else None)
        t_load = time.time()
        if kind in ("ollama", "ocr"):
            record["ollama_load_s"] = ollama_load(host, tag)
        elif kind == "local":
            transcriber.client.load()
        record["first_load_s"] = time.time() - t_load
        print(f"{route}: first load {record['first_load_s']:.1f} s", flush=True)

        pages = {}
        for page in PAGES:
            pdf = page_pdf(work, page)
            current.update(page=page, phase="pipeline")
            out = base / page
            n0, t0 = len(calls), time.perf_counter()
            summary = rc.convert(pdf, out, assist="none" if route == "none" else "local",
                                 page_images=False, jobs=1)
            entry = {"seconds": time.perf_counter() - t0, "stages": dict(summary.seconds),
                     "pipeline_calls": len(calls) - n0}
            doc = Document.from_json((out / DOCUMENT_JSON).read_text(encoding="utf-8"))
            entry["assist"] = doc.meta.get("ai_assist")
            entry["ocr_text"] = [n.text for n in doc.root.walk()
                                 if n.text and n.source != Source.LLM]
            entry["drafts"] = [{"text": n.text, "reason": n.attrs.get("assist_reason"),
                                "handwritten": n.attrs.get("handwritten"),
                                "bbox": list(n.bbox or ())}
                               for n in doc.root.walk() if n.attrs.get("provenance") == "ai-drafted"]
            if transcriber is None:
                # Four of the pages keep a vendor's text layer under --ocr auto; read every
                # page with Tesseract too, as OCRmyPDF does when it redoes a page.
                t1 = time.perf_counter()
                entry["tesseract_forced"] = forced_tesseract(pdf, base / f"{page}.forced.pdf")
                entry["tesseract_forced_seconds"] = time.perf_counter() - t1
            if transcriber is not None:
                image = whole_page(pdf)
                sha = hashlib.sha1(image).hexdigest()
                sent = next((c for c in calls[n0:] if c["sha1"] == sha), None)
                if sent is None:
                    current["phase"] = "whole"
                    try:
                        transcriber.transcribe(image, "image/png")
                    except ProviderError:
                        pass
                    sent = calls[-1]
                entry["whole"] = {k: sent.get(k) for k in
                                  ("phase", "status", "text", "handwritten", "error", "seconds")}
            pages[page] = entry
            print(f"{route}: {page}: {entry['seconds']:.1f} s, {entry['pipeline_calls']} "
                  f"pipeline calls, assist {entry['assist']}", flush=True)
        t_end = time.time()
        record["pages"] = pages
        record["peak_mib"] = max(sampler.values(t_load, t_end), default=float("nan"))
        record["peak_above_idle_mib"] = record["peak_mib"] - record["idle_mib"]
        if kind in ("ollama", "ocr"):
            ps = ollama_loaded(host, tag) or {}
            record.update(ps_size=ps.get("size"), ps_size_vram=ps.get("size_vram"),
                          ps_context=ps.get("context_length"))
        elif kind == "local":
            import torch
            record["torch_max_reserved_mib"] = torch.cuda.max_memory_reserved() / 2**20
        record["calls"] = calls
    finally:
        sampler.close()
        rp.make_provider = real
        if kind in ("ollama", "ocr"):
            ollama_stop(host, tag)
        record["gpu_processes_after"] = gpu_processes()
        (base / "run.json").write_text(json.dumps(record, indent=1, ensure_ascii=False))


# -- score -----------------------------------------------------------------------------------

FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                      "—": "-", "−": "-", " ": " ", "•": "-"})


LATEX = {r"\Delta": "Δ", r"\times": "×", r"\circ": "°", r"\cdot": "·", r"\%": "%"}


def normalize(text: str) -> str:
    """Compare text, not formatting: NFKC, case folded, one kind of quote and dash, no markup
    (Markdown heading hashes, emphasis and table pipes, HTML or OTSL tags, LaTeX commands and
    braces), whitespace collapsed."""
    text = unicodedata.normalize("NFKC", text).translate(FOLD)
    for command, char in LATEX.items():
        text = text.replace(command, char)
    text = re.sub(r"\\[A-Za-z]+", " ", text)                  # \frac, \text, \mathrm, ...
    text = re.sub(r"</?[A-Za-z][^>]{0,40}>", " ", text)       # <fcel>, <nl>, <br>, ...
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    text = re.sub(r"[{}^_]", "", text)                        # K_{f} reads Kf
    text = re.sub(r"[*`|$\\]", " ", text)
    text = re.sub(r"^\s*[-:]{3,}\s*$", " ", text, flags=re.M)   # Markdown table rules
    return re.sub(r"\s+", " ", text).strip().casefold()


def squeeze(text: str) -> str:
    """Characters only: the error rate counts no space a model adds or drops."""
    return re.sub(r"\s+", "", normalize(text))


def substring_distance(needle: str, hay: str) -> int:
    """The least edits that turn `needle` into some substring of `hay` (semi-global alignment):
    a printed line is scored against the best place for it in the output, wherever that is,
    so reading order and the text around it cost nothing."""
    if not needle:
        return 0
    prev = [0] * (len(hay) + 1)
    for i, a in enumerate(needle, 1):
        cur = [i] + [0] * len(hay)
        for j, b in enumerate(hay, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b))
        prev = cur
    return min(prev)


WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'.,/&-]*[A-Za-z0-9]|[A-Za-z0-9]")


def words(text: str) -> list[str]:
    return [w.lower() for w in WORD.findall(normalize(text))]


def edit_ratio(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def unmatched(hyp: list[str], ref: set[str]) -> list[str]:
    """Words of the output that match no word on the page, even loosely (ratio below 0.75):
    junk from ink, made-up words, or words misread past recognition. `[illegible]` is the
    prompt's marker, not junk."""
    out = []
    for w in hyp:
        if w in ref or w == "illegible" or (len(w) <= 2 and not w.isalpha()):
            continue
        if not any(edit_ratio(w, r) >= 0.75 for r in ref if abs(len(r) - len(w)) <= 4):
            out.append(w)
    return out


ITEM_READ = 0.2   # a handwritten cell is read when its best match needs at most 20% edits


FACT = re.compile(r"\d|@|\.com|http")


def score_text(text: str, printed: list[str], items: list[str], ref_words: set[str]) -> dict:
    """The error rate of the printed lines (edits over characters, spaces ignored, each line
    against its best place in the output); the handwritten cells read; the words that match
    nothing on the page; and the numbers, emails and addresses that are not on the page
    exactly (a misread or made-up fact)."""
    ref_facts = {w.strip(".,") for w in ref_words if FACT.search(w)}
    hay = squeeze(text)
    lines = [squeeze(line) for line in printed if squeeze(line)]
    total = sum(len(line) for line in lines)
    errors = sum(substring_distance(line, hay) for line in lines)
    read = [i for i in items if substring_distance(squeeze(i), hay) <= ITEM_READ * len(squeeze(i))]
    hyp = words(text)
    junk = unmatched(hyp, ref_words)
    facts = [w for w in hyp if FACT.search(w) and w.strip(".,") not in ref_facts]
    return {"wrong_facts": len(facts), "facts": facts, "chars": len(hay), "printed_chars": total,
            "cer": errors / total if total else None, "words": len(hyp),
            "items": len(items), "items_read": len(read),
            "missed": [i for i in items if i not in read],
            "junk_words": len(junk), "junk_share": len(junk) / len(hyp) if hyp else 0.0,
            "junk": junk}


def texts_of(route: str, entry: dict) -> dict[str, str]:
    """The texts a route produced for one page, by what a reader would see."""
    texts = {}
    if route == "none":
        texts["reflow today (--ocr auto)"] = "\n".join(entry["ocr_text"])
        texts["tesseract (forced)"] = entry.get("tesseract_forced", "")
    if entry.get("whole"):
        texts[f"{route} (whole page)"] = entry["whole"].get("text") or ""
        if entry["drafts"]:
            texts[f"{route} (assist drafts)"] = "\n\n".join(d["text"] for d in entry["drafts"])
    return texts


def score(work: Path, truth: Path, out_json: Path | None) -> None:
    results: dict[str, dict] = {}
    for path in sorted(work.glob("*/run.json")):
        rec = json.loads(path.read_text())
        for page, entry in rec.get("pages", {}).items():
            printed_path = truth / f"{page}.printed.txt"
            if not printed_path.exists():
                continue
            printed = printed_path.read_text().splitlines()
            hand_path = truth / f"{page}.hand.txt"
            hand = hand_path.read_text().splitlines() if hand_path.exists() else []
            items = [line for line in hand if line.strip() and not line.startswith("~")]
            allowed = " ".join(line.lstrip("~") for line in hand)
            ref_words = set(words("\n".join(printed) + "\n" + allowed))
            for key, text in texts_of(rec["route"], entry).items():
                results.setdefault(key, {})[page] = score_text(text, printed, items, ref_words)
                results[key][page]["text"] = text
    for key, pages in results.items():
        cers = [p["cer"] for p in pages.values() if p["cer"] is not None]
        junk = sum(p["junk_words"] for p in pages.values())
        read = sum(p["items_read"] for p in pages.values())
        items = sum(p["items"] for p in pages.values())
        facts = sum(p["wrong_facts"] for p in pages.values())
        print(f"{key}: mean CER {statistics.fmean(cers):.3f}, handwritten cells {read}/{items}, "
              f"junk words {junk}, wrong facts {facts}")
        for page, p in pages.items():
            print(f"  {page}: CER {p['cer']:.3f}, cells {p['items_read']}/{p['items']}, "
                  f"words {p['words']}, junk {p['junk_words']}: {' '.join(p['junk'][:30])}"
                  f" | facts {p['wrong_facts']}: {' '.join(p['facts'][:30])}")
    if out_json:
        out_json.write_text(json.dumps(results, indent=1, ensure_ascii=False))


# -- compare (#291) ----------------------------------------------------------------------------


def issue_words(spec_id: str) -> tuple[list[str], list[str]]:
    """The words a page's spec asks for under #291 (`contains`, the name an `alt_has` pattern
    ends with, and a `contains_in_order` run as one item: a tuple, found only in that order) and
    the junk it forbids (`no_text`), from the assertions whose xfail or why names #291."""
    data = json.loads((SPECS / f"{spec_id}.json").read_text(encoding="utf-8"))
    want, junk = [], []
    for a in data["assertions"]:
        if "#291" not in f"{a.get('xfail', '')} {a.get('why', '')}":
            continue
        want += a.get("contains", [])
        want += [p.rsplit(".*", 1)[-1] for p in a.get("alt_has", [])]
        if "contains_in_order" in a:
            want.append(tuple(a["contains_in_order"]))
        junk += a.get("no_text", [])
    return want, junk


def transcript_page(out: Path, page: int) -> str | None:
    """A converted document's page as the page checks read it (tests/pages), then the alt
    of each picture on it, which a reader hears too."""
    sys.path.insert(0, str(REPO / "tests" / "pages"))
    from checks import page_text
    from corpus import DocOutput

    if not (out / "manifest.json").exists():
        return None
    pg = DocOutput(out.name, out).page(page)
    return "\n".join([page_text(pg)] + [img.attrs.get("alt", "") for img in pg.find_all("img")])


def found_in(item: str | tuple[str, ...], text: str) -> bool:
    """Whether `item` is in `text`, case and markup apart. A one-word item is found whatever the
    spacing inside it ("CuSO_4", "CuSO 4"); a phrase keeps its spaces, so "ERAsinthe" is not
    "ERAs in the". A tuple is found when its pieces are, each after the one before."""
    if isinstance(item, tuple):
        hay, at = normalize(text), 0
        for piece in item:
            at = hay.find(normalize(piece), at)
            if at < 0:
                return False
            at += len(normalize(piece))
        return True
    return (normalize(item) in normalize(text)
            or (" " not in item and squeeze(item) in squeeze(text)))


def label(item: str | tuple[str, ...]) -> str:
    return " … ".join(item) if isinstance(item, tuple) else item


def compare(work: Path, out_json: Path | None) -> None:
    """Grade every reading of each #291 page with its spec's words: the correct words found
    and the known junk left. The readings are each route's (reflow today is the transcript a
    reader gets) and the Haiku prompt-only pipeline (`direct-haiku`) where the
    site has built it; the harness calls no API model."""
    runs = [json.loads(p.read_text()) for p in sorted(work.glob("*/run.json"))]
    table: dict[str, dict[str, dict]] = {}
    for page, (spec_id, site) in COMPARED.items():
        want, junk = issue_words(spec_id)
        number = PAGES[page][1]
        texts: dict[str, str | None] = {}
        for rec in runs:
            entry = rec.get("pages", {}).get(page)
            if entry is None:
                continue
            if rec["route"] == "none":
                texts["reflow today"] = transcript_page(work / "none" / page, 1)
            else:
                texts[f"{rec['route']} (transcript)"] = transcript_page(
                    route_dir(work, rec["route"]) / page, 1)
            if entry.get("whole"):
                texts[f"{rec['route']} (whole page)"] = entry["whole"].get("text") or ""
        texts["Haiku, direct"] = transcript_page(
            MAIN / "web" / "public" / "docs" / f"{site}~direct-haiku", number)
        for source, text in texts.items():
            if text is None:
                continue
            # Junk keeps its spaces too: "HOWTO" is junk where "HOW TO" is right.
            found = [w for w in want if found_in(w, text)]
            left = [j for j in junk if normalize(j) in normalize(text)]
            table.setdefault(page, {})[source] = {"found": len(found), "want": len(want),
                                                  "missed": [label(w) for w in want if w not in found],
                                                  "junk": left}
    for page, sources in table.items():
        print(f"{page} ({COMPARED[page][0]})")
        for source, r in sources.items():
            print(f"  {source:40} words {r['found']:2}/{r['want']:<2}  junk {len(r['junk'])}"
                  f"  missed: {', '.join(r['missed']) or '-'}"
                  f"{'  junk: ' + ', '.join(r['junk']) if r['junk'] else ''}")
    if out_json:
        out_json.write_text(json.dumps(table, indent=1, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("route")
    r.add_argument("--work", type=Path, default=Path("/tmp/bench209"))
    r.add_argument("--host", default=None, help="Ollama server (default: OLLAMA_HOST, else local)")
    s = sub.add_parser("score")
    s.add_argument("--work", type=Path, default=Path("/tmp/bench209"))
    s.add_argument("--truth", type=Path, default=MAIN / "research" / "transcription-2026-11" / "truth")
    s.add_argument("--json", type=Path)
    c = sub.add_parser("compare")
    c.add_argument("--work", type=Path, default=Path("/tmp/bench209"))
    c.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.cmd == "run":
        run(args.route, args.work, ollama_host(args.host))
    elif args.cmd == "compare":
        compare(args.work, args.json)
    else:
        score(args.work, args.truth, args.json)


if __name__ == "__main__":
    main()
