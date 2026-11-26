"""Cached stages publish what an uncached conversion publishes.

A warm page-check run is served whole from the conversion cache and never reads the stage cache,
so this converts on its own (tests/stage_equivalence.py): uncached, then cold and warm through a
private stage root, and compares what each published. It also plants a stale entry in each stage
(the check must catch it), and edits each stage's code (the stage must miss, and publish the same).

    pytest -o pythonpath=src -m pages --quick -k fw9 tests/pages/test_stage_equivalence.py
    # The failing run a planted stale entry gives (for a PR): the same check, with the plant
    REFLOW_STAGE_EQUIVALENCE_PLANT=extract pytest -o pythonpath=src -m pages -k "fw9 and publish_what" tests/pages/test_stage_equivalence.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import make_tagged_pdf
from reflow.ingest import witness
from stage_equivalence import OCR_NOISE, Code, Stage, Subject, ocr_available
from testkit import shifted_pdf

pytestmark = pytest.mark.pages

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
# Each document and the stages it must exercise; priming checks this (NOT_EXERCISED). fw9 is a
# tagged, linked form; scan-w4 is a scanned form, so it is OCR'd; fw9-shifted is fw9 with a
# shifted /ToUnicode on page 1 (tests/robustness), a suspect page the text witness reads.
DOCS = {
    "fw9": frozenset({Stage.INGEST, Stage.EXTRACT, Stage.PAGES, Stage.MODEL}),
    "scan-w4": frozenset({Stage.INGEST, Stage.OCR, Stage.EXTRACT, Stage.MODEL}),
    "fw9-shifted": frozenset({Stage.INGEST, Stage.WITNESS, Stage.EXTRACT, Stage.PAGES,
                              Stage.MODEL}),
}
CASES = [(slug, stage) for slug, stages in DOCS.items() for stage in Stage if stage in stages]
PLANT = os.environ.get("REFLOW_STAGE_EQUIVALENCE_PLANT")   # the failing run, on purpose


def _needs(slug: str) -> None:
    if Stage.OCR in DOCS[slug] and not ocr_available():
        pytest.skip("no OCR runner: an OCR'd document cannot be converted")
    if Stage.WITNESS in DOCS[slug] and (not witness.available() or witness.languages("") is None):
        pytest.skip("no Tesseract with English data: the text witness cannot read")


def _source(slug: str, work: Path) -> Path:
    if slug == "fw9-shifted":
        return shifted_pdf(SAMPLES / "fw9.pdf", work / "fw9-shifted.pdf")
    return SAMPLES / f"{slug}.pdf"


@pytest.fixture(scope="module")
def subjects(tmp_path_factory):
    made: dict[str, Subject] = {}

    def get(slug: str) -> Subject:
        if slug not in made:
            work = tmp_path_factory.mktemp(slug)
            made[slug] = Subject(_source(slug, work), work, expects=DOCS[slug])
        return made[slug]
    return get


@pytest.fixture(scope="module")
def donor(tmp_path_factory, subjects):
    """A stale entry for each stage: the entry another document's conversion wrote for it, so
    it is well formed and served under the key the current code computes."""
    made: dict[str, Subject] = {}

    def get(slug: str) -> Subject:
        if slug not in made:
            work = tmp_path_factory.mktemp(f"donor-{slug}")
            if slug == "fw9":     # a tagged PDF with a link: every stage fw9 caches
                pdf = make_tagged_pdf(work / "donor.pdf")
            elif slug == "fw9-shifted":   # a tagged PDF with a link and a suspect page
                pdf = shifted_pdf(make_tagged_pdf(work / "tagged.pdf"), work / "donor.pdf")
            else:                 # another three-page scanned form
                pdf = SAMPLES / "scan-i9.pdf"
            made[slug] = Subject(pdf, work)
        return made[slug]
    return get


@pytest.mark.parametrize("model", [True, False], ids=["saved-model", "reading-stages"])
@pytest.mark.parametrize("slug", list(DOCS))
def test_cached_stages_publish_what_an_uncached_conversion_publishes(slug, model, subjects, donor):
    _needs(slug)
    subject = subjects(slug)
    plant = None
    if PLANT:
        plant = (Stage(PLANT), donor(slug).primed.root)
    subject.check_equivalence(plant=plant, model=model).raise_if_failed()


@pytest.mark.parametrize("slug,stage", CASES, ids=[f"{s}-{t.value}" for s, t in CASES])
def test_a_planted_stale_entry_is_caught(slug, stage, subjects, donor):
    _needs(slug)
    given = donor(slug)
    if stage not in given.primed.stages:
        pytest.skip(f"the donor wrote no {stage.value} entry")
    if stage is Stage.MODEL:
        # A saved model proves itself (reflow.saved): another model's entry under this key is
        # refused, built again in its place, and the output is right.
        verdict = subjects(slug).check_equivalence(plant=(stage, given.primed.root))
        assert Code.OUTPUT_DIFFERS not in verdict.codes, verdict.render()
        assert "built: model" in verdict.render(), verdict.render()
        return
    verdict = subjects(slug).check_equivalence(plant=(stage, given.primed.root), model=False)
    assert Code.OUTPUT_DIFFERS in verdict.codes, verdict.render()


@pytest.mark.parametrize("slug,stage", CASES, ids=[f"{s}-{t.value}" for s, t in CASES])
def test_editing_a_stage_misses_it_and_publishes_the_same(slug, stage, subjects):
    _needs(slug)
    subjects(slug).check_invalidation(stage).raise_if_failed()


@pytest.mark.parametrize("slug", list(DOCS))
def test_a_render_only_edit_renders_the_saved_model_and_publishes_the_same(slug, subjects):
    _needs(slug)
    subjects(slug).check_render_edit().raise_if_failed()


def test_every_ocr_exclusion_is_needed(subjects):
    """The exclusions are measured, not guessed: on an OCR'd document each one is what makes
    two independent OCR runs agree."""
    _needs("scan-w4")
    applied = subjects("scan-w4").check_equivalence(model=False).applied
    assert set(applied) == set(OCR_NOISE)
