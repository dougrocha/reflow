"""OCR as a stage of ingest: an image-only PDF would otherwise convert to blank pages and
an empty review queue.

  1. `needs_ocr(doc, working)` lists the SCANNED pages (and MIXED ones whose text layer is
     poor: an alphabetic share under MIN_LETTER_SHARE, measured from the working copy).
  2. `ocr_pages(working, pages, runner)` adds a text layer to those pages of the working
     copy, in place, through a Runner (OCRmyPDF by default; a FakeRunner in tests). Every
     later stage reads the working copy, so extraction sees the new text with no change.
  3. Each page OCR'd gets PageInfo.facts += Fact(FACT_OCR_PAGE, Source.OCR); nodes on such a
     page get source=Source.OCR in confidence.apply (page facts apply to the page's nodes).
  4. A SCANNED page that was not OCR'd (no runner, runner failed, --ocr none) gets
     Fact("ocr-missing", ...) with severity error, and the document Fact("ocr-doc").

The binary is optional: `reflow doctor` (reflow.checks.doctor) reports whether it is there.
Page numbers are 1-based, as in PageInfo.number. `needs_ocr` opens the working copy with
pdfium, so a caller that shares pdfium across threads holds its lock around the call.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from reflow.identity import program_output
from reflow.tessdata import data_digest
from reflow.pdfobjects import INVISIBLE, iter_objects, layer_words, to_top_left
import reflow.ingest.ocr_trust as ocr_trust
from reflow.model import (FACT_OCR_MISSING, FACT_OCR_PAGE, BBox, Document, Fact, PageKind,
                          Source)

OCR_MODES = ("auto", "none", "ocrmypdf")  # the --ocr values; reflow.run.Options validates against it
MIN_LETTER_SHARE = 0.6  # a MIXED page's text layer with fewer letters than this is redone
LANGS_DEFAULT = ("eng",)


OcrReason = Literal["no-text", "outlined-text", "unreadable-text", "textless-band"]
TEXTLESS = frozenset({"no-text", "outlined-text", "unreadable-text"})  # no usable text without OCR
OcrMode = Literal["force", "redo"]
MODE_ORDER: tuple[OcrMode, ...] = ("force", "redo")  # the runner's passes, in this order

# unreadable-text: visible junk text of at least this many non-space characters is rasterized
# and read again (force); fewer (a lone folio, an invisible layer) keep the page's objects (redo).
FORCE_MIN_VISIBLE = 20
OUTLINE_SEGMENTS = 100  # a sparse page with this many path segments may hold its words as paths
# textless-band: a horizontal band of the page image, at least BAND_MIN_HEIGHT of the page tall,
# that holds no text character but has a dark (< BAND_DARK grey) share of at least BAND_MIN_INK
# in a render at BAND_SCALE. Character boxes are padded by BAND_PAD of their height.
BAND_MIN_HEIGHT = 0.1
BAND_SCALE = 0.5
BAND_DARK = 180
BAND_MIN_INK = 0.01
BAND_PAD = 0.25


@dataclass(frozen=True)
class OcrPage:
    """Why a page is OCR'd, and so how: redo keeps the page's objects and visible text and reads
    its images; force rasterizes the page and reads all of it."""
    reason: OcrReason
    mode: OcrMode
    region_boxes: tuple[BBox, ...] = ()   # textless-band: its bands, top to bottom; else ()


@dataclass(frozen=True)
class OcrPlan:
    pages: dict[int, OcrPage] = field(default_factory=dict)   # 1-based page number
    # The pages whose own OCR layer is kept (a readable invisible layer, see needs_ocr): read
    # again only to grade that layer's words (ocr_trust.grade_layer), never printed.
    keep: tuple[int, ...] = ()

    def by_mode(self) -> dict[str, list[int]]:
        """{"force": [...], "redo": [...]} in MODE_ORDER, empty modes omitted."""
        out = {mode: sorted(n for n, p in self.pages.items() if p.mode == mode)
               for mode in MODE_ORDER}
        return {mode: pages for mode, pages in out.items() if pages}

    def key(self) -> tuple:
        """What the runner's output depends on, for the stage cache (not the band boxes)."""
        return (tuple(sorted((n, p.reason, p.mode) for n, p in self.pages.items()))
                + tuple((n, "kept-layer", "keep") for n in self.keep))


class Runner(Protocol):
    """Runs OCR on the plan's pages of `src`, each in its mode, and writes the result to `dst`
    (same page count, new text layer on those pages; a copy of `src` when the plan has none).
    `evidence` is an empty directory of this conversion that the engine writes word evidence
    into: a runner whose `grades` is True writes one `<page>.json` per plan page
    (ocr_trust.PageGrade), and a redo one per `plan.keep` page from a reading it does not write
    to `dst`; `ocr_pages` holds it to that. A runner whose `grades` is False grades nothing and
    reads no `plan.keep` page. Raises OcrUnavailable when it can't run at all."""
    name: str
    grades: bool

    def run(self, src: Path, dst: Path, plan: OcrPlan, langs: tuple[str, ...],
            evidence: Path) -> None: ...

    def environment(self, langs: tuple[str, ...]) -> str | None:
        """A digest of what `run`'s output depends on outside reflow's code and the installed
        packages (the engine's models and configuration), or None when it can't be told; the
        stage cache (reflow.stagecache.CachedRunner) then runs it uncached."""
        ...


class OcrUnavailable(Exception):
    """No OCR engine is installed: conversion continues, the pages are flagged."""


@dataclass
class OcrResult:
    plan: OcrPlan = field(default_factory=OcrPlan)   # kept in every outcome, for record
    done: list[int] = field(default_factory=list)    # every plan page of a successful run, else []
    failed: dict[int, str] = field(default_factory=dict)   # page → error
    runner: str = "none"
    seconds: float = 0.0
    graded: bool = False   # the runner graded every page's words and the sidecar holds them

    @property
    def attempted(self) -> list[int]:
        return sorted(self.plan.pages)


def letter_share(text: str) -> float:
    """Letters ÷ non-space characters; 0.0 when the text has no non-space character."""
    chars = [c for c in text if not c.isspace()]
    return sum(c.isalpha() for c in chars) / len(chars) if chars else 0.0


def needs_ocr(doc: Document, working: Path) -> OcrPlan:
    """Why and how each page is OCR'd, read from `working`, the copy the text is read from. A
    page gets the first reason below whose rule matches it:
      no-text          a SCANNED page                                                 redo
      outlined-text    a sparse page (< 20 characters) with >= OUTLINE_SEGMENTS path
                       segments: its words may be outlines, leaving only a page number force
      unreadable-text  a MIXED page whose text has fewer letters than MIN_LETTER_SHARE
                       of its non-space characters        force with >= FORCE_MIN_VISIBLE
                                                          visible characters, else redo
      textless-band    a MIXED page with no invisible character and a band of its
                       image that holds ink but no text character (a letterhead, a
                       body under a visible title), in a document without a tag tree:
                       OCRmyPDF drops the tree, and a tagged document's images are
                       described by it                                                redo
    MIXED pages are those with text over images covering >= 90% of the page. A readable
    invisible layer (render mode 3, someone else's OCR) is kept, as it was before the plan:
    Tesseract reads the NACA report's small capitals, the chemistry worksheet's "Ionic" and the
    permit's title worse than the layers those files carry. Its page is in `keep`: read again
    only to grade the layer's words."""
    pages: dict[int, OcrPage] = {p.number: OcrPage("no-text", "redo")
                                 for p in doc.pages if p.kind is PageKind.SCANNED}
    keep: list[int] = []
    mixed = [p.number for p in doc.pages if p.kind is PageKind.MIXED]
    sparse = [p.number for p in doc.pages
              if p.char_count < 20 and p.kind in (PageKind.BORN_DIGITAL, PageKind.EMPTY)]
    if mixed or sparse:
        pdf = pdfium.PdfDocument(working)
        try:
            for number in sorted(set(mixed + sparse)):
                page = pdf[number - 1]
                try:
                    found = (_outlined(page) if number in sparse
                             else _mixed(page, bands=not doc.meta.get("has_struct_tree")))
                finally:
                    page.close()
                if found == KEPT:
                    keep.append(number)
                elif found is not None:
                    pages[number] = found
        finally:
            pdf.close()
    return OcrPlan(dict(sorted(pages.items())), tuple(keep))


def _outlined(page: pdfium.PdfPage) -> OcrPage | None:
    segments = sum(pdfium_c.FPDFPath_CountSegments(obj.raw)
                   for obj, _ in iter_objects(page, {pdfium_c.FPDF_PAGEOBJ_PATH}))
    return OcrPage("outlined-text", "force") if segments >= OUTLINE_SEGMENTS else None


KEPT = "kept"   # _mixed: a readable invisible layer, which is kept and graded


def _mixed(page: pdfium.PdfPage, *, bands: bool) -> OcrPage | str | None:
    """The reason of a MIXED page, read from its characters (pdfium's text page also holds the
    characters of nested form XObjects); KEPT, a readable invisible layer, which is kept; or
    None: its text is readable and explains its image. `bands`: look for textless bands."""
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_range()
        visible, invisible, boxes = 0, False, []
        for i in range(textpage.count_chars()):
            obj = pdfium_c.FPDFText_GetTextObject(textpage.raw, i)
            if not obj or chr(pdfium_c.FPDFText_GetUnicode(textpage.raw, i)).isspace():
                continue   # a space, or a break pdfium inserted
            if pdfium_c.FPDFTextObj_GetTextRenderMode(obj) == INVISIBLE:
                invisible = True
            else:
                visible += 1
            boxes.append(textpage.get_charbox(i))
    finally:
        textpage.close()
    if letter_share(text) < MIN_LETTER_SHARE:
        return OcrPage("unreadable-text", "force" if visible >= FORCE_MIN_VISIBLE else "redo")
    if invisible:
        return KEPT
    if not bands:
        return None
    found = _textless_bands(page, [to_top_left(page, box) for box in boxes])
    return OcrPage("textless-band", "redo", found) if found else None


def _textless_bands(page: pdfium.PdfPage, chars: list[BBox]) -> tuple[BBox, ...]:
    """The horizontal bands of the page's images, at least BAND_MIN_HEIGHT of the page tall,
    that hold no character but have ink: page-space boxes (top-left points) spanning the images'
    width, top to bottom."""
    _, _, width, height = to_top_left(page, page.get_cropbox())
    images = [to_top_left(page, box)
              for _, box in iter_objects(page, {pdfium_c.FPDF_PAGEOBJ_IMAGE})]
    if not images:
        return ()
    x0, top = max(0.0, min(b[0] for b in images)), max(0.0, min(b[1] for b in images))
    x1, bottom = min(width, max(b[2] for b in images)), min(height, max(b[3] for b in images))
    taken = sorted((c[1] - BAND_PAD * (c[3] - c[1]), c[3] + BAND_PAD * (c[3] - c[1]))
                   for c in chars)
    gaps, cursor = [], top
    for start, end in taken + [(bottom, bottom)]:
        if min(start, bottom) - cursor >= BAND_MIN_HEIGHT * height:
            gaps.append((cursor, min(start, bottom)))
        cursor = max(cursor, end)
        if cursor >= bottom:
            break
    if not gaps or x1 <= x0:
        return ()
    bitmap = page.render(scale=BAND_SCALE)
    try:
        grey = bitmap.to_pil().convert("L")
    finally:
        bitmap.close()
    sx, sy = grey.width / width, grey.height / height
    bands = []
    for g0, g1 in gaps:
        crop = grey.crop((int(x0 * sx), int(g0 * sy), max(int(x0 * sx) + 1, round(x1 * sx)),
                          max(int(g0 * sy) + 1, round(g1 * sy))))
        histogram = crop.histogram()
        if sum(histogram[:BAND_DARK]) >= BAND_MIN_INK * max(1, sum(histogram)):
            bands.append((round(x0, 2), round(g0, 2), round(x1, 2), round(g1, 2)))
    return tuple(bands)


def ocr_pages(working: Path, plan: OcrPlan, runner: Runner | None,
              langs: tuple[str, ...] = LANGS_DEFAULT) -> OcrResult:
    """Add a text layer to the plan's pages of the working copy, replacing the file atomically.
    With runner None, or OcrUnavailable, nothing is written and `done` is empty.

    OCRmyPDF reports no per-page failures, so a run is all-or-nothing: on any other error
    nothing is written, `done` is empty and the message is in `failed[0]`.

    A runner that grades words (`grades`, ocr_trust) must leave exactly one evidence file per plan
    page, of the page's mode, in the conversion's own temp directory. Anything missing, corrupt,
    extra or of the wrong mode fails the run likewise, and `failed[0]` names the page. Only then
    is the PDF replaced and the sidecar written beside it (ocr_trust.write_sidecar), PDF first:
    the sidecar records that PDF's hash, so a crash between the two leaves a stale sidecar, never
    a wrong one. The temp directory is removed either way.

    The plan's `keep` pages keep their own OCR layer and are not replaced: the runner reads
    them only for their evidence, and their grades become the layer's (`_kept`, mode "keep"), so
    extraction can leave the layer's unreadable words out as it does reflow's own."""
    result = OcrResult(plan=plan)
    if runner is None:
        return result
    result.runner = runner.name
    if not plan.pages and not (plan.keep and runner.grades):
        return result
    tmp = working.with_suffix(".ocr.tmp")
    evidence = Path(tempfile.mkdtemp(prefix="reflow-ocr-"))
    start = time.monotonic()
    try:
        runner.run(working, tmp, plan, langs, evidence)
        grades = _evidence(evidence, plan) if runner.grades else None
        if grades is not None and plan.keep:
            grades = [_kept(working, g) if g.page in plan.keep else g for g in grades]
        if plan.pages:
            os.replace(tmp, working)
            result.done = sorted(plan.pages)
        if grades is not None:
            ocr_trust.write_sidecar(working, grades)
            result.graded = True
    except OcrUnavailable:
        result.runner = "unavailable"
    except Exception as error:  # any engine failure leaves the copy untouched
        result.failed[0] = f"{type(error).__name__}: {error}"
    finally:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(evidence, ignore_errors=True)
        result.seconds = round(time.monotonic() - start, 3)
    return result


class OcrEvidenceError(Exception):
    """The word grades a grading runner left do not match the plan."""


def _evidence(evidence: Path, plan: OcrPlan) -> list[ocr_trust.PageGrade]:
    """The plan pages' word grades, one evidence file each; raises OcrEvidenceError naming the
    first page whose file is missing, extra, unreadable or of another mode."""
    found: dict[int, ocr_trust.PageGrade] = {}
    for path in sorted(evidence.iterdir()):
        if not (path.suffix == ".json" and path.stem.isdigit()):
            raise OcrEvidenceError(f"unexpected evidence file {path.name}")
        n = int(path.stem)
        if n not in plan.pages and n not in plan.keep:
            raise OcrEvidenceError(f"page {n}: word grades for a page that was not planned")
        try:
            grade = ocr_trust.read_evidence(path)
        except Exception as error:   # noqa: BLE001 - any malformed file fails the run
            raise OcrEvidenceError(f"page {n}: unreadable word grades ({error})") from error
        planned = plan.pages[n].mode if n in plan.pages else "redo"
        if grade.page != n or grade.mode != planned:
            raise OcrEvidenceError(f"page {n}: word grades of page {grade.page} in {grade.mode} "
                                   f"mode, planned {planned}")
        found[n] = grade
    if missing := sorted((set(plan.pages) | set(plan.keep)) - set(found)):
        raise OcrEvidenceError(f"page {missing[0]}: no word grades")
    return [found[n] for n in sorted(found)]


def _kept(working: Path, read: ocr_trust.PageGrade) -> ocr_trust.PageGrade:
    """The grades of a kept layer's words (ocr_trust.grade_layer), from Tesseract's reading of
    the page (`read`), over the page rendered at that reading's size."""
    pdf = pdfium.PdfDocument(working)
    try:
        page = pdf[read.page - 1]
        try:
            _, _, width, height = to_top_left(page, page.get_cropbox())
            sx, sy = read.image_px[0] / width, read.image_px[1] / height
            layer = [(text, (round(b[0] * sx), round(b[1] * sy), round(b[2] * sx),
                             round(b[3] * sy))) for text, b in layer_words(page)]
            image = page.render(scale=sx).to_pil().convert("RGB")
        finally:
            page.close()
    finally:
        pdf.close()
    if image.size != read.image_px:
        image = image.resize(read.image_px)
    words = ocr_trust.grade_layer(layer, read.words, image)
    return ocr_trust.PageGrade(read.page, "keep", read.image_px, tuple(words))


def record(doc: Document, result: OcrResult) -> None:
    """Write the OCR facts onto the pages and the document (see the module docstring), from the
    plan's reason for each page:
      done                     ocr-page, with the reason and mode (and the bands it read)
      not done, TEXTLESS       ocr-missing (an error), counted in the document's ocr-doc
      not done, textless-band  ocr-band-missing, a warning with the bands' boxes"""
    by_number = {p.number: p for p in doc.pages}
    done = set(result.done)
    missing = 0
    for n, planned in result.plan.pages.items():
        page = by_number.get(n)
        if page is None:
            continue
        if n in done:
            data: dict = {"reason": planned.reason, "mode": planned.mode}
            if planned.region_boxes:   # redo read only the bands: the rest is the file's text
                data["region_boxes"] = [v for box in planned.region_boxes for v in box]
            page.facts.append(Fact(FACT_OCR_PAGE, Source.OCR, f"text recognized by {result.runner}",
                                   data))
        elif planned.reason in TEXTLESS:
            page.facts.append(Fact(FACT_OCR_MISSING, Source.HEURISTIC,
                                   "image-only page, no text layer"))
            missing += 1
        elif planned.reason == "textless-band":
            page.facts.append(Fact("ocr-band-missing", Source.HEURISTIC,
                                   "Part of the page image holds text that was not read",
                                   {"region_boxes": [v for box in planned.region_boxes
                                                     for v in box]}))
    if missing:
        doc.root.facts.append(Fact("ocr-doc", Source.HEURISTIC,
                                   f"{missing} image-only pages without text"))


def _page_ranges(pages: list[int]) -> str:
    """[1, 3, 4, 5] → "1,3-5" (the OCRmyPDF page syntax, 1-based)."""
    parts: list[str] = []
    ordered = sorted(set(pages))
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1] == ordered[j] + 1:
            j += 1
        parts.append(str(ordered[i]) if i == j else f"{ordered[i]}-{ordered[j]}")
        i = j + 1
    return ",".join(parts)


def _import_ocrmypdf():
    try:
        return importlib.import_module("ocrmypdf")
    except ImportError:
        return None


class OcrmypdfRunner:
    """One OCRmyPDF pass per mode of the plan, force first, then redo:
    `ocrmypdf --pages a,b-c --force-ocr|--redo-ocr --pdf-renderer fpdf2 --output-type pdf -l eng
    --plugin ocr_trust.py --reflow-evidence DIR --reflow-drop on src dst`, through the ocrmypdf
    Python API when importable, else the binary on PATH. With both modes, the force pass writes
    dst.with_suffix(".force.tmp") and the redo pass reads it. The plugin (ingest.ocr_trust)
    grades every word, writes the grades to `evidence` and leaves the unreadable words out of
    the text layer. The plan's `keep` pages get a redo pass of their own, with the drop off, into
    a file that is then deleted: only their evidence is kept."""
    name = "ocrmypdf"
    grades = True
    PLUGIN = Path(ocr_trust.__file__)

    @staticmethod
    def available() -> bool:
        return _import_ocrmypdf() is not None or shutil.which("ocrmypdf") is not None

    def environment(self, langs: tuple[str, ...]) -> str | None:
        return ocr_environment(langs)

    def run(self, src: Path, dst: Path, plan: OcrPlan, langs: tuple[str, ...],
            evidence: Path) -> None:
        module = _import_ocrmypdf()
        binary = None if module is not None else shutil.which("ocrmypdf")
        if module is None and binary is None:
            raise OcrUnavailable("neither the ocrmypdf module nor the binary is installed")
        passes = list(plan.by_mode().items())
        middle = dst.with_suffix(".force.tmp")
        thrown = dst.with_suffix(".keep.tmp")
        try:
            current = src
            for i, (mode, pages) in enumerate(passes):
                out = dst if i == len(passes) - 1 else middle
                self._pass(module, binary, current, out, mode, _page_ranges(pages),
                           "+".join(langs), evidence)
                current = out
            if not passes:
                shutil.copyfile(src, dst)
            if plan.keep:   # graded only: the layer the file carries stays
                self._pass(module, binary, src, thrown, "redo", _page_ranges(list(plan.keep)),
                           "+".join(langs), evidence, drop=False)
        finally:
            middle.unlink(missing_ok=True)
            thrown.unlink(missing_ok=True)

    @staticmethod
    def _pass(module, binary: str | None, src: Path, dst: Path, mode: str, spec: str,
              language: str, evidence: Path, drop: bool = True) -> None:
        if module is not None:
            try:
                module.ocr(src, dst, pages=spec, **{f"{mode}_ocr": True}, pdf_renderer="fpdf2",
                           output_type="pdf", language=language, progress_bar=False,
                           plugins=[OcrmypdfRunner.PLUGIN], reflow_evidence=str(evidence),
                           reflow_drop="on" if drop else "off")
            except Exception as error:
                if type(error).__name__ == "MissingDependencyError":  # no tesseract
                    raise OcrUnavailable(str(error)) from error
                raise
            return
        command = [binary, "--pages", spec, f"--{mode}-ocr", "--pdf-renderer", "fpdf2",
                   "--output-type", "pdf", "-l", language, "--plugin", str(OcrmypdfRunner.PLUGIN),
                   "--reflow-evidence", str(evidence), "--reflow-drop", "on" if drop else "off",
                   str(src), str(dst)]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as error:
            tail = (error.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError(f"ocrmypdf exited {error.returncode}: {' | '.join(tail)}") \
                from error


def ocr_environment(langs: tuple[str, ...]) -> str | None:
    """A digest of the OCR engine's data (reflow.tessdata.data_digest: the tessdata directory
    and TESSDATA_PREFIX, each language's traineddata, every file in configs/ and tessconfigs/,
    the output configs OCRmyPDF asks tesseract for), and the Ghostscript version OCRmyPDF may
    rasterize with. None when the directory or a language's model can't be found."""
    digest = data_digest(langs)
    if digest is None:
        return None
    gs = (program_output("gs", "--version") or "absent").strip()
    return hashlib.sha256(f"{digest};gs={gs}".encode()).hexdigest()


def make_runner(name: str) -> Runner | None:
    """"auto" → OcrmypdfRunner when available else None; "none" → None; "ocrmypdf" →
    OcrmypdfRunner or OcrUnavailable."""
    if name == "none":
        return None
    if name == "auto":
        return OcrmypdfRunner() if OcrmypdfRunner.available() else None
    if name == "ocrmypdf":
        if not OcrmypdfRunner.available():
            raise OcrUnavailable("ocrmypdf is not installed")
        return OcrmypdfRunner()
    raise ValueError(f"unknown OCR runner {name!r}: use {', '.join(OCR_MODES)}")
