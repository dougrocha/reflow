"""`reflow doctor`: what this machine can run (SPEC §10 risk 5)."""

from __future__ import annotations

import importlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import pikepdf


@dataclass(frozen=True)
class Probe:
    name: str          # "tesseract", "ocrmypdf", "docling", "cuda", "qpdf", "chromium"
    needed_for: str    # "OCR of scanned pages (--ocr)"
    status: Literal["ok", "missing", "broken"]
    detail: str = ""   # version, path, or the import error


_CHROMIUM = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome")

type Outcome = tuple[Literal["ok", "missing", "broken"], str]   # status, detail


def _version(module) -> str:
    return str(getattr(module, "__version__", "") or "installed")


def _qpdf() -> Outcome:
    return "ok", f"libqpdf {pikepdf.__libqpdf_version__} via pikepdf {pikepdf.__version__}"


def _tesseract() -> Outcome:
    path = shutil.which("tesseract")
    return ("ok", path) if path else ("missing", "not on PATH; install the tesseract package")


def _ocrmypdf() -> Outcome:
    try:
        module = importlib.import_module("ocrmypdf")
    except ImportError as e:
        return "missing", f"not installed ({e})"
    return "ok", _version(module)


def _docling() -> Outcome:
    try:
        module = importlib.import_module("docling")
    except ImportError as e:
        return "missing", f"not installed ({e}); pip extra 'docling'"
    return "ok", _version(module)


def _cuda() -> Outcome:
    try:
        torch = importlib.import_module("torch")
    except ImportError as e:
        return "missing", f"torch not installed ({e})"
    if torch.cuda.is_available():
        return "ok", f"{torch.cuda.get_device_name(0)} (torch {_version(torch)})"
    return "missing", f"torch {_version(torch)} sees no CUDA device"


def _chromium() -> Outcome:
    for name in _CHROMIUM:
        if path := shutil.which(name):
            return "ok", path
    return "missing", "no chromium or google-chrome on PATH"


def _verapdf() -> Outcome:
    if path := shutil.which("verapdf"):
        return "ok", path
    return "missing", "no verapdf on PATH"


PROBES: tuple[tuple[str, str, Callable[[], Outcome]], ...] = (
    ("qpdf", "repairing and normalizing the working copy", _qpdf),
    ("tesseract", "OCR of scanned pages (--ocr)", _tesseract),
    ("ocrmypdf", "OCR of scanned pages (--ocr ocrmypdf)", _ocrmypdf),
    ("docling", "ML layout (--engine docling|hybrid) and local alt text", _docling),
    ("cuda", "optional GPU to speed up docling and local alt text (they run on the CPU without one)", _cuda),
    ("chromium", "the web a11y checks", _chromium),
    ("verapdf", "PDF/UA-1 validation in the audit (audit --verapdf)", _verapdf),
)


def probes() -> list[Probe]:
    """Every probe, each one isolated: a broken import never stops the others."""
    out = []
    for name, needed, probe in PROBES:
        try:
            status, detail = probe()
        except Exception as e:  # noqa: BLE001 - a probe that raises is itself the finding
            status, detail = "broken", f"{type(e).__name__}: {e}"
        out.append(Probe(name, needed, status, detail))
    return out
