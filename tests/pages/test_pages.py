"""Page-check regression tests: run with `uv run pytest -m pages`.

Each spec in pages/*.json becomes one test per assertion, so `-k fw9-1` runs one page and
`-k fw9` converts only fw9.pdf. Assertions marked "xfail" are strict: when a fix makes one
pass, pytest reports XPASS and the spec must be updated (see README.md).
"""

from __future__ import annotations

import pytest

from checks import CheckFailed
from corpus import Corpus
from specs import load_specs, run_assertion

pytestmark = pytest.mark.pages

SPECS = {s.id: s for s in load_specs()}


def _params():
    for spec in SPECS.values():
        for a in spec.assertions:
            marks = []
            if a.xfail:
                # raises= keeps a conversion error or a KeyError a real failure.
                marks.append(pytest.mark.xfail(strict=True, raises=CheckFailed,
                                               reason=a.xfail))
            yield pytest.param(spec.id, a.index, marks=marks,
                               id=f"{spec.id}-{a.index}-{a.check}")


@pytest.fixture(scope="session")
def corpus(request: pytest.FixtureRequest) -> Corpus:
    c = Corpus()
    # Convert everything the selected tests need up front, two documents at a time on one
    # shared pool, instead of one by one on first use (`-k fw9` converts only fw9).
    # In quick mode only the tested pages (and their neighbours) of long documents are converted.
    needed: dict[str, set[int]] = {}
    for item in request.session.items:
        if getattr(item, "originalname", None) == "test_page" and hasattr(item, "callspec"):
            spec = SPECS[item.callspec.params["spec_id"]]
            needed.setdefault(spec.doc, set()).add(spec.page)
    c.prepare(needed, pages=needed)
    return c


@pytest.mark.parametrize("spec_id,index", list(_params()))
def test_page(corpus: Corpus, spec_id: str, index: int) -> None:
    spec = SPECS[spec_id]
    a = spec.assertions[index]
    doc = corpus.doc(spec.doc)
    try:
        run_assertion(spec, a, doc)
    except CheckFailed as e:
        raise CheckFailed(f"{spec_id} {a.check}: {e}" + (f" [{a.why}]" if a.why else "")) from None
