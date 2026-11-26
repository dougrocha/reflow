"""Do cached stages publish what an uncached conversion publishes?

The page-check harness serves a document whole from its conversion cache when it can, so a warm
run never reads the stage cache (reflow.stagecache) and nothing there checks it. This module
converts on its own instead, never through tests/pages/corpus.py, so neither side can be served
by the conversion cache:

  A  uncached: no stage cache.
  P  cold: a private, empty stage root, which this run fills.
  B  warm: a copy of P's root. Every stage P wrote must be read, and none built again.

B must publish what A publishes (OCR_NOISE aside, on an OCR'd document), and byte for byte what
P published, since a hit restores the bytes P cached. Editing a stage's module (`edited_tree`)
must make that stage miss, the stages it reads from hit, and the output still match an uncached
run of the same code.

Hits and misses are read off the root's layout (<stage kind>/<source>/<entry>, as
cachedir.evict(root, 3) walks it), never off a key. Before a run every entry is marked and backdated
(`arm`). A read touches its entry (stagecache._try_read), so a marked entry whose mtime moved was
a hit. An entry without the mark was written by the run, new or in an old one's place: a miss.
The private root is the only one written; the shared caches in .pytest_cache are never read or
written here.
Conversions run in a child process, with the reflow tree they are given first on PYTHONPATH.
"""

from __future__ import annotations

import difflib
import enum
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Callable

import pikepdf

from reflow import stagecache
from reflow.names import BUILD_JSON, OCR_WORDS, WORKING_PDF

SRC = Path(__file__).resolve().parents[1] / "src"   # the tree holding reflow/
BACKDATE_SECONDS = 3600


class Stage(enum.Enum):
    INGEST = "ingest"
    OCR = "ocr"
    EXTRACT = "extract"
    PAGES = "pages"
    WITNESS = "witness"   # the text witness (reflow.ingest.witness): OCR of a suspect page
    MODEL = "model"   # the finished model (reflow.saved): a hit reads no other stage

    @property
    def upstream(self) -> frozenset[Stage]:
        """The stages whose artifact this one reads (its key holds their bytes): an edit to this
        stage must leave them hits. Extraction and the page reads both read the OCR'd working
        copy and not each other; the witness reads the copy before OCR; the model is built from
        all of them."""
        return {Stage.INGEST: frozenset(), Stage.OCR: frozenset({Stage.INGEST}),
                Stage.WITNESS: frozenset({Stage.INGEST}),
                Stage.EXTRACT: frozenset({Stage.INGEST, Stage.OCR}),
                Stage.PAGES: frozenset({Stage.INGEST, Stage.OCR}),
                Stage.MODEL: READING}[self]


READING = frozenset({Stage.INGEST, Stage.OCR, Stage.EXTRACT, Stage.PAGES, Stage.WITNESS})   # read the PDF
# The kind folders stagecache writes, by stage. A kind not here fails the check: a new stage
# must say which stage it is.
KINDS = {"ingest": Stage.INGEST, "ocr": Stage.OCR, "extract": Stage.EXTRACT,
         "pages": Stage.PAGES, "pages-words": Stage.PAGES, "witness": Stage.WITNESS,
         "model": Stage.MODEL}
# The module an invalidation case edits: the stage's entry module, which its key always holds.
EDITED_MODULE = {stage: SRC.joinpath(*stagecache.STAGES[stage.value][0].split(".")).with_suffix(".py")
                 for stage in Stage}


# --------------------------------------------------------------------------- comparing outputs

@dataclass(frozen=True)
class Exclusion:
    """A known difference between two correct conversions of one file: `normalize` removes
    exactly that part of its bytes, and everything else is still compared. Never a pattern, and
    never without its reason."""
    path: str
    reason: str
    normalize: Callable[[bytes], bytes]

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError(f"{self.path}: an exclusion needs its reason")


def _without_ocr_stamps(data: bytes) -> bytes:
    """The PDF without the trailer /ID, ModDate, the XMP dates, and with each page's /OCR-<random>
    XObjects renamed in page order."""
    with pikepdf.open(io.BytesIO(data)) as pdf:
        if "/ID" in pdf.trailer:
            del pdf.trailer["/ID"]
        if "/ModDate" in pdf.docinfo:
            del pdf.docinfo["/ModDate"]
        with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as meta:
            for key in ("xmp:ModifyDate", "xmp:MetadataDate"):
                if key in meta:
                    del meta[key]
        for i, page in enumerate(pdf.pages):
            xobjects = page.Resources.get("/XObject", {})
            ocr = [k for k in list(xobjects.keys()) if k.startswith("/OCR-")]
            if not ocr:
                continue
            contents = page.Contents.read_bytes()
            for j, name in enumerate(ocr):
                xobjects[f"/OCR-{i}-{j}"] = xobjects[name]
                del xobjects[name]
                contents = contents.replace(name.encode() + b" ", f"/OCR-{i}-{j} ".encode())
            page.Contents.write(contents)
        out = io.BytesIO()
        pdf.save(out, static_id=True)
        return out.getvalue()


def _without_pdf_digest(data: bytes) -> bytes:
    value = json.loads(data)
    value.pop("pdf_sha256", None)
    return json.dumps(value, sort_keys=True).encode()


# Two OCR runs of one PDF differ in exactly these, measured on samples/scan.pdf and scan-w4.pdf;
# every other file and every other byte of these two is the same. Applied only between
# independent runs of an OCR'd document (ocr-words.json present): a stage-cache hit restores the
# cached bytes, so the cold and warm runs are compared without it. A document that is not OCR'd
# compares working.pdf byte for byte, since qpdf's copy is the same bytes each time.
OCR_NOISE = (
    Exclusion(WORKING_PDF,
              "ocrmypdf stamps each run's copy with a new second /ID, ModDate and XMP dates, and "
              "names each page's text layer XObject /OCR-<random>", _without_ocr_stamps),
    Exclusion(OCR_WORDS,
              "pdf_sha256 is the digest of working.pdf, which differs as above",
              _without_pdf_digest),
)


@dataclass(frozen=True)
class Presence:
    """A file one side writes and the other never does, with its reason. Its absence on the
    left is allowed; where both sides have it, it is compared as any file is."""
    path: str
    reason: str


# A conversion records its identities only with the stage cache on (reflow.saved): an uncached
# run computes no key, so it writes no build.json.
CACHED_ONLY = (Presence(BUILD_JSON, "the identities a conversion records only with the stage "
                                      "cache on (reflow.saved)"),)


def noise_of(out: Path) -> tuple[Exclusion, ...]:
    """The exclusions between independent conversions to `out`'s document: OCR_NOISE if it was
    OCR'd, else none."""
    return OCR_NOISE if (out / OCR_WORDS).exists() else ()


@dataclass(frozen=True)
class Difference:
    path: str
    kind: str       # "content", "only-left", "only-right"
    excerpt: str = ""


@dataclass(frozen=True)
class Comparison:
    left: str
    right: str
    compared: int
    differences: tuple[Difference, ...]
    applied: tuple[Exclusion, ...]   # the exclusions that hid a difference

    @property
    def ok(self) -> bool:
        return not self.differences

    def render(self) -> str:
        lines = [f"{self.left} ~ {self.right}: {self.compared} files, "
                 f"{len(self.differences)} differ"]
        for e in self.applied:
            lines.append(f"  normalized {e.path}: {e.reason}")
        for d in self.differences:
            lines.append(f"  {d.kind:<10} {d.path}")
            lines.extend(f"    {line}" for line in d.excerpt.splitlines())
        return "\n".join(lines)


def _files(root: Path) -> dict[str, Path]:
    return {str(p.relative_to(root)): p for p in sorted(root.rglob("*")) if p.is_file()}


def _excerpt(a: bytes, b: bytes) -> str:
    try:
        left, right = a.decode().splitlines(), b.decode().splitlines()
    except UnicodeDecodeError:
        return (f"sha256 {hashlib.sha256(a).hexdigest()[:16]} != "
                f"{hashlib.sha256(b).hexdigest()[:16]}")
    if len(left) == 1 == len(right):   # one-line JSON: split it so the diff can say where
        left, right = left[0].split(", "), right[0].split(", ")
    diff = difflib.unified_diff(left, right, lineterm="", n=1)
    return "\n".join(line[:200] for line in list(diff)[2:22])


def compare_outputs(left: Path, right: Path, exclusions: tuple[Exclusion, ...] = (),
                    names: tuple[str, str] = ("left", "right"),
                    right_only: tuple[Presence, ...] = ()) -> Comparison:
    """Every file of two conversions' outputs, by bytes. A file that differs is compared again
    through its exclusion's `normalize`, and the exclusion is reported when that hid the
    difference."""
    a, b = _files(left), _files(right)
    by_path = {e.path: e for e in exclusions}
    differences: list[Difference] = []
    applied: list[Exclusion] = []
    for rel in sorted(a.keys() | b.keys()):
        if rel not in a and rel in {p.path for p in right_only}:
            continue
        if rel not in b or rel not in a:
            differences.append(Difference(rel, "only-left" if rel not in b else "only-right"))
            continue
        x, y = a[rel].read_bytes(), b[rel].read_bytes()
        if x == y:
            continue
        exclusion = by_path.get(rel)
        if exclusion is not None and exclusion.normalize(x) == exclusion.normalize(y):
            applied.append(exclusion)
            continue
        differences.append(Difference(rel, "content", _excerpt(x, y)))
    return Comparison(*names, len(a.keys() | b.keys()), tuple(differences), tuple(applied))


# --------------------------------------------------------------------------- observing a root

# Set on every entry before a run. A read keeps the entry, so it keeps the mark; an entry built
# again (stagecache._cached removes an unreadable one, then renames a new one into its place) is a
# new file without it, at the same path.
MARK = "user.reflow-stage-equivalence"


@dataclass(frozen=True)
class Seen:
    mtime_ns: int
    armed: bool   # the entry `arm` marked: not written since


def _armed(path: Path) -> bool:
    try:
        return os.getxattr(path, MARK) == b"1"
    except OSError:
        return False


def snapshot(root: Path) -> dict[str, Seen]:
    """{"<kind>/<source>/<entry>": Seen} of the stage root. Lock and temp files (cachedir names
    start with a dot) are not entries."""
    if not root.is_dir():
        return {}
    return {str(p.relative_to(root)): Seen(p.stat().st_mtime_ns, _armed(p))
            for p in sorted(root.glob("*/*/*")) if not p.name.startswith(".")}


def arm(root: Path) -> None:
    """Before a run: move every entry's mtime BACKDATE_SECONDS back, so a read (which touches
    it) shows whatever the file system's timestamp resolution, and mark it (MARK), so an entry
    built again is not taken for one read."""
    past = time.time() - BACKDATE_SECONDS
    for rel in snapshot(root):
        os.setxattr(root / rel, MARK, b"1")   # raises where the file system has no user xattrs
        os.utime(root / rel, (past, past))


def _stage(rel: str) -> Stage:
    kind = rel.split("/", 1)[0]
    if kind not in KINDS:
        raise ValueError(f"stage root holds kind {kind!r}, which no Stage claims: add it to KINDS")
    return KINDS[kind]


@dataclass(frozen=True)
class StageEvents:
    hit: frozenset[Stage]    # an armed entry was read
    miss: frozenset[Stage]   # an entry was written, new or in an old one's place: it was built

    @staticmethod
    def between(before: dict[str, Seen], after: dict[str, Seen]) -> StageEvents:
        hit = {_stage(rel) for rel, seen in after.items()
               if seen.armed and rel in before and seen.mtime_ns > before[rel].mtime_ns}
        miss = {_stage(rel) for rel, seen in after.items() if not seen.armed}
        return StageEvents(frozenset(hit), frozenset(miss))

    def render(self) -> str:
        names = lambda s: ", ".join(sorted(x.value for x in s)) or "none"   # noqa: E731
        return f"hit: {names(self.hit)}; built: {names(self.miss)}"


def stages_in(root: Path) -> frozenset[Stage]:
    return frozenset(_stage(rel) for rel in snapshot(root))


# --------------------------------------------------------------------------- planting

class PlantError(RuntimeError):
    """The stale entry could not be planted. A setup error, never a caught divergence."""


def plant_stale(root: Path, stage: Stage, donor_root: Path) -> list[tuple[str, str]]:
    """Make `stage`'s entries in `root` stale: each is replaced by an entry of the same kind
    that the donor's conversion wrote, kept at the target's own path, so the current code's key
    finds it. That is what a key missing a dependency serves: a well-formed entry made from other
    inputs. Returns (target, donor) pairs."""
    donors = {rel.split("/", 1)[0]: rel for rel in sorted(snapshot(donor_root))}
    pairs = []
    for rel in sorted(snapshot(root)):
        kind = rel.split("/", 1)[0]
        if _stage(rel) is not stage or kind not in donors:
            continue
        target, given = root / rel, donor_root / donors[kind]
        if _digest(target) == _digest(given):
            raise PlantError(f"{donors[kind]} holds the same bytes as {rel}")
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        (shutil.copytree if given.is_dir() else shutil.copyfile)(given, target)
        pairs.append((rel, donors[kind]))
    if not pairs:
        raise PlantError(f"no {stage.value} entry to plant: target or donor wrote none")
    return pairs


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    for p in [path] if path.is_file() else sorted(q for q in path.rglob("*") if q.is_file()):
        h.update(str(p.relative_to(path)).encode() if p != path else b"")
        h.update(p.read_bytes())
    return h.hexdigest()


# --------------------------------------------------------------------------- running conversions

def edited_tree(stage: Stage, dest: Path) -> Path:
    """A copy of src/ (without __pycache__) whose `stage` entry module ends with one more
    comment: behaviour is the same, and the bytes the stage's key hashes are not."""
    return _edited(EDITED_MODULE[stage].relative_to(SRC), f"\n# stage-equivalence: edits {stage.value}\n", dest)


def _edited(rel: Path, text: str, dest: Path) -> Path:
    tree = dest / "src"
    shutil.copytree(SRC / "reflow", tree / "reflow", ignore=shutil.ignore_patterns("__pycache__"))
    (tree / rel).write_bytes((tree / rel).read_bytes() + text.encode())
    return tree


# A render-only edit: a rule appended to the viewer's stylesheet, which every chunk's page links
# to and assets/viewer.css holds, so the published files change and the model does not.
RENDER_EDIT = (Path("reflow/viewer/viewer.css"), "\n.stage-equivalence-edit { color: inherit; }\n")


class ConversionFailed(RuntimeError):
    pass


def convert_in_child(pdf: Path, out: Path, *, tree: Path = SRC, stage_root: Path | None) -> None:
    """Convert `pdf` into `out` in a child process running the reflow found in `tree`, with the
    stage cache at `stage_root` (None: uncached). One page at a time, without page images, as the
    page-check harness converts."""
    env = {**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, __file__, str(tree), str(pdf), str(out),
                           str(stage_root or "")], env=env, capture_output=True, text=True)
    if done.returncode != 0:
        raise ConversionFailed(f"{pdf.name} -> {out.name}: {done.stderr.strip()[-2000:]}")


def ocr_available() -> bool:
    from reflow.ingest.ocr import make_runner
    return make_runner("auto") is not None


# --------------------------------------------------------------------------- verdicts

class Code(enum.Enum):
    OUTPUT_DIFFERS = "the cached conversion published something else"
    CONVERSION_FAILED = "the cached conversion failed"
    STAGE_NOT_READ = "a stage the cold run cached was not read"   # the check would be vacuous
    UNEXPECTED_MISS = "a stage the cold run cached was built again"
    NOT_EXERCISED = "the document did not exercise a stage the test expects"
    NO_MISS = "editing the stage's code did not make it miss"
    UPSTREAM_MISS = "editing the stage's code made a stage it reads from miss"
    UNEXPECTED_READ = "a stage the saved model stands for was read"


@dataclass(frozen=True)
class Problem:
    code: Code
    detail: str


@dataclass(frozen=True)
class Verdict:
    what: str
    problems: tuple[Problem, ...]
    evidence: tuple[str, ...]
    applied: tuple[Exclusion, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def codes(self) -> frozenset[Code]:
        return frozenset(p.code for p in self.problems)

    def render(self) -> str:
        head = f"{self.what}: " + ("equivalent" if self.ok else "FAILED")
        problems = [f"  {p.code.name}: {p.code.value}. {p.detail}" for p in self.problems]
        return "\n".join([head, *problems, *self.evidence])

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise AssertionError(self.render())


@dataclass(frozen=True)
class Primed:
    out: Path
    root: Path            # never written after priming: each check works on a copy
    stages: frozenset[Stage]


@dataclass
class Subject:
    """One PDF: its uncached conversion and its primed stage root, each made once. Every check
    runs on its own copy of the root, so checks do not share state."""
    pdf: Path
    work: Path
    expects: frozenset[Stage] = frozenset()
    _runs: int = field(default=0, repr=False)

    def _fresh(self, name: str) -> Path:
        self._runs += 1
        return self.work / f"{self._runs:02d}-{name}"

    @cached_property
    def uncached(self) -> Path:
        out = self._fresh("uncached") / "out"
        convert_in_child(self.pdf, out, stage_root=None)
        return out

    @cached_property
    def primed(self) -> Primed:
        run = self._fresh("cold")
        convert_in_child(self.pdf, run / "out", stage_root=run / "root")
        return Primed(run / "out", run / "root", stages_in(run / "root"))

    def _root_copy(self, run: Path) -> Path:
        root = run / "root"
        shutil.copytree(self.primed.root, root)
        return root

    def _not_exercised(self) -> list[Problem]:
        missing = self.expects - self.primed.stages
        return [Problem(Code.NOT_EXERCISED, ", ".join(sorted(s.value for s in missing)))] \
            if missing else []

    def check_equivalence(self, *, plant: tuple[Stage, Path] | None = None,
                          model: bool = True) -> Verdict:
        """The warm run against the uncached one and the cold one. `plant` (stage, donor root)
        makes that stage's entries stale first: the check must then fail. With the saved model
        (`model`) the warm run reads it and no other stage; without it (its entries removed
        first) every stage that reads the PDF must be read."""
        what = (f"{self.pdf.name}: cached stages" + ("" if model else " without the model")
                + (f", {plant[0].value} planted stale" if plant else ""))
        run = self._fresh("warm")
        root = self._root_copy(run)
        if not model:
            shutil.rmtree(root / Stage.MODEL.value, ignore_errors=True)
        expected = ({Stage.MODEL} if model and Stage.MODEL in self.primed.stages
                    else self.primed.stages - {Stage.MODEL})
        evidence = []
        if plant is not None:
            pairs = plant_stale(root, plant[0], plant[1])
            evidence += [f"  planted {t} <- donor's {d}" for t, d in pairs]
        problems = self._not_exercised()
        arm(root)
        before = snapshot(root)
        try:
            convert_in_child(self.pdf, run / "out", stage_root=root)
        except ConversionFailed as e:
            return Verdict(what, (*problems, Problem(Code.CONVERSION_FAILED, str(e))), tuple(evidence))
        events = StageEvents.between(before, snapshot(root))
        evidence.append(f"  warm run: {events.render()}")
        against_uncached = compare_outputs(self.uncached, run / "out", noise_of(self.uncached),
                                           ("uncached", "warm"), CACHED_ONLY)
        against_cold = compare_outputs(self.primed.out, run / "out", (), ("cold", "warm"))
        for comparison in (against_uncached, against_cold):
            evidence.append(comparison.render())
            if not comparison.ok:
                problems.append(Problem(Code.OUTPUT_DIFFERS, f"{comparison.left} ~ "
                                        f"{comparison.right}: "
                                        + ", ".join(d.path for d in comparison.differences)))
        if unread := expected - events.hit:
            problems.append(Problem(Code.STAGE_NOT_READ, ", ".join(sorted(s.value for s in unread))))
        if model and events.hit - expected:
            problems.append(Problem(Code.UNEXPECTED_READ,
                                    ", ".join(sorted(s.value for s in events.hit - expected))))
        if plant is None and events.miss - ({Stage.MODEL} if not model else set()):
            problems.append(Problem(Code.UNEXPECTED_MISS, ", ".join(
                sorted(s.value for s in events.miss - ({Stage.MODEL} if not model else set())))))
        return Verdict(what, tuple(problems), tuple(evidence), against_uncached.applied)

    def check_invalidation(self, stage: Stage) -> Verdict:
        """Edit `stage`'s module: it must miss, the stages it reads from must hit, and the
        output must equal an uncached conversion by the edited code. The stages after it are
        not constrained: their keys hold its artifact, which may be the same bytes again."""
        what = f"{self.pdf.name}: {EDITED_MODULE[stage].relative_to(SRC)} edited"
        if stage not in self.primed.stages:
            return Verdict(what, (Problem(Code.NOT_EXERCISED, stage.value),), ())
        run = self._fresh(f"edit-{stage.value}")
        tree = edited_tree(stage, run)
        convert_in_child(self.pdf, run / "uncached", tree=tree, stage_root=None)
        root = self._root_copy(run)
        if stage is not Stage.MODEL:   # else the saved model, which no edit upstream touches here
            shutil.rmtree(root / Stage.MODEL.value, ignore_errors=True)
        arm(root)
        before = snapshot(root)
        try:
            convert_in_child(self.pdf, run / "out", tree=tree, stage_root=root)
        except ConversionFailed as e:
            return Verdict(what, (Problem(Code.CONVERSION_FAILED, str(e)),), ())
        events = StageEvents.between(before, snapshot(root))
        comparison = compare_outputs(run / "uncached", run / "out", noise_of(run / "uncached"),
                                     ("uncached (edited)", "cached (edited)"), CACHED_ONLY)
        problems = []
        if stage not in events.miss:
            problems.append(Problem(Code.NO_MISS, f"{stage.value} was served from the cache"))
        upstream = stage.upstream & self.primed.stages
        if missed := upstream & events.miss:
            problems.append(Problem(Code.UPSTREAM_MISS, ", ".join(sorted(s.value for s in missed))))
        if unread := upstream - events.hit:
            problems.append(Problem(Code.STAGE_NOT_READ, ", ".join(sorted(s.value for s in unread))))
        if not comparison.ok:
            problems.append(Problem(Code.OUTPUT_DIFFERS,
                                    ", ".join(d.path for d in comparison.differences)))
        return Verdict(what, tuple(problems), (f"  cached run: {events.render()}",
                                               comparison.render()), comparison.applied)


    def check_render_edit(self) -> Verdict:
        """Edit what only the renderer reads (RENDER_EDIT): the saved model must hit, no stage be
        built, and the output equal, byte for byte, a conversion by the edited code that builds
        the model again from the same reading (the model entries removed), and an uncached one
        (OCR_NOISE aside, on an OCR'd document)."""
        what = f"{self.pdf.name}: {RENDER_EDIT[0]} edited"
        if Stage.MODEL not in self.primed.stages:
            return Verdict(what, (Problem(Code.NOT_EXERCISED, Stage.MODEL.value),), ())
        run = self._fresh("edit-render")
        tree = _edited(*RENDER_EDIT, run)
        convert_in_child(self.pdf, run / "uncached", tree=tree, stage_root=None)
        rebuilt = run / "rebuilt"
        shutil.copytree(self.primed.root, rebuilt / "root")
        shutil.rmtree(rebuilt / "root" / Stage.MODEL.value)
        convert_in_child(self.pdf, rebuilt / "out", tree=tree, stage_root=rebuilt / "root")
        root = self._root_copy(run)
        arm(root)
        before = snapshot(root)
        try:
            convert_in_child(self.pdf, run / "out", tree=tree, stage_root=root)
        except ConversionFailed as e:
            return Verdict(what, (Problem(Code.CONVERSION_FAILED, str(e)),), ())
        events = StageEvents.between(before, snapshot(root))
        comparisons = [compare_outputs(rebuilt / "out", run / "out", (),
                                       ("model built (edited)", "model reused (edited)"))]
        comparisons.append(compare_outputs(run / "uncached", run / "out",
                                           noise_of(run / "uncached"),
                                           ("uncached (edited)", "model reused (edited)"),
                                           CACHED_ONLY))
        unchanged = compare_outputs(self.uncached, run / "uncached", noise_of(self.uncached),
                                    ("before", "edited"))
        problems = []
        if Stage.MODEL not in events.hit:
            problems.append(Problem(Code.STAGE_NOT_READ, Stage.MODEL.value))
        if events.miss:
            problems.append(Problem(Code.UNEXPECTED_MISS,
                                    ", ".join(sorted(s.value for s in events.miss))))
        for comparison in comparisons:
            if not comparison.ok:
                problems.append(Problem(Code.OUTPUT_DIFFERS, f"{comparison.left} ~ "
                                        f"{comparison.right}: "
                                        + ", ".join(d.path for d in comparison.differences)))
        if unchanged.ok:   # the edit must reach the output, or the check proves nothing
            problems.append(Problem(Code.NOT_EXERCISED, "the edit changed no published file"))
        return Verdict(what, tuple(problems), (f"  cached run: {events.render()}",
                                               *(c.render() for c in comparisons)))


def _child(tree: str, pdf: str, out: str, stage_root: str) -> None:
    import reflow
    from reflow.convert import convert
    if not Path(reflow.__file__).resolve().is_relative_to(Path(tree).resolve()):
        raise SystemExit(f"reflow came from {reflow.__file__}, not the tree {tree}")
    convert(Path(pdf), Path(out), jobs=1, page_images=False,
            stage_cache=Path(stage_root) if stage_root else None)


if __name__ == "__main__":
    _child(*sys.argv[1:])
