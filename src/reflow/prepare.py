"""The source pass of a conversion: publishes the original, writes the working copy, refuses or
flags a restricted PDF, OCRs the image-only pages and builds the Run that every later pass
reads."""

from __future__ import annotations

import shutil
import time
from functools import partial
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium

from reflow.identity import file_sha256
from reflow.ingest import flattened, font_map, text_trust, witness
from reflow.ingest.load import restricted_action
from reflow.ingest.ocr import OcrPlan, make_runner, needs_ocr, ocr_pages, record
from reflow.ingest.pages import page_infos, read_source
from reflow.ingest.text_trust import TrustReport
from reflow import stagecache
from reflow.model import Document, Fact, Source
from reflow.names import ORIGINAL_PDF, WORKING_PDF
from reflow.run import Options, Progress, Run, Workers

__all__ = ["RestrictedPdf", "SourceUnreadable", "prepare"]


class RestrictedPdf(PermissionError):
    """The owner forbids text extraction and allow_restricted is off. CLI → exit 3."""


class SourceUnreadable(Exception):
    """Opening the source PDF failed: `__cause__` is the pikepdf.PasswordError (it needs a
    password) or the pikepdf.PdfError (not a PDF, or too damaged to repair). CLI → exit 2 or 1.
    A pikepdf error from any later step is a bug in the run, not a bad file."""


def prepare(pdf: Path, stage: Path, options: Options, workers: Workers, *,
            progress: Progress | None = None) -> Run:
    """Open the source and return the Run: original published, working copy written and OCR'd,
    the Document with page info, the file's page labels and the source facts.
    Order, each step reading only the previous one's output:
      0. make_runner(options.ocr)                                  OcrUnavailable before any write
      1. copy pdf to stage/ORIGINAL_PDF                            byte for byte
      2. normalize(pdf, working, options.password)                 qpdf repair, decrypt, linearize
         (with the stage cache on, steps 2 and 3 are one cached read: reflow.stagecache)
      3. with workers.pdfium: doc = ingest(working, options.password, original=pdf)
         (ingest records PageInfo.label from the file's page labels)
      4. restricted_action(doc.meta["allows_extract"], options.allow_restricted):
         "refuse" → raise RestrictedPdf (output.staged discards the stage); "flag" → root
         fact extract-forbidden; "proceed" → nothing
         flattened.record(doc, flattened.find(working))   form-flattened on each page that
         draws the traces of a flattened form (ingest.flattened). Reads `working` only
      5. text_trust.preflight(doc, working, ...)   judge each BORN_DIGITAL page's text layer;
         OCR a witness of the suspect ones (one task per page on workers.pool, joined here,
         in page order) and record text-disputed / text-witness-unavailable. Reads `working`,
         never writes it, and never replaces text
         font_map.repair(doc, working, trust, ...)  a font the witness reads as other letters
         gets the decoded codes in its /ToUnicode: written to a sibling, checked, then
         os.replace'd over `working` under the pdfium lock; text_trust.recheck then judges the
         pages it changed again against the same witness words
      6. with workers.pdfium: plan = needs_ocr(doc, working)   why and how each page is OCR'd
         result = ocr_pages(working, plan, runner)   no lock: a subprocess and os.replace,
                 with no pdfium handle on `working` open and no pool task submitted yet
         record(doc, result)                         facts from each plan page's reason
         with workers.pdfium: re-read page_info for result.done, so kind/char_count describe
                 the copy the engine reads (the ocr-page fact keeps the history)
    Invariant: after prepare returns, nothing writes `working` again."""
    report: Progress = progress or (lambda *_: None)
    # The direct engine's model reads every page's picture itself: no OCR, and no OCR facts.
    ocr = options.engine_info.needs_ocr
    runner = make_runner(options.ocr) if ocr else None
    if runner is not None and options.stage_cache is not None:
        runner = stagecache.CachedRunner(runner, options.stage_cache, pdf)
    start = time.monotonic()
    shutil.copyfile(pdf, stage / ORIGINAL_PDF)   # byte for byte
    working = stage / WORKING_PDF
    # Opt-in (reflow.stagecache): the page-check harness reuses the reading of a PDF it has seen.
    # A password is never part of a key, so a locked PDF is always read.
    root = options.stage_cache if not options.password else None

    # All of the reading is in read_source, which is in the ingest stage's key; the arguments
    # bound here only schedule it and report progress.
    read = partial(read_source, pdf, working, options.password,
                   on_page=lambda d, n: report("Reading pages", d, n),
                   executor=workers.pool, jobs=workers.jobs, lock=workers.pdfium)
    try:
        if root is None:
            doc = read()
        else:
            doc, hit = stagecache.cached_ingest(root, pdf, working, read)
            if hit:
                report("Reading pages", len(doc.pages), len(doc.pages))
    except (pikepdf.PasswordError, pikepdf.PdfError) as e:
        raise SourceUnreadable(str(e)) from e

    action = restricted_action(bool(doc.meta.get("allows_extract", True)),
                               options.allow_restricted)
    if action == "refuse":
        raise RestrictedPdf(
            f"{pdf.name} forbids text extraction (allow.extract is false); pass "
            "--allow-restricted to convert it anyway and flag it")
    if action == "flag":
        doc.root.facts.append(Fact("extract-forbidden", Source.TAGS,
                                   "the PDF's owner forbids text extraction"))
    # A form flattened into its pages has no field left to read: say so (ingest.flattened).
    flattened.record(doc, flattened.find(working))
    seconds = {"ingest": round(time.monotonic() - start, 2)}

    # The text-trust preflight (ingest.text_trust): before replacement OCR, so the OCR sidecar
    # is written against the copy's final bytes. It reads `working` and never writes it.
    start = time.monotonic()
    trust = TrustReport()
    if ocr:
        trust = text_trust.preflight(
            doc, working, lock=workers.pdfium, executor=workers.pool,
            enabled=options.ocr != "none",
            reader=partial(witness_reader, root, pdf, working) if root is not None else None)
    seconds["text_trust"] = round(time.monotonic() - start, 2)

    # The font repair (ingest.font_map): the codes of a font the witness reads as other letters,
    # decoded into its /ToUnicode in the working copy, so every reader sees one decode. Then the
    # pages it changed are judged again against the same witness words.
    start = time.monotonic()
    fonts = font_map.repair(doc, working, trust, lock=workers.pdfium) if ocr else font_map.Repair()
    if fonts.changes:
        text_trust.recheck(doc, working, trust, sorted(fonts.changes), lock=workers.pdfium)
    seconds["font_map"] = round(time.monotonic() - start, 2)

    start = time.monotonic()
    with workers.pdfium:
        plan = needs_ocr(doc, working) if ocr else OcrPlan()
    if (plan.pages or plan.keep) and runner is not None:
        report("Recognizing text", 0, None)
    result = ocr_pages(working, plan, runner)
    record(doc, result)
    if result.done:
        with workers.pdfium:
            _reread_pages(doc, working, result.done)
    seconds["ocr"] = round(time.monotonic() - start, 2)

    return Run(pdf=pdf, stage=stage, options=options, workers=workers, progress=report,
               doc=doc, working=working, ocr=result, seconds=seconds, trust=trust,
               fonts=fonts)


def _reread_pages(doc: Document, working: Path, numbers: list[int]) -> None:
    """Refresh kind, char_count and image_coverage of `numbers` from the (now OCR'd) working
    copy; the label and the facts recorded so far stay. Caller holds the pdfium lock."""
    by_number = {p.number: p for p in doc.pages}
    pdf = pdfium.PdfDocument(working)
    try:
        fresh = page_infos(pdf, [n for n in numbers if n in by_number])
    finally:
        pdf.close()
    for info in fresh:
        page = by_number[info.number]
        page.kind, page.char_count, page.image_coverage = info.kind, info.char_count, info.image_coverage


def read_witness(root: Path, pdf: Path, working_sha: str, working: Path, number: int,
                 langs: tuple[str, ...]) -> witness.WitnessPage:
    """witness.read_page of one page through the stage cache's "witness" stage, keyed by the
    working copy's hash, the page, the render scale, the languages and their Tesseract data;
    uncached when the data can't be identified. Runs in a pool worker or in the caller."""
    data = witness.environment(langs)
    if data is None:
        return witness.read_page(working, number, langs)
    value, _ = stagecache.cached(root, witness.STAGE, stagecache.STAGES[witness.STAGE], pdf,
                                 partial(witness.read_page, working, number, langs),
                                 working_sha, number, witness.SCALE, langs, data)
    return value


def witness_reader(root: Path, pdf: Path, working: Path):
    """The page reader witness.read_pages takes to read through the stage cache: a partial of a
    module function, so it pickles to the pool's workers."""
    return partial(read_witness, root, pdf, file_sha256(working))
