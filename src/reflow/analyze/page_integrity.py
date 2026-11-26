"""Flag rendered content that has no text in the finished transcript."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pypdfium2 as pdfium

from reflow.analyze.evidence import useful_alt
from reflow.model import FACT_SCAN_NOISY, Document, Fact, Node, Role, Source
from reflow.text_quality import fold

if TYPE_CHECKING:
    from reflow.extract.pages import PageContent


def flag_empty(doc: Document, working: Path) -> None:
    """Check every textless page, including figure-only and apparently empty source pages."""
    with_text = {n.page for n in doc.root.walk()
                 if n.role not in (Role.ARTIFACT, Role.TR) and n.text and n.text.strip()}
    empty = [p for p in doc.pages if p.number not in with_text]
    if not empty:
        return
    pdf = pdfium.PdfDocument(working)
    try:
        for info in empty:
            page = pdf[info.number - 1]
            try:
                bitmap = page.render(scale=1)
                try:
                    histogram = bitmap.to_pil().convert("L").histogram()
                finally:
                    bitmap.close()
            finally:
                page.close()
            # Ignore isolated scan dust, but count even a short missing heading.
            ink = sum(histogram[:230])
            if ink >= 24 and not any(f.code == "empty-transcript" for f in info.facts):
                info.facts.append(Fact("empty-transcript", Source.HEURISTIC,
                                       "Rendered page has content but its transcript is empty",
                                       {"ink_pixels": ink}))
    finally:
        pdf.close()


# The findings that already send a reviewer to what a figure shows: its words are theirs, as an
# unread figure's error covers the OCR words omitted in it (text_evidence.flag_ocr_marks).
UNDER_REVIEW = frozenset({"alt-missing", "decorative-guessed", "unread-handwriting",
                          "unread-text", "unread-signature", "suggested-alt",
                          "suggested-description", "suggested-alt-uncertain"})


def covers_words(figure: Node) -> bool:
    """Whether the words printed in a figure's box are accounted for, so text_trust.flag_missing
    does not report them: the figure carries a finding UNDER_REVIEW, or its alt describes it.
    An alt describes it when it says something (evidence.useful_alt) and render.alt.figure_alt
    shows it: not decorative, and not only a repeat of the caption (shown as the figure's kind).
    A fallback, a generic word or a decorative "" never hides a missing paragraph."""
    if any(f.code in UNDER_REVIEW for f in figure.facts) or figure.attrs.get("unread"):
        return True
    if figure.attrs.get("decorative"):
        return False
    alt = useful_alt(figure.attrs.get("alt"))
    caption = next((c.text or "" for c in figure.children if c.role == Role.CAPTION), "")
    return alt is not None and not (caption and fold(alt) == fold(caption))


def flag_scan_noisy(doc: Document, contents: list[PageContent] | None) -> None:
    """Flag the pages whose image is too grainy for the page scan to find pictures in (status
    "noisy"): their pictures may be missing from the transcript, so a reviewer checks."""
    if contents is None:
        return
    noisy = {p.number for p in contents if p.scan.status == "noisy"}
    for info in doc.pages:
        if info.number in noisy and not any(f.code == FACT_SCAN_NOISY for f in info.facts):
            info.facts.append(Fact(FACT_SCAN_NOISY, Source.HEURISTIC,
                                   "Grain covers most of the page image, so no picture was "
                                   "looked for in it; check for missing figures"))

