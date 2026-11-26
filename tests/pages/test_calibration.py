"""Calibration of the review queue: does it flag the pages the page checks know are bad?

Joins each page-check spec's xfail pages with that document's review.json and asserts that the
queue is neither blind to the known-bad pages nor so noisy that it stops being usable:
at least PAGE_FLOOR of the xfail pages carry a review item, the documents in MIN_ITEMS have at
least that many items, and no document's non-error items on born-digital content exceed
MAX_ITEM_SHARE of its born-digital nodes (error items and OCR content are not counted).
Run with `pytest -o pythonpath=src -m pages tests/pages/test_calibration.py`.
"""

from __future__ import annotations

import json

import pytest

from reflow.names import DOCUMENT_JSON, REVIEW_JSON

from corpus import Corpus, absent_reason, converted_whole, quick_mode
from specs import load_specs

pytestmark = pytest.mark.pages

# Share of xfail pages that carry at least one review item on that page (the issue's 80% aim).
PAGE_FLOOR = 0.80
# Documents whose queue must leave the single digits.
MIN_ITEMS = {"budget-appendix": 20, "p17": 20}
MAX_ITEM_SHARE = 0.05
# Items the text layer proves by construction: a U+FFFD where a letter or vowel sign was,
# or a legacy-encoded font. They are not noise, so they are left out of the cap.
LOST_TEXT = "unicode-errors"
# Documents with no xfail that the queue cap measures anyway: an all-pass spec would otherwise
# never enter test_queue_stays_usable. The calendar's months are hundreds of nodes, the
# directory's counties and providers a thousand.
CAP_DOCS = {"docaccess-calendar", "docaccess-directory"}


# A quick-mode excerpt's counts say nothing about the whole document, so only the queue cap (over
# the documents quick mode converts whole) runs there.
whole_documents_only = pytest.mark.skipif(quick_mode(), reason="quick mode: calibration counts whole documents")


@pytest.fixture(scope="module")
def corpus(request: pytest.FixtureRequest) -> Corpus:
    c = Corpus()
    covered = _calibrated_docs()
    if quick_mode():
        skipped = sorted(_all_docs() - set(covered))
        reporter = request.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_line(f"\nquick mode, queue covered: {', '.join(covered)}")
            reporter.write_line(f"quick mode, queue skipped (excerpted): {', '.join(skipped)}")
    c.prepare(covered)
    return c


def _all_docs() -> set[str]:
    return {doc for doc, _, _ in _xfail_pages()} | set(MIN_ITEMS) | CAP_DOCS


def _calibrated_docs() -> list[str]:
    """Every document the calibration measures. In quick mode only the ones it converts whole:
    an excerpt's node and item counts say nothing about the whole document's queue."""
    return sorted(d for d in _all_docs() if absent_reason(d) is None
                  and (not quick_mode() or converted_whole(d)))


def _review(corpus: Corpus, slug: str) -> list[dict]:
    return json.loads((corpus.doc(slug).out_dir / REVIEW_JSON).read_text(encoding="utf-8"))


def _count_nodes(node: dict, ocr_pages: set[int]) -> int:
    born_digital = node["source"] != "ocr" and node.get("page") not in ocr_pages
    return int(born_digital) + sum(_count_nodes(c, ocr_pages) for c in node.get("children", []))


def _xfail_pages() -> list[tuple[str, int, str]]:
    pages = {(s.doc, s.page, s.id) for s in load_specs() if any(a.xfail for a in s.assertions)}
    return sorted(pages)


@whole_documents_only
def test_xfail_pages_carry_review_items(corpus: Corpus) -> None:
    pages = _xfail_pages()
    assert pages, "no xfail pages: the calibration has nothing to measure"
    flagged: dict[str, set[int]] = {}
    missed = []
    for slug, page, spec_id in pages:
        if slug not in flagged:
            flagged[slug] = {i["page"] for i in _review(corpus, slug) if i.get("page")}
        if page not in flagged[slug]:
            missed.append(spec_id)
    share = 1 - len(missed) / len(pages)
    assert share >= PAGE_FLOOR, (f"{share:.1%} of {len(pages)} xfail pages have a review item "
                                 f"(floor {PAGE_FLOOR:.0%}); none on: {', '.join(missed)}")


@whole_documents_only
def test_order_xfail_pages_carry_a_reading_order_item(corpus: Corpus) -> None:
    """Every page with an xfail contains_in_order check (a known reading-order error) gets the
    "order-disagreement" review item."""
    pages = sorted({(s.doc, s.page, s.id) for s in load_specs()
                    for a in s.assertions if a.check == "contains_in_order" and a.xfail})
    assert pages, "no contains_in_order xfail: nothing to measure"
    missed = [spec_id for slug, page, spec_id in pages
              if not any(i["page"] == page and i["warn"] == "order-disagreement"
                         for i in _review(corpus, slug))]
    assert not missed, f"no reading-order review item on: {', '.join(missed)}"


@whole_documents_only
@pytest.mark.parametrize("slug,minimum", sorted(MIN_ITEMS.items()))
def test_big_documents_leave_the_single_digits(corpus: Corpus, slug: str, minimum: int) -> None:
    count = len(_review(corpus, slug))
    assert count >= minimum, f"{slug}: {count} review items, expected at least {minimum}"


def queue_over_cap(corpus: Corpus, slugs: list[str]) -> list[str]:
    """The documents among `slugs` whose review queue is above MAX_ITEM_SHARE of their
    born-digital nodes. Required errors and OCR content are legitimately review-worthy, so
    neither belongs in the capped item count. OCR nodes also leave the denominator so they
    cannot dilute born-digital false positives."""
    over = []
    for slug in slugs:
        doc = json.loads((corpus.doc(slug).out_dir / DOCUMENT_JSON).read_text(encoding="utf-8"))
        # OCR provenance is recorded on the page; extracted nodes may still say heuristic.
        # A "mixed" page is a scan that already carried an OCR text layer (naca).
        ocr_pages = {p["number"] for p in doc["pages"]
                     if p.get("kind") == "mixed"
                     or any(f["code"] == "ocr-page" for f in p.get("facts", []))}
        blank = {p["number"] for p in doc["pages"] if p.get("kind") == "empty"}
        if len(ocr_pages | blank) == len(doc["pages"]):
            continue  # all OCR or blank: no born-digital content to cap
        nodes = _count_nodes(doc["root"], ocr_pages)
        count = sum(1 for i in _review(corpus, slug)
                    if i["severity"] != "error" and i["source"] != "ocr"
                    and i.get("page") not in ocr_pages and i.get("warn") != LOST_TEXT)
        if count > MAX_ITEM_SHARE * nodes:
            over.append(f"{slug}: {count} items for {nodes} nodes ({count / nodes:.1%})")
    return over


def test_queue_stays_usable(corpus: Corpus) -> None:
    over = queue_over_cap(corpus, _calibrated_docs())
    assert not over, "review queue above 5% of born-digital nodes: " + "; ".join(over)
