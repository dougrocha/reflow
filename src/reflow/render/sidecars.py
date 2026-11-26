"""Per-page layout sidecars.

document.json is up to 109 MB (budget-appendix), and the inspector and "Show on page" only ever
need one page. So the conversion writes `layout/0001.json` per page, a few KB each, and the web
reads those. document.json stays as the complete model but is not published (it is an INTERNAL
output name in reflow.names); the web reads it only for the full text of the blocks whose
sidecar text was cut at TEXT_LIMIT.

Sidecar shape (one file per page; keys short, like the manifest):

    {"n": 12, "w": 612, "h": 792,
     "nodes": [{"id", "role", "bbox", "source", "confidence", "text"(≤200 chars),
                "attrs": {only the keys the inspector shows: why, method, rows, columns,
                          header_rows, needs, warn, alt, label, marker, level},
                "facts": [...]}],                       # every node with a bbox on the page
     "sections": [{"id": <section heading id>, "level": 2, "bbox": [...]}],
     "labels": {fact code: review label, for the codes this page's facts use},
     "review": [item ids on the page]}

The trace (`trace/NNNN.json`) stays separate: it is the heuristic engine's own record and
is written only by that engine; the sidecar is engine-neutral.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reflow.analyze.confidence import CODES
from reflow.analyze.sections import heading_of, section_region
from reflow.model import Document, Role
from reflow.names import LAYOUT_DIR

TEXT_LIMIT = 200
INSPECTOR_ATTRS = ("why", "method", "rows", "columns", "header_rows", "needs", "warn",
                   "alt", "label", "marker", "level", "scope", "ocr_guess")


def sidecar_name(page: int) -> str:
    return f"{LAYOUT_DIR}/{page:04d}.json"


def _by_page(doc: Document) -> tuple[dict[int, list], dict[int, list]]:
    """The tree walked once: its nodes with a box, and its sections, grouped by page (the
    section's by the page of its heading), each in document order."""
    nodes: dict[int, list] = {}
    sections: dict[int, list] = {}
    for node in doc.root.walk():
        if node.page is not None and node.bbox is not None:
            nodes.setdefault(node.page, []).append(node)
        if node.role == Role.SECTION:
            sections.setdefault(heading_of(node).page, []).append(node)
    return nodes, sections


def page_sidecar(doc: Document, page: int,
                 grouped: tuple[dict[int, list], dict[int, list]] | None = None) -> dict[str, Any]:
    """The sidecar for one page (see the module docstring). `grouped` is `_by_page(doc)`, to
    walk the tree once for all its pages rather than once for each."""
    info = next((p for p in doc.pages if p.number == page), None)
    if info is None:
        raise KeyError(f"no page {page} in the document")
    on_page, sections = grouped or _by_page(doc)
    nodes: list[dict[str, Any]] = []
    review: list[str] = []
    for node in on_page.get(page, ()):
        nodes.append({
            "id": node.id,
            "role": node.role.value,
            "bbox": list(node.bbox),
            "source": node.source.value,
            "confidence": node.confidence,
            "text": node.text[:TEXT_LIMIT] if node.text is not None else None,
            "attrs": {k: v for k, v in node.attrs.items() if k in INSPECTOR_ATTRS},
            "facts": [f.to_dict() for f in node.facts],
        })
        if any(f.code in CODES and CODES[f.code].severity for f in node.facts):
            review.append(node.id)
    codes = sorted({f["code"] for n in nodes for f in n["facts"]})
    return {
        "n": page,
        "w": info.width,
        "h": info.height,
        "sections": [
            {"id": heading_of(sec).id, "level": sec.attrs.get("level", 0), "bbox": list(box)}
            for sec in sections.get(page, ()) if (box := section_region(sec, page))],
        "nodes": nodes,
        "labels": {c: CODES[c].label for c in codes if c in CODES},
        "review": review,
    }


def write_page_layouts(doc: Document, out_dir: Path) -> int:
    """Write every page's sidecar under out_dir/layout/. Returns the count written."""
    layout_dir = out_dir / LAYOUT_DIR
    layout_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    grouped = _by_page(doc)
    for info in doc.pages:
        sidecar = page_sidecar(doc, info.number, grouped)
        path = out_dir / sidecar_name(info.number)
        path.write_text(json.dumps(sidecar, separators=(",", ":"), ensure_ascii=False),
                        encoding="utf-8")
        count += 1
    return count
