"""Fetch the real badly made PDFs of samples/robustness/MANIFEST.tsv that git does not hold.

A row whose file is not committed (it may not be redistributed, or it is large) keeps its hash and
its retrieval URL in the manifest. This script downloads each such file into the main checkout,
where every worktree reads it (checkout.sample), and keeps it only when its SHA-256 is the
manifest's.

    .venv/bin/python scripts/fetch_robustness_corpus.py           # missing files that have a recorded URL
    .venv/bin/python scripts/fetch_robustness_corpus.py ID...     # only these rows
    .venv/bin/python scripts/fetch_robustness_corpus.py --list    # each row and whether it is here

A URL of the form `https://.../NNNN.zip#member.pdf` names one member of a remote zip (the SafeDocs
corpus packs 1,000 PDFs into each 1 to 3 GB zip). The script reads the zip's directory and that one
member with HTTP range requests, so it never downloads the whole zip.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

from checkout import main_checkout, sample  # noqa: E402
from robustness.real import MANIFEST, Row, load  # noqa: E402

AGENT = "reflow-robustness-corpus/1 (test corpus fetch)"


class RangeFile(io.RawIOBase):
    """A read-only, seekable view of a remote file over HTTP range requests, for zipfile."""

    def __init__(self, url: str):
        self.url, self.pos = url, 0
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence] + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        end = self.size if n is None or n < 0 else min(self.size, self.pos + n)
        if end <= self.pos:
            return b""
        req = urllib.request.Request(self.url, headers={"User-Agent": AGENT,
                                                        "Range": f"bytes={self.pos}-{end - 1}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        self.pos += len(data)
        return data

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def download(url: str) -> bytes:
    if "#" in url:
        zip_url, member = url.split("#", 1)
        with zipfile.ZipFile(io.BufferedReader(RangeFile(zip_url), buffer_size=1 << 16)) as z:
            return z.read(member)
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def fetch(row: Row) -> str:
    """What happened to `row`'s file: "here", "fetched", or why it could not be."""
    if (have := sample(REPO, row.path)).exists():
        ok = hashlib.sha256(have.read_bytes()).hexdigest() == row.sha256
        return "here" if ok else f"here, but its SHA-256 is not the manifest's: {have}"
    if row.source_url == "-":
        return f"no source URL recorded: put the file at {row.path} yourself"
    try:
        data = download(row.source_url)
    except OSError as e:
        return f"failed: {e}"
    if (got := hashlib.sha256(data).hexdigest()) != row.sha256:
        return f"failed: the download's SHA-256 is {got}, not {row.sha256}"
    dst = main_checkout(REPO) / row.path
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    return "fetched"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="manifest ids (default: every row)")
    ap.add_argument("--list", action="store_true", help="list the rows and whether each file is here")
    args = ap.parse_args()
    rows = load(MANIFEST)
    unknown = set(args.ids) - {r.id for r in rows}
    if unknown:
        ap.error(f"not in the manifest: {', '.join(sorted(unknown))}")
    rows = [r for r in rows if not args.ids or r.id in args.ids]
    bad = 0
    for row in rows:
        if args.list:
            print(f"{row.id:32} {'here' if sample(REPO, row.path).exists() else 'missing':8} {row.path}")
            continue
        what = fetch(row)
        bad += what not in ("here", "fetched")
        print(f"{row.id:32} {what}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
