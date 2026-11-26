"""Fetch the sample PDFs that are not committed, and check every file's hash.

The two arXiv papers are used all over the tests and the README, but this repository does not
redistribute them. They go to samples/ in the main checkout, which every worktree reads, and a
file is kept only when its SHA-256 is the one below.

    .venv/bin/python scripts/fetch_samples.py           # every missing file
    .venv/bin/python scripts/fetch_samples.py --list    # each file and whether it is here
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from checkout import main_checkout  # noqa: E402

USER_AGENT = "reflow-samples/1 (test sample fetch)"


@dataclass(frozen=True)
class Sample:
    name: str      # the file name under samples/
    url: str       # a versioned URL, so the bytes do not change
    sha256: str


SAMPLES = [
    Sample("arxiv.pdf", "https://arxiv.org/pdf/1706.03762v7",
           "bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697"),
    Sample("resnet.pdf", "https://arxiv.org/pdf/1512.03385v1",
           "1e0651b6810ecba34a3dbc5b5b0209226f889004607c1f203540a48d64e5a93a"),
]


def samples_dir() -> Path:
    return main_checkout(REPO) / "samples"


def fetch(sample: Sample, folder: Path) -> str:
    """Download one sample into `folder`. Returns what happened, for the log."""
    dest = folder / sample.name
    if dest.exists():
        got = hashlib.sha256(dest.read_bytes()).hexdigest()
        return "here" if got == sample.sha256 else (
            f"mismatch: {dest} has sha256 {got}, expected {sample.sha256}")
    req = urllib.request.Request(sample.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sample.sha256:
        return f"refused: sha256 {got}, expected {sample.sha256}"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return "fetched"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="each file and whether it is here")
    args = ap.parse_args(argv)
    folder = samples_dir()
    failed = 0
    for sample in SAMPLES:
        if args.list:
            print(f"{sample.name}\t{'here' if (folder / sample.name).exists() else 'missing'}")
            continue
        try:
            outcome = fetch(sample, folder)
        except OSError as e:
            outcome = f"failed: {e}"
        failed += not outcome.startswith(("here", "fetched"))
        print(f"{sample.name}: {outcome}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
