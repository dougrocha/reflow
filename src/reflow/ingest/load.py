"""Open a PDF with pikepdf and read document-level facts (metadata, tagging, security)."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pikepdf

from reflow.ingest.frame import upright_frame


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def read_meta(pdf: pikepdf.Pdf) -> dict[str, Any]:
    root = pdf.Root
    docinfo = pdf.docinfo

    # XMP dc:title wins over the legacy Info dictionary, as PDF/UA requires XMP.
    # Not used as a context manager: exiting one writes XMP back into docinfo, which
    # would clobber /Title when there is no XMP title.
    title = None
    try:
        xmp = pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False)
        title = _str(xmp.get("dc:title"))
    except Exception:
        pass  # malformed XMP is common; fall back to docinfo
    title = title or _str(docinfo.get("/Title"))

    mark_info = root.get("/MarkInfo")
    viewer_prefs = root.get("/ViewerPreferences")

    return {
        "title": title,
        "lang": _str(root.get("/Lang")),
        "author": _str(docinfo.get("/Author")),
        "producer": _str(docinfo.get("/Producer")),
        "creator": _str(docinfo.get("/Creator")),
        "pdf_version": pdf.pdf_version,
        "page_count": len(pdf.pages),
        "tagged": bool(mark_info is not None and mark_info.get("/Marked")),
        "has_struct_tree": "/StructTreeRoot" in root,
        "display_doc_title": bool(viewer_prefs is not None
                                  and viewer_prefs.get("/DisplayDocTitle")),
        "encrypted": pdf.is_encrypted,
        "allows_accessibility": pdf.allow.accessibility,
        "allows_extract": pdf.allow.extract,
        "repair_warnings": len(pdf.get_warnings()),
    }


def open_pdf(path: Path, password: str = "") -> pikepdf.Pdf:
    """Open (and implicitly repair) a PDF. Raises pikepdf.PasswordError if locked, and
    pikepdf.PdfError with a plain message if the file is not a PDF or cannot be repaired."""
    try:
        return pikepdf.open(path, password=password)
    except pikepdf.PasswordError:
        raise
    except pikepdf.PdfError as e:
        raise pikepdf.PdfError("not a PDF, or too damaged to repair") from e


def restricted_action(allows_extract: bool, allow_restricted: bool) -> str:
    """What to do with a PDF whose owner may forbid text extraction: "proceed"
    (extraction permitted), "flag" (convert and record an extract-forbidden fact; the caller
    passed --allow-restricted) or "refuse" (stop before extracting)."""
    if allows_extract:
        return "proceed"
    return "flag" if allow_restricted else "refuse"


def normalize(src: Path, dst: Path, password: str = "", *,
              lock: AbstractContextManager | None = None) -> None:
    """Write the canonical working copy: repaired by qpdf, decrypted, each page in the frame its
    text is read in (reflow.ingest.frame), and linearized.

    Every stage after ingest reads this copy, never the original. qpdf tolerates damage
    that other readers don't (e.g. pdfplumber sees 0 pages when junk precedes `%PDF-`),
    and linearizing makes the same file servable with HTTP range requests. The copy is the
    same bytes each time it is made (its /ID is derived from its content, not the clock), so a
    stage keyed on those bytes (reflow.stagecache) still hits after the copy is made again.
    `lock` is pdfium's, held while the frame reads a turned page's text.
    """
    with open_pdf(src, password) as pdf:
        upright_frame(pdf, lock)
        pdf.save(dst, linearize=True, deterministic_id=True)
