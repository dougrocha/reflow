"""The robustness suite: each base document damaged by each kind (tests/robustness/kinds.py),
converted whole, and scored against its clean conversion (tests/robustness/harness.py).

    .venv/bin/python -m pytest -o pythonpath=src -q -m pages --robustness tests/robustness \\
        [--robustness-out DIR] [--robustness-doc]

It is collected only with --robustness, so `-m pages` and the unit suite never see it. A cell
fails pytest when its mutation is invalid (a harness bug), when a control is not the same as its
base or raises a fact some kind watches for (a false alarm), or when an enforced cell misses its
expectation. A cell the kind can damage that is not enforced must say why (the kind's
`unenforced`), or the session fails. The real files of
samples/robustness/MANIFEST.tsv (real.py) run too: one that is not here is skipped, and one whose
hash is not the manifest's fails. Every other miss is a scorecard entry:
the session writes scorecard.md and run.json (input hashes, seeds and tool versions) to
--robustness-out, or to its temporary folder, and with --robustness-doc into
docs/robustness-scorecard.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from robustness.harness import CONTROL, REPO, Run, cells, unexplained, update_doc
from robustness.oracle import Outcome
from robustness.real import load

pytestmark = pytest.mark.pages

CELLS = cells()
REAL = load()


@pytest.fixture(scope="session")
def run(request, tmp_path_factory):
    from reflow import budget

    params = [item.callspec.params for item in request.session.items
              if getattr(item, "callspec", None)]
    wanted = [p["cell"] for p in params if "cell" in p]
    rows = [p["row"] for p in params if "row" in p]
    root = tmp_path_factory.mktemp("robustness")
    jobs = len(wanted) + len(rows)
    with budget.lease() as lease:
        held = Run(root, wanted, min(jobs, budget.size() or os.cpu_count() or 1), rows, lease)
        held.ensure()
    yield held
    out = request.config.getoption("--robustness-out")
    folder = Path(out) if out else root / "report"
    held.write(folder)
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if request.config.getoption("--robustness-doc"):
        import json
        update_doc(REPO / "docs/robustness-scorecard.md",
                   json.loads((folder / "run.json").read_text(encoding="utf-8")))
    if reporter is not None:
        reporter.write_line(f"robustness scorecard: {folder / 'scorecard.md'}")


@pytest.mark.parametrize("cell", CELLS, ids=[f"{b}-{k}" for b, k in CELLS])
def test_cell(cell, run):
    c = run.verdict(*cell)
    v = c.verdict
    if v.outcome is Outcome.UNSUPPORTED:
        pytest.skip(f"unsupported: {v.detail}")
    if v.outcome is Outcome.INVALID:
        pytest.fail(f"invalid: {v.detail}")
    if c.kind == CONTROL:
        assert v.outcome is Outcome.PRESERVED, f"{v.outcome.value}: {v.detail}"
        return
    if c.enforced:
        assert v.met, f"enforced cell missed: {v.outcome.value}: {v.detail}"


def test_every_cell_that_is_not_enforced_says_why(run):
    """The scorecard prints each open cell's reason; a kind that gains a base it can damage, or
    loses an enforced bit, has to say why that cell is not held."""
    assert unexplained(run.record()) == []


@pytest.mark.parametrize("row", REAL, ids=[r.id for r in REAL])
def test_real(row, run):
    r = run.real_result(row.id)
    if r.outcome == "absent" and "SHA-256" in r.detail:
        pytest.fail(r.detail)
    if r.outcome == "absent":
        pytest.skip(f"{r.detail}: scripts/fetch_robustness_corpus.py {row.id}")
