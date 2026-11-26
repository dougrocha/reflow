"""The page-check documents (DOCUMENTS in tests/pages/corpus.py) and the site's sample documents
(SAMPLE_DOCS in web/src/docs.ts) are two lists over one folder of PDFs, samples/. A PDF on both is
the same document, and mostly under one slug. This test states where the two differ, so a new
difference is a decision and not an accident."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/pages"))

from corpus import DOCUMENTS  # noqa: E402

# Page-check slug → the slug of the same PDF on the site, where they differ.
SITE_SLUG = {
    "docaccess-chem-math": "da-chem-math",
    "docaccess-permit": "da-building-permit-applications",
    "docaccess-assessment-appeal": "da-assessment-appeal-application",
    "docaccess-calendar": "da-school-district-calendar",
    "docaccess-legislation": "da-legislation-tracked-changes",
    "docaccess-rfp": "da-complex-rfp-diagrams",
    "docaccess-audit": "da-financial-audit-comprehensive",
    "docaccess-brochure": "da-multi-column-brochure",
    "docaccess-budget": "da-scanned-budget-handwriting",
    "docaccess-directory": "da-directory-providers",
    "docaccess-carmel": "da-timelines-difficult-graphics",
    "az-medsup-outline": "az-medsup-outline-coverage",
    "az-remittance-advice-scan": "az-remittance-advice-medical",
    "az-bluedental-manual": "az-bluedental-provider-manual",
    "az-advance-directive-bilingual": "az-advance-directive-notice-bilingual",
    "az-hca-member-handbook": "az-hca-member-handbook-2026",
    "az-assistant-surgeon-codes": "az-assistant-surgeon-codes-2026-01",
    "az-ma-appeals-form": "az-ma-appeals-form-2026",
    "az-sbc-p7950": "az-sbc-2026-p7950",
    "az-stigma-media-kit": "az-stigma-free-az-media-kit",
}
# Page-check documents the site does not list.
NOT_ON_SITE = {
    "scan", "arxiv", "practitioner-data-form", "provider-guide-05-admin", "az-medsup-application",
    "az-tsmc-hsa-mandarin", "az-aca-guide-es", "az-ma-formulary-es",
}


def site_docs() -> dict[str, str]:
    """Site slug → its PDF's path under the repository, read from web/src/docs.ts."""
    text = (ROOT / "web/src/docs.ts").read_text(encoding="utf-8")
    return {slug: f"samples/{source}" for slug, source in
            re.findall(r'slug: "([^"]+)", source: "([^"]+)"', text)}


def test_a_pdf_on_both_lists_is_one_document_under_a_known_slug():
    on_site = {path: slug for slug, path in site_docs().items()}
    for slug, path in DOCUMENTS.items():
        if path in on_site:
            assert on_site[path] == SITE_SLUG.get(slug, slug), f"{slug}: the site calls {path} {on_site[path]}"
        else:
            assert slug in NOT_ON_SITE, f"{slug} ({path}) is not on the site and not listed in NOT_ON_SITE"


def test_the_known_differences_are_still_differences():
    on_site = {path: slug for slug, path in site_docs().items()}
    for slug, site in SITE_SLUG.items():
        assert DOCUMENTS[slug] in on_site and slug != site, slug
    for slug in NOT_ON_SITE:
        assert DOCUMENTS[slug] not in on_site, slug
