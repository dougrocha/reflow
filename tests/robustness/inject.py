"""Run the defect cells: inject each defect into a correct conversion of each document, render it
again, and score which page checks and which review items catch it.

A cell is (document, defect). Its steps:
  1. the document's conversion is copied into this run's folder: the page-check cache's entry for
     the current code when there is one (read under its shared lock, never written), else a
     conversion of the PDF into this run's folder (convert_in_child, uncached);
  2. the control copy is rendered again from its model, with no defect, and must come out byte
     for byte the conversion it was copied from: the render path itself changes nothing;
  3. the defect changes the model of another copy (defects.py), the copy is rendered again the
     way a conversion publishes it (document.json, review.json from review.queue, the layout
     sidecars, the site and the passages), and the model must read back through Model.read. A
     defect that changes nothing, or that the page's HTML does not show, is invalid;
  4. the damaged page's spec assertions run on the copy (specs.run_assertion, with a DocOutput
     around the copy) and on the control. An assertion that passes on the control and fails on
     the copy caught the defect. A review item caught it when it covers the changed region and
     the control had none of its code there, or when it was already there with one of the
     defect's own codes.

Nothing here writes the page-check caches or the documents' published output.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
import random
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from robustness.defects import DEFECTS, Defect
from robustness.harness import REPO, _short, tool_versions
from robustness.kinds import Damage, Unsupported
from robustness.oracle import Fact, Outcome, Verdict, covers

from reflow import cachedir
from reflow.names import (
    ASSETS_DIR,
    CHUNKS_DIR,
    DECISIONS_LEDGER,
    DOCUMENT_JSON,
    INDEX_HTML,
    LAYOUT_DIR,
    MANIFEST_JSON,
    PASSAGES_JSONL,
    REVIEW_JSON,
    TOC_JSON,
)

PAGES_DIR = REPO / "tests" / "pages"
if str(PAGES_DIR) not in sys.path:   # the page-check modules import each other by bare name
    sys.path.insert(0, str(PAGES_DIR))

from checks import CheckFailed  # noqa: E402
from corpus import Corpus, DocOutput, absent_reason, source  # noqa: E402
from specs import PageSpec, load_specs, run_assertion  # noqa: E402

SEED = 306
CONTROL = "clean"

# The documents defects are injected into: short, converted whole in quick mode too, and their
# specs test headings, tables, lists, reading order and fields between them.
DOCS = {
    "fw9": "a tagged IRS form, 6 pages: headings, lists, tables and fields",
    "resnet": "a two-column paper, 12 pages: headings, tables, figures and footnotes",
    "uscis-i9": "a tagged form, 4 pages, Type0 fonts: fields and instructions",
    "irs-w4-2026": "a tagged IRS form, 5 pages: steps, worksheets and fields",
    "cdc-antibiotics-trifold": "a two-page trifold brochure: headings and lists",
}
DEFECT_BY_ID = {d.id: d for d in DEFECTS}
# The files the render writes, derived from the model; the rest of a conversion is kept.
DERIVED = (CHUNKS_DIR, INDEX_HTML, MANIFEST_JSON, TOC_JSON, ASSETS_DIR, LAYOUT_DIR, PASSAGES_JSONL,
           DECISIONS_LEDGER)


def cells() -> list[tuple[str, str]]:
    """Every cell: each document's control, then each document with each defect."""
    return [(doc, d) for doc in DOCS for d in [CONTROL, *DEFECT_BY_ID]]


def cell_seed(doc: str, defect: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{SEED}:{doc}:{defect}".encode()).digest()[:4], "big")


# ---------------------------------------------------------------------------- rendering

def render(folder: Path, mutate=None) -> Damage | None:
    """Render the conversion in `folder` again from its model, after `mutate(doc)` when given
    (it returns the Damage). Writes what output.publish writes, but no page images."""
    from reflow.analyze import review
    from reflow.output import VERSION_SLOT, stamp_version
    from reflow.pipeline import Model
    from reflow.render.passages import write_passages
    from reflow.render.sidecars import write_page_layouts
    from reflow.render.site import render_html

    model = Model.read(folder)
    doc = model.doc
    damage = mutate(doc) if mutate else None
    items = review.queue(doc)
    body = doc.to_json(indent=None)
    for name in DERIVED:
        path = folder / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    (folder / DOCUMENT_JSON).write_text(body, encoding="utf-8")
    (folder / REVIEW_JSON).write_text(json.dumps(items, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    write_page_layouts(doc, folder)
    if (ledger := doc.decisions_ledger) is not None:
        (folder / DECISIONS_LEDGER).write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    render_html(doc, doc.root.attrs["title"], folder, figures=model.figures,
                labels={p.number: p.label for p in doc.pages}, page_widths=(),
                version=VERSION_SLOT)
    write_passages(doc, folder, stamp_version(folder, body))
    Model.read(folder)   # ValueError when the defect's model does not round-trip
    return damage


@contextmanager
def _read_lock(entry: Path) -> Iterator[bool]:
    """Hold the cache entry's shared lock (reflow.cachedir) without creating anything: True
    while held, False when the entry has no lock file to open (then it is not read)."""
    path = cachedir.lock_path(entry)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:   # an eviction removed the lock file meanwhile: the entry is going
            st, now = os.fstat(fd), os.stat(path)
            held = (st.st_dev, st.st_ino) == (now.st_dev, now.st_ino)
        except FileNotFoundError:
            held = False
        yield held
    finally:
        os.close(fd)


def digests(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------- scoring

def signals(out: DocOutput) -> list[Fact]:
    """Every review code in review.json, each where it points: the boxes of its own targets,
    else the boxes of the item's nodes (all the members of a collapsed item), else the whole
    page. A code takes no box from another code's targets."""
    boxes = {}
    stack = [out.document()["root"]]
    while stack:
        node = stack.pop()
        if node.get("bbox"):
            boxes[node["id"]] = tuple(node["bbox"])
        stack.extend(node.get("children", []))
    found = []
    for item in out.review():
        members = (item.get("group") or {}).get("nodeIds") or [item["id"]]
        own = [boxes[m] for m in members if m in boxes]
        codes = {item.get("warn"), item.get("needs")} | {f["code"] for f in item.get("findings", [])}
        for code in sorted(c for c in codes if c):
            where = [tuple(t["box"]) for t in item.get("targets", []) if t["code"] == code] or own
            found += [Fact(code, item.get("page"), box) for box in where or [None]]
    return found


def review_catches(defect: Defect, clean: list[Fact], damaged: list[Fact],
                   damage: Damage) -> list[str]:
    """The review codes that catch the defect: over the changed region, and either new there
    (the control raised no fact of that code over it) or one of the defect's own codes. An
    unrelated warning that was already on the page never counts."""
    before = {f.code for f in clean if covers(f, damage)}
    return sorted({f.code + (" (already there)" if f.code in before else "")
                   for f in damaged if covers(f, damage)
                   and (f.code not in before or f.code in defect.codes)})


def run_spec(spec: PageSpec, out: DocOutput) -> dict[str, str | None]:
    """Each assertion of `spec` on `out`: None when it holds, else why it failed."""
    results: dict[str, str | None] = {}
    for a in spec.assertions:
        name = f"{a.check}#{a.index}"
        try:
            run_assertion(spec, a, out)
            results[name] = None
        except (CheckFailed, AssertionError, KeyError) as e:
            results[name] = f"{type(e).__name__}: {e}"
    return results


def page_shape(out: DocOutput, page: int):
    """The page's published HTML as a tree of (tag, attributes, children): what the defect
    must change for the checks to have something to see."""
    def shape(n):
        if isinstance(n, str):
            return n
        return (n.tag, tuple(sorted(n.attrs.items())), tuple(shape(c) for c in n.children))
    try:
        return shape(out.page(page))
    except KeyError:
        return None


@dataclass
class Cell:
    doc: str
    defect: str
    seed: int
    out: Path | None = None
    damage: Damage | None = None
    verdict: Verdict | None = None
    checks: list[str] = field(default_factory=list)   # the assertions that caught it
    review: list[str] = field(default_factory=list)   # the review codes that caught it
    failing: list[str] = field(default_factory=list)  # assertions that fail on the control too
    why: dict[str, str] = field(default_factory=dict)  # each catching assertion's message
    seconds: float = 0.0

    @property
    def enforced(self) -> bool:
        return self.defect != CONTROL and DEFECT_BY_ID[self.defect].enforced_on(self.doc)

    def end(self, outcome: Outcome, detail: str) -> None:
        self.verdict = Verdict(outcome, None, detail)


class Run:
    """The cells of one pytest session, worked out together on first use."""

    def __init__(self, root: Path, wanted: list[tuple[str, str]]):
        self.root = root
        self.cells = {(d, k): Cell(d, k, cell_seed(d, k)) for d, k in wanted}
        self.docs: dict[str, dict] = {}
        self.specs = {}
        for spec in load_specs():
            self.specs.setdefault(spec.doc, {})[spec.page] = spec
        self.started = datetime.datetime.now().isoformat(timespec="seconds")
        self.ended = ""

    def _base(self, slug: str) -> dict:
        """The document's conversion in this run's folder, and its control rendered again."""
        if slug in self.docs:
            return self.docs[slug]
        folder = self.root / slug
        folder.mkdir(parents=True, exist_ok=True)
        base = folder / "base"
        info = {"base": base, "control": folder / f"out-{CONTROL}", "error": None,
                "from": None, "pdf_sha256": hashlib.sha256(source(slug).read_bytes()).hexdigest()}
        self.docs[slug] = info
        cached = Corpus(quick=False)._path(slug)
        with _read_lock(cached) as held:
            if held and (cached / MANIFEST_JSON).exists():
                shutil.copytree(cached, base)
                info["from"] = f"page-check cache {cached.parent.name}/{cached.name}"
                info["cached"], info["digests"] = cached, digests(base)
        if info["from"] is None:
            from stage_equivalence import ConversionFailed, convert_in_child
            try:
                convert_in_child(source(slug), base, stage_root=None)
                info["from"] = "converted in this run"
            except ConversionFailed as e:
                info["error"] = f"the document did not convert: {e}"
                return info
        shutil.copytree(base, info["control"])
        try:
            render(info["control"])
        except Exception as e:   # noqa: BLE001 - a render that breaks is a harness bug
            info["error"] = f"rendering the clean model failed: {type(e).__name__}: {e}"
            return info
        want, got = digests(base), digests(info["control"])
        if changed := sorted(k for k in want.keys() | got.keys() if want.get(k) != got.get(k)):
            info["error"] = ("rendering the clean model again changed the output: "
                             + ", ".join(changed[:5]))
        return info

    def _spec_pages(self, slug: str) -> frozenset[int]:
        return frozenset(self.specs.get(slug, {}))

    def _work(self, cell: Cell) -> None:
        start = time.monotonic()
        try:
            self._score(cell)
        finally:
            cell.seconds = time.monotonic() - start

    def _score(self, cell: Cell) -> None:
        if reason := absent_reason(cell.doc):   # a sample that is not in this clone
            return cell.end(Outcome.UNSUPPORTED, reason)
        info = self._base(cell.doc)
        if info["error"]:
            return cell.end(Outcome.INVALID, info["error"])
        control = DocOutput(cell.doc, info["control"])
        if cell.defect == CONTROL:
            cell.out, cell.damage = info["control"], Damage(frozenset())
            for _, spec in sorted(self.specs.get(cell.doc, {}).items()):
                for name, why in run_spec(spec, control).items():
                    if why is not None and not next(a for a in spec.assertions
                                                    if f"{a.check}#{a.index}" == name).xfail:
                        cell.failing.append(f"{spec.id} {name}")
                        cell.why[f"{spec.id} {name}"] = why
            cell.verdict = (Verdict(Outcome.FALSE_ALARM, None, "assertions fail with no defect: "
                                    + ", ".join(cell.failing))
                            if cell.failing else Verdict(Outcome.PRESERVED, True))
            return
        defect = DEFECT_BY_ID[cell.defect]
        out = self.root / cell.doc / f"out-{cell.defect}"
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(info["base"], out)
        cell.out = out
        rng = random.Random(cell.seed)
        try:
            cell.damage = render(out, lambda doc: defect.mutate(doc, rng, self._spec_pages(cell.doc)))
        except Unsupported as e:
            return cell.end(Outcome.UNSUPPORTED, str(e))
        except ValueError as e:
            return cell.end(Outcome.INVALID, f"the defect's model does not round-trip: {e}")
        except Exception as e:   # noqa: BLE001 - a defect that breaks is a harness bug
            return cell.end(Outcome.INVALID, f"the defect failed: {type(e).__name__}: {e}")
        damaged = DocOutput(cell.doc, out)
        if (out / DOCUMENT_JSON).read_bytes() == (info["control"] / DOCUMENT_JSON).read_bytes():
            return cell.end(Outcome.INVALID, "the defect changed nothing")
        (page,) = cell.damage.pages
        if page_shape(damaged, page) == page_shape(control, page):
            return cell.end(Outcome.INVALID, f"page {page} renders the same after the defect")
        spec = self.specs[cell.doc][page]
        before, after = run_spec(spec, control), run_spec(spec, damaged)
        cell.failing = [n for n, why in before.items() if why is not None]
        cell.checks = [n for n, why in after.items() if why is not None and before[n] is None]
        cell.why = {n: after[n] for n in cell.checks}
        cell.review = review_catches(defect, signals(control), signals(damaged), cell.damage)
        caught = bool(cell.checks or cell.review)
        parts = [f"{spec.id}: {cell.damage.note}"]
        if cell.checks:
            parts.append("checks: " + ", ".join(cell.checks))
        if cell.review:
            parts.append("review: " + ", ".join(cell.review))
        cell.verdict = Verdict(Outcome.CAUGHT if caught else Outcome.SILENT_LOSS, caught,
                               "; ".join(parts))

    def ensure(self) -> None:
        for cell in self.cells.values():
            if cell.verdict is None:
                self._work(cell)
        self.ended = datetime.datetime.now().isoformat(timespec="seconds")

    def touched(self) -> list[str]:
        """The cache entries this run copied whose files changed since: none, ever."""
        return [f"{slug}: {info['cached']}" for slug, info in sorted(self.docs.items())
                if info.get("cached") and info["cached"].exists()   # gone: another run evicted it
                and digests(info["cached"]) != info["digests"]]

    def verdict(self, doc: str, defect: str) -> Cell:
        self.ensure()
        return self.cells[(doc, defect)]

    # ------------------------------------------------------------------------ reports

    def record(self) -> dict:
        return {
            "started": self.started, "ended": self.ended, "seed": SEED, "tools": tool_versions(),
            "documents": {s: {"blurb": DOCS[s], "from": d["from"], "error": d["error"],
                              "pdf_sha256": d["pdf_sha256"],
                              "spec_pages": sorted(self._spec_pages(s))}
                          for s, d in sorted(self.docs.items())},
            "defects": {d.id: {"blurb": d.blurb, "codes": sorted(d.codes),
                               "enforced": sorted(d.enforced)} for d in DEFECTS},
            "cells": [{"doc": c.doc, "defect": c.defect, "seed": c.seed,
                       "outcome": c.verdict.outcome.value if c.verdict else None,
                       "enforced": c.enforced, "detail": c.verdict.detail if c.verdict else "",
                       "page": min(c.damage.pages) if c.damage and c.damage.pages else None,
                       "note": c.damage.note if c.damage else "",
                       "checks": c.checks, "review": c.review, "failing": c.failing,
                       "why": c.why,
                       "document_sha256": (hashlib.sha256((c.out / DOCUMENT_JSON).read_bytes())
                                           .hexdigest() if c.out else None)}
                      for c in self.cells.values()],
        }

    def write(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        record = self.record()
        (folder / "defects.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        (folder / "defects.md").write_text(scorecard(record), encoding="utf-8")



# ---------------------------------------------------------------------------- the scorecard

SCORECARD_BEGIN = "<!-- defects: generated by tests/robustness (--robustness-doc) -->"
SCORECARD_END = "<!-- end of defects -->"


def _mark(c: dict) -> str:
    if c["outcome"] == "caught":
        mark = " + ".join(w for w, hit in (("checks", c["checks"]), ("review", c["review"])) if hit)
    else:
        mark = {"silent loss": "LOSS", "unsupported": "n/a", "invalid": "INVALID",
                "preserved": "ok", "false alarm": "ALARM", "crashed": "CRASH"}[c["outcome"]]
    return f"**{mark}**" if c["enforced"] else mark


def scorecard(record: dict) -> str:
    """Markdown: defect x document, then one row per cell naming what caught it."""
    by = {(c["doc"], c["defect"]): c for c in record["cells"]}
    docs = [d for d in DOCS if d in record["documents"]]
    defects = [CONTROL] + [d for d in record["defects"] if any((s, d) in by for s in docs)]
    lines = ["| Defect | " + " | ".join(docs) + " | checks | review |",
             "| --- | " + " | ".join("---" for _ in docs) + " | --- | --- |"]
    for defect in defects:
        cs = [by[(s, defect)] for s in docs if (s, defect) in by and by[(s, defect)]["outcome"]]
        scored = [c for c in cs if c["outcome"] in ("caught", "silent loss")]
        tally = ("", "") if defect == CONTROL else (
            f"{sum(bool(c['checks']) for c in scored)}/{len(scored)}",
            f"{sum(bool(c['review']) for c in scored)}/{len(scored)}")
        lines.append(f"| `{defect}` | " + " | ".join(_mark(by[(s, defect)]) if (s, defect) in by
                                                     and by[(s, defect)]["outcome"] else ""
                                                     for s in docs)
                     + f" | {tally[0]} | {tally[1]} |")
    lines += ["", "| Defect | Document | Page | What changed | Assertions failed | Review codes |",
              "| --- | --- | --- | --- | --- | --- |"]
    for defect in defects[1:]:
        for s in docs:
            c = by.get((s, defect))
            if c is None or c["outcome"] is None:
                continue
            if c["outcome"] in ("caught", "silent loss"):
                what = c["note"].replace("|", "\\|")
                lines.append(f"| `{defect}` | {s} | {c['page']} | {_short(what, 90)} | "
                             + (", ".join(f"`{n}`" for n in c["checks"]) or "none") + " | "
                             + (", ".join(f"`{n}`" for n in c["review"]) or "none") + " |")
            else:
                lines.append(f"| `{defect}` | {s} | | {c['outcome']}: "
                             f"{_short(c['detail'], 90).replace('|', '/')} | | |")
    t = record["tools"]
    lines += ["", f"Run {record['started']} to {record['ended']}, seed {record['seed']}, reflow "
              f"{t['reflow']}. Each document's source and PDF hash are in defects.json.", "",
              *_currency(record)]
    alarms = [f"- {c['doc']}: {c['detail']}" for c in record["cells"]
              if c["defect"] == CONTROL and c["outcome"] != "preserved"]
    if alarms:
        lines += ["", "Controls that are not clean:", "", *alarms]
    return "\n".join(lines) + "\n"


def _currency(record: dict) -> list[str]:
    """Where each document's conversion came from. Either way it is this checkout's: a
    page-check cache entry is keyed by the converter source, the tool versions and the PDF
    (corpus.Corpus._key), the same identity reflow.provenance checks."""
    by: dict[str, list[str]] = {}
    for slug, d in record["documents"].items():
        where = ("from the page-check cache" if (d["from"] or "").startswith("page-check cache")
                 else d["from"] or "not converted")
        by.setdefault(where, []).append(slug)
    said = "; ".join(f"{', '.join(slugs)} {where}" for where, slugs in by.items())
    return [f"The conversions the cells read: {said}. A cache entry is keyed by the converter "
            "source by content, the tool versions and the PDF, so each one is what the source "
            "that ran makes of the PDF: current, in the sense of `bun run docs:check`."]


def update_doc(doc: Path, record: dict) -> None:
    text = doc.read_text(encoding="utf-8")
    begin = text.index(SCORECARD_BEGIN) + len(SCORECARD_BEGIN)
    end = text.index(SCORECARD_END)
    doc.write_text(f"{text[:begin]}\n{scorecard(record)}{text[end:]}", encoding="utf-8")
