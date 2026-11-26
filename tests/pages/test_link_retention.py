"""Link retention: the converted p17 keeps its link annotations as links.

Publication 17 has 273 /URI and 2,685 /GoTo annotations. The test counts, per target, the
annotations `read_links` resolves and the `<a href>` the output carries for that target, and
requires the output to keep at least 90% of each kind. It needs the whole document: a quick
excerpt renumbers and drops pages, so it is skipped in quick mode.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from corpus import DOCUMENTS, REPO, Corpus, quick_mode
from reflow.extract.links import read_links

pytestmark = pytest.mark.pages

KEEP = 0.90
HREF = re.compile(r'<a [^>]*?href="([^"]*)"')


@pytest.fixture(scope="module")
def retention() -> dict[str, tuple[int, int]]:
    if quick_mode():
        pytest.skip("p17 is excerpted in quick mode; link retention is a whole-document measure")
    corpus = Corpus()
    corpus.prepare(["p17"])
    out = corpus.doc("p17").out_dir
    hrefs = Counter()
    for html in [out / "index.html", *sorted((out / "chunks").glob("*.html"))]:
        hrefs.update(HREF.findall(html.read_text(encoding="utf-8")))
    links = [link for page in read_links(REPO / DOCUMENTS["p17"]).values() for link in page]
    result = {}
    for kind in ("uri", "page"):
        wanted = Counter(re.sub(r"^.*#", "#", l.target) if kind == "page" else l.target
                         for l in links if l.kind == kind)
        have = Counter()
        for href, n in hrefs.items():
            if kind == "page" and re.search(r"#p\d+$", href):
                have[re.sub(r"^.*#", "#", href)] += n
            elif kind == "uri":
                have[href] += n
        result[kind] = (sum(min(n, have[t]) for t, n in wanted.items()), sum(wanted.values()))
    return result


@pytest.mark.parametrize("kind,label", [("uri", "URI links"), ("page", "internal links")])
def test_p17_keeps_its_links(retention: dict[str, tuple[int, int]], kind: str, label: str) -> None:
    kept, total = retention[kind]
    assert total, f"p17 has no {label} to keep"
    assert kept >= KEEP * total, f"p17 keeps {kept} of {total} {label} ({kept / total:.1%})"
