"""The defect-injection suite: each defect of tests/robustness/defects.py injected into a correct
conversion of each document, rendered again, and scored by which page checks and which review
items catch it (tests/robustness/inject.py).

    .venv/bin/python -m pytest -o pythonpath=src -q -m pages --robustness \\
        tests/robustness/test_defects.py [--robustness-out DIR] [--robustness-doc]

Collected only with --robustness, beside the damage suite. A cell fails pytest when its
mutation is invalid (a harness bug), when a control does not render back to its conversion or
fails a spec assertion with no defect (a false alarm), when recall catches swapped paragraphs (it
is order-blind, so that would be a harness bug), or when an enforced cell is missed. The session also checks that the cached conversions it copied are
untouched, and that a second run scores every cell the same. Every other miss is a scorecard
entry: the session writes defects.md and defects.json to --robustness-out, or to its temporary
folder, and with --robustness-doc into docs/robustness-scorecard.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from robustness.harness import REPO
from robustness.inject import CONTROL, Run, cells, update_doc
from robustness.oracle import Outcome

pytestmark = pytest.mark.pages

CELLS = cells()


@pytest.fixture(scope="session")
def injected(request, tmp_path_factory):
    wanted = [item.callspec.params["defect_cell"] for item in request.session.items
              if getattr(item, "callspec", None) and "defect_cell" in item.callspec.params]
    root = tmp_path_factory.mktemp("defects")
    held = Run(root, wanted)
    held.ensure()
    yield held
    out = request.config.getoption("--robustness-out")
    folder = Path(out) if out else root / "report"
    held.write(folder)
    if request.config.getoption("--robustness-doc"):
        update_doc(REPO / "docs/robustness-scorecard.md",
                   json.loads((folder / "defects.json").read_text(encoding="utf-8")))
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"defect scorecard: {folder / 'defects.md'}")


@pytest.mark.parametrize("defect_cell", CELLS, ids=[f"{d}-{k}" for d, k in CELLS])
def test_defect(defect_cell, injected):
    c = injected.verdict(*defect_cell)
    v = c.verdict
    if v.outcome is Outcome.UNSUPPORTED:
        pytest.skip(f"unsupported: {v.detail}")
    if v.outcome is Outcome.INVALID:
        pytest.fail(f"invalid: {v.detail}")
    if c.defect == CONTROL:
        assert v.outcome is Outcome.PRESERVED, f"{v.outcome.value}: {v.detail}"
        return
    if c.defect == "swap-paragraphs":
        recall = [n for n in c.checks if n.startswith("min_recall#")]
        assert not recall, f"recall is order-blind, yet {recall} caught a swap: {c.why}"
    if c.enforced:
        assert v.met, f"enforced cell missed: {v.outcome.value}: {v.detail}"


def test_originals_untouched(injected):
    """The cached conversions the cells were copied from are as they were."""
    injected.ensure()
    assert not injected.touched()


def test_cells_reproduce(injected, tmp_path):
    """A second run, in a folder of its own, scores every cell the same, from the same model."""
    again = Run(tmp_path, list(injected.cells))
    again.ensure()
    key = lambda c: (c["outcome"], c["page"], c["note"], c["checks"], c["review"],  # noqa: E731
                     c["document_sha256"])
    first = {(c["doc"], c["defect"]): key(c) for c in injected.record()["cells"]}
    second = {(c["doc"], c["defect"]): key(c) for c in again.record()["cells"]}
    assert second == first
