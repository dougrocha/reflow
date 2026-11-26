"""Running a list of calls on an executor, results in the order of the calls."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Executor, Future


def submit_runs[T](executor: Executor, fn: Callable[..., T], calls: Sequence[tuple],
                   *, order: Sequence[int] | None = None) -> Iterator[T]:
    """`fn(*args)` for each `args` in `calls`, submitted in `order` (the indexes of `calls`;
    the list's own order when None: start the heaviest run first) and yielded in the order of
    `calls`, whichever finished first. The runs not started are cancelled when a call fails or
    the caller stops reading."""
    futures: list[Future[T] | None] = [None] * len(calls)
    try:
        for k in order if order is not None else range(len(calls)):
            futures[k] = executor.submit(fn, *calls[k])
        for future in futures:
            yield future.result()
    finally:
        for future in futures:
            if future is not None:
                future.cancel()
