"""Inline marks on the real sample documents: the style marks sit on the words they cover,
no heading carries one, and no text edit lost a mark.

Reads the cached `document.json` of the DocAccess samples, fw9, resnet, p17 and arxiv, the
documents whose page checks guard links, notes, lists and italics. Run with
`pytest -o pythonpath=src -m pages tests/pages/test_mark_layer.py`.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from corpus import DOCUMENTS, Corpus
from specs import load_specs
from reflow.model import ACCENTS, HEADING_ROLES, Document, Role, Source

pytestmark = pytest.mark.pages

SLUGS = sorted(slug for slug in DOCUMENTS
               if slug.startswith("docaccess-") or slug in ("fw9", "resnet", "p17", "arxiv"))
MARKED_ROLES = {Role.P, Role.LI, Role.NOTE, Role.CAPTION}
# A heading's style is its level, so it carries no emphasis, but an added section's title is an
# insertion like the rest of the section. Struck text is never a heading.
HEADING_MARKS = {"ins"}
# The documents test_the_marks_of_the_sample_documents_are_counted reads.
COUNTED = ("docaccess-assessment-appeal", "docaccess-rfp", "p17")


def selected_slugs(items: list[pytest.Item]) -> list[str]:
    """The documents the selected tests of this module read (`-k resnet` reads only resnet)."""
    slugs: set[str] = set()
    for item in items:
        if item.module.__name__ != __name__:
            continue
        if hasattr(item, "callspec") and "slug" in item.callspec.params:
            slugs.add(item.callspec.params["slug"])
        elif item.originalname == "test_the_marks_of_the_sample_documents_are_counted":
            slugs.update(COUNTED)
    return sorted(slugs)


@pytest.fixture(scope="module")
def corpus(request: pytest.FixtureRequest) -> Corpus:
    slugs = selected_slugs(request.session.items)
    needed: dict[str, set[int]] = {}
    for spec in load_specs():
        if spec.doc in slugs:
            needed.setdefault(spec.doc, set()).add(spec.page)
    c = Corpus()
    c.prepare(slugs, pages=needed)
    return c


def _document(corpus: Corpus, slug: str) -> Document:
    text = (corpus.doc(slug).out_dir / "document.json").read_text(encoding="utf-8")
    return Document.from_dict(json.loads(text))


@pytest.mark.parametrize("slug", SLUGS)
def test_no_mark_is_lost_stale_or_on_a_heading(corpus: Corpus, slug: str) -> None:
    doc = _document(corpus, slug)
    for node in doc.root.walk():
        assert not [f for f in node.facts if f.code == "ocr-mark-lost"], (
            f"{slug}: a mark was lost on {node.text!r}")
        for mark in node.marks:
            assert (node.text or "")[mark.start:mark.end] == mark.text, (
                f"{slug}: {mark} is stale on {node.text!r}")
            assert node.role in MARKED_ROLES or (
                node.role in HEADING_ROLES and mark.kind in HEADING_MARKS), (
                f"{slug}: {mark.kind} mark on a {node.role} node")
            # A doubt and a tracked change are readings of OCR text only; emphasis,
            # raised text and a printed colour are the page's own.
            assert mark.kind in ("strong", "em", "sup", *ACCENTS) or (
                mark.kind in ("doubt", "ins", "del", "del-unread") and node.source == Source.OCR), (
                f"{slug}: {mark.kind} mark on a {node.source} node")


def test_the_marks_of_the_sample_documents_are_counted(corpus: Corpus) -> None:
    """The appeal form's bold lead-ins, the RFP's bold and italic words and p17's emphasis are
    on the documents (guards against marks_for quietly returning nothing)."""
    counts = {slug: Counter(m.kind for n in _document(corpus, slug).root.walk() for m in n.marks)
              for slug in COUNTED}
    for slug, kinds in counts.items():
        assert kinds["strong"] + kinds["em"] > 0, f"{slug} has no strong or em mark"
