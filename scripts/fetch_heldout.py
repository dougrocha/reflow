"""Fetch the held-out corpus of samples/heldout/MANIFEST.tsv and check every file's hash.

The files go to samples/heldout/files/<id>.pdf in the main checkout, which every worktree shares,
and a file is kept only when its SHA-256 is the manifest's. This script never opens a PDF: the
corpus is for the scorer alone (samples/heldout/README.md).

    .venv/bin/python scripts/fetch_heldout.py           # every missing file
    .venv/bin/python scripts/fetch_heldout.py --list    # each row and whether its file is here
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from checkout import main_checkout  # noqa: E402

MANIFEST = REPO / "samples/heldout/MANIFEST.tsv"
COLUMNS = ["id", "url", "sha256", "licence", "added"]
AGENT = "reflow-heldout-corpus/1 (test corpus fetch)"


@dataclass(frozen=True)
class Row:
    id: str
    url: str
    sha256: str
    licence: str
    added: str


def load(path: Path = MANIFEST) -> list[Row]:
    """The manifest's rows. Raises ValueError on a malformed row or a repeated id."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    if not lines or lines[0].split("\t") != COLUMNS:
        raise ValueError(f"{path}: the header must be {' '.join(COLUMNS)}")
    rows, seen = [], set()
    for n, line in enumerate(lines[1:], 2):
        cells = line.split("\t")
        if len(cells) != len(COLUMNS):
            raise ValueError(f"{path}: row {n} has {len(cells)} cells, not {len(COLUMNS)}")
        row = Row(*cells)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", row.id) or row.id in seen:
            raise ValueError(f"{path}: row {n}: bad or repeated id {row.id!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", row.sha256):
            raise ValueError(f"{path}: row {n}: sha256 is not 64 hex digits")
        if not row.url.startswith(("https://", "http://")):
            raise ValueError(f"{path}: row {n}: url must be http(s)")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.added) or not row.licence.strip():
            raise ValueError(f"{path}: row {n}: needs a licence and an added date")
        seen.add(row.id)
        rows.append(row)
    return rows


def files_dir() -> Path:
    return main_checkout(REPO) / "samples/heldout/files"


def fetch(row: Row, folder: Path) -> str:
    """Download one row's file into `folder`. Returns what happened, for the log."""
    dest = folder / f"{row.id}.pdf"
    if dest.exists():
        got = hashlib.sha256(dest.read_bytes()).hexdigest()
        return "here" if got == row.sha256 else (
            f"mismatch: {dest} has sha256 {got}, the manifest has {row.sha256}")
    # irs.gov is sent urllib's own default user agent.
    host = urllib.parse.urlsplit(row.url).hostname or ""
    req = urllib.request.Request(row.url, headers={} if host.endswith("irs.gov") else {"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != row.sha256:
        return f"refused: sha256 {got}, the manifest has {row.sha256}"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return "fetched"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="each row and whether its file is here")
    args = ap.parse_args(argv)
    rows, folder = load(), files_dir()
    if not rows:
        print("fetch_heldout: the manifest has no rows yet")
        return 0
    failed = 0
    for row in rows:
        if args.list:
            print(f"{row.id}\t{'here' if (folder / f'{row.id}.pdf').exists() else 'missing'}")
            continue
        try:
            outcome = fetch(row, folder)
        except OSError as e:
            outcome = f"failed: {e}"
        failed += not outcome.startswith(("here", "fetched"))
        print(f"{row.id}: {outcome}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
