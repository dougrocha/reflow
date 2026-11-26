"""The page-vision checks on real conversions: one test per wrong case, which the check
meant for it must reject, and one per heading and row header the specs require, which no check
that is on may reject. tests/pages/vision_gates.py prints the scores behind vision_gates.ON."""

from __future__ import annotations

import pytest
from corpus import Corpus
from vision_gates import (
    Converted,
    judge_columns,
    judge_heading,
    pages_needed,
    required_columns,
    required_headings,
    wrong_columns,
    wrong_headings,
)

from reflow.analyze import vision_gates as vg

pytestmark = pytest.mark.pages

WRONG = [h for h in wrong_headings() if h.gate]
REQUIRED = required_headings()


@pytest.fixture(scope="module")
def converted(request: pytest.FixtureRequest):
    wanted: set[str] = set()
    for item in request.session.items:
        if item.module is request.module and hasattr(item, "callspec"):
            wanted.add(item.callspec.params["case"].doc)
    needed = pages_needed()
    corpus = Corpus()
    corpus.prepare(sorted(wanted), pages={s: needed[s] for s in wanted})
    cache: dict[str, Converted] = {}

    def get(slug: str) -> Converted:
        if slug not in cache:
            cache[slug] = Converted(corpus.doc(slug))
        return cache[slug]
    return get


@pytest.mark.parametrize("case", [
    pytest.param(h, id=h.id, marks=[pytest.mark.xfail(strict=True, reason=h.xfail)] if h.xfail else [])
    for h in WRONG])
def test_a_wrong_heading_is_rejected_by_its_check(converted, case):
    judged = judge_heading(converted(case.doc), case)
    assert judged.found, f"{case.text!r} is not on page {case.page}"
    assert vg.Gate(case.gate) in judged.gates, judged.seen


@pytest.mark.parametrize("case", REQUIRED, ids=[h.id for h in REQUIRED])
def test_no_check_that_is_on_rejects_a_required_heading(converted, case):
    judged = judge_heading(converted(case.doc), case)
    if not judged.found:
        pytest.skip("not a block of its own here: test_pages checks the heading itself")
    assert not set(judged.gates) & vg.ON, judged.seen


@pytest.mark.parametrize("case", wrong_columns(), ids=[c.id for c in wrong_columns()])
def test_a_data_column_the_model_called_row_headers_is_rejected(converted, case):
    judged = [j for j in judge_columns(converted(case.doc), case) if j.found]
    if not judged:
        pytest.skip("no table on this page in reflow's conversion")
    assert all(vg.Gate.DATA_COLUMN in j.gates for j in judged), [j.seen for j in judged]


@pytest.mark.parametrize("case", required_columns(), ids=[c.id for c in required_columns()])
def test_required_row_headers_are_no_data_column(converted, case):
    judged = [j for j in judge_columns(converted(case.doc), case) if j.found]
    assert judged, "the spec's table is not on the page"
    assert not any(j.gates for j in judged), [j.seen for j in judged]
