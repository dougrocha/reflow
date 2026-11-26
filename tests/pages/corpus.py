"""Sample documents, conversions cached by code, packages, PDF and OCR availability, and page-check
specs.

A conversion is reused while src/reflow, the conversion's package versions, the PDF and OCR
availability are unchanged. The cache lives under .pytest_cache/reflow-page-checks/<slug>/<hash>/
in the main checkout, shared by every worktree. A run converts a key only while it holds that
key's lock (reflow.cachedir) and pins every entry it reads. A page-check session ends with
`prune`, which evicts by size and age and never removes a pinned entry.

Quick mode (`pytest -m pages --quick`, or REFLOW_PAGE_CHECKS_QUICK=1) converts only the pages the
selected tests need, with QUICK_CONTEXT pages either side plus a spread of pages for the font
statistics, cut from a copy of each document longer than QUICK_FULL_PAGES. Whole-document
heuristics (running headers, the title, chunking) see that excerpt instead of the whole document.
Use quick mode on branches; run the full suite once on main after merging.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from checks import Node, parse_html

from reflow import budget, cachedir
from reflow.identity import tool_key
from reflow.ingest.ocr import make_runner
from reflow.names import DOCUMENT_JSON, INDEX_HTML, MANIFEST_JSON, REVIEW_JSON
from reflow.provenance import source_identity

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))   # checkout.py: the report and capture tools import this outside pytest
import checkout  # noqa: E402

MAIN = checkout.main_checkout(REPO)


def _shared_root() -> Path:
    """The main checkout's .pytest_cache, so one warm cache serves every worktree.
    REFLOW_PAGE_CHECKS_CACHE overrides the directory."""
    if override := os.environ.get("REFLOW_PAGE_CHECKS_CACHE"):
        return Path(override)
    return MAIN / ".pytest_cache"


def source(slug: str) -> Path:
    """The document's PDF. Some samples are gitignored and live only in the main checkout, so a
    fresh worktree reads them from there."""
    return checkout.sample(REPO, DOCUMENTS[slug])


def absent_reason(slug: str) -> str | None:
    """Why the document cannot be read here, or None when its PDF is present. The gitignored
    samples are not in a fresh clone, and resnet and arxiv come from scripts/fetch_samples.py."""
    path = source(slug)
    if path.exists():
        return None
    hint = "; run scripts/fetch_samples.py" if path.name in ("resnet.pdf", "arxiv.pdf") else ""
    return f"{path} is not here{hint}"


# Entries are keyed by the code's hash, so branches with different code can share the cache.
CACHE_ROOT = _shared_root() / "reflow-page-checks"
QUICK_CACHE_ROOT = _shared_root() / "reflow-page-checks-quick"
STAGE_ROOT = _shared_root() / "reflow-page-checks-stages"   # OCR and extraction, see reflow.stagecache
# What `prune` keeps of each cache, across every worktree and branch: the most recently used
# entries that fit in the budget (bytes), none unused for longer than MAX_AGE (seconds).
FULL_BUDGET = 10 << 30
QUICK_BUDGET = 4 << 30
STAGE_BUDGET = 4 << 30
MAX_AGE = 14 * 86400
QUICK_FULL_PAGES = 20  # documents up to this long are converted whole even in quick mode
QUICK_CONTEXT = 2  # pages kept either side of a tested page, so running headers still repeat
# Heading levels rank font sizes over the whole document, so an excerpt also keeps the front
# matter and pages spread evenly through the rest, to see every heading size.
QUICK_FRONT = 6
QUICK_SPREAD = 40


def quick_mode() -> bool:
    return os.environ.get("REFLOW_PAGE_CHECKS_QUICK", "") not in ("", "0")


def converted_whole(slug: str) -> bool:
    """True when quick mode converts the document whole, so its output matches a full run."""
    with pikepdf.open(source(slug)) as pdf:
        return len(pdf.pages) <= QUICK_FULL_PAGES

# The documents the page checks convert. The site lists most of them too, mostly under the same
# slug: tests/test_sample_docs.py states where the two lists differ.
DOCUMENTS = {
    "resnet": "samples/resnet.pdf",
    "fw9": "samples/fw9.pdf",
    "uscis-i9": "samples/corpus/uscis-i9.pdf",
    "p17": "samples/p17.pdf",
    "naca": "samples/naca-ocr.pdf",
    "pima-directory": "samples/azblue/ma-provider-directory-pima.pdf",
    "maricopa-directory": "samples/azblue/ma-provider-directory-maricopa-pinal.pdf",
    "budget-appendix": "samples/budget-app.pdf",
    "nchs-bar-chart": "samples/corpus/nchs-bar-chart.pdf",
    "az-privacy-release-spanish": "samples/azblue/site/privacy-release-spanish.pdf",
    "az-deceased-member-affidavit": "samples/azblue/site/deceased-member-affidavit.pdf",
    "az-medical-claim-es": "samples/azblue/site/medical-claim-es.pdf",
    "az-group-contract-form": "samples/azblue/site/group-contract-form.pdf",
    "az-dental-claim-form": "samples/azblue/site/dental-claim-form.pdf",
    "census-p60-273": "samples/corpus/census-p60-273.pdf",
    "cdc-firstline-slides-es": "samples/corpus/cdc-firstline-slides-es.pdf",
    "irs-w4-2026": "samples/corpus/irs-w4-2026.pdf",
    "cdc-hepb-arabic": "samples/corpus/cdc-hepb-arabic.pdf",
    "scotus-mahmoud-v-taylor": "samples/corpus/scotus-mahmoud-v-taylor.pdf",
    "frcp-2025": "samples/corpus/frcp-2025.pdf",
    "nasa-se-handbook": "samples/corpus/nasa-se-handbook.pdf",
    "epa-ghg-inventory-2024": "samples/corpus/epa-ghg-inventory-2024.pdf",
    "cdc-antibiotics-trifold": "samples/corpus/cdc-antibiotics-trifold.pdf",
    "cdc-antibiotics-korean": "samples/corpus/cdc-antibiotics-korean.pdf",
    "scan": "samples/scan.pdf",
    "arxiv": "samples/arxiv.pdf",
    # Not on the site (web/src/docs.ts): kept as page checks only.
    "bluejourney-directory": "samples/azblue/ma-provider-directory-bluejourney-sp.pdf",
    # samples/docaccess (gitignored):
    "docaccess-chem-math": "samples/docaccess/01-chem-math.pdf",
    "docaccess-permit": "samples/docaccess/02-building-permit-applications.pdf",
    "docaccess-assessment-appeal": "samples/docaccess/03-assessment-appeal-application.pdf",
    "docaccess-calendar": "samples/docaccess/04-school-district-calendar.pdf",
    "docaccess-legislation": "samples/docaccess/05-legislation-tracked-changes.pdf",
    "docaccess-rfp": "samples/docaccess/06-complex-rfp-diagrams.pdf",
    "docaccess-audit": "samples/docaccess/07-financial-audit-comprehensive.pdf",
    "docaccess-brochure": "samples/docaccess/08-multi-column-brochure.pdf",
    "docaccess-budget": "samples/docaccess/09-scanned-budget-handwriting.pdf",
    "docaccess-directory": "samples/docaccess/10-directory-providers.pdf",
    "docaccess-carmel": "samples/docaccess/11-timelines-difficult-graphics.pdf",
    "practitioner-data-form": "samples/azblue/practitioner-data-form.pdf",
    "provider-guide-05-admin": "samples/azblue/provider-guide-05-admin.pdf",
    # More of samples/azblue/site (gitignored, like the three az-* forms above).
    "az-medsup-application": "samples/azblue/site/medsup-application.pdf",
    "az-hca-nondiscrim-multilang": "samples/azblue/site/hca-nondiscrim-multilang.pdf",
    "az-tsmc-hsa-mandarin": "samples/azblue/site/tsmc-hsa-mandarin-2024.pdf",
    "az-medsup-outline": "samples/azblue/site/medsup-outline-coverage.pdf",
    "az-aca-guide-es": "samples/azblue/site/aca-shoppers-guide-es.pdf",
    "az-tucson-employee-booklet": "samples/azblue/site/tucson-employee-booklet.pdf",
    "az-remittance-advice-scan": "samples/azblue/site/remittance-advice-medical.pdf",
    "az-tucson-member-guide": "samples/azblue/site/tucson-member-guide.pdf",
    "az-ma-formulary-es": "samples/azblue/site/ma-formulary-es.pdf",
    "az-bluedental-manual": "samples/azblue/site/bluedental-provider-manual.pdf",
    "az-advance-directive-bilingual": "samples/azblue/site/advance-directive-notice-bilingual.pdf",
    "az-checkup-checklist": "samples/azblue/site/checkup-checklist.pdf",
    "az-medsup-rate-sheet": "samples/azblue/site/medsup-rate-sheet.pdf",
    "az-accident-insurance-flyer": "samples/azblue/site/accident-insurance-flyer.pdf",
    # The covers whose title was once published as a fragment or the wrong block.
    "az-bluecard-provider-guide": "samples/azblue/site/bluecard-provider-guide.pdf",
    "az-appt-representative-bilingual": "samples/azblue/site/appt-representative-bilingual.pdf",
    "az-ma-vision-flyer-bilingual": "samples/azblue/site/ma-vision-flyer-bilingual.pdf",
    "az-large-group-member-guide-with-rx":
        "samples/azblue/site/large-group-member-guide-with-rx.pdf",
    "az-ma-sob-classic-maricopa-en": "samples/azblue/site/ma-sob-classic-maricopa-en.pdf",
    "az-claim-form-instruction-booklet": "samples/azblue/site/claim-form-instruction-booklet.pdf",
    "az-maternal-risk-assessment": "samples/azblue/site/maternal-risk-assessment.pdf",
    "az-nurse-on-call-flyer": "samples/azblue/site/nurse-on-call-flyer.pdf",
    "az-bluehpn-benefits": "samples/azblue/site/bluehpn-benefits.pdf",
    "az-claim-submission-guide":
        "samples/azblue/site/19-claim-submission-provider-operating-guide.pdf",
    "az-audit-compliance-and-risk-committee-charter":
        "samples/azblue/site/audit-compliance-and-risk-committee-charter.pdf",
    "az-hca-member-handbook": "samples/azblue/site/hca-member-handbook-2026.pdf",
    "az-assistant-surgeon-codes": "samples/azblue/site/assistant-surgeon-codes-2026-01.pdf",
    "az-ma-appeals-form": "samples/azblue/site/ma-appeals-form-2026.pdf",
    "az-psych-pa-form": "samples/azblue/site/psych-pa-form.pdf",
    "az-group-ppo-sob": "samples/azblue/site/group-ppo-sob.pdf",
    "az-sbc-p7950": "samples/azblue/site/sbc-2026-p7950.pdf",
    "az-provider-portal-guide": "samples/azblue/site/provider-portal-guide.pdf",
    "az-stigma-media-kit": "samples/azblue/site/stigma-free-az-media-kit.pdf",
    "az-ms-dental-claim-form": "samples/azblue/site/ms-dental-claim-form.pdf",
    # Forms whose blanks are printed as underscores.
    "az-newborn-reporting-form": "samples/azblue/site/newborn-reporting-form.pdf",
    "az-pediatric-nicu-case-management-referral-form":
        "samples/azblue/site/pediatric-nicu-case-management-referral-form.pdf",
    "az-standardappealpacket": "samples/azblue/site/standardappealpacket.pdf",
    "az-cobra-application": "samples/azblue/site/cobra-application.pdf",
    # Reading order of columns and cards.
    "az-tier-1a-rx-list": "samples/azblue/site/tier-1a-rx-list.pdf",
}


def source_text(slug: str, page: int) -> str:
    """Read a 1-based source PDF page's text layer without converting the document."""
    with pdfium.PdfDocument(str(source(slug))) as pdf:
        with closing(pdf[page - 1]) as pdf_page:
            with closing(pdf_page.get_textpage()) as textpage:
                return textpage.get_text_range()


_code_hash: str | None = None


def code_hash() -> str:
    """Hash of every file under src/reflow (code and viewer assets),
    computed once per process. Bytecode caches are skipped: they change on every run. It is the
    source digest a published document's provenance records (reflow.provenance)."""
    global _code_hash
    if _code_hash is None:
        _code_hash = source_identity(REPO / "src" / "reflow")[0]
    return _code_hash


TIMING_FILE = "page-checks-timing.json"
REUSED = ("previous output", "stage cache")   # Summary.model of a run that rendered a saved model


def ocr_failed(summary) -> bool:
    """An OCR runner was available but failed on some pages: the output is incomplete."""
    return summary.ocr.runner != "none" and bool(summary.ocr.failed)


def publish(tmp: Path, out: Path, *, replace: bool = False) -> None:
    """Move a finished conversion into the shared cache without a window in which `out` holds
    partial content. An existing complete entry is kept (another worktree published first)
    unless `replace`; then it is renamed aside, ours renamed in, and the old one removed."""
    if (out / MANIFEST_JSON).exists() and not replace:
        shutil.rmtree(tmp, ignore_errors=True)
        return
    aside = None
    if out.exists():
        aside = cachedir.temp_path(out, "old")
        try:
            out.rename(aside)
        except OSError:  # another process moved it first
            aside = None
    try:
        tmp.rename(out)
    except OSError:  # another process published in the gap: keep theirs, discard ours
        shutil.rmtree(tmp, ignore_errors=True)
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)


def _write_timing(out_dir: Path, summary) -> None:
    """Keep the conversion's stage seconds and page count next to its output, for the scorecard
    (tests/pages/report.py --scorecard). Entries converted before this existed have no file."""
    seconds = {k: round(float(v), 3) for k, v in dict(summary.seconds).items()}
    (out_dir / TIMING_FILE).write_text(
        json.dumps({"pages": summary.pages, "seconds": seconds}), encoding="utf-8")


class DocOutput:
    """A converted document: the page transcript and the PDF's text layer."""

    def __init__(self, slug: str, out_dir: Path, page_map: dict[int, int] | None = None):
        self.slug = slug
        self.out_dir = out_dir
        self.page_map = page_map  # source page → excerpt page, for a quick-mode excerpt
        self.manifest = json.loads((out_dir / MANIFEST_JSON).read_text(encoding="utf-8"))
        self._chunks: dict[str, dict[int, Node]] = {}
        self._shell: Node | None = None
        self._document: dict | None = None
        self._review: list[dict] | None = None
        self._pdf = None

    def review(self) -> list[dict]:
        """The parsed review.json: one item per node or page in the review queue."""
        if self._review is None:
            self._review = json.loads((self.out_dir / REVIEW_JSON).read_text(encoding="utf-8"))
        return self._review

    def _chunk_pages(self, href: str) -> dict[int, Node]:
        if href not in self._chunks:
            html = (self.out_dir / href).read_text(encoding="utf-8")
            root = parse_html(html)
            pages = {}
            for n in root.walk():
                if n.tag == "div" and n.attrs.get("class", "") == "pg" and "data-page" in n.attrs:
                    pages[int(n.attrs["data-page"])] = n
            self._chunks[href] = pages
        return self._chunks[href]

    def shell(self) -> Node:
        """The document shell (index.html): the header, and the first chunk's pages."""
        if self._shell is None:
            self._shell = parse_html((self.out_dir / INDEX_HTML).read_text(encoding="utf-8"))
        return self._shell

    def excerpt_page(self, number: int) -> int:
        """The page's number in the converted output: the same in a whole conversion, the
        excerpt's own in quick mode. KeyError for a page outside the excerpt."""
        if self.page_map is None:
            return number
        if number not in self.page_map:
            raise KeyError(f"{self.slug}: page {number} is not in the quick excerpt")
        return self.page_map[number]

    def document(self) -> dict:
        """The parsed document.json: the node tree with every node's facts (the ledger)."""
        if self._document is None:
            self._document = json.loads((self.out_dir / DOCUMENT_JSON).read_text(encoding="utf-8"))
        return self._document

    def page(self, number: int) -> Node:
        number = self.excerpt_page(number)
        for c in self.manifest["chunks"]:
            if c["first"] <= number <= c["last"]:
                pages = self._chunk_pages(c["href"])
                if number in pages:
                    return pages[number]
        raise KeyError(f"{self.slug}: page {number} is not in the converted output")

    def pdf_text(self, number: int) -> str:
        if self._pdf is None:
            self._pdf = pdfium.PdfDocument(str(source(self.slug)))
        page = self._pdf[number - 1]
        textpage = page.get_textpage()
        try:
            return textpage.get_text_range()
        finally:
            textpage.close()
            page.close()


class Corpus:
    """Converts documents (all the ones a run needs at once, in `prepare`) and keeps the
    results for the session. In quick mode `prepare` also takes the pages each document needs.
    Every cached conversion it reads stays pinned (a shared lock) until the process ends, so no
    other run's eviction removes it mid-session."""

    def __init__(self, quick: bool | None = None) -> None:
        self.quick = quick_mode() if quick is None else quick
        self._docs: dict[str, DocOutput] = {}
        self._errors: dict[str, BaseException] = {}
        self._pages: dict[str, tuple[int, ...]] = {}  # quick mode: the excerpt's source pages
        self._unpublished: dict[str, Path] = {}  # conversions kept for this run only, not cached
        self._pins: dict[str, cachedir.Lock | None] = {}  # held shared while this run reads them

    def doc(self, slug: str) -> DocOutput:
        if slug not in self._docs:
            if reason := absent_reason(slug):   # `prepare` left it out: its tests skip
                if "PYTEST_CURRENT_TEST" in os.environ:
                    import pytest
                    pytest.skip(reason)
                raise FileNotFoundError(reason)
            for _ in range(3):   # an entry evicted between its conversion and its pin: again
                if slug in self._errors or slug in self._unpublished or self._pin(slug):
                    break
                self.prepare([slug])
            if slug in self._errors:
                raise RuntimeError(f"{slug}: conversion failed") from self._errors[slug]
            if slug not in self._unpublished and slug not in self._pins:
                raise RuntimeError(f"{slug}: its cached conversion was removed three times")
            pages = self._pages.get(slug)
            page_map = {p: i for i, p in enumerate(pages, 1)} if pages else None
            out = self._out(slug)
            if slug not in self._unpublished:
                os.utime(out)  # most recently used: kept when older entries are pruned
            self._docs[slug] = DocOutput(slug, out, page_map)
        return self._docs[slug]

    def _key(self, slug: str) -> str:
        h = hashlib.sha256(code_hash().encode())
        h.update(tool_key().encode())
        h.update(source(slug).read_bytes())
        h.update(b"ocr" if make_runner("auto") is not None else b"no-ocr")
        if pages := self._pages.get(slug):
            h.update(b"pages:" + ",".join(map(str, pages)).encode())
        return h.hexdigest()[:16]

    def _path(self, slug: str) -> Path:
        return (QUICK_CACHE_ROOT if self._pages.get(slug) else CACHE_ROOT) / slug / self._key(slug)

    def _out(self, slug: str) -> Path:
        """Where this run reads the document's conversion from: the cache, or the private copy
        of one that was not published."""
        return self._unpublished.get(slug) or self._path(slug)

    def _cached(self, slug: str) -> bool:
        return (self._out(slug) / MANIFEST_JSON).exists()

    def _pin(self, slug: str) -> bool:
        """Hold the document's cached conversion shared for the rest of the run; False if there
        is none. Waits while another run converts or removes it."""
        if slug in self._pins:
            return True
        if not self._cached(slug):
            return False
        held = cachedir.try_lock(self._path(slug), shared=True)
        if self._cached(slug):
            self._pins[slug] = held   # None on a cache that cannot be locked: read it anyway
            return True
        if held is not None:
            held.release()
        return False

    def _claim(self, slugs: list[str], fresh: bool, wait: bool
               ) -> tuple[dict[str, cachedir.Lock | None], set[str]]:
        """Take the lock of each slug's cache key: ({slug: lock} to convert now, the slugs some
        other run converted meanwhile). Without `wait`, a key another run holds is skipped."""
        claimed: dict[str, cachedir.Lock | None] = {}
        done: set[str] = set()
        for slug in slugs:
            try:
                held = cachedir.lock(self._path(slug), wait=wait)
            except OSError:   # a cache that cannot be locked: convert unlocked
                held = None
            else:
                if held is None:
                    continue
            if not fresh and self._cached(slug):
                done.add(slug)
                if held is not None:
                    held.release()
            else:
                claimed[slug] = held
        return claimed, done

    def _excerpt_pages(self, slug: str, wanted: Iterable[int]) -> tuple[int, ...] | None:
        """The source pages of a quick excerpt, or None to convert the whole document."""
        with pikepdf.open(source(slug)) as pdf:
            count = len(pdf.pages)
        if count <= QUICK_FULL_PAGES:
            return None
        keep = {p for w in wanted for p in range(w - QUICK_CONTEXT, w + QUICK_CONTEXT + 1)}
        keep |= set(range(1, QUICK_FRONT + 1))
        keep |= {1 + i * (count - 1) // (QUICK_SPREAD - 1) for i in range(QUICK_SPREAD)}
        return tuple(sorted(p for p in keep if 1 <= p <= count))

    @staticmethod
    def _write_excerpt(slug: str, pages: tuple[int, ...], dest: Path) -> Path:
        """A copy of the document with only `pages`. Deleting the other pages keeps the
        catalog: the tag tree, the form fields and the outline stay for the pages kept."""
        with pikepdf.open(source(slug)) as pdf:
            keep = set(pages)
            for i in reversed(range(len(pdf.pages))):
                if i + 1 not in keep:
                    del pdf.pages[i]
            if "/PageLabels" in pdf.Root:
                del pdf.Root.PageLabels  # labels number the original pages
            pdf.save(dest, static_id=True)  # same bytes each run, so stage caches can key on them
        return dest

    def prepare(self, slugs: Iterable[str], *, pages: dict[str, set[int]] | None = None,
                jobs: int | None = None, max_docs: int | None = None, fresh: bool = False) -> None:
        """Convert every document in `slugs` that is not cached (keyed by code, PDF, OCR
        availability and, in quick mode, the excerpt's pages), `max_docs` at a time, each in
        a process of its own (documents of more than 200 pages also get `jobs` page workers).
        The PDF reading (OCR, extraction) is kept in STAGE_ROOT, keyed by the PDF and only the
        code it runs (reflow.stagecache), so an edit to analysis or rendering code redoes
        only the later stages. A document that fails is remembered and raised when its tests ask
        for it, so the others still run. In quick mode `pages` names the pages each document's
        tests need; a document missing from it is converted whole. `fresh` converts even a cached
        document, to record its conversion timing again.

        A document is converted only while this run holds its cache key's lock, and only if the
        key is still missing once it holds it: two runs never convert one key at once. A run
        that finds a key being converted elsewhere converts its other documents first, then
        waits and reads the result."""
        slugs = [s for s in dict.fromkeys(slugs) if absent_reason(s) is None]
        if self.quick and pages:
            for slug in slugs:
                if slug in pages and slug not in self._pages:
                    if excerpt := self._excerpt_pages(slug, pages[slug]):
                        self._pages[slug] = excerpt
        if fresh:   # replacing an entry waits for its readers, so this run lets go of its own
            for slug in slugs:
                if (held := self._pins.pop(slug, None)) is not None:
                    held.release()
                self._docs.pop(slug, None)
        todo = [s for s in slugs if s not in self._errors and (fresh or not self._cached(s))]
        while todo:
            claimed, done = self._claim(todo, fresh, wait=False)
            if not claimed and not done:   # every key is being converted elsewhere: wait
                claimed, done = self._claim(todo[:1], fresh, wait=True)
            try:
                if claimed:
                    self._convert(list(claimed), jobs=jobs, max_docs=max_docs, fresh=fresh)
            finally:
                for held in claimed.values():
                    if held is not None:
                        held.release()
            todo = [s for s in todo if s not in claimed and s not in done
                    and (fresh or not self._cached(s))]

    def _convert(self, todo: list[str], *, jobs: int | None, max_docs: int | None,
                 fresh: bool) -> None:
        """Convert `todo`, whose cache keys this run holds, and publish each result."""
        from reflow.convert import convert_many

        # Biggest first, so the long documents don't start last and run alone at the end.
        todo.sort(key=lambda s: len(self._pages.get(s, ())) or source(s).stat().st_size,
                  reverse=True)
        tmps, sources = {}, {}
        for slug in todo:
            slug_dir = self._path(slug).parent
            slug_dir.mkdir(parents=True, exist_ok=True)
            tmps[slug] = cachedir.temp_path(self._path(slug))
            sources[slug] = source(slug)
            if excerpt := self._pages.get(slug):
                # Keep the document's file name: the filename-fallback title depends on it.
                src_dir = cachedir.temp_path(self._path(slug), "src")
                src_dir.mkdir()
                sources[slug] = self._write_excerpt(slug, excerpt, src_dir / sources[slug].name)
        # Within the machine's worker budget, shared with every other conversion running now.
        with budget.lease() as lease:
            results = convert_many([(sources[s], tmps[s]) for s in todo], isolate=True,
                                   jobs=jobs or min(4, budget.cpu_slots()),
                                   max_docs=max_docs or max(2, (os.cpu_count() or 1) // 2),
                                   lease=lease, page_images=False, stage_cache=STAGE_ROOT,
                                   reuse_model=not fresh)   # fresh: real timings
        for slug, result in zip(todo, results):
            if sources[slug].parent.name.startswith("."):
                shutil.rmtree(sources[slug].parent, ignore_errors=True)
            if not result.ok:
                self._errors[slug] = result.error
                shutil.rmtree(tmps[slug], ignore_errors=True)
                continue
            key = self._key(slug)
            slug_dir = self._path(slug).parent
            out = slug_dir / key
            if result.summary.model not in REUSED:   # a reused model's seconds are a render's
                _write_timing(tmps[slug], result.summary)
            if ocr_failed(result.summary):
                # Tests read this run's output, but a conversion that lost pages to an OCR
                # failure (a timeout under load) must not satisfy later runs.
                print(f"warning: {slug}: OCR failed on pages "
                      f"{sorted(result.summary.ocr.failed)}; the conversion is not cached",
                      file=sys.stderr)
                private = cachedir.temp_path(out, "unpublished")
                tmps[slug].rename(private)
                atexit.register(shutil.rmtree, private, ignore_errors=True)
                self._unpublished[slug] = private
                continue
            publish(tmps[slug], out, replace=fresh)


def prune(dry_run: bool | None = None) -> list[cachedir.Report]:
    """Evict from the shared caches (whole conversions, quick excerpts, stages) by size and
    age, and sweep the temp files of runs that were killed. See cachedir.evict: entries in use
    are kept, and the first eviction of each cache only reports what it would remove."""
    caches = ((CACHE_ROOT, 2, FULL_BUDGET), (QUICK_CACHE_ROOT, 2, QUICK_BUDGET),
              (STAGE_ROOT, 3, STAGE_BUDGET))
    reports = (cachedir.evict(root, depth, budget=budget, max_age=MAX_AGE, dry_run=dry_run,
                              min_idle=cachedir.MIN_IDLE)
               for root, depth, budget in caches)
    return [r for r in reports if r is not None]
