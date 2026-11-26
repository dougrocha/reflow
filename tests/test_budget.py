import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import make_pdf

from reflow import budget
from reflow.convert import convert_many


def test_a_lone_lease_takes_every_slot_and_two_share_them(tmp_path):
    with budget.lease(tmp_path, 6) as a:
        assert a.take(10) == 6
        assert a.take(1) == 0
        a.give(6)
        with budget.lease(tmp_path, 6) as b:
            assert (a.share(), b.share()) == (3, 3)
            assert a.take(10) == 3 and b.take(10) == 3
            a.give(3)
            assert a.held == 0 and b.take(1) == 0   # b is at its share, though slots are free
        assert a.share() == 6 and a.take(10) == 6   # b closed: a may use them all again


def test_slots_never_exceed_the_budget_across_leases(tmp_path):
    with budget.lease(tmp_path, 5) as a, budget.lease(tmp_path, 5) as b, \
            budget.lease(tmp_path, 5) as c:
        got = [lease.take(5) for lease in (a, b, c)]
        assert got == [2, 2, 1]   # shares of 2; c gets what is left
        assert a.take(4) == 0 and a.held == 2   # nothing free: nothing more taken


def test_a_dead_process_frees_its_slots_and_its_membership(tmp_path):
    code = ("import sys, os; from pathlib import Path; from reflow import budget\n"
            "l = budget.Lease(Path(sys.argv[1]), 4); assert l.take(4) == 4; os._exit(0)")
    src = str(Path(budget.__file__).parents[1])   # this checkout's code, not the installed one
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True,
                   env={**os.environ, "PYTHONPATH": src})
    with budget.lease(tmp_path, 4) as lease:
        assert lease.members() == 1
        assert lease.take(4) == 4


def test_off_or_unusable_grants_what_is_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "0")
    with budget.lease(tmp_path) as lease:
        assert lease.take(99) == 99 and lease.cap(7) == 7 and lease.where() is None
    (tmp_path / "file").write_text("")
    with budget.lease(tmp_path / "file" / "sub", 4) as lease:
        assert lease.take(7) == 7 and lease.cap(7) == 7 and lease.where() is None


def test_isolated_documents_with_no_free_slot_run_one_at_a_time(tmp_path):
    pdfs = [make_pdf(tmp_path / f"{n}.pdf", [f"{n} page"]) for n in "abc"]
    events = []
    slots = tmp_path / "slots"
    with budget.lease(slots, 2) as one, budget.lease(slots, 2) as two, \
            budget.lease(slots, 2) as lease:
        assert one.take(2) == 1 and two.take(2) == 1   # every slot held by others
        results = convert_many([(p, tmp_path / p.stem) for p in pdfs], isolate=True, jobs=2,
                               max_docs=3, lease=lease, page_images=False,
                               on_start=lambda i: events.append(("start", i)),
                               on_done=lambda i, r: events.append(("done", i)))
        assert [r.ok for r in results] == [True] * 3
        assert lease.held == 0
    # The floor: one document runs without a slot, so they never overlap.
    assert events == [("start", 0), ("done", 0), ("start", 1), ("done", 1),
                      ("start", 2), ("done", 2)]


def test_isolated_documents_take_free_slots_and_give_them_back(tmp_path):
    pdfs = [make_pdf(tmp_path / f"{n}.pdf", [f"{n} page"]) for n in "abc"]
    events = []
    with budget.lease(tmp_path / "slots", 3) as lease:
        results = convert_many([(p, tmp_path / p.stem) for p in pdfs], isolate=True, jobs=2,
                               max_docs=3, lease=lease, page_images=False,
                               on_start=lambda i: events.append(("start", i)))
        assert [r.ok for r in results] == [True] * 3 and lease.held == 0
    assert events[:3] == [("start", 0), ("start", 1), ("start", 2)]


def test_a_broken_pool_fails_the_documents_not_yet_started(tmp_path, monkeypatch):
    from concurrent.futures import Future
    from concurrent.futures.process import BrokenProcessPool

    import reflow.convert as convert

    class Crashed:   # the first document's worker dies, so the pool takes no more work
        def __init__(self, **_):
            self.submitted = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def submit(self, *args):
            self.submitted += 1
            if self.submitted > 1:
                raise BrokenProcessPool("a worker died")
            future = Future()
            future.set_exception(BrokenProcessPool("a worker died"))
            return future

    monkeypatch.setattr(convert, "ProcessPoolExecutor", Crashed)
    pdfs = [make_pdf(tmp_path / f"{n}.pdf", [f"{n} page"]) for n in "abc"]
    with budget.lease(tmp_path / "slots", 4) as lease:
        results = convert_many([(p, tmp_path / p.stem) for p in pdfs], isolate=True, jobs=2,
                               max_docs=1, lease=lease, page_images=False)
        assert lease.held == 0
    assert [type(r.error) for r in results] == [BrokenProcessPool] * 3


# Metered pools: a task holds a slot only while it runs, so a late arrival gets its share.

class Tasks:
    """Tasks that run on threads until the test lets them end, and the number running."""

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.release = threading.Event()
        self.running = 0
        self.peak = 0

    def swap(self):
        """Let the tasks running now end; the ones that start later wait for a new event."""
        old, self.release = self.release, type(self.release)()
        old.set()

    def run(self):
        release = self.release
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        release.wait(10)
        with self.lock:
            self.running -= 1
        return "done"


def until(condition, timeout=10.0):
    import time
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def metered(lease, workers=8):
    from concurrent.futures import ThreadPoolExecutor
    return lease.meter(ThreadPoolExecutor(max_workers=workers))


def test_a_lone_pool_uses_every_slot_and_gives_them_back(tmp_path):
    tasks = Tasks()
    with budget.lease(tmp_path, 4) as lease:
        pool = metered(lease)
        futures = [pool.submit(tasks.run) for _ in range(10)]
        until(lambda: tasks.running == 4)
        assert lease.held == 4
        tasks.release.set()
        assert [f.result(10) for f in futures] == ["done"] * 10
        assert tasks.peak == 4
        pool.shutdown()
        assert lease.held == 0


def test_a_late_arrival_gets_its_share_from_a_pool_that_was_alone(tmp_path):
    first, second = Tasks(), Tasks()
    with budget.lease(tmp_path, 4) as a:
        pool_a = metered(a)
        done_a = [pool_a.submit(first.run) for _ in range(12)]
        until(lambda: first.running == 4)           # alone: every slot
        with budget.lease(tmp_path, 4) as b:
            pool_b = metered(b)
            done_b = [pool_b.submit(second.run) for _ in range(12)]
            # Let a's running tasks end, one batch of four. a's share is now 2, so b gets 2.
            first.swap()
            until(lambda: first.running == 2 and second.running == 2)
            assert (a.held, b.held) == (2, 2)
            first.release.set()
            second.release.set()
            for f in done_a + done_b:
                assert f.result(10) == "done"
            assert second.peak <= 4
            pool_b.shutdown()
        pool_a.shutdown()
        assert a.held == 0


def test_three_pools_share_the_budget_and_never_exceed_it(tmp_path):
    tasks = [Tasks() for _ in range(3)]
    with budget.lease(tmp_path, 6) as a, budget.lease(tmp_path, 6) as b, \
            budget.lease(tmp_path, 6) as c:
        pools = [metered(lease) for lease in (a, b, c)]
        futures = [[pool.submit(t.run) for _ in range(9)] for pool, t in zip(pools, tasks, strict=True)]
        until(lambda: [t.running for t in tasks] == [2, 2, 2])
        assert (a.held, b.held, c.held) == (2, 2, 2)
        assert sum(t.running for t in tasks) <= 6
        for t in tasks:
            t.release.set()
        for group in futures:
            assert [f.result(10) for f in group] == ["done"] * 9
        assert max(t.peak for t in tasks) <= 2
        for pool in pools:
            pool.shutdown()


def test_a_pool_with_no_free_slot_still_runs_one_task_at_a_time(tmp_path):
    tasks = Tasks()
    tasks.release.set()
    with budget.lease(tmp_path, 3) as other, budget.lease(tmp_path, 3) as lease:
        assert other.take(3) == 2 and lease.take(3) == 1   # shares of 2: one slot left...
        lease.give(1)
        other.take(1)                                       # ...and now every slot is held
        pool = metered(lease)
        assert [pool.submit(tasks.run).result(10) for _ in range(3)] == ["done"] * 3
        pool.shutdown()
        assert tasks.peak == 1 and lease.held == 0


def test_a_metered_pool_relays_results_errors_and_cancellation(tmp_path):
    import pytest
    tasks = Tasks()
    with budget.lease(tmp_path, 1) as lease:
        pool = metered(lease)
        blocker = pool.submit(tasks.run)
        until(lambda: tasks.running == 1)
        queued = pool.submit(tasks.run)
        assert queued.cancel()                  # not started: one slot, and the blocker has it
        tasks.release.set()
        assert blocker.result(10) == "done"
        with pytest.raises(ZeroDivisionError):
            pool.submit(lambda: 1 / 0).result(10)
        assert list(pool.map(abs, [-1, -2])) == [1, 2]
        until(lambda: lease.held == 0)
        pool.shutdown()
        with pytest.raises(RuntimeError):
            pool.submit(abs, 1)


def test_shutdown_can_cancel_what_has_not_started(tmp_path):
    from concurrent.futures import CancelledError

    import pytest
    tasks = Tasks()
    with budget.lease(tmp_path, 1) as lease:
        pool = metered(lease)
        running = pool.submit(tasks.run)
        until(lambda: tasks.running == 1)
        waiting = pool.submit(tasks.run)
        tasks.release.set()
        pool.shutdown(cancel_futures=True)
        assert running.result(10) == "done"
        with pytest.raises(CancelledError):
            waiting.result(10)
        assert lease.held == 0


def test_with_the_budget_off_the_executor_is_not_wrapped(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "0")
    with budget.lease(tmp_path) as lease, ThreadPoolExecutor(1) as inner:
        assert lease.size == 0 and lease.meter(inner) is inner


def test_convert_many_on_a_busy_budget_still_converts_every_document(tmp_path):
    pdfs = [make_pdf(tmp_path / f"{n}.pdf", [f"{n} page"]) for n in "ab"]
    with budget.lease(tmp_path / "slots", 4) as other, budget.lease(tmp_path / "slots", 4) as lease:
        other.take(4)
        results = convert_many([(p, tmp_path / p.stem) for p in pdfs], jobs=2, max_docs=2,
                               lease=lease, page_images=False)
        assert [r.ok for r in results] == [True] * 2 and lease.held == 0


# The GPU lease: slots that wait, a whole-card holder, and the wait reported per stage.

HOLD = ("import sys, time; from pathlib import Path; from reflow import budget\n"
        "budget.hold_gpu(folder=Path(sys.argv[1]), holders=int(sys.argv[2]), "
        "whole=sys.argv[3] == 'whole')\n"
        "print('held', flush=True); time.sleep(120)")


def holder(folder, holders, whole=False):
    """A process that takes the GPU lease and keeps it; returns once it holds, unless `whole`
    waits (then it returns at once, the process still waiting)."""
    src = str(Path(budget.__file__).parents[1])
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(folder), str(holders),
                             "whole" if whole else "light"], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": src})
    return proc


def wait_until_held(path, timeout=10.0):
    import fcntl
    import time
    end = time.monotonic() + timeout
    while True:
        with open(path, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        assert time.monotonic() < end, f"nobody took {path}"
        time.sleep(0.02)


def take_in_thread(folder, holders, whole=False):
    import threading
    got = []
    thread = threading.Thread(target=lambda: got.append(
        budget.hold_gpu(folder=folder, holders=holders, whole=whole)), daemon=True)
    thread.start()
    return thread, got


def test_a_killed_gpu_holder_frees_the_lease(tmp_path):
    proc = holder(tmp_path, 1)
    try:
        assert proc.stdout.readline().strip() == "held"
        thread, got = take_in_thread(tmp_path, 1)
        thread.join(0.5)
        assert thread.is_alive() and not got   # one holder at a time: it waits
        proc.kill()                            # SIGKILL: no cleanup runs in the holder
        proc.wait()
        thread.join(5)
        assert got and got[0].waited >= 0.5
        got[0].release()
    finally:
        proc.kill()


def test_a_killed_whole_holder_waiting_at_the_gate_frees_it(tmp_path):
    first = budget.hold_gpu(folder=tmp_path, holders=2)   # one of two slots
    proc = holder(tmp_path, 2, whole=True)                # waits for both, holding the gate
    try:
        wait_until_held(tmp_path / "gate")
        thread, got = take_in_thread(tmp_path, 2)
        thread.join(0.5)
        assert thread.is_alive()   # a slot is free, but the whole holder waits first
        proc.kill()
        proc.wait()
        thread.join(5)
        assert got
        got[0].release()
    finally:
        proc.kill()
        first.release()


def test_a_waiting_whole_holder_goes_before_later_light_ones(tmp_path):
    light = budget.hold_gpu(folder=tmp_path, holders=2)
    whole_thread, whole = take_in_thread(tmp_path, 2, whole=True)
    whole_thread.join(0.3)
    later_thread, later = take_in_thread(tmp_path, 2)
    later_thread.join(0.3)
    assert not whole and not later   # the later one waits though a slot is free
    light.release()
    whole_thread.join(5)
    assert whole and not later
    whole[0].release()
    later_thread.join(5)
    assert later
    later[0].release()


def test_two_light_holders_share_two_slots_and_a_third_waits(tmp_path):
    a = budget.hold_gpu(folder=tmp_path, holders=2)
    b = budget.hold_gpu(folder=tmp_path, holders=2)
    thread, got = take_in_thread(tmp_path, 2)
    thread.join(0.3)
    assert not got
    b.release()
    b.release()   # twice is harmless
    thread.join(5)
    assert got
    a.release()
    got[0].release()


def test_a_nested_take_in_one_thread_does_not_wait_for_itself(tmp_path):
    with budget.gpu(folder=tmp_path, holders=1), budget.gpu(folder=tmp_path, holders=1):
        pass
    with budget.gpu("cpu", folder=tmp_path, holders=1):   # a model on the CPU takes none
        assert budget.hold_gpu(folder=tmp_path, holders=1, whole=True).waited < 1


def test_off_holds_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLOW_GPU_HOLDERS", "0")
    a, b = budget.hold_gpu(folder=tmp_path), budget.hold_gpu(folder=tmp_path, whole=True)
    assert (a.waited, b.waited) == (0.0, 0.0)


def test_a_step_reports_the_time_it_waited_for_the_gpu(tmp_path):
    from reflow.ingest.ocr import OcrResult
    from reflow.model import Document, Node, Role
    from reflow.run import Options, Run, Workers
    pdf = tmp_path / "x.pdf"
    run = Run(pdf=pdf, stage=tmp_path, options=Options(), workers=Workers.serial(),
              progress=lambda *_: None, doc=Document(root=Node(Role.DOC)), working=pdf,
              ocr=OcrResult())
    blocker = budget.hold_gpu(folder=tmp_path, holders=1)
    import threading
    threading.Timer(0.4, blocker.release).start()
    with run.step("outer"), run.step("docling"):
        with budget.gpu(folder=tmp_path, holders=1):
            pass
    with run.step("review"):
        pass
    assert run.gpu_wait["docling"] >= 0.3 and run.gpu_wait["outer"] == run.gpu_wait["docling"]
    assert run.seconds["docling"] >= run.gpu_wait["docling"] and "review" not in run.gpu_wait
    assert budget.GPU_WAIT.get() is None   # each step put the callback back


def test_alt_text_threads_report_their_wait_to_the_step(tmp_path):
    from reflow.analyze.enrich import AltResult, enrich_figures
    from reflow.model import Document, Node, PageInfo, Role

    class Waits:   # a provider whose every call waited 0.25 s for the lease
        name = cache_id = "waits"
        confidence, workers = 0.8, 2

        def describe(self, image, media_type, ctx):
            budget.GPU_WAIT.get()(0.25)
            return AltResult("photo", False, "A photo", "", False)

    figures = [Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10), attrs={"needs": "alt"})
               for _ in range(3)]
    doc = Document(root=Node(Role.DOC, children=figures), pages=[PageInfo(1, 612, 792)])
    doc.ensure_unique_ids()
    images = {}
    for i, f in enumerate(figures):
        images[f.id] = tmp_path / f"f{i}.webp"
        images[f.id].write_bytes(f"image {i}".encode())
    waited = []
    token = budget.GPU_WAIT.set(waited.append)
    try:
        assert enrich_figures(doc, images, Waits())["described"] == 3
    finally:
        budget.GPU_WAIT.reset(token)
    assert waited == [0.25] * 3


def test_the_last_local_model_to_close_gives_the_whole_lease_back(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")

    class Loaded:
        def to(self, device):
            return self

    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(transformers.AutoProcessor, "from_pretrained", lambda *a, **k: Loaded())
    monkeypatch.setattr(transformers.AutoModelForImageTextToText, "from_pretrained",
                        lambda *a, **k: Loaded())
    monkeypatch.setattr("reflow.analyze.local_model.unload_ollama", lambda: None)
    monkeypatch.setenv("REFLOW_WORKER_DIR", str(tmp_path))
    monkeypatch.setenv("REFLOW_GPU_HOLDERS", "2")   # the suite turns the lease off
    from reflow.analyze.local_model import LocalModel
    first, second = LocalModel(device="cuda"), LocalModel(device="cuda")
    first.load()
    second.load()
    thread, got = take_in_thread(budget.root() / "gpu", 2)
    thread.join(0.3)
    assert not got
    first.close()
    thread.join(0.3)
    assert not got   # a model is still loaded
    second.close()
    second.close()   # idempotent
    thread.join(5)
    assert got
    got[0].release()
