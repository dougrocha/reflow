import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from reflow.pool import submit_runs


def test_results_come_back_in_the_order_of_the_calls_whatever_the_submission_order():
    started = []

    def run(n):
        started.append(n)
        return n * n

    with ThreadPoolExecutor(max_workers=1) as pool:
        got = list(submit_runs(pool, run, [(1,), (2,), (3,)], order=[2, 0, 1]))
    assert got == [1, 4, 9]
    assert started == [3, 1, 2]


def test_a_failing_run_cancels_the_runs_not_started():
    ran = []
    release = threading.Event()

    def run(n):
        ran.append(n)
        if n == 0:
            raise ValueError("boom")
        release.wait(5)
        return n

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError):
            list(submit_runs(pool, run, [(n,) for n in range(50)]))
        release.set()
    assert len(ran) < 50
