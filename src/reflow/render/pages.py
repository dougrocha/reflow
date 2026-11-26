"""Render page images ("view original") as WebP, in parallel (the figure crops are model
code: reflow.extract.crops).

Encoding WebP costs ~10x more than rendering, so pages are spread over worker
processes. Each worker opens its own PdfDocument: pdfium isn't thread-safe.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor, Future, as_completed, wait
from pathlib import Path

import pypdfium2 as pdfium

from reflow.pageimages import PAGE_WIDTH

QUALITY = 60
BATCH = 16  # pages per task


def page_filename(number: int, width: int | None = None) -> str:
    """NNNN.webp for the base width (None), NNNN-<width>.webp for a larger one."""
    return f"{number:04d}.webp" if width is None else f"{number:04d}-{width}.webp"


def _render_batch(pdf: str, numbers: list[int], out_dir: str, width: int,
                  quality: int, extra_widths: tuple[int, ...] = ()) -> list[int]:
    doc = pdfium.PdfDocument(pdf)
    try:
        for n in numbers:
            page = doc[n - 1]
            try:
                for w in (width, *extra_widths):
                    image = page.render(scale=w / page.get_width()).to_pil()
                    name = page_filename(n, None if w == width else w)
                    image.save(Path(out_dir) / name, "WEBP", quality=quality, method=4)
            finally:
                page.close()
    finally:
        doc.close()
    return numbers


class PageImages:
    """Page images being rendered on a pool: started with `start_page_images`, collected with
    `finish`. A conversion starts them as soon as the working PDF is final and collects them in
    publish, so the pool works while the parent does its serial passes."""

    def __init__(self, futures: list[Future], total: int) -> None:
        self.futures, self.total = futures, total

    def finish(self, on_done: Callable[[int], None] | None = None) -> None:
        try:
            for future in as_completed(self.futures):
                done = future.result()
                if on_done:
                    on_done(len(done))
        except BaseException:
            self.cancel()
            raise

    def cancel(self) -> None:
        """Drop what has not started and wait for what has, so nothing writes after the caller
        removes the folder."""
        for future in self.futures:
            future.cancel()
        wait(self.futures)


def _batches(page_count: int, workers: int | None) -> list[list[int]]:
    """Runs of pages for the pool: BATCH at most, smaller so that every worker gets about four
    (a 142-page document in runs of 16 leaves 9 runs for 8 workers: two rounds)."""
    size = BATCH if not workers else max(1, min(BATCH, -(-page_count // (workers * 4))))
    return [list(range(i, min(i + size, page_count + 1))) for i in range(1, page_count + 1, size)]


def start_page_images(pdf: Path, out_dir: Path, page_count: int, executor: Executor, *,
                      width: int = PAGE_WIDTH, quality: int = QUALITY,
                      extra_widths: tuple[int, ...] = (), workers: int | None = None
                      ) -> PageImages:
    """Submit the page-image batches to `executor` and return at once."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return PageImages([executor.submit(_render_batch, str(pdf), b, str(out_dir), width, quality,
                                       extra_widths)
                       for b in _batches(page_count, workers)], page_count)


def render_pages(pdf: Path, out_dir: Path, page_count: int, *, width: int = PAGE_WIDTH,
                 quality: int = QUALITY, extra_widths: tuple[int, ...] = (),
                 workers: int | None = None, on_done: Callable[[int], None] | None = None,
                 executor: Executor | None = None) -> None:
    """Write out_dir/0001.webp... for every page, plus 0001-<w>.webp for each of `extra_widths`
    (the srcset candidates). `on_done(n)` reports pages finished. The batches go to `executor`
    (the run's process pool), or are rendered in this process without one. Each page is rendered
    and encoded on its own, so the files don't depend on the batching."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if executor is not None:
        start_page_images(pdf, out_dir, page_count, executor, width=width, quality=quality,
                          extra_widths=extra_widths, workers=workers).finish(on_done)
        return
    for b in _batches(page_count, None):
        _render_batch(str(pdf), b, str(out_dir), width, quality, extra_widths)
        if on_done:
            on_done(len(b))
