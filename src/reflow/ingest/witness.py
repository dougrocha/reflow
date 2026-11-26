"""The text witness: an independent reading of a page, OCR of its rendered image, that
reflow.ingest.text_trust compares with the page's text layer (#308).

A witness only ever reads. It never writes the working copy and never writes `ocr-words.json`
(whose hash is an extraction key: reflow.stagecache._extract_parts), and its `WitnessWord` is
not a `GradedWord`, so it can never reach `bind_words` or the keep-grade deletion path
(reflow.extract.ocrwords). It is not an OcrPlan mode either.

`read_page` renders one page with pdfium at SCALE and runs `tesseract ... tsv` on it, one
process with one thread (OMP_THREAD_LIMIT=1). `read_pages` runs one page per task on the
conversion's pool, whose workers hold the machine's worker slots (reflow.budget), and gathers
them in page order. With the stage cache on (reflow.prepare.witness_reader), each page is its
own entry under the "witness" stage, keyed by the working copy's bytes, the page, the scale, the
languages and the tessdata digest (reflow.tessdata), beside the code and tool versions every
stage key holds.

This module imports neither the trust module nor reflow.ingest.ocr.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium

from reflow.model import BBox
from reflow.tessdata import data_digest, installed

SCALE = 150 / 72          # the render: 150 dpi
MIN_CONF = 30.0           # Tesseract's words under this confidence are not testimony
TIMEOUT = 120             # seconds one page may take
# The languages a witness reads in, by the primary subtag of the page's language ("" is no
# /Lang, read as English). A page in any other language gets no witness.
LANG_MODELS = {"": "eng", "en": "eng", "es": "spa"}
STAGE = "witness"   # its stage in reflow.stagecache.STAGES


@dataclass(frozen=True)
class WitnessWord:
    """A word the witness read: its text and box (top-left displayed points, the model's frame)
    and Tesseract's confidence, 0-100."""
    text: str
    box: BBox
    conf: float


@dataclass(frozen=True)
class WitnessPage:
    page: int
    words: tuple[WitnessWord, ...] = ()
    error: str = ""          # why the page could not be read; words are then empty


def languages(lang: str) -> tuple[str, ...] | None:
    """The Tesseract languages a page whose /Lang is `lang` is read in, or None when the witness
    has no model for it here."""
    model = LANG_MODELS.get((lang or "").split("-")[0].lower())
    return (model,) if model is not None and installed((model,)) else None


def environment(langs: tuple[str, ...]) -> str | None:
    """A digest of the data a reading in `langs` depends on outside the code and the tool versions
    (reflow.tessdata.data_digest), or None when it can't be told."""
    return data_digest(langs)


def available() -> bool:
    return shutil.which("tesseract") is not None


def parse_tsv(text: str, scale: float) -> tuple[WitnessWord, ...]:
    """The words of Tesseract's TSV (level 5 rows with text and a confidence of MIN_CONF or
    more), their boxes divided by `scale` back into points."""
    words = []
    for row in csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        try:
            if row.get("level") != "5" or not (row.get("text") or "").strip():
                continue
            conf = float(row["conf"])
            left, top = int(row["left"]), int(row["top"])
            width, height = int(row["width"]), int(row["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if conf < MIN_CONF:
            continue
        words.append(WitnessWord(row["text"].strip(),
                                 (round(left / scale, 2), round(top / scale, 2),
                                  round((left + width) / scale, 2),
                                  round((top + height) / scale, 2)), round(conf, 1)))
    return tuple(words)


def read_page(working: Path, number: int, langs: tuple[str, ...], scale: float = SCALE
              ) -> WitnessPage:
    """Page `number` (1-based) of `working`, rendered at `scale` and read by Tesseract. Raises
    on any failure (the caller records it)."""
    pdf = pdfium.PdfDocument(working)
    try:
        page = pdf[number - 1]
        try:
            bitmap = page.render(scale=scale)
            try:
                image = bitmap.to_pil().convert("L")
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()
    return WitnessPage(number, read_image(image, langs, scale))


def read_image(image, langs: tuple[str, ...], scale: float = SCALE, sparse: bool = False
               ) -> tuple[WitnessWord, ...]:
    """The words Tesseract reads in `image` (a greyscale PIL image of a page rendered at
    `scale`): read_page's reading, and text_trust.flag_missing's of a page it rendered and
    masked itself, which reads it as sparse text (`sparse`: --psm 11, scattered words in no
    order). Raises on any failure."""
    with tempfile.TemporaryDirectory(prefix="reflow-witness-") as tmp:
        png = Path(tmp) / "page.png"
        image.save(png)
        env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        done = subprocess.run(["tesseract", str(png), "stdout", "-l", "+".join(langs),
                               "--dpi", str(round(72 * scale)),
                               *(("--psm", "11") if sparse else ()), "tsv"],
                              capture_output=True, text=True, env=env, timeout=TIMEOUT,
                              check=False)
    if done.returncode != 0:
        tail = " | ".join((done.stderr or "").strip().splitlines()[-2:])
        raise RuntimeError(f"tesseract exited {done.returncode}: {tail}")
    return parse_tsv(done.stdout, scale)


def witness_page(working: Path, number: int, langs: tuple[str, ...],
                 read: Callable[[Path, int, tuple[str, ...]], WitnessPage] | None = None
                 ) -> WitnessPage:
    """`read` (`read_page` by default; reflow.prepare.witness_reader reads through the stage
    cache) of one page; a failure is a WitnessPage with its error. Runs in a pool worker or in
    the caller."""
    try:
        return (read or read_page)(working, number, langs)
    except Exception as error:  # noqa: BLE001 - an unread page is recorded, never fatal
        return WitnessPage(number, error=f"{type(error).__name__}: {error}")


def read_pages(working: Path, pages: dict[int, tuple[str, ...]], *,
               executor: Executor | None = None,
               read: Callable[[Path, int, tuple[str, ...]], WitnessPage] | None = None,
               lock: AbstractContextManager | None = None) -> dict[int, WitnessPage]:
    """Each page of `pages` (number -> languages) read by `witness_page`: one task per page on
    `executor` when given (one page too: the pool's workers hold the worker slots and their own
    pdfium), else one after another here under `lock`, the run's pdfium lock, since the render
    shares this process's pdfium; gathered in page order. Every page is in the result, read or
    with its error. `read` must pickle (a module function or a partial of one) when there is an
    executor."""
    order = sorted(pages)
    if executor is None:
        with lock if lock is not None else nullcontext():
            return {n: witness_page(working, n, pages[n], read) for n in order}
    futures = [executor.submit(witness_page, working, n, pages[n], read) for n in order]
    out: dict[int, WitnessPage] = {}
    for n, future in zip(order, futures, strict=True):
        try:
            out[n] = future.result()
        except Exception as error:  # noqa: BLE001 - a broken pool reads nothing, it fails nothing
            out[n] = WitnessPage(n, error=f"{type(error).__name__}: {error}")
    return out
