"""prefetch: the pages the evidence pass will read are parsed behind the page planning, in one request."""

from pathlib import Path
from types import SimpleNamespace

from reflow.analyze import prefetch
from reflow.extract import pagecontent, tags
from reflow.extract.pagecontent import PlumberPage
from reflow.run import RunState

BOX = (0.0, 0.0, 10.0, 10.0)


def fake_run(pages: int) -> SimpleNamespace:
    return SimpleNamespace(
        pdf=Path("doc.pdf"), working=Path("working.pdf"), state=RunState(),
        doc=SimpleNamespace(pages=[None] * pages),
        workers=SimpleNamespace(pool=object(), jobs=2),
        options=SimpleNamespace(stage_cache=None), ocr=SimpleNamespace(runner="", done=[]))


def prefetched(monkeypatch, *, pages, link_pages, tagged_pages):
    requests = []

    def read_contents(pdf, numbers, jobs, executor=None, words=False, weights=None):
        requests.append((list(numbers), words))
        return [(n, PlumberPage(0, {}, {})) for n in numbers]

    monkeypatch.setattr(pagecontent, "read_contents", read_contents)
    monkeypatch.setattr(prefetch, "walk_tags", lambda pdf: SimpleNamespace(pages=list(tagged_pages)))
    monkeypatch.setattr(prefetch, "scan_links",
                        lambda pdf, pages=None: {p: [(BOX, "https://example.com", "uri", None)]
                                                 for p in link_pages})
    monkeypatch.setattr(prefetch, "MAX_TAG_PAGES", pages)
    monkeypatch.setattr(tags, "MAX_TAG_PAGES", pages)
    run = fake_run(pages)
    prefetch.early(run)
    prefetch.start(run)
    prefetch.finish(run)
    return requests


def test_the_link_pages_and_the_tagged_pages_are_one_request(monkeypatch):
    requests = prefetched(monkeypatch, pages=9, link_pages=[2, 6], tagged_pages=[2, 3, 7])
    assert requests == [([2, 3, 6, 7], (2, 6))]   # words for the links' pages only


def test_pages_the_tag_tree_does_not_reach_are_not_parsed(monkeypatch):
    requests = prefetched(monkeypatch, pages=40, link_pages=[], tagged_pages=[5])
    assert requests == [([5], False)]


def test_a_document_with_nothing_to_read_makes_no_request(monkeypatch):
    assert prefetched(monkeypatch, pages=3, link_pages=[], tagged_pages=[]) == []
