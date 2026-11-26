"""Capture what OCRmyPDF hands Tesseract and what Tesseract returns, changing nothing.

Loaded by the system OCRmyPDF (`--plugin tests/ocr/capture_plugin.py --capture-dir DIR`), it
copies each page's hOCR to DIR/<page>.hocr and the page image to DIR/<page>.png. tests/ocr/capture.py
runs it over the calibration pages, in the mode the OCR plan gives each page."""
import shutil
from pathlib import Path

from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOcrEngine


@hookimpl
def add_options(parser):
    parser.add_argument("--capture-dir", metavar="DIR", required=True)


class CaptureEngine(TesseractOcrEngine):
    @staticmethod
    def generate_hocr(input_file, output_hocr, output_text, options):
        TesseractOcrEngine.generate_hocr(input_file, output_hocr, output_text, options)
        page = int(Path(output_hocr).name.split("_")[0])
        dest = Path(options.extra_attrs.get("capture_dir") or getattr(options, "capture_dir"))
        shutil.copyfile(output_hocr, dest / f"{page}.hocr")
        shutil.copyfile(input_file, dest / f"{page}.png")


@hookimpl
def get_ocr_engine(options):
    return CaptureEngine()
