"""Per-page work on the worker pool gives the output the serial run gives (analyze.pageplan,
the pooled ingest, figure crops and early page images)."""

import pickle

import pytest

from reflow.analyze import pageplan
from reflow.analyze.blocks import Block
from reflow.convert import convert
from reflow.extract.lines import Line
from reflow.ingest import pages as ingest_pages
from testkit import digests, needs, sample


@pytest.mark.parametrize("name", ["fw9", pytest.param("resnet", marks=needs(sample("resnet.pdf")))])
def test_pooled_page_work_matches_serial(tmp_path, monkeypatch, name):
    """Even a short document takes the pooled paths when the thresholds are lowered."""
    monkeypatch.setattr(pageplan, "MIN_PAGES", 1)
    monkeypatch.setattr(ingest_pages, "PARALLEL_MIN_PAGES", 1)
    pdf = sample(f"{name}.pdf")
    convert(pdf, tmp_path / "one", jobs=1)
    convert(pdf, tmp_path / "two", jobs=2)
    one, two = digests(tmp_path / "one"), digests(tmp_path / "two")
    assert one.keys() == two.keys()
    assert {k for k in one if one[k] != two[k]} == set()


def _line(text: str, top: float) -> Line:
    return Line(1, (10, top, 90, top + 12), text, 12.0, False, False, "Helvetica")


def test_a_block_changed_after_it_was_read_pickles_with_its_current_text():
    lines = [_line("alpha beta", 10), _line("gamma", 24)]
    block = Block(lines=lines)
    assert pickle.loads(pickle.dumps(block)).text == block.text == "alpha beta gamma"
    block.lines.append(lines[0])
    assert pickle.loads(pickle.dumps(block)).text == block.text == "alpha beta alpha beta gamma"
