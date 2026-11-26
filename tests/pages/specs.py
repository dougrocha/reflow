"""The page specs (tests/pages/specs/<id>.json): loading them, and running one assertion on a
converted page. The page tests (test_pages.py), the scorecard (report.py) and the
direct-transcription eval (scripts/eval_direct.py) all read and run specs through here."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

from reflow.ingest.ocr import make_runner

from checks import Assertion, Node, assertion_kind, run_check
from corpus import DOCUMENTS, DocOutput, source_text

SPEC_DIR = Path(__file__).resolve().parent / "specs"


@dataclass
class PageSpec:
    id: str  # e.g. "fw9-1"; the file is tests/pages/specs/<id>.json
    doc: str
    page: int
    description: str
    assertions: list[Assertion]


def load_specs(only: str | None = None) -> list[PageSpec]:
    """All page specs, sorted; `only` keeps the ids that contain the substring."""
    specs = []
    has_ocr = make_runner("auto") is not None
    for path in sorted(SPEC_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        spec_id = path.stem
        if only and only not in spec_id:
            continue
        doc, page = data["doc"], int(data["page"])
        if doc not in DOCUMENTS:
            raise ValueError(f"{path.name}: unknown doc {doc!r}")
        asserts = []
        for i, raw in enumerate(data["assertions"]):
            check = assertion_kind(raw)
            if check == "min_recall" and isinstance(raw[check], dict):
                value = raw[check]
                if type(value["floor"]) not in (int, float):
                    raise ValueError(f"{path.name}: min_recall floor must be a number")
                ref = value["reference"]
                if ref["doc"] not in DOCUMENTS:
                    raise ValueError(f"{path.name}: unknown reference doc {ref['doc']!r}")
                if type(ref["page"]) is not int or ref["page"] < 1:
                    raise ValueError(f"{path.name}: reference page must be a positive integer")
            xfail = raw.get("xfail") or (raw.get("xfail_without_ocr") if not has_ocr else None)
            asserts.append(Assertion(check=check, value=raw[check], xfail=xfail,
                                     why=raw.get("why"), index=i, spec_id=spec_id))
        checks = {a.check for a in asserts}
        if "alt_not" in checks and not checks & {"figures", "min_figures"}:
            # A page that lost every picture has no alt to match: it would pass alt_not.
            raise ValueError(f"{path.name}: alt_not needs a figures or min_figures check")
        specs.append(PageSpec(spec_id, doc, page, data.get("description", ""), asserts))
    return specs


def run_assertion(spec: PageSpec, a: Assertion, doc: DocOutput, pg: Node | None = None,
                  pdf: str | None = None) -> None:
    """Run `a`, an assertion of `spec`, on its page of `doc`; raise CheckFailed when it fails.
    `pg` and `pdf` are the page and its text layer when the caller has them already.

    A quick excerpt ranks font sizes over its own pages, not the document's, so heading levels can
    shift: an excerpt checks the heading itself, its level in the full suite."""
    if doc.page_map is not None and a.check == "heading" and not a.xfail:
        a = replace(a, value={k: v for k, v in a.value.items() if k != "levels"})

    def pdf_text() -> str:
        if a.check == "min_recall" and isinstance(a.value, dict):
            ref = a.value["reference"]
            return source_text(ref["doc"], ref["page"])
        return pdf if pdf is not None else doc.pdf_text(spec.page)

    run_check(a, pg if pg is not None else doc.page(spec.page), pdf_text, doc, spec.page)
