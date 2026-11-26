"""`reflow audit`: the SRC-* checks on the original PDF (SPEC §7).

Each check reads what ingest already collects (read_meta, page classification) plus tag
coverage, link annotations and widget tooltips. The report is a table, JSON, Markdown or
HTML (checks.report); the exit code is 1 when any check fails, or when veraPDF (opt-in,
checks.verapdf) finds the file not PDF/UA-1 compliant, so a CI job can gate a document library.

    reflow audit file.pdf [--format text|json|markdown|html] [--verapdf] [--password ...]

The audit reads the file the user gave, never a normalized copy, so it describes the
document as it will be published. The review queue's "not converted" rows link to this
report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pikepdf

from reflow.checks.verapdf import VeraResult, run_verapdf
from reflow.fontcensus import font_census
from reflow.ingest.pages import ingest
from reflow.model import PageKind

Status = Literal["pass", "fail", "warn", "skip"]


@dataclass(frozen=True)
class SourceCheck:
    id: str          # "SRC-META-001"
    title: str       # "Tagged (MarkInfo/Marked, StructTreeRoot)"
    wcag: str        # "1.3.1"
    matterhorn: str  # Matterhorn 1.1 checkpoint, e.g. "01-005"; "" when none maps


# The table from SPEC §7, in order. One source of truth: the CLI table and the JSON report
# both iterate this.
CHECKS: tuple[SourceCheck, ...] = (
    SourceCheck("SRC-META-001", "Tagged (MarkInfo/Marked, StructTreeRoot)", "1.3.1", "01-005"),
    SourceCheck("SRC-META-002", "Title present and DisplayDocTitle set", "2.4.2", "06-003"),
    SourceCheck("SRC-META-003", "Language set", "3.1.1", "11-006"),
    SourceCheck("SRC-META-004", "Security doesn't block assistive tech", "4.1.2", "26-001"),
    SourceCheck("SRC-TXT-001", "Real text present (not image-only)", "1.4.5", ""),
    SourceCheck("SRC-TXT-002", "Fonts embedded and Unicode-mapped", "1.3.1", "10-001"),
    SourceCheck("SRC-FIG-001", "Figures have alt text", "1.1.1", "13-004"),
    SourceCheck("SRC-TBL-001", "Tables have header cells", "1.3.1", "15-003"),
    SourceCheck("SRC-FRM-001", "Form fields have tooltips", "4.1.2", "28-008"),
)
CHECK_OF = {c.id: c for c in CHECKS}


@dataclass
class Finding:
    check: SourceCheck
    status: Status
    detail: str = ""                 # "3 of 6 pages are image-only (2, 4, 5)"
    pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.check.id, "title": self.check.title, "wcag": self.check.wcag,
                "matterhorn": self.check.matterhorn, "status": self.status,
                "detail": self.detail, "pages": list(self.pages)}


@dataclass
class AuditReport:
    filename: str
    findings: list[Finding]
    verapdf: VeraResult | None = None   # set only when the audit ran veraPDF

    @property
    def failed(self) -> bool:
        return (any(f.status == "fail" for f in self.findings)
                or (self.verapdf is not None and self.verapdf.status == "fail"))

    @property
    def by_id(self) -> dict[str, Finding]:
        return {f.check.id: f for f in self.findings}

    def to_dict(self) -> dict:
        out = {"filename": self.filename, "passed": not self.failed,
               "findings": [f.to_dict() for f in self.findings]}
        if self.verapdf is not None:
            out["verapdf"] = self.verapdf.to_dict()
        return out


def _pages_text(pages: list[int], limit: int = 20) -> str:
    shown = ", ".join(str(p) for p in pages[:limit])
    return shown + (", …" if len(pages) > limit else "")


def _finding(check_id: str, status: Status, detail: str = "",
             pages: list[int] | None = None) -> Finding:
    return Finding(CHECK_OF[check_id], status, detail, sorted(set(pages or [])))


def _skipped(reason: str, ids: tuple[str, ...]) -> list[Finding]:
    return [_finding(i, "skip", reason) for i in ids]


# ---- META ----------------------------------------------------------------------------------

def _meta_checks(meta: dict) -> list[Finding]:
    tagged, tree = meta["tagged"], meta["has_struct_tree"]
    if tagged and tree:
        f1 = _finding("SRC-META-001", "pass", "MarkInfo/Marked and StructTreeRoot present")
    elif tree:
        f1 = _finding("SRC-META-001", "warn", "StructTreeRoot present but MarkInfo/Marked unset")
    else:
        f1 = _finding("SRC-META-001", "fail",
                      "no structure tree" if not tagged else "MarkInfo/Marked without a tree")

    title, shown = meta["title"], meta["display_doc_title"]
    if title and shown:
        f2 = _finding("SRC-META-002", "pass", f"title {title!r}, shown in the window")
    elif title:
        f2 = _finding("SRC-META-002", "warn",
                      f"title {title!r} present but DisplayDocTitle is off")
    else:
        f2 = _finding("SRC-META-002", "fail", "no title")

    lang = meta["lang"]
    f3 = (_finding("SRC-META-003", "pass", f"lang {lang}") if lang
          else _finding("SRC-META-003", "fail", "no /Lang on the catalog"))

    if meta["allows_accessibility"]:
        f4 = _finding("SRC-META-004", "pass",
                      "encrypted, assistive technology allowed" if meta["encrypted"]
                      else "not encrypted")
    else:
        f4 = _finding("SRC-META-004", "fail", "permissions forbid assistive technology")
    return [f1, f2, f3, f4]


# ---- TXT ------------------------------------------------------------------------------------

def _text_check(pages) -> Finding:
    scanned = [p.number for p in pages if p.kind == PageKind.SCANNED]
    if not pages:
        return _finding("SRC-TXT-001", "fail", "no pages")
    if scanned:
        return _finding("SRC-TXT-001", "fail",
                        f"{len(scanned)} of {len(pages)} pages are image-only "
                        f"({_pages_text(scanned)})", scanned)
    return _finding("SRC-TXT-001", "pass", f"text on every page ({len(pages)} pages)")


def _font_check(pdf: pikepdf.Pdf) -> Finding:
    found = font_census(pdf)
    not_embedded, no_unicode = found.not_embedded, found.no_unicode

    def names(d: dict[str, list[int]]) -> str:
        return ", ".join(sorted(d)[:5]) + (", …" if len(d) > 5 else "")

    pages = sorted({p for ps in (*not_embedded.values(), *no_unicode.values()) for p in ps})
    if not_embedded:
        detail = f"{len(not_embedded)} fonts not embedded ({names(not_embedded)})"
        if no_unicode:
            detail += f"; {len(no_unicode)} without /ToUnicode ({names(no_unicode)})"
        return _finding("SRC-TXT-002", "fail", detail, pages)
    if no_unicode:
        return _finding("SRC-TXT-002", "warn",
                        f"{len(no_unicode)} Type3 or symbolic fonts without /ToUnicode "
                        f"({names(no_unicode)})", pages)
    return _finding("SRC-TXT-002", "pass", "every font embedded")


# ---- FIG / TBL (the tag tree) ------------------------------------------------------------------

def _walk(elements):
    for e in elements:
        yield e
        yield from _walk(e.children)


def _tag_checks(pdf: Path, meta: dict, jobs: int = 1) -> list[Finding]:
    ids = ("SRC-FIG-001", "SRC-TBL-001")
    if not meta["has_struct_tree"]:
        return _skipped("untagged", ids)
    if meta["encrypted"]:
        return _skipped("encrypted: tag tree not read", ids)
    try:
        tags = _read_tags(pdf, jobs)
    except ImportError:
        return _skipped("tags reader not available", ids)
    if not tags:
        return _skipped("untagged", ids)
    figures = tables = 0
    no_alt: list[int] = []
    no_th: list[int] = []
    for page in tags.values():
        for e in _walk(page.elements):
            if e.tag == "/Figure":
                figures += 1
                if not (e.alt and e.alt.strip()):
                    no_alt.append(page.page)
            elif e.tag == "/Table":
                tables += 1
                if not any(k.tag == "/TH" for k in _walk(e.children)):
                    no_th.append(page.page)
    if not figures:
        fig = _finding("SRC-FIG-001", "pass", "no tagged figures")
    elif no_alt:
        fig = _finding("SRC-FIG-001", "fail",
                       f"{len(no_alt)} of {figures} figures have no /Alt "
                       f"(pages {_pages_text(sorted(set(no_alt)))})", no_alt)
    else:
        fig = _finding("SRC-FIG-001", "pass", f"all {figures} figures have /Alt")
    if not tables:
        tbl = _finding("SRC-TBL-001", "pass", "no tagged tables")
    elif no_th:
        tbl = _finding("SRC-TBL-001", "fail",
                       f"{len(no_th)} of {tables} tables have no /TH "
                       f"(pages {_pages_text(sorted(set(no_th)))})", no_th)
    else:
        tbl = _finding("SRC-TBL-001", "pass", f"all {tables} tables have header cells")
    return [fig, tbl]


def _read_tags(pdf: Path, jobs: int):
    """The tag tree's pages, parsed on a pool of up to `jobs` workers when the tree spans enough
    pages to use one. The pool's tasks hold slots of the machine's budget (reflow.budget) only
    while they run, and the budget is joined only then, so a short document neither holds slots
    nor shrinks another's share."""
    from reflow.extract.pagecontent import pool_workers
    from reflow.extract.tags import read_tags, walk_tags
    walked = walk_tags(pdf)
    want = pool_workers(len(walked.pages), jobs)
    if want <= 1:
        return read_tags(pdf, jobs=1, walked=walked)
    from reflow import budget
    from reflow.extract.parallel import make_pool
    with budget.lease() as lease:
        want = lease.cap(want)
        if want <= 1:
            return read_tags(pdf, jobs=1, walked=walked)
        pool = lease.meter(make_pool(want))
        try:
            return read_tags(pdf, jobs=want, executor=pool, walked=walked)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


# ---- FRM --------------------------------------------------------------------------------------

def _form_check(pdf: Path, meta: dict) -> Finding:
    if meta["encrypted"]:
        return _finding("SRC-FRM-001", "skip", "encrypted: widgets not read")
    from reflow.extract.forms import read_widgets
    widgets = [w for ws in read_widgets(pdf).values() for w in ws]
    if not widgets:
        return _finding("SRC-FRM-001", "pass", "no form fields")
    missing = [w.page for w in widgets if not w.tooltip.strip()]
    if missing:
        return _finding("SRC-FRM-001", "fail",
                        f"{len(missing)} of {len(widgets)} fields have no tooltip "
                        f"(pages {_pages_text(sorted(set(missing)))})", missing)
    return _finding("SRC-FRM-001", "pass", f"all {len(widgets)} fields have a tooltip")


# ---- the audit ----------------------------------------------------------------------------------

def _guard(check_id: str, fn, *args) -> Finding:
    """One check, isolated: an exception becomes a `skip` with the error, not a crash."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - the report must always come back
        return _finding(check_id, "skip", f"could not check: {type(e).__name__}: {e}")


def _guard_many(ids: tuple[str, ...], fn, *args) -> list[Finding]:
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return _skipped(f"could not check: {type(e).__name__}: {e}", ids)


def audit(pdf: Path, password: str = "", *, jobs: int = 1, verapdf: bool = False) -> AuditReport:
    """Run every check on the original file. Never raises for a bad document: an unreadable
    PDF is one failing report, SRC-TXT-001 `fail` with the error and every other check
    `skip`, so the exit code still gates it. `jobs` caps the worker processes that parse the
    pages of the tag tree (1: this process only); the report is the same for any count.
    `verapdf` adds veraPDF's PDF/UA-1 result to the report."""
    pdf = Path(pdf)
    report = _audit(pdf, password, jobs)
    if verapdf:
        report.verapdf = run_verapdf(pdf, password=password)
    return report


def _audit(pdf: Path, password: str, jobs: int) -> AuditReport:
    try:
        doc = ingest(pdf, password)
    except pikepdf.PasswordError:
        return _unreadable(pdf, "encrypted; pass --password")
    except Exception as e:  # noqa: BLE001 - pikepdf, pdfium or OS errors alike
        return _unreadable(pdf, f"could not open: {e}")

    meta = doc.meta
    findings = _guard_many(("SRC-META-001", "SRC-META-002", "SRC-META-003", "SRC-META-004"),
                           _meta_checks, meta)
    findings.append(_guard("SRC-TXT-001", _text_check, doc.pages))

    def fonts() -> Finding:
        with pikepdf.open(pdf, password=password) as opened:
            return _font_check(opened)

    findings.append(_guard("SRC-TXT-002", fonts))
    findings.extend(_guard_many(("SRC-FIG-001", "SRC-TBL-001"), _tag_checks, pdf, meta, jobs))
    findings.append(_guard("SRC-FRM-001", _form_check, pdf, meta))
    order = {c.id: i for i, c in enumerate(CHECKS)}
    findings.sort(key=lambda f: order[f.check.id])
    return AuditReport(meta["filename"], findings)


def _unreadable(pdf: Path, reason: str) -> AuditReport:
    findings = [_finding(c.id, "fail" if c.id == "SRC-TXT-001" else "skip", reason)
                for c in CHECKS]
    return AuditReport(pdf.name, findings)
