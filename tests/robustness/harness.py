"""Run the robustness cells: damage each base document with each kind, convert the damaged copy,
and score what reflow published against the clean conversion.

A cell is (base, kind). Its steps:
  1. the base is written (a synthetic PDF from tests/conftest.py, or a short sample), and saved
     again through pikepdf as the clean control, the way every mutant is saved;
  2. the kind's transform writes the mutant. Unsupported (a precondition) ends the cell as
     UNSUPPORTED. A transform that fails, that changes nothing (`fingerprint`), or whose mutant
     renders differently when the kind declares the pixels the same (`render_difference`) ends
     it as INVALID;
  3. the base, the control and the mutants are converted whole, each in a child process
     (tests/stage_equivalence.convert_in_child) with no stage cache, into this run's private
     folder: never the page-check caches, never as a page-check document;
  4. the mutant's projection and facts are judged against the base's conversion (oracle.judge).
     The control is judged the same way: it must be the same, and a fact some kind watches for
     on a clean file is a false alarm.

The real files of samples/robustness/MANIFEST.tsv (real.py) are converted in the same pool, each
read where it lives and never copied into a cache. A row with a hand transcript is judged against
it; every other row is inventory. The scorecard groups them by the family of the tool that made
them, read from the converted Document's producer and creator.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import platform
import random
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pikepdf
from checkout import sample
from conftest import make_scan_pdf, make_tagged_pdf, make_text_pdf
from robustness import oracle, real
from robustness.kinds import KINDS, WATCHED_CODES, Damage, Pixels, Unsupported, save
from robustness.oracle import Outcome, Verdict
from robustness.real import RealResult, Row
from stage_equivalence import ConversionFailed, convert_in_child

from reflow import budget

REPO = Path(__file__).resolve().parents[2]
SEED = 305
CONTROL = "clean"     # the kind id of a base's control cell
PIXEL_SCALE = 1.5     # 108 dpi
PIXEL_TOLERANCE = 24  # the largest channel difference a pixel-same kind may show ...
PIXEL_REACH = 2       # ... against some pixel this near (see _unmatched) ...
PIXEL_SLACK = 2e-4    # ... on all but this share of a page's pixels (218 at this scale): the
                      # glyphs a split text run hints differently (up to 63 pixels on the bases).
                      # A 0.5 pt line fits in it too, so the tiny-text kind declares CHANGES.


# ---------------------------------------------------------------------------- bases

PROSE = [[
    (72, 72, 18, "Annual office report", "B"),
    (72, 110, 11, "The staff filed five official files after the final fit check of the year."),
    (72, 126, 11, "Our office flew the flag of the field team in the fall, as it does often."),
    (72, 142, 11, "A brief note of thanks for the effort of everyone who offered to help."),
    (72, 180, 14, "Second section", "B"),
    (72, 210, 11, "Each fluent reader of this file will find it clear and in the right order."),
]]
SCAN = [[(72, "Scanned letter of the county office"), (100, "The clerk filed the deed in March."),
         (128, "Fees are due within thirty days.")]]


def _sample(rel: str, pages: list[int] | None = None) -> Callable[[Path], Path]:
    def make(dst: Path) -> Path:
        src = sample(REPO, f"samples/{rel}")
        if not src.exists():   # gitignored or fetched: not in a fresh clone, so its cells skip
            hint = "; run scripts/fetch_samples.py" if rel == "arxiv.pdf" else ""
            raise Unsupported(f"{src} is not here{hint}")
        if pages is None:
            shutil.copyfile(src, dst)
            return dst
        with pikepdf.open(src) as pdf:
            for i in reversed(range(len(pdf.pages))):
                if i + 1 not in pages:
                    del pdf.pages[i]
            pdf.save(dst, deterministic_id=True)
        return dst
    return make


@dataclass(frozen=True)
class Base:
    name: str
    blurb: str
    make: Callable[[Path], Path]


BASES = [
    Base("prose", "synthetic: one page of Helvetica prose with fi and fl words "
         "(make_text_pdf)", lambda d: make_text_pdf(d, PROSE)),
    Base("tagged", "synthetic: two tagged Courier pages with a table and an outline "
         "(make_tagged_pdf)", make_tagged_pdf),
    Base("scan", "synthetic: a page image under an invisible OCR text layer (make_scan_pdf)",
         lambda d: make_scan_pdf(d, SCAN)),
    Base("fw9", "samples/fw9.pdf: a tagged IRS form, 6 pages, links and fields",
         _sample("fw9.pdf")),
    Base("arxiv-p1-2", "samples/arxiv.pdf pages 1-2: an untagged pdfTeX paper",
         _sample("arxiv.pdf", [1, 2])),
    Base("i9", "samples/corpus/uscis-i9.pdf: a tagged form, 4 pages, Type0 fonts",
         _sample("corpus/uscis-i9.pdf")),
]
BASE_BY_NAME = {b.name: b for b in BASES}
KIND_BY_ID = {k.id: k for k in KINDS}


def cells() -> list[tuple[str, str]]:
    """Every cell: each base's control, then each base with each kind."""
    return [(b.name, k) for b in BASES for k in [CONTROL, *KIND_BY_ID]]


def cell_seed(base: str, kind: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{SEED}:{base}:{kind}".encode()).digest()[:4], "big")


# ---------------------------------------------------------------------------- validity

def fingerprint(path: Path) -> str:
    """A digest of what a damage kind can change, independent of object numbers and of how the
    file was written: each page's content, boxes, rotation, fonts and annotations, the tag tree,
    the language, the mark info and the form."""
    h = hashlib.sha256()

    def put(*parts) -> None:
        for p in parts:
            h.update(repr(p).encode() if not isinstance(p, bytes) else p)
            h.update(b"\0")

    with pikepdf.open(path) as pdf:
        from robustness.kinds import _fonts, _inherited
        for page in pdf.pages:
            put(b"page", page.obj.get("/Contents") is not None and b"".join(
                pikepdf.unparse_content_stream([i]) for i in pikepdf.parse_content_stream(page)))
            for key in ("/MediaBox", "/CropBox", "/Rotate"):
                v = _inherited(page, key)
                put(key, [float(x) for x in v] if isinstance(v, pikepdf.Array) else
                    int(v) if v is not None else None)
            for name, font in sorted(_fonts(page).items()):
                tu = font.get("/ToUnicode")
                put(name, str(font.get("/BaseFont")), repr(font.get("/Encoding")),
                    tu.read_bytes() if isinstance(tu, pikepdf.Stream) else None)
            for annot in page.obj.get("/Annots", []):
                put(str(annot.get("/Subtype")), [float(x) for x in annot.get("/Rect", [])])
        root = pdf.Root
        put(str(root.get("/Lang")), repr(root.get("/MarkInfo")),
            len(root.AcroForm.get("/Fields", [])) if "/AcroForm" in root else None)
        if "/StructTreeRoot" in root:
            from robustness.kinds import _struct_elems
            for elem in _struct_elems(root.StructTreeRoot):
                kids = elem.get("/K")
                kids = list(kids) if isinstance(kids, pikepdf.Array) else [kids]
                put(str(elem.S), [int(k) if isinstance(k, int) else str(k.get("/S", "ref"))
                                  if isinstance(k, pikepdf.Dictionary) else None for k in kids])
    return h.hexdigest()


def _unmatched(x, y):
    """Pixels of `x` that no pixel within PIXEL_REACH pixels of the same place in `y` matches to
    within PIXEL_TOLERANCE levels. pdfium snaps the origin of each text run to the pixel grid, so
    splitting one Tj into several moves a glyph by a pixel or so without moving its text (the
    char boxes stay the same)."""
    import numpy as np

    r = PIXEL_REACH
    padded = np.pad(y, ((r, r), (r, r), (0, 0)), mode="edge")
    h, w = x.shape[:2]
    best = np.full(x.shape[:2], 255)
    for dy in range(2 * r + 1):
        for dx in range(2 * r + 1):
            best = np.minimum(best, np.abs(x - padded[dy:dy + h, dx:dx + w]).max(axis=-1))
    return best > PIXEL_TOLERANCE


def render_difference(a: Path, b: Path) -> str | None:
    """None when every page of the two files renders the same with pdfium (forms drawn), to
    within PIXEL_REACH pixels (`_unmatched`) and PIXEL_SLACK, else what differs."""
    import logging

    import numpy as np
    import pypdfium2 as pdfium

    docs = [pdfium.PdfDocument(str(p)) for p in (a, b)]
    try:
        quiet = logging.getLogger("pypdfium2")
        level, quiet.level = quiet.level, logging.ERROR   # "init_forms() called on XFA pdf"
        try:
            for d in docs:
                d.init_forms()
        finally:
            quiet.setLevel(level)
        if len(docs[0]) != len(docs[1]):
            return f"{len(docs[0])} pages became {len(docs[1])}"
        for i in range(len(docs[0])):
            x, y = (d[i].render(scale=PIXEL_SCALE, may_draw_forms=True).to_numpy().astype(int)
                    for d in docs)
            if x.shape != y.shape:
                return f"page {i + 1} renders {x.shape[1]}x{x.shape[0]}, then {y.shape[1]}x{y.shape[0]}"
            bad = _unmatched(x, y) | _unmatched(y, x)
            if bad.sum() > PIXEL_SLACK * bad.size:
                ys, xs = np.nonzero(bad)
                return (f"page {i + 1}: {len(ys)} pixels differ, around "
                        f"({xs.min() / PIXEL_SCALE:.0f}, {ys.min() / PIXEL_SCALE:.0f})-"
                        f"({xs.max() / PIXEL_SCALE:.0f}, {ys.max() / PIXEL_SCALE:.0f}) pt")
    finally:
        for d in docs:
            d.close()
    return None


# ---------------------------------------------------------------------------- one run

@dataclass
class Cell:
    base: str
    kind: str
    seed: int
    pdf: Path | None = None
    out: Path | None = None
    damage: Damage | None = None
    verdict: Verdict | None = None
    seconds: float = 0.0

    @property
    def enforced(self) -> bool:
        return self.kind != CONTROL and KIND_BY_ID[self.kind].enforced_on(self.base)

    @property
    def why_unenforced(self) -> str:
        return "" if self.kind == CONTROL else KIND_BY_ID[self.kind].why_unenforced(self.base)

    def end(self, outcome: Outcome, detail: str) -> None:
        self.verdict = Verdict(outcome, None, detail)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tool(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return (done.stdout or done.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return "missing"


def tool_versions() -> dict[str, str]:
    import importlib.metadata

    import pdfplumber

    from reflow.ingest.ocr import make_runner
    ocr = make_runner("auto")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                            text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src"], cwd=REPO,
                           capture_output=True, text=True).stdout.strip()
    return {"reflow": commit + (" (src edited)" if dirty else ""), "python": platform.python_version(),
            "pikepdf": pikepdf.__version__, "qpdf": pikepdf.__libqpdf_version__,
            "pypdfium2": importlib.metadata.version("pypdfium2"), "pdfplumber": pdfplumber.__version__,
            "qpdf-cli": _tool(["qpdf", "--version"]), "gs": _tool(["gs", "--version"]),
            "ocr": type(ocr).__name__ if ocr is not None else "none"}


class Run:
    """The cells of one pytest session, worked out together on first use so the conversions run
    side by side: up to `workers` child processes at once. With a `lease` (reflow.budget) each
    child holds one of its slots only while it runs, so the run shrinks to its fair share when
    another run starts converting."""

    def __init__(self, root: Path, wanted: list[tuple[str, str]], workers: int = 1,
                 real_rows: list[Row] = (), lease: budget.Lease | None = None):
        self.root = root
        self.workers = max(1, workers)
        self.lease = lease
        self.cells = {(b, k): Cell(b, k, cell_seed(b, k)) for b, k in wanted}
        self.real_rows = {r.id: r for r in real_rows}
        self.real: dict[str, RealResult] = {}
        self.bases: dict[str, dict] = {}
        self.started = datetime.datetime.now().isoformat(timespec="seconds")
        self.ended = ""

    def _base(self, name: str) -> dict:
        """The base's files: the PDF as made, and the control saved through pikepdf."""
        if name not in self.bases:
            folder = self.root / name
            folder.mkdir(parents=True, exist_ok=True)
            pdf = BASE_BY_NAME[name].make(folder / "base.pdf")
            control = folder / f"{CONTROL}.pdf"
            with pikepdf.open(pdf) as doc:
                save(doc, control)
            self.bases[name] = {"pdf": pdf, "control": control, "out": folder / "out-base",
                                "fingerprint": fingerprint(control), "error": None}
        return self.bases[name]

    def _mutate(self, cell: Cell) -> None:
        try:
            base = self._base(cell.base)
        except Unsupported as e:
            return cell.end(Outcome.UNSUPPORTED, str(e))
        if cell.kind == CONTROL:
            cell.pdf, cell.damage = base["control"], Damage(frozenset())
            return
        kind = KIND_BY_ID[cell.kind]
        dst = self.root / cell.base / f"{cell.kind}.pdf"
        try:
            cell.damage = kind.transform(base["control"], dst, random.Random(cell.seed))
        except Unsupported as e:
            return cell.end(Outcome.UNSUPPORTED, str(e))
        except Exception as e:   # a transform that breaks is a harness bug: the cell is invalid
            return cell.end(Outcome.INVALID, f"the transform failed: {type(e).__name__}: {e}")
        if fingerprint(dst) == base["fingerprint"]:
            return cell.end(Outcome.INVALID, "the transform changed nothing")
        if kind.pixels is Pixels.SAME and (why := render_difference(base["control"], dst)):
            return cell.end(Outcome.INVALID, f"declared pixel-same, but it renders differently: {why}")
        cell.pdf = dst

    def _convert(self, pdf: Path, out: Path) -> tuple[str | None, float]:
        start = time.monotonic()
        if out.exists():
            shutil.rmtree(out)
        try:
            convert_in_child(pdf, out, stage_root=None)
            return None, time.monotonic() - start
        except ConversionFailed as e:
            return str(e), time.monotonic() - start

    def _score(self, cell: Cell, error: str | None) -> None:
        base = self.bases[cell.base]
        if base["error"]:
            return cell.end(Outcome.INVALID, f"the clean base did not convert: {base['error']}")
        if error:
            return cell.end(Outcome.CRASHED, error.splitlines()[-1][:300] if error else "")
        want, got = oracle.project(base["out"]), oracle.project(cell.out)
        clean, damaged = oracle.facts(base["out"]), oracle.facts(cell.out)
        diffs = oracle.differences(want, got)
        if cell.kind == CONTROL:
            if diffs:
                return cell.end(Outcome.INVALID, "saving the base again changed the conversion: "
                                + "; ".join(diffs[:3]))
            watched = sorted(_watched() & {f.code for f in clean})
            cell.verdict = (Verdict(Outcome.FALSE_ALARM, None, f"the clean file raises {watched}")
                            if watched else Verdict(Outcome.PRESERVED, True))
            return
        kind = KIND_BY_ID[cell.kind]
        flagged = any(oracle.flag_found(code, clean, damaged, cell.damage)
                      for code in kind.expect.codes)
        cell.verdict = oracle.judge(kind.expect, diffs, flagged)

    def _locate(self, row: Row) -> Path | RealResult:
        """The real file to convert, or the result that says why there is none."""
        path = sample(REPO, row.path)
        if not path.exists():
            return RealResult(row, "absent", detail=f"{row.path} is not here")
        if (got := _sha(path)) != row.sha256:
            return RealResult(row, "absent", detail=f"{path} has SHA-256 {got}, not the manifest's")
        return path

    def _score_real(self, row: Row, out: Path, error: str | None, seconds: float) -> None:
        if error:
            result = RealResult(row, "crashed", detail=error.splitlines()[-1][:300])
        else:
            want = real.read_transcript(REPO / row.transcript) if row.transcript else None
            twin = KIND_BY_ID.get(row.damage_id)
            result = real.judge_real(row, out, want, twin.expect.code if twin else None)
        result.seconds = seconds
        self.real[row.id] = result

    def ensure(self) -> None:
        """Work out every cell and real file that has no verdict yet."""
        todo = [c for c in self.cells.values() if c.verdict is None and c.pdf is None]
        rows = [r for r in self.real_rows.values() if r.id not in self.real]
        if not todo and not rows:
            return
        for cell in todo:
            self._mutate(cell)
        jobs: list[tuple[str, Path, Path, Cell | Row | None]] = []
        for name, base in self.bases.items():
            if not base["out"].exists():
                jobs.append((name, base["pdf"], base["out"], None))
        for cell in todo:
            if cell.verdict is None:
                cell.out = self.root / cell.base / f"out-{cell.kind}"
                jobs.append((cell.base, cell.pdf, cell.out, cell))
        for row in rows:
            found = self._locate(row)
            if isinstance(found, RealResult):
                self.real[row.id] = found
            else:
                jobs.append((row.id, found, self.root / "real" / row.id, row))
        results = self._convert_all(jobs)
        for (name, _, _, cell), (error, _) in zip(jobs, results, strict=True):
            if cell is None:
                self.bases[name]["error"] = error
        for (_, _, out, job), (error, seconds) in zip(jobs, results, strict=True):
            if isinstance(job, Cell):
                job.seconds = seconds
                self._score(job, error)
            elif isinstance(job, Row):
                self._score_real(job, out, error, seconds)
        self.ended = datetime.datetime.now().isoformat(timespec="seconds")

    def _convert_all(self, jobs: list[tuple[str, Path, Path, Cell | Row | None]]
                     ) -> list[tuple[str | None, float]]:
        """Each job's (error, seconds), on up to `workers` threads, each holding a slot of the
        lease (when there is one) while its child process runs."""
        pool: Executor = ThreadPoolExecutor(self.workers)
        if self.lease is not None:
            pool = self.lease.meter(pool)
        try:
            return list(pool.map(lambda j: self._convert(j[1], j[2]), jobs))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def real_result(self, row_id: str) -> RealResult:
        self.ensure()
        return self.real[row_id]

    def verdict(self, base: str, kind: str) -> Cell:
        self.ensure()
        return self.cells[(base, kind)]

    # ------------------------------------------------------------------------ reports

    def record(self) -> dict:
        """What a scorecard rests on: the inputs' hashes, the seeds, the tools and each cell."""
        return {
            "started": self.started, "ended": self.ended, "seed": SEED, "tools": tool_versions(),
            "bases": {n: {"blurb": BASE_BY_NAME[n].blurb, "sha256": _sha(b["pdf"]),
                          "control_sha256": _sha(b["control"]), "error": b["error"]}
                      for n, b in sorted(self.bases.items())},
            "kinds": {k.id: {"blurb": k.blurb, "pixels": k.pixels.value, "expect": str(k.expect),
                             "target": str(k.target) if k.target else None, "tool": k.tool,
                             "enforced": sorted(k.enforced), "unenforced": k.unenforced}
                      for k in KINDS},
            "cells": [{"base": c.base, "kind": c.kind, "seed": c.seed,
                       "outcome": c.verdict.outcome.value if c.verdict else None,
                       "met": c.verdict.met if c.verdict else None, "enforced": c.enforced,
                       "why_unenforced": c.why_unenforced,
                       "detail": c.verdict.detail if c.verdict else "",
                       "damage": {"pages": sorted(c.damage.pages),
                                  "boxes": {str(p): b for p, b in c.damage.boxes.items()},
                                  "note": c.damage.note} if c.damage else None,
                       "input_sha256": _sha(c.pdf) if c.pdf and c.pdf.exists() else None,
                       "seconds": round(c.seconds, 2)}
                      for c in self.cells.values()],
            "real": [{"id": r.row.id, "manifest_family": r.row.family, "family": r.family,
                      "producer": r.producer, "creator": r.creator, "sha256": r.row.sha256,
                      "damage_id": r.row.damage_id, "quirk": r.row.quirk,
                      "transcript": bool(r.row.transcript), "outcome": r.outcome, "met": r.met,
                      "codes": dict(sorted(r.codes.items())), "detail": r.detail,
                      "seconds": round(r.seconds, 2)}
                     for _, r in sorted(self.real.items())],
        }

    def write(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        record = self.record()
        (folder / "run.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        (folder / "scorecard.md").write_text(scorecard(record), encoding="utf-8")


# ---------------------------------------------------------------------------- the scorecard

MARK = {"preserved": "ok", "caught": "caught", "silent loss": "LOSS", "false alarm": "ALARM",
        "invalid": "INVALID", "unsupported": "n/a", "crashed": "CRASH"}
SCORECARD_BEGIN = "<!-- scorecard: generated by tests/robustness (--robustness-doc) -->"
SCORECARD_END = "<!-- end of scorecard -->"


def scorecard(record: dict) -> str:
    """Markdown: damage kind x document, and each kind's counts of every outcome."""
    by = {(c["base"], c["kind"]): c for c in record["cells"]}
    bases = [b.name for b in BASES if b.name in record["bases"]]
    kinds = [CONTROL] + [k for k in record["kinds"] if any((b, k) in by for b in bases)]
    lines = ["| Kind | Expect | " + " | ".join(bases) + " |",
             "| --- | --- | " + " | ".join("---" for _ in bases) + " |"]
    for kind in kinds:
        info = record["kinds"].get(kind, {})
        row = []
        for b in bases:
            c = by.get((b, kind))
            if c is None or c["outcome"] is None:
                row.append("")
                continue
            mark = MARK[c["outcome"]]
            if c["met"] is False and c["outcome"] in ("preserved", "caught"):
                mark += " (miss)"
            row.append(f"**{mark}**" if c["enforced"] else mark)
        expect = "control" if kind == CONTROL else info["expect"] + (
            f" → {info['target']}" if info.get("target") else "")
        lines.append(f"| `{kind}` | {expect} | " + " | ".join(row) + " |")
    outcomes = [o.value for o in Outcome]
    lines += ["", "| Kind | " + " | ".join(outcomes) + " | met |",
              "| --- | " + " | ".join("---" for _ in outcomes) + " | --- |"]
    for kind in kinds:
        cs = [by[(b, kind)] for b in bases if (b, kind) in by and by[(b, kind)]["outcome"]]
        counts = [sum(c["outcome"] == o for c in cs) or "" for o in outcomes]
        scored = [c for c in cs if c["met"] is not None]
        lines.append(f"| `{kind}` | " + " | ".join(map(str, counts))
                     + f" | {sum(bool(c['met']) for c in scored)}/{len(scored)} |")
    t = record["tools"]
    lines += ["", f"Run {record['started']} to {record['ended']}, seed {record['seed']}, reflow "
              f"{t['reflow']}; pikepdf {t['pikepdf']} (qpdf {t['qpdf']}), pypdfium2 "
              f"{t['pypdfium2']}, pdfplumber {t['pdfplumber']}, {t['qpdf-cli']}, Ghostscript "
              f"{t['gs']}, OCR {t['ocr']}. The input hashes are in run.json."]
    notes = [f"- `{c['kind']}` on {c['base']}: {c['outcome']}. {_short(c['detail'])}"
             for c in record["cells"] if c["outcome"] in ("invalid", "crashed", "false alarm")
             or (c["outcome"] == "silent loss" and c["detail"])]
    if notes:
        lines += ["", "Why each cell that is not ok or n/a scored as it did:", "", *notes]
    unsupported = [f"- `{c['kind']}` on {c['base']}: {c['detail']}" for c in record["cells"]
                   if c["outcome"] == "unsupported"]
    if unsupported:
        lines += ["", "Unsupported preconditions:", "", *unsupported]
    waiting = _unenforced(record, bases)
    if waiting:
        lines += ["", "Cells that are not enforced, and why:", "", *waiting]
    if record.get("real"):
        lines += ["", *real_scorecard(record["real"])]
    return "\n".join(lines) + "\n"


def _open(c: dict) -> bool:
    """A cell the kind could damage that no enforced bit holds."""
    return c["kind"] != CONTROL and c["outcome"] not in (None, "unsupported") and not c["enforced"]


def unexplained(record: dict) -> list[str]:
    """The cells that are neither enforced nor say why: each must be one or the other."""
    return [f"{c['kind']} on {c['base']}" for c in record["cells"]
            if _open(c) and not c.get("why_unenforced")]


def _unenforced(record: dict, bases: list[str]) -> list[str]:
    """One line per kind and reason: the open cells it covers, with their outcomes."""
    groups: dict[tuple[str, str], list[str]] = {}
    for c in sorted((c for c in record["cells"] if _open(c)),
                    key=lambda c: (list(record["kinds"]).index(c["kind"]), bases.index(c["base"]))):
        why = c.get("why_unenforced") or "no reason written (add one to the kind's `unenforced`)"
        groups.setdefault((c["kind"], why), []).append(f"{c['base']} ({MARK[c['outcome']]})")
    return [f"- `{kind}` on {', '.join(where)}: {why}." for (kind, why), where in groups.items()]


def _watched() -> set[str]:
    return {code for k in KINDS for code in k.expect.codes} | WATCHED_CODES


def real_scorecard(rows: list[dict]) -> list[str]:
    """The real files: counts per family of the tool that made them, then each file."""
    watched = _watched()
    groups: dict[str, list[dict]] = {}
    for r in rows:   # a file that did not convert has no Document: its manifest family stands in
        groups.setdefault(r["family"] or r["manifest_family"], []).append(r)
    lines = ["### Real files by the tool that made them", "",
             f"{len(rows)} files from `samples/robustness/MANIFEST.tsv`, grouped by the family of "
             "the producer and creator that the converted Document records. They were picked "
             "because they are bad: the counts describe this manifest, not how common a quirk is. "
             "Only a file with a hand transcript is scored; the rest are inventory.", "",
             "| Family | files | converted | crashed | absent | flagged | scored | met |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    def tally(name: str, rs: list[dict]) -> str:
        converted = [r for r in rs if r["outcome"] not in ("crashed", "absent")]
        scored = [r for r in rs if r["met"] is not None]
        flagged = [r for r in converted if watched & set(r["codes"])]
        return (f"| {name} | {len(rs)} | {len(converted)} | "
                f"{sum(r['outcome'] == 'crashed' for r in rs)} | "
                f"{sum(r['outcome'] == 'absent' for r in rs)} | {len(flagged)}/{len(converted)} | "
                f"{len(scored)} | {sum(bool(r['met']) for r in scored)}/{len(scored)} |")
    lines += [tally(family, groups[family]) for family in sorted(groups)]
    lines.append(tally("all", rows))
    unmeasured = [name for name, _ in real.FAMILIES if name not in groups]
    lines += ["", "\"flagged\" counts the files that raised a fact some damage kind watches for "
              f"({', '.join(sorted(watched))}). An absent file is counted only as absent.", "",
              f"No file here comes from {', '.join(unmeasured)}: the scorecard makes no claim "
              "about those tools." if unmeasured else "Every family of `real.FAMILIES` has a file.",
              "",
              "| File | Family | Twin | Outcome | Facts raised | Quirk |", "| --- | --- | --- | --- | --- | --- |"]
    for r in sorted(rows, key=lambda r: (r["family"] or r["manifest_family"], r["id"])):
        codes = ", ".join(f"{c} {n}" for c, n in r["codes"].items()) or "none"
        outcome = r["outcome"] + (f": {_short(r['detail'], 160)}" if r["detail"] else "")
        family = r["family"] or r["manifest_family"]
        if r["family"] and r["family"] != r["manifest_family"]:
            family += f" (manifest: {r['manifest_family']})"
        quirk = r["quirk"].replace("|", "\\|")
        lines.append(f"| `{r['id']}` | {family} | {r['damage_id'] or '-'} | {outcome} | {codes} | "
                     f"{_short(quirk, 200)} |")
    return lines


def _short(text: str, limit: int = 240) -> str:
    """A detail cut for the scorecard; run.json keeps it whole."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def update_doc(doc: Path, record: dict) -> None:
    text = doc.read_text(encoding="utf-8")
    begin = text.index(SCORECARD_BEGIN) + len(SCORECARD_BEGIN)
    end = text.index(SCORECARD_END)
    doc.write_text(f"{text[:begin]}\n{scorecard(record)}{text[end:]}", encoding="utf-8")
