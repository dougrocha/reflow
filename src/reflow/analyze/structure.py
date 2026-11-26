"""The structure pass: the tree post-passes, in the one order their data dependence allows.

Pure over the model and the engine's lines: no file, no pdfium, no pool. New tree passes go
here, not in reflow.convert."""

from __future__ import annotations

from reflow.analyze import notes, ocr_words, row_headers, sections
from reflow.analyze.headings import body_size
from reflow.analyze.engine import Backbone
from reflow.analyze.page_numbers import fill_labels
from reflow.analyze.table_evidence import flag_structure
from reflow.analyze.text_evidence import flag_text
from reflow.run import Run


def finish(run: Run, backbone: Backbone) -> None:
    """Make the backbone a finished tree. The order is data dependence: flag_structure reads
    tables as the engine built them → row_headers.apply after fuse filled authoritative /TH
    scope, so the heuristic only fills gaps → notes (need backbone.pages; before sections so
    NOTE nodes nest) → ocr_words.flag, on the final text → nest_sections (last: every earlier
    pass reads the flat list) → fill_labels → ensure_unique_ids after every node exists
    (SECTIONs included) → check_inline on every node, which raises if a span or mark no
    longer fits its text (the renderer trusts them)."""
    flag_structure(run.doc.root)
    flag_text(run.doc, backbone.pages)
    row_headers.apply(run.doc.root)
    if backbone.pages:
        notes.attach_footnotes(run.doc.root.children, backbone.pages, body_size(backbone.pages))
    ocr_words.flag(run.doc, backbone.pages)
    run.doc.root.children = sections.nest_sections(run.doc.root.children)
    fill_labels(run.doc, backbone.pages)
    run.doc.ensure_unique_ids()
    for node in run.doc.root.walk():
        node.check_inline()
