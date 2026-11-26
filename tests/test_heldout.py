"""scripts/fetch_heldout.py: the held-out corpus manifest and its hash-checked fetch."""
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("fetch_heldout", ROOT / "scripts/fetch_heldout.py")
heldout = importlib.util.module_from_spec(_spec)
sys.modules["fetch_heldout"] = heldout
_spec.loader.exec_module(heldout)

HEADER = "\t".join(heldout.COLUMNS)
SHA = "0" * 64


def manifest(tmp_path, *rows):
    path = tmp_path / "MANIFEST.tsv"
    path.write_text("\n".join(["# comment", HEADER, *rows]) + "\n")
    return path


def test_the_committed_manifest_loads():
    heldout.load()


def test_a_good_row_loads(tmp_path):
    path = manifest(tmp_path, f"w9-2024\thttps://x/w9.pdf\t{SHA}\tUS public domain\t2026-11-13")
    [row] = heldout.load(path)
    assert row == heldout.Row("w9-2024", "https://x/w9.pdf", SHA, "US public domain", "2026-11-13")


@pytest.mark.parametrize("row, message", [
    (f"Bad_Id\thttps://x/a.pdf\t{SHA}\tCC0\t2026-11-13", "bad or repeated id"),
    ("a\thttps://x/a.pdf\tabc\tCC0\t2026-11-13", "64 hex digits"),
    (f"a\tftp://x/a.pdf\t{SHA}\tCC0\t2026-11-13", "must be http"),
    (f"a\thttps://x/a.pdf\t{SHA}\t \t2026-11-13", "licence"),
    (f"a\thttps://x/a.pdf\t{SHA}\tCC0", "cells"),
])
def test_a_malformed_row_is_refused(tmp_path, row, message):
    with pytest.raises(ValueError, match=message):
        heldout.load(manifest(tmp_path, row))


def test_a_repeated_id_is_refused(tmp_path):
    row = f"a\thttps://x/a.pdf\t{SHA}\tCC0\t2026-11-13"
    with pytest.raises(ValueError, match="repeated"):
        heldout.load(manifest(tmp_path, row, row))


class _Response:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_keeps_only_a_file_with_the_manifests_hash(tmp_path, monkeypatch):
    data = b"%PDF-1.7 fake"
    monkeypatch.setattr(heldout.urllib.request, "urlopen", lambda req, timeout: _Response(data))
    good = heldout.Row("good", "https://x/g.pdf", hashlib.sha256(data).hexdigest(), "CC0",
                       "2026-11-13")
    bad = heldout.Row("bad", "https://x/b.pdf", SHA, "CC0", "2026-11-13")
    folder = tmp_path / "files"
    assert heldout.fetch(good, folder) == "fetched"
    assert (folder / "good.pdf").read_bytes() == data
    assert heldout.fetch(good, folder) == "here"
    assert heldout.fetch(bad, folder).startswith("refused")
    assert not (folder / "bad.pdf").exists()


def test_a_file_already_here_with_another_hash_is_a_failure(tmp_path):
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "a.pdf").write_bytes(b"other bytes")
    row = heldout.Row("a", "https://x/a.pdf", SHA, "CC0", "2026-11-13")
    assert heldout.fetch(row, folder).startswith("mismatch")
