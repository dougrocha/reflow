"""A budget of worker processes for the whole machine, shared by every conversion that runs on it
at once: the CLI (and so build-docs, which runs `reflow convert-many`) and the page-check harness,
in any worktree. Without it, each run sizes its pool by the CPU count, so N runs start N times
the machine.

The budget is `size()` slots, each a file `slot-<i>` under `root()`, held by an exclusive flock.
A worker process needs a slot; the process that asks (the CLI, a pytest process) is not counted.
A lock goes away with its process, so a run that is killed frees its slots. Each `lease` is a
member while it is open (a locked file under `members/`), and takes at most its fair share,
the size divided by the live members, rounded up: a lone run may use every slot, and two get
half each. Taking a slot never waits; a lease that gets none runs serially, so no run waits for
another's whole run, and the machine runs at most `size()` plus one worker per run.

A pool does not hold its slots for the whole run. `Lease.meter` wraps an executor so that a task
holds a slot only while it runs: the tasks queue in the wrapper and start as the lease's share
allows. A lone run still uses every slot while it has tasks to run, but it gives them back
between tasks and stages, and when a second run arrives its running tasks end, the share falls
to half, and the newcomer gets the other half. (A pool that took its slots up front and held them
to the end left a late arrival with none, so it ran serially.) The floor stays: a pool with no task
running and no free slot starts one task without a slot. Every pool of worker processes is metered
this way: `reflow convert`, `rerender` and `audit` (through `convert`'s `lease`, and the tag-tree
pool), `convert-many` (one shared pool, or with `isolate` a process per document, a big one a
member of its own) and the robustness harness. The one fleet that is not is pytest-xdist's: it
starts its workers up front, so tests/conftest.py takes slots for the session with `take`.

REFLOW_WORKER_BUDGET sets the size (0 turns the budget off); REFLOW_WORKER_DIR moves the slots.

The GPU lease (`gpu`, `hold_gpu`) is the same idea for the one GPU: `gpu_holders()` slot files
under `root()/gpu`, each an exclusive flock, so a holder that dies frees its slot. Unlike a
worker slot, it waits: a stage that loads a model on the GPU cannot run serially instead. A
model that fills the card takes every slot (`whole`). Each holder takes a `gate` lock first and
keeps it until it has its slots, so a waiting whole holder is not starved by a stream of small
ones. The time spent waiting goes to the callback in `GPU_WAIT` (reflow.run.Run.step sets one per
stage). REFLOW_GPU_HOLDERS sets the slot count (0 turns the lease off).
"""

from __future__ import annotations

import fcntl
import os
import random
import secrets
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError, Executor, Future
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


def cpu_slots() -> int:
    """CPU count minus 2, at least 1: the default budget, and the workers a command starts."""
    return max(1, (os.cpu_count() or 1) - 2)


def size() -> int:
    """The number of slots: REFLOW_WORKER_BUDGET, else `cpu_slots`. 0 is off."""
    if (value := os.environ.get("REFLOW_WORKER_BUDGET", "").strip()):
        return max(0, int(value))
    return cpu_slots()


def root() -> Path:
    """REFLOW_WORKER_DIR, else a folder of this user's runtime dir (or of the temp dir)."""
    if (override := os.environ.get("REFLOW_WORKER_DIR")):
        return Path(override)
    if (runtime := os.environ.get("XDG_RUNTIME_DIR")) and Path(runtime).is_dir():
        return Path(runtime) / "reflow-workers"
    return Path(tempfile.gettempdir()) / f"reflow-workers-{os.getuid()}"


def _try_flock(path: Path, *, create: bool = True) -> int | None:
    """An fd holding an exclusive flock on `path`, or None if it is held. Raises
    FileNotFoundError for a missing path when not `create`."""
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | (os.O_CREAT if create else 0), 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


class Lease:
    """One run's slots, from `lease()` until it closes. Used from one thread."""

    def __init__(self, folder: Path | None, slots: int) -> None:
        self._folder, self._size = folder, slots
        self._held: list[int] = []   # fds of the slots held
        self._member: int | None = None
        self._name = ""
        if folder is not None:
            members = folder / "members"
            members.mkdir(parents=True, exist_ok=True)
            # Locked before it appears, so a count never takes a new member for a dead one.
            self._name = f"{os.getpid()}-{secrets.token_hex(4)}"
            temp = members / f".{self._name}"
            self._member = _try_flock(temp)
            os.rename(temp, members / self._name)

    @property
    def held(self) -> int:
        return len(self._held)

    @property
    def size(self) -> int:
        """The budget's slots (0 when the budget is off)."""
        return self._size if self._folder is not None else 0

    def members(self) -> int:
        """The live leases on the machine, this one included. A member file whose lock is free
        belongs to a process that is gone, and is removed."""
        if self._folder is None:
            return 1
        live = 1
        for entry in os.scandir(self._folder / "members"):
            if entry.name.startswith(".") or entry.name == self._name:
                continue
            try:
                fd = _try_flock(Path(entry.path), create=False)
            except FileNotFoundError:   # it closed while we looked
                continue
            if fd is None:
                live += 1
                continue
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                pass
            os.close(fd)
        return live

    def share(self) -> int:
        """The most slots this lease may hold now: its fair share of the budget."""
        return -(-self._size // self.members())

    def take(self, want: int) -> int:
        """Up to `want` more slots, within the fair share, without waiting. Returns how many."""
        if self._folder is None:
            return want
        want = min(want, self.share() - self.held)
        got = 0
        if want <= 0:
            return 0
        for i in random.sample(range(self._size), self._size):   # spread the runs' probes
            if got == want:
                break
            if (fd := _try_flock(self._folder / f"slot-{i}")) is not None:
                self._held.append(fd)
                got += 1
        return got

    def give(self, n: int) -> None:
        """Release `n` of the slots held."""
        if self._folder is None:
            return
        for _ in range(n):
            os.close(self._held.pop())

    def cap(self, jobs: int) -> int:
        """`jobs` workers, no more than the budget has slots (what a pool of this size can use
        at once); `jobs` itself when the budget is off."""
        return min(jobs, self.size) if self.size else jobs

    def where(self) -> tuple[Path, int] | None:
        """The folder and size of the budget this lease is a member of, or None when it is off:
        what another process passes to `lease` to join the same budget."""
        return None if self._folder is None else (self._folder, self._size)

    def meter(self, executor: Executor) -> Executor:
        """`executor` with its tasks metered by this lease (`Metered`); `executor` itself when the
        budget is off. Every pool of worker processes goes through this: none holds slots for its
        whole life."""
        return executor if self._folder is None else Metered(executor, self)

    def close(self) -> None:
        self.give(self.held)
        if self._member is not None:
            try:
                os.unlink(self._folder / "members" / self._name)
            except FileNotFoundError:
                pass
            os.close(self._member)
            self._member = None


class Metered(Executor):
    """An executor whose tasks each hold one of a lease's slots while they run.

    `submit` queues the task and returns a future at once; a dispatcher thread starts queued tasks
    on `inner` as the lease gives slots (`Lease.take`, within its fair share), and a task gives its
    slot back when it ends. So the slots follow the work: a run that has nothing to run holds
    none, and one that holds more than its share gets down to it as its tasks end. When no task is
    running and none can get a slot, one runs without a slot (the floor: no run
    waits for another's whole run). `shutdown` stops the dispatcher after the queue has run (or
    cancels it) and shuts `inner` down. The lease is used only under `_lock`."""

    POLL = 0.1   # s between looks for a slot when none was given back (another run may have left)

    def __init__(self, inner: Executor, lease: Lease) -> None:
        self._inner, self._lease = inner, lease
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._queue: deque[tuple[Future, Callable, tuple, dict]] = deque()
        self._running = 0   # tasks on `inner`, with a slot or without
        self._stopped = False
        self._wait, self._cancel = True, False
        self._thread = threading.Thread(target=self._dispatch, name="reflow-metered", daemon=True)
        self._thread.start()

    def submit(self, fn, /, *args, **kwargs) -> Future:
        outer: Future = Future()
        with self._lock:
            if self._stopped:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._queue.append((outer, fn, args, kwargs))
        self._wake.set()
        return outer

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            self._stopped, self._wait, self._cancel = True, wait, cancel_futures
        self._wake.set()
        if wait:
            self._thread.join()

    def _claim(self) -> list[tuple[Future, Callable, tuple, dict, bool]]:
        """The queued tasks to start now, each with whether it holds a slot."""
        with self._lock:
            if self._cancel:
                while self._queue:
                    self._queue.popleft()[0].cancel()
            if not self._queue:
                return []
            got = self._lease.take(len(self._queue))
            floor = got == 0 and self._running == 0
            batch: list[tuple[Future, Callable, tuple, dict, bool]] = []
            while self._queue and (len(batch) < got or (floor and not batch)):
                outer, fn, args, kwargs = self._queue.popleft()
                if outer.set_running_or_notify_cancel():
                    batch.append((outer, fn, args, kwargs, len(batch) < got))
            self._lease.give(got - sum(held for *_, held in batch))   # cancelled ones' slots
            self._running += len(batch)
            return batch

    def _start(self, outer: Future, fn, args, kwargs, held: bool) -> None:
        try:
            inner = self._inner.submit(fn, *args, **kwargs)
        except BaseException as e:   # noqa: BLE001 - e.g. a broken pool: the task fails with it
            self._ended(held)
            outer.set_exception(e)
            return
        inner.add_done_callback(lambda done: self._relay(done, outer, held))

    def _ended(self, held: bool) -> None:
        with self._lock:
            self._running -= 1
            if held:
                self._lease.give(1)
        self._wake.set()

    def _relay(self, done: Future, outer: Future, held: bool) -> None:
        self._ended(held)
        if done.cancelled():
            outer.set_exception(CancelledError())
        elif (error := done.exception()) is not None:
            outer.set_exception(error)
        else:
            outer.set_result(done.result())

    def _dispatch(self) -> None:
        try:
            while True:
                self._wake.wait(self.POLL)
                self._wake.clear()
                for task in self._claim():
                    self._start(*task)
                with self._lock:
                    if self._stopped and not self._queue:
                        break
        except BaseException as e:   # noqa: BLE001 - never leave a caller waiting on a dead thread
            with self._lock:
                self._stopped = True
                queued, self._queue = list(self._queue), deque()
            for outer, *_ in queued:
                if outer.set_running_or_notify_cancel():
                    outer.set_exception(e)
        self._inner.shutdown(wait=self._wait, cancel_futures=self._cancel)


@contextmanager
def lease(folder: Path | None = None, slots: int | None = None) -> Iterator[Lease]:
    """A Lease on the machine's budget (`root()` and `size()` unless given). With the budget off,
    or a folder that can't be made, every `take` gets what it asks for."""
    slots = size() if slots is None else slots
    folder = root() if folder is None else folder
    try:
        held = Lease(folder, slots) if slots > 0 else Lease(None, 0)
    except OSError:
        held = Lease(None, 0)
    try:
        yield held
    finally:
        held.close()


# Two light holders fit on a 24 GB card, the one this was sized on (measured with nvidia-smi):
# docling peaks at 1.6 GB, Ollama's qwen3-vl:8b at 9.2 GB and gemma4:e4b at 5.4 GB, beside
# 1.6 GB of desktop. The local transformers model peaks at 17.9 GB, so it takes the whole card
# (`whole`). REFLOW_GPU_HOLDERS sets the count for another card.
GPU_HOLDERS = 2
GPU_POLL = 0.05   # s between tries while a light holder waits for a free slot

# Called with the seconds a holder waited, in the context that took the lease.
GPU_WAIT: ContextVar[Callable[[float], None] | None] = ContextVar("reflow_gpu_wait", default=None)
_thread = threading.local()   # whether this thread holds the lease, so a nested take is a no-op


def gpu_holders() -> int:
    """The number of GPU slots: REFLOW_GPU_HOLDERS, else GPU_HOLDERS. 0 is off."""
    if (value := os.environ.get("REFLOW_GPU_HOLDERS", "").strip()):
        return max(0, int(value))
    return GPU_HOLDERS


class GpuHold:
    """The GPU slots one holder took, until `release` (safe to call twice)."""

    def __init__(self, fds: list[int], waited: float = 0.0) -> None:
        self._fds, self.waited = fds, waited

    def release(self) -> None:
        fds, self._fds = self._fds, []
        for fd in fds:
            os.close(fd)


def hold_gpu(*, whole: bool = False, folder: Path | None = None,
             holders: int | None = None) -> GpuHold:
    """Wait for a GPU slot (every slot when `whole`) and hold it until `release`. With the lease
    off, or a folder that can't be made, it holds nothing and returns at once."""
    holders = gpu_holders() if holders is None else holders
    folder = root() / "gpu" if folder is None else folder
    if holders <= 0:
        return GpuHold([])
    try:
        folder.mkdir(parents=True, exist_ok=True)
        gate = os.open(folder / "gate", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
    except OSError:
        return GpuHold([])
    start = time.monotonic()
    fds: list[int] = []
    try:
        fcntl.flock(gate, fcntl.LOCK_EX)
        if whole:   # in order, holding the gate: no new holder starts while this one waits
            for i in range(holders):
                fds.append(os.open(folder / f"slot-{i}",
                                   os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666))
                fcntl.flock(fds[-1], fcntl.LOCK_EX)
        else:
            while not fds:
                for i in random.sample(range(holders), holders):
                    if (fd := _try_flock(folder / f"slot-{i}")) is not None:
                        fds.append(fd)
                        break
                else:
                    time.sleep(GPU_POLL)
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise
    finally:
        os.close(gate)
    waited = time.monotonic() - start
    if (note := GPU_WAIT.get()) is not None:
        note(waited)
    return GpuHold(fds, waited)


@contextmanager
def gpu(device: str = "cuda", *, whole: bool = False, folder: Path | None = None,
        holders: int | None = None) -> Iterator[None]:
    """Hold the GPU lease for the body when `device` is "cuda" (a model on the CPU or on MPS
    needs none). A take inside one this thread already holds is a no-op: a second open of a
    slot file would wait for the first, in the same thread, forever."""
    if device != "cuda" or getattr(_thread, "holds", False):
        yield
        return
    hold = hold_gpu(whole=whole, folder=folder, holders=holders)
    _thread.holds = True
    try:
        yield
    finally:
        _thread.holds = False
        hold.release()
