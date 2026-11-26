"""Small helpers that several test files share."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pikepdf

from reflow import pipeline
from reflow.extract.pages import extract_pages
from reflow.model import Mark, Node, Role


REPO = Path(__file__).resolve().parents[1]
NOT_COMMITTED = "is not here: run scripts/fetch_samples.py"


def sample(name: str) -> Path:
    """A PDF of samples/, from this checkout or else the main one. resnet.pdf and arxiv.pdf are
    not committed: scripts/fetch_samples.py downloads them into the main checkout."""
    import checkout   # scripts/checkout.py, on the path from tests/conftest.py
    return checkout.sample(REPO, f"samples/{name}")


def needs(pdf: Path):
    """A skip mark for the tests that read a sample which is not committed."""
    import pytest
    return pytest.mark.skipif(not pdf.exists(), reason=f"samples/{pdf.name} {NOT_COMMITTED}")


class FakeHttp:
    """Stands in for reflow.analyze.vision.send: records the request and whether it could
    connect only to this machine (`loopback`), and returns or raises."""

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.requests, self.loopback = reply, error, [], []

    def __call__(self, request, timeout=None, loopback=False):
        self.requests.append(request)
        self.loopback.append(loopback)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.reply).encode())


def resolving(monkeypatch, table: dict[str, list[str]]) -> list[str]:
    """socket.getaddrinfo from `table` (a host's addresses), so no test needs DNS; a host not in
    it does not resolve. Returns the hosts looked up, in order."""
    import socket
    looked = []

    def getaddrinfo(host, port, *_, **__):
        looked.append(host)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0)) for ip in table[host]]

    monkeypatch.setattr("socket.getaddrinfo", getaddrinfo)
    return looked


def blank_pdf(path: Path, pages: int = 1) -> Path:
    """A PDF of `pages` blank pages at `path`."""
    with pikepdf.new() as pdf:
        for _ in range(pages):
            pdf.add_blank_page()
        pdf.save(path)
    return path


def digests(root: Path) -> dict[str, str]:
    """The sha256 of every file under `root`, by relative path. working.pdf is qpdf's rewrite of
    the input, with a fresh /ID each run: not our output."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and p.name != "working.pdf"}


def walk_json(node):
    """A published node and all its descendants, depth first."""
    yield node
    for child in node.get("children", []):
        yield from walk_json(child)


def ocr_available() -> bool:
    from reflow.ingest.ocr import make_runner
    return make_runner("auto") is not None


def no_build(monkeypatch):
    """Fail the test if the model is built again instead of read back."""
    def refuse(*_):
        raise AssertionError("the model was built again")
    monkeypatch.setattr(pipeline, "read_pages", refuse)


def para(text, box, page=1):
    return Node(Role.P, page=page, bbox=box, text=text)


def heading(text, font_size=14.0, page=1, role=Role.H2, **attrs):
    return Node(role, page=page, text=text, attrs={"font_size": font_size, **attrs})


def mark_at(text: str, word: str, kind: str, **kw) -> Mark:
    """A mark of `kind` over the first `word` in `text`."""
    at = text.index(word)
    return Mark(at, at + len(word), kind, word, **kw)


def line_texts(path) -> list[str]:
    """The text of each line of a one-page PDF."""
    [page] = extract_pages(path)
    return [line.text for line in page.lines]


def shifted_pdf(src: Path, dst: Path) -> Path:
    """A copy of `src` whose page-1 main font reads each letter as the next one ("Gpsn" for
    "Form"): the robustness harness's tounicode-shifted kind, pixel-identical to `src`. The
    text-trust witness must dispute it (reflow.ingest.text_trust)."""
    import random

    from robustness.kinds import KINDS
    [kind] = [k for k in KINDS if k.id == "tounicode-shifted"]
    kind.transform(src, dst, random.Random(308))
    return dst
