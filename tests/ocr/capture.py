"""Capture the hOCR fixtures of the grade report through the shipped OCRmyPDF path.

    python tests/ocr/capture.py            # every page of CALIBRATION, serially and niced

Each page is OCR'd alone (`--pages N`) in the mode reflow's OCR plan gives it, with
`--pdf-renderer fpdf2`, as reflow.ingest.ocr.OcrmypdfRunner runs it. The hOCR of every page is
kept; the page image only for the pages whose colour the report measures, downscaled to
FIXTURE_DPI. Committed samples go to tests/ocr/fixtures/; the gitignored DocAccess samples to
samples/docaccess/fixtures/ocr/ in the main checkout, which test_grade_words reads from."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "pages"))
from corpus import MAIN, source  # noqa: E402

FIXTURE_DPI = 150
# (corpus slug, page, mode, keep the image)
CALIBRATION = [
    ("docaccess-budget", 4, "redo", True),
    ("docaccess-chem-math", 1, "redo", True),
    ("docaccess-permit", 1, "redo", False),
    ("docaccess-permit", 3, "redo", False),
    ("docaccess-legislation", 1, "redo", False),
    ("docaccess-legislation", 3, "redo", False),
    ("docaccess-legislation", 5, "redo", False),
    ("docaccess-brochure", 2, "redo", False),
    ("docaccess-brochure", 3, "redo", False),
    ("docaccess-audit", 30, "force", False),
    ("docaccess-carmel", 4, "redo", False),
    ("naca", 1, "redo", True),
    ("naca", 8, "redo", False),
    ("scan", 1, "redo", True),
]
COMMITTED = HERE / "fixtures"
PRIVATE = MAIN / "samples" / "docaccess" / "fixtures" / "ocr"


def fixture_dir(slug: str) -> Path:
    return PRIVATE if slug.startswith("docaccess-") else COMMITTED


def capture(slug: str, page: int, mode: str, keep_image: bool) -> None:
    from PIL import Image

    out = fixture_dir(slug)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        subprocess.run(["nice", "ocrmypdf", "--pages", str(page), f"--{mode}-ocr",
                        "--pdf-renderer", "fpdf2", "--output-type", "pdf", "-l", "eng",
                        "--plugin", str(HERE / "capture_plugin.py"), "--capture-dir", str(tmp),
                        str(source(slug)), str(tmp / "out.pdf")], check=True, capture_output=True)
        name = f"{slug}-{page}"
        shutil.copyfile(tmp / f"{page}.hocr", out / f"{name}.hocr")
        with Image.open(tmp / f"{page}.png") as image:
            meta = {"slug": slug, "page": page, "mode": mode, "image_px": list(image.size),
                    "dpi": [round(float(v)) for v in image.info.get("dpi", (300, 300))]}
            if keep_image:
                scale = FIXTURE_DPI / meta["dpi"][0]
                small = image.convert("RGB").resize((round(image.width * scale),
                                                     round(image.height * scale)), Image.LANCZOS)
                small.save(out / f"{name}.jpg", quality=85, optimize=True)
        (out / f"{name}.json").write_text(json.dumps(meta) + "\n")
    print(f"captured {name} ({mode})")


if __name__ == "__main__":
    for item in CALIBRATION:
        capture(*item)
