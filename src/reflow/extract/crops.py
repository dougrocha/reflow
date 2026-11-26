"""Crop the figures from their pages as WebP, in parallel. The crops are model code: the figure
pass reads their pixels to tell a logo or a blank band from a picture (analyze.enrich.mark_roles),
so they are in the model's key (reflow.saved), not the renderer's."""

from __future__ import annotations

from concurrent.futures import Executor
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pypdfium2 as pdfium

from reflow.model import BBox
from reflow.pool import submit_runs

FIGURE_SCALE = 2.0  # px per point, so figures stay sharp when zoomed


def _crop_figures(pdf: str, by_page: dict[int, list[tuple[BBox, str]]], out_dir: str,
                  quality: int) -> dict[str, tuple[int, int]]:
    """Render the pages in `by_page` and crop each page's figures to out_dir (one worker's batch)."""
    sizes: dict[str, tuple[int, int]] = {}
    doc = pdfium.PdfDocument(pdf)
    try:
        for number, items in by_page.items():
            page = doc[number - 1]
            try:
                image = page.render(scale=FIGURE_SCALE).to_pil()
            finally:
                page.close()
            for (x0, top, x1, bottom), name in items:
                box = tuple(max(0, round(v * FIGURE_SCALE)) for v in (x0, top, x1, bottom))
                crop = image.crop(box)
                crop.save(Path(out_dir) / name, "WEBP", quality=quality, method=4)
                sizes[name] = crop.size
    finally:
        doc.close()
    return sizes


def render_figures(pdf: Path, figures: list[tuple[int, BBox, str]], out_dir: Path,
                   quality: int = 80, *, executor: Executor | None = None, jobs: int = 1,
                   lock: AbstractContextManager | None = None) -> dict[str, tuple[int, int]]:
    """Crop figures from their pages. `figures` is (page, bbox, filename); returns the
    pixel size of each file, for width/height attributes (no layout shift). With an `executor`
    the pages are cut into runs that go to it; each file is cropped from its own page, so the
    files do not depend on the batching. Cropping in this process holds `lock` (pdfium's)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    by_page: dict[int, list[tuple[BBox, str]]] = {}
    for number, bbox, name in figures:
        by_page.setdefault(number, []).append((bbox, name))
    if executor is None or jobs <= 1 or len(by_page) < 2:
        with lock or nullcontext():
            return _crop_figures(str(pdf), by_page, str(out_dir), quality)
    numbers = list(by_page)
    size = max(1, -(-len(numbers) // (jobs * 2)))
    calls = [(str(pdf), {n: by_page[n] for n in numbers[i:i + size]}, str(out_dir), quality)
             for i in range(0, len(numbers), size)]
    sizes: dict[str, tuple[int, int]] = {}
    for part in submit_runs(executor, _crop_figures, calls):
        sizes.update(part)
    return sizes
