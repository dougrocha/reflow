"""The hOCR fixtures captured by tests/ocr/capture.py, graded as the plugin grades them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

from reflow.ingest.ocr_trust import Word, grade_words, parse_hocr

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))   # checkout.py: tests/ocr/capture.py runs outside pytest
import checkout  # noqa: E402

COMMITTED = HERE / "fixtures"


PRIVATE = checkout.main_checkout(HERE) / "samples" / "docaccess" / "fixtures" / "ocr"


def path_of(name: str) -> Path:
    """The directory of fixture `name` ("naca-1", "docaccess-budget-4")."""
    return PRIVATE if name.startswith("docaccess-") else COMMITTED


def available(name: str) -> bool:
    return (path_of(name) / f"{name}.hocr").exists()


def meta(name: str) -> dict:
    return json.loads((path_of(name) / f"{name}.json").read_text())


def graded(name: str) -> list[Word]:
    """The words of the fixture, graded with its page image (restored to the OCR image's size,
    so the boxes fall on it) when the fixture kept one."""
    base = path_of(name)
    raw = parse_hocr((base / f"{name}.hocr").read_text(encoding="utf-8"))
    png = base / f"{name}.jpg"
    if not png.exists():
        return grade_words(raw, None)
    with Image.open(png) as small:
        image = small.convert("RGB").resize(tuple(meta(name)["image_px"]), Image.BICUBIC)
    return grade_words(raw, image)


def by_text(words: list[Word], text: str) -> list[Word]:
    return [w for w in words if w.text == text]
