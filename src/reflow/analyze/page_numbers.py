"""Page labels: the file's own, completed from the printed page numbers in the margins."""

from __future__ import annotations

from statistics import mode

from reflow.analyze.artifacts import Folio, printed_numbers
from reflow.extract.pages import PageContent
from reflow.model import Document


def fill_labels(doc: Document, pages: list[PageContent] | None) -> None:
    """Give every page a label. Ingest recorded what the file declares (PageInfo.label, ""
    when nothing). A file with real labels (one that is not just the page's own number) keeps
    them and blank pages get their number. Otherwise the printed page numbers in `pages`
    (the engine's lines; None for --engine docling) are inferred and win where they exist,
    and every other page gets its number. The only writer of PageInfo.label after ingest."""
    real = any(p.label and p.label != str(p.number) for p in doc.pages)
    inferred: dict[int, str] = {}
    if not real and pages is not None:
        candidates = printed_numbers(pages)
        if candidates:
            inferred = infer_printed_labels(candidates, len(doc.pages))
    for page in doc.pages:
        if page.number in inferred:
            page.label = inferred[page.number]
        elif not page.label:
            page.label = str(page.number)


MIN_PAGES = 5  # pages that print the dominant offset, at least
MIN_SHARE = 0.6  # ...and this share of the pages that print a number


def infer_printed_labels(candidates: dict[int, list[Folio]], page_count: int) -> dict[int, str]:
    """Page labels from the printed page numbers found in margins: `candidates` are the folios of
    each page (1-based). The labels follow the document's dominant offset (printed value - page),
    on the pages that print it, and fill a gap between two pages that agree on it. Roman
    numerals count as a page that prints a number but give no offset. Pages without a label are
    left out."""
    offsets = [(page, f.value - page) for page, folios in candidates.items()
               for f in folios if not f.roman]
    if not offsets:
        return {}
    dominant = mode(offset for _, offset in offsets)
    on_dominant = sum(1 for _, offset in offsets if offset == dominant)
    if on_dominant < MIN_PAGES or on_dominant < MIN_SHARE * len(candidates):
        return {}

    result: dict[int, str] = {}
    for page, folios in candidates.items():
        for f in folios:
            if not f.roman and f.value - page == dominant:
                result[page] = str(f.value)
                break

    for page in range(1, page_count + 1):
        if page in result:
            continue
        left = next((n for n in range(page - 1, 0, -1) if n in result), None)
        right = next((n for n in range(page + 1, page_count + 1) if n in result), None)
        if left is not None and right is not None \
                and int(result[right]) - int(result[left]) == right - left:
            result[page] = str(int(result[left]) + (page - left))
    return result
