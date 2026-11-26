"""Docling's real output against the heuristic engine's geometry and order.

The unit tests of the Docling order comparison use made-up nodes. These run Docling itself
(hybrid.docling_nodes, the call analyze_hybrid makes) on one page of an untagged sample and
check what the mocks cannot: Docling's boxes share our coordinate units and top-left origin,
its nodes carry the source page number, and on a simple page its order is ours.

Skipped when Docling or its models are not available. Run with `-m pages`.
"""

from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from reflow.analyze import order_evidence as oe
from reflow.model import Source

pytestmark = pytest.mark.pages

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
MATCHED_SHARE = 0.8   # share of heuristic blocks that land in a Docling block


@pytest.fixture(scope="module")
def converter():
    pytest.importorskip("docling")
    from reflow.analyze.docling_layout import make_converter
    try:
        return make_converter(device="cpu")
    except Exception as error:  # models not downloaded, no network
        pytest.skip(f"Docling unavailable: {error}")


def _page_blocks(converter, sample: str, page: int):
    from reflow.analyze.hybrid import docling_nodes
    from reflow.analyze.layout import analyze
    from reflow.extract.parallel import extract_document

    pdf = SAMPLES / sample
    if not pdf.exists():
        pytest.skip(f"{sample} missing")
    with pikepdf.open(pdf) as src:
        count = len(src.pages)
    pages, tables = extract_document(pdf, count)
    ours = oe.node_blocks(n for n in analyze(pages, tables) if n.page == page).get(page, [])
    nodes = docling_nodes(pdf, [page], converter)
    theirs = oe.node_blocks(nodes).get(page, [])
    return nodes, ours, theirs


def test_docling_geometry_and_pages_line_up(converter):
    """resnet p1: Docling's nodes carry page 1, and nearly every heuristic block sits in a
    Docling block, which only holds when units and origin agree."""
    nodes, ours, theirs = _page_blocks(converter, "resnet.pdf", 1)
    assert nodes and {part.page for node in nodes for part in node.walk()} == {1}
    assert ours and theirs
    verdict = oe.compare_page(1, Source.DOCLING, ours, theirs)
    assert verdict is not None, "too few matched blocks to compare"
    assert verdict.matched >= MATCHED_SHARE * len(ours), f"{verdict.matched} of {len(ours)} blocks matched"


def test_docling_order_agrees_on_a_single_column_page(converter):
    """arxiv p2 is one column: Docling's reading order is the heuristic order."""
    _, ours, theirs = _page_blocks(converter, "arxiv.pdf", 2)
    verdict = oe.compare_page(2, Source.DOCLING, ours, theirs)
    assert verdict is not None, "too few matched blocks to compare"
    assert verdict.matched >= oe.MIN_MATCHED and verdict.blocks == len(ours)
    assert not verdict.disagrees, verdict.detail()
