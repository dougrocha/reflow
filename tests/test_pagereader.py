"""PageReader.get: a read that raises leaves the pages unread, so the next get tries again."""

from pathlib import Path

import pytest

from reflow.extract import pagecontent
from reflow.extract.pagecontent import PlumberPage, PageReader


def test_a_failed_read_is_raised_again_by_the_next_get(monkeypatch):
    calls = []

    def read_contents(pdf, numbers, jobs, executor=None, words=False, weights=None):
        calls.append(list(numbers))
        if len(calls) == 1:
            raise RuntimeError("pdfplumber could not parse page 2")
        return [(n, PlumberPage(0, {}, {})) for n in numbers]

    monkeypatch.setattr(pagecontent, "read_contents", read_contents)
    reader = PageReader(Path("doc.pdf"), jobs=1)
    with pytest.raises(RuntimeError):
        reader.get([1, 2])
    assert sorted(reader.get([1, 2])) == [1, 2]
    assert calls == [[1, 2], [1, 2]]
    reader.get([1, 2])
    assert len(calls) == 2   # read once and kept


def test_a_page_past_the_end_is_not_read_again(monkeypatch):
    calls = []

    def read_contents(pdf, numbers, jobs, executor=None, words=False, weights=None):
        calls.append(list(numbers))
        return [(n, PlumberPage(0, {}, {})) for n in numbers if n <= 1]

    monkeypatch.setattr(pagecontent, "read_contents", read_contents)
    reader = PageReader(Path("doc.pdf"), jobs=1)
    assert sorted(reader.get([1, 2])) == [1]
    assert sorted(reader.get([1, 2])) == [1]
    assert calls == [[1, 2]]


def _recording(monkeypatch):
    calls = []

    def read_contents(pdf, numbers, jobs, executor=None, words=False, weights=None):
        calls.append((list(numbers), words))
        return [(n, PlumberPage(0, {}, {}, [] if words is True or (words is not False and n in words)
                                else None)) for n in numbers]

    monkeypatch.setattr(pagecontent, "read_contents", read_contents)
    return calls


def test_one_get_reads_the_words_of_some_pages_only(monkeypatch):
    calls = _recording(monkeypatch)
    reader = PageReader(Path("doc.pdf"), jobs=1)
    got = reader.get([1, 2, 3, 4], words=[3, 1])
    assert calls == [([1, 2, 3, 4], (1, 3))]
    assert {n: c.words is not None for n, c in got.items()} == {1: True, 2: False, 3: True, 4: False}
    reader.get([1, 2, 3, 4])                  # asked without words: read already
    reader.get([1, 3], words=True)            # their words are kept
    assert len(calls) == 1


def test_a_page_read_without_its_words_is_read_again_when_they_are_asked_for(monkeypatch):
    calls = _recording(monkeypatch)
    reader = PageReader(Path("doc.pdf"), jobs=1)
    reader.get([1, 2, 3, 4], words=[1])
    reader.get([1, 2, 3, 4], words=[1, 2, 3])
    assert calls == [([1, 2, 3, 4], (1,)), ([2, 3], True)]
    assert reader.get([2, 3], words=True)[2].words is not None


def test_a_request_for_every_page_or_none_passes_a_flag(monkeypatch):
    calls = _recording(monkeypatch)
    reader = PageReader(Path("doc.pdf"), jobs=1)
    reader.get([1, 2], words=True)
    reader.get([3, 4], words=[])
    reader.get([5, 6], words=[5, 6, 9])
    assert calls == [([1, 2], True), ([3, 4], False), ([5, 6], True)]
