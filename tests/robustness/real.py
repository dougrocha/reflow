"""Real badly made PDFs, grouped by the tool that made them (samples/robustness/MANIFEST.tsv).

The damage kinds (kinds.py) say how reflow handles damage we can imagine. These rows are files that
real tools made badly, so the quirks the harness tests are ones users upload. Each row records
where its file came from, its licence and whether it may be redistributed, the quirk observed in
it, and the `damage_id` of its synthetic twin in KINDS.

A row with a hand transcript is scored against it: the published words must be the transcript's,
in its order, or the twin kind's fact must flag the document. A row without one is inventory: the
scorecard reports what reflow flagged on it, and it moves no failure rate. The files were picked
because they are bad, so the counts describe this manifest, never how common a quirk is.

A file that may not be redistributed is never committed. Its row keeps the hash and the retrieval
URL, and scripts/fetch_robustness_corpus.py downloads it into the main checkout, where every
worktree reads it (scripts/checkout.py sample), as for samples/docaccess.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "samples/robustness/MANIFEST.tsv"
COLUMNS = ("id", "path", "sha256", "producer", "creator", "family", "source_url", "retrieved",
           "license", "redistribute", "language", "pages", "quirk", "damage_id", "transcript")
# Where a file that may not be redistributed can live: folders git ignores, which the main
# checkout holds for every worktree.
UNCOMMITTED = ("samples/robustness/fetched/", "samples/azblue/site/", "samples/docaccess/")

# The tool a file's /Producer and /Creator name, first match wins. A design or OCR tool that hands
# its output to another PDF writer (Canva to Distiller, ABBYY to PDF-XChange, dvips to Ghostscript)
# names the family, because its quirks are the ones the file shows. A later tool that only
# "modified" the file (iText stamping a Distiller PDF) does not.
FAMILIES: list[tuple[str, str]] = [
    ("Canva", r"\bcanva\b"),
    ("ABBYY", r"abbyy|finereader"),
    ("dvips", r"\bdvips"),
    ("Microsoft Print to PDF", r"print to pdf"),
    ("Acrobat Paper Capture", r"paper capture"),
    ("pdfTeX", r"\bpdf(e)?tex\b"),
    ("Skia", r"\bskia\b"),
    ("cairo", r"\bcairo\b"),
    ("Quartz", r"\bquartz\b"),
    ("Snagit", r"\bsnagit\b"),
    ("Amyuni", r"\bamyuni\b"),
    ("PDFlib", r"\bpdflib\b"),
    ("Adobe PDF Library", r"adobe pdf library"),
    ("Acrobat Distiller", r"\bdistiller\b"),
    ("iText", r"\bitext"),
    ("Microsoft Word", r"microsoft.{0,4} (office )?word\b"),
    ("Microsoft Excel", r"\bexcel\b"),
    ("Ghostscript", r"\bghostscript\b"),
    ("OpenOffice", r"\b(openoffice|libreoffice)\b"),
    ("Adobe Designer", r"^designer\b"),
    ("veraPDF Test Builder", r"\bverapdf\b"),
]
UNKNOWN = "unknown"   # neither /Producer nor /Creator is set
OTHER = "other"       # set, and no family above matches


def family_of(producer: str | None, creator: str | None) -> str:
    """The family of the tool that made a PDF, from its /Producer and /Creator."""
    names = [re.split(r";\s*modified using", s or "", flags=re.IGNORECASE)[0].strip()
             for s in (producer, creator)]
    if not any(names):
        return UNKNOWN
    for name, pattern in FAMILIES:
        if any(re.search(pattern, s, re.IGNORECASE) for s in names if s):
            return name
    return OTHER


@dataclass(frozen=True)
class Row:
    id: str
    path: str
    sha256: str
    producer: str
    creator: str
    family: str
    source_url: str
    retrieved: str
    license: str
    redistribute: str
    language: str
    pages: str
    quirk: str
    damage_id: str
    transcript: str

    @property
    def committed(self) -> bool:
        return not self.path.startswith(UNCOMMITTED)


def load(path: Path = MANIFEST) -> list[Row]:
    """The manifest's rows. Lines that start with # are comments; empty cells are ""."""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    reader = csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader)
    if tuple(header) != COLUMNS:
        raise ValueError(f"{path}: the header is {header}, not {list(COLUMNS)}")
    rows = []
    for number, cells in enumerate(reader, start=2):
        if len(cells) != len(COLUMNS):
            raise ValueError(f"{path}: row {number} has {len(cells)} cells, not {len(COLUMNS)}")
        rows.append(Row(*(c.strip() for c in cells)))
    return rows


def problems(rows: list[Row], kind_ids: set[str]) -> list[str]:
    """What is wrong with the rows, without reading their files: [] when nothing is."""
    out = []
    seen: set[str] = set()
    for r in rows:
        where = f"{r.id or '(no id)'}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", r.id):
            out.append(f"{where}: the id must be lower-case letters, digits, dots and dashes")
        if r.id in seen:
            out.append(f"{where}: the id is used twice")
        seen.add(r.id)
        if r.damage_id and r.damage_id not in kind_ids:
            out.append(f"{where}: damage_id {r.damage_id!r} is not a kind in KINDS")
        if not re.fullmatch(r"[0-9a-f]{64}", r.sha256):
            out.append(f"{where}: sha256 must be 64 lower-case hex digits")
        if r.family != family_of(r.producer, r.creator):
            out.append(f"{where}: family {r.family!r}, but the producer and creator say "
                       f"{family_of(r.producer, r.creator)!r}")
        if r.redistribute not in ("yes", "no"):
            out.append(f"{where}: redistribute must be yes or no")
        if r.redistribute == "no" and r.committed:
            out.append(f"{where}: a file that may not be redistributed must live in a folder git "
                       f"ignores ({', '.join(UNCOMMITTED)})")
        if not (r.source_url.startswith(("https://", "http://")) or r.source_url == "-"):
            out.append(f"{where}: source_url must be an http(s) URL, or - when none is known")
        if r.source_url == "-" and r.committed:
            out.append(f"{where}: a committed file needs a source URL")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.retrieved):
            out.append(f"{where}: retrieved must be a date, YYYY-MM-DD")
        if not r.license or not r.quirk:
            out.append(f"{where}: license and quirk must be filled in")
        if not r.pages.isdigit():
            out.append(f"{where}: pages must be a number")
        if r.transcript and not r.transcript.startswith("samples/robustness/transcripts/"):
            out.append(f"{where}: a transcript lives in samples/robustness/transcripts/")
    return out


# ---------------------------------------------------------------------------- scoring

def read_transcript(path: Path) -> dict[int, tuple[str, ...]]:
    """A hand transcript's words per page: pages are separated by a form feed."""
    from pages.checks import page_tokens
    pages = path.read_text(encoding="utf-8").split("\f")
    return {i + 1: tuple(page_tokens(text)) for i, text in enumerate(pages)}


def document_meta(out: Path) -> dict:
    """The converted Document's meta: producer and creator are read from here, not the manifest."""
    return json.loads((out / "document.json").read_text(encoding="utf-8")).get("meta", {})


@dataclass
class RealResult:
    """What reflow published for one real file."""
    row: Row
    outcome: str                       # see REAL_OUTCOMES
    producer: str | None = None        # from the converted Document
    creator: str | None = None
    family: str = ""                   # family_of the Document's producer and creator
    codes: Counter = field(default_factory=Counter)   # every review fact code it raised
    detail: str = ""
    seconds: float = 0.0

    @property
    def scored(self) -> bool:
        """A transcribed file is scored, and a crash on one counts as a miss."""
        return self.outcome in ("preserved", "caught", "silent loss") or (
            self.outcome == "crashed" and bool(self.row.transcript))

    @property
    def met(self) -> bool | None:
        return self.outcome in ("preserved", "caught") if self.scored else None


REAL_OUTCOMES = {
    "preserved": "the published words are the transcript's, in its order",
    "caught": "the words differ, and the twin kind's fact flags the document",
    "silent loss": "the words differ, and nothing says so",
    "inventory": "no transcript: what it flagged is reported, and it moves no rate",
    "crashed": "the conversion failed",
    "absent": "the file is not here (scripts/fetch_robustness_corpus.py fetches it)",
}


def judge_real(row: Row, out: Path, want: dict[int, tuple[str, ...]] | None,
               watched: str | None) -> RealResult:
    """Score a converted real file. `want` is its hand transcript's words per page (None for
    inventory), and `watched` the fact code its twin kind expects (None for a kind that expects
    the text to be the same)."""
    from robustness import oracle
    meta = document_meta(out)
    found = oracle.facts(out)
    res = RealResult(row, "inventory", meta.get("producer"), meta.get("creator"),
                     family_of(meta.get("producer"), meta.get("creator")),
                     Counter(f.code for f in found))
    if want is None:
        return res
    got = oracle.project(out)
    diffs = [f"page {n} text: {oracle._first_divergence(want[n], got.pages[n].tokens)}"
             for n in sorted(want) if n in got.pages and want[n] != got.pages[n].tokens]
    diffs += [f"page {n} is missing" for n in sorted(want) if n not in got.pages]
    if not diffs:
        res.outcome = "preserved"
    elif watched and res.codes[watched]:
        res.outcome, res.detail = "caught", "; ".join(diffs)
    else:
        res.outcome = "silent loss"
        res.detail = (f"{watched} not raised; " if watched else "") + "; ".join(diffs)
    return res
