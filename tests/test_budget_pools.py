"""Every pool of worker processes is metered by the machine's worker budget (reflow.budget): its
tasks hold slots only while they run. These tests cover each caller that makes a pool of its own:
`reflow convert`, `reflow rerender`, the source audit's tag-tree pool, the isolated documents of
convert_many (the page-check harness) and the robustness harness. The metered executor itself is
in test_budget.py."""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor

import pytest
from conftest import make_pdf
from test_budget import Tasks, until
from testkit import digests
from typer.testing import CliRunner

from reflow import budget
from reflow.cli import app
from reflow.convert import convert, convert_many
from reflow.run import workers_for


class Spy:
    """Wraps `Lease.meter` to count the executors it metered."""

    def __init__(self, monkeypatch):
        self.metered: list[Executor] = []
        original = budget.Lease.meter
        spy = self

        def meter(lease, executor):
            wrapped = original(lease, executor)
            spy.metered.append(wrapped)
            return wrapped

        monkeypatch.setattr(budget.Lease, "meter", meter)


# ---- reflow convert ---------------------------------------------------------------------------

def test_a_conversions_pool_shrinks_to_its_share_when_another_run_arrives(tmp_path, monkeypatch):
    """`convert --jobs 4` alone runs 4 tasks; a second run starts, the first's tasks end, and
    the pool is down to half while the newcomer has the other half."""
    monkeypatch.setattr("reflow.run.make_pool", lambda jobs: ThreadPoolExecutor(max_workers=jobs))
    first, second = Tasks(), Tasks()
    with budget.lease(tmp_path, 4) as a, workers_for(None, 4, a) as workers:
        done_a = [workers.pool.submit(first.run) for _ in range(10)]
        until(lambda: first.running == 4)   # alone: every slot
        assert a.held == 4
        with budget.lease(tmp_path, 4) as b:
            pool_b = b.meter(ThreadPoolExecutor(max_workers=4))
            done_b = [pool_b.submit(second.run) for _ in range(10)]
            first.swap()
            until(lambda: first.running == 2 and second.running == 2)
            assert (a.held, b.held) == (2, 2)
            first.release.set()
            second.release.set()
            for f in done_a + done_b:
                assert f.result(10) == "done"
            pool_b.shutdown()
    assert first.peak == 4 and a.held == 0


def test_a_conversions_pool_is_made_per_run_and_shut_down(tmp_path, monkeypatch):
    spy = Spy(monkeypatch)
    monkeypatch.setattr("reflow.run.make_pool", lambda jobs: ThreadPoolExecutor(max_workers=jobs))
    with budget.lease(tmp_path, 4) as lease:
        with workers_for(None, 3, lease) as workers:
            assert workers.pool is spy.metered[0] and workers.jobs == 3
            assert workers.pool.submit(abs, -1).result(10) == 1
        with pytest.raises(RuntimeError):   # shut down on exit
            workers.pool.submit(abs, 1)
        with workers_for(None, 1, lease) as serial:   # one job: no pool, no slots
            assert serial.pool is None
        assert lease.held == 0 and len(spy.metered) == 1


def test_a_conversion_with_a_lease_gives_the_same_output_as_serial(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "a.pdf", ["text", "text", "text", "empty"])
    convert(pdf, tmp_path / "serial", jobs=1, page_images=False)
    spy = Spy(monkeypatch)
    with budget.lease(tmp_path / "slots", 4) as other, budget.lease(tmp_path / "slots", 4) as lease:
        other.take(4)   # every slot is held elsewhere: the pool still runs, one task at a time
        convert(pdf, tmp_path / "pooled", jobs=3, lease=lease, chunk_pages=1, page_images=False)
        assert len(spy.metered) == 1 and lease.held == 0
    assert digests(tmp_path / "pooled") == digests(tmp_path / "serial")


def test_the_convert_command_hands_its_lease_to_the_conversion(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLOW_WORKER_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "4")
    seen = []

    def fake(pdf, output, **kwargs):
        seen.append({**kwargs, "held": kwargs["lease"].held})
        raise SystemExit(0)

    monkeypatch.setattr("reflow.cli.run_convert", fake)
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    for given in ("64", "2"):   # never more workers than the budget has slots
        CliRunner().invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "out"), "--jobs", given])
    assert [k["jobs"] for k in seen] == [4, 2]
    assert all(isinstance(k["lease"], budget.Lease) for k in seen)
    assert [k["held"] for k in seen] == [0, 0]   # nothing is taken up front


def test_the_convert_command_with_the_budget_off_asks_for_what_the_user_gave(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "0")
    seen = []

    def fake(pdf, output, **kwargs):
        seen.append(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr("reflow.cli.run_convert", fake)
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    CliRunner().invoke(app, ["convert", str(pdf), "-o", str(tmp_path / "out"), "--jobs", "64"])
    assert seen[0]["jobs"] == 64 and seen[0]["lease"].size == 0


# ---- reflow rerender --------------------------------------------------------------------------

def test_the_rerender_command_hands_its_lease_to_the_render(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLOW_WORKER_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "3")
    seen = []

    def fake(out, store, **kwargs):
        seen.append({**kwargs, "held": kwargs["lease"].held})
        raise SystemExit(0)

    monkeypatch.setattr("reflow.rerender.rerender", fake)
    store = tmp_path / "decisions.jsonl"
    store.write_text("")
    CliRunner().invoke(app, ["rerender", str(tmp_path), "--decisions", str(store), "--jobs", "9"])
    assert seen[0]["jobs"] == 3 and isinstance(seen[0]["lease"], budget.Lease)
    assert seen[0]["held"] == 0


def test_a_rerender_passes_its_lease_to_convert(tmp_path, monkeypatch):
    import reflow.rerender as rerender_module
    seen = []

    def fake(pdf, out, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(rerender_module, "convert", fake)
    monkeypatch.setattr(rerender_module, "_refusal", lambda *a: None)
    monkeypatch.setattr(rerender_module, "_options", lambda *a: {})
    out = tmp_path / "out"
    out.mkdir()
    (out / "document.json").write_text("{}")
    (out / "original.pdf").write_bytes(b"%PDF")
    monkeypatch.setattr(rerender_module.saved, "read_record", lambda _out: {
        "options": {}, "source": {"name": "a.pdf", "sha256": "x"}, "model": {"key": "k"}})
    store = tmp_path / "s.jsonl"
    store.write_text("")
    with budget.lease(tmp_path / "slots", 4) as lease, pytest.raises(RuntimeError, match="stop here"):
        rerender_module.rerender(out, store, jobs=3, lease=lease)
    assert seen[0]["lease"] is lease and seen[0]["jobs"] == 3


# ---- the source audit's tag-tree pool ---------------------------------------------------------

def test_the_tag_tree_pool_of_the_audit_is_metered_and_gives_the_same_report(tmp_path, monkeypatch):
    from conftest import make_tagged_pdf

    from reflow.checks.source import audit
    from reflow.extract import pagecontent, parallel
    path = make_tagged_pdf(tmp_path / "tagged.pdf")
    serial = audit(path, jobs=1).to_dict()
    monkeypatch.setenv("REFLOW_WORKER_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "4")
    monkeypatch.setattr(pagecontent, "PAGES_PER_WORKER", 1)   # a pool even for two pages
    spy = Spy(monkeypatch)
    sizes = []

    def make(jobs):
        sizes.append(jobs)
        return ThreadPoolExecutor(max_workers=jobs)   # no process spawned for the test

    monkeypatch.setattr(parallel, "make_pool", make)
    with budget.lease(tmp_path / "slots", 4) as other:
        other.take(4)   # the budget is all taken: the audit still runs, on one task at a time
        assert audit(path, jobs=8).to_dict() == serial
        assert other.held == 4
    assert sizes == [2] and len(spy.metered) == 1   # one worker a page, metered
    sizes.clear()
    with budget.lease(tmp_path / "one", 1):
        monkeypatch.setenv("REFLOW_WORKER_DIR", str(tmp_path / "one"))
        monkeypatch.setenv("REFLOW_WORKER_BUDGET", "1")
        assert audit(path, jobs=8).to_dict() == serial   # one slot: serial, no pool at all
    assert sizes == []


# ---- convert_many, isolated: the page-check harness -------------------------------------------

def test_an_isolated_big_document_holds_no_slot_for_its_process(tmp_path):
    """A short document is a worker and holds a slot while it converts. A big one asks for
    workers: it holds none in the parent, and joins the budget in its own process."""
    pdfs = [make_pdf(tmp_path / f"{n}.pdf", [f"{n} page", "text"]) for n in "ab"]
    held = {}
    with budget.lease(tmp_path / "slots", 4) as lease:
        for big_pages, name in ((1, "big"), (100, "short")):
            convert_many([(p, tmp_path / name / p.stem) for p in pdfs], isolate=True, jobs=2,
                         max_docs=1, big_pages=big_pages, lease=lease, page_images=False,
                         on_start=lambda i, name=name: held.setdefault(name, []).append(lease.held),
                         on_done=lambda i, r: None)
            assert lease.held == 0
    assert held == {"big": [0, 0], "short": [1, 1]}


def test_an_isolated_big_document_converts_on_a_busy_budget_with_the_same_output(tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", ["text", "text", "empty"])
    convert(pdf, tmp_path / "serial", jobs=1, page_images=False)
    with budget.lease(tmp_path / "slots", 2) as other, budget.lease(tmp_path / "slots", 2) as lease:
        other.take(2)
        results = convert_many([(pdf, tmp_path / "isolated")], isolate=True, jobs=2, big_pages=1,
                               lease=lease, page_images=False, chunk_pages=1)
        assert [r.ok for r in results] == [True] and lease.held == 0
    assert digests(tmp_path / "isolated") == digests(tmp_path / "serial")


def test_an_isolated_big_document_with_a_budget_of_one_converts_serially(tmp_path):
    """Two workers cannot run on one slot, so the document is a worker itself: it takes the slot."""
    pdf = make_pdf(tmp_path / "a.pdf", ["text", "text"])
    held = []
    with budget.lease(tmp_path / "slots", 1) as lease:
        results = convert_many([(pdf, tmp_path / "out")], isolate=True, jobs=8, big_pages=1,
                               lease=lease, page_images=False,
                               on_start=lambda i: held.append(lease.held))
        assert results[0].ok
    assert held == [1]


# ---- the robustness harness -------------------------------------------------------------------

def test_the_robustness_harness_runs_its_children_within_the_leases_share(tmp_path):
    from robustness.harness import Run
    tasks = Tasks()
    with budget.lease(tmp_path, 4) as lease, budget.lease(tmp_path, 4) as other:
        run = Run(tmp_path / "root", [], workers=6, lease=lease)
        run._convert = lambda pdf, out: (tasks.run(), (None, 0.0))[1]   # noqa: SLF001
        import threading
        out = []
        thread = threading.Thread(target=lambda: out.append(
            run._convert_all([(str(i), tmp_path, tmp_path, None) for i in range(8)])))   # noqa: SLF001
        thread.start()
        until(lambda: tasks.running == 2)   # two runs: half of four slots, not the six threads
        assert lease.held == 2 and other.held == 0
        tasks.release.set()
        thread.join(10)
        assert tasks.peak == 2 and len(out[0]) == 8 and lease.held == 0
