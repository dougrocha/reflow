"""Make an image-only "scanned" copy of some pages of a PDF, the way samples/scan.pdf was made.

Each page is rendered at 150 dpi in grayscale (form fields drawn as they print) and saved as one
JPEG per page with Pillow, with fixed dates so the same inputs give the same bytes.

    .venv/bin/python scripts/make_scan.py SOURCE.pdf OUT.pdf --pages 1,3,4
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pypdfium2 as pdfium

DPI = 150
FIXED = time.strptime("2026-10-21", "%Y-%m-%d")


def make_scan(source: Path, out: Path, pages: list[int]) -> None:
    pdf = pdfium.PdfDocument(source)
    try:
        images = [pdf[n - 1].render(scale=DPI / 72, grayscale=True, may_draw_forms=True)
                  .to_pil().convert("L") for n in pages]
    finally:
        pdf.close()
    images[0].save(out, save_all=True, append_images=images[1:], resolution=float(DPI),
                   title=out.stem, creationDate=FIXED, modDate=FIXED)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--pages", required=True, help="1-based page numbers, comma-separated")
    args = parser.parse_args()
    make_scan(args.source, args.out, [int(p) for p in args.pages.split(",")])


if __name__ == "__main__":
    main()
