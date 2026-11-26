"""Caches and output folders on disk that several processes and threads share: the stage cache
(reflow.stagecache), the saved models (reflow.saved), the staging and swap of an output folder
(reflow.output) and the model's answer caches. A folder is shared across processes by a lock
beside each entry; the page-check harness (tests/pages/corpus.py) keeps its conversions and its
stage cache this way too, in the main checkout's .pytest_cache, used by every worktree at once.

  * `temp_path` names one writer's work in progress beside its entry. The name is unique per
    process, thread and call, and its leading dot keeps it out of the entries.
  * `lock` is a per-entry flock on `.<name>.lock` beside the entry. A producer holds it
    exclusively while it builds and publishes, and checks the entry again once it holds it, so
    two writers never build one key at once. A reader holds it shared while it reads (a pin),
    so eviction never removes an entry in use.
  * `evict` keeps the most recently used entries within a byte budget, removes the ones not used
    for `max_age`, skips pinned ones, and sweeps leftover temp files of writers whose process is
    gone. Its first run on a cache root only reports what it would remove.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# A temp name: `.<entry>.<tag>-<pid>[-...]`. The first PID in the name is its writer's.
_DEBRIS = re.compile(r"\.(?:tmp|src|old|unpublished)-(\d+)")
DEBRIS_AGE = 6 * 3600   # a dead writer's temp files are swept once this old (seconds)
MIN_IDLE = 3600   # an entry used more recently than this is never evicted for size (seconds)
REPORT = ".eviction.json"   # the last eviction's report, in the cache root


def temp_path(dest: Path, tag: str = "tmp") -> Path:
    """A private path beside `dest` for one writer to build in, then rename into place."""
    return dest.with_name(f".{dest.name}.{tag}-{os.getpid()}-{threading.get_ident():x}-"
                          f"{secrets.token_hex(4)}")


def lock_path(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.lock")


class Lock:
    """A held flock on an entry's lock file. Released by `release`, by leaving the `with`
    block, or by the process exiting."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> Lock:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def lock(dest: Path, *, shared: bool = False, wait: bool = True) -> Lock | None:
    """Lock the entry `dest` (which need not exist): exclusive to build or remove it, shared to
    read it. Without `wait`, None when another holder has it. Every open gets its own lock, so
    two threads of one process exclude each other too. The lock file is removed only by
    `evict`, while it holds it, so a lock that is held on a file no longer at the path is
    taken again on the new file."""
    path = lock_path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if wait else fcntl.LOCK_NB)
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o666)
        try:
            fcntl.flock(fd, mode)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        try:
            st, now = os.fstat(fd), os.stat(path)
            if (st.st_dev, st.st_ino) == (now.st_dev, now.st_ino):
                return Lock(fd)
        except FileNotFoundError:
            pass
        os.close(fd)


def try_lock(dest: Path, *, shared: bool = False, wait: bool = True) -> Lock | None:
    """`lock`, or None where locking fails (a read-only cache): the caches are best effort."""
    try:
        return lock(dest, shared=shared, wait=wait)
    except OSError:
        return None


def remove(path: Path) -> None:
    """Remove a file or a directory tree. A tree is renamed aside first, so its path never holds
    a part of it; if this process dies mid-way, `evict` sweeps the rest."""
    if path.is_dir() and not path.is_symlink():
        aside = temp_path(path, "old")
        try:
            path.rename(aside)
        except OSError:
            aside = path
        shutil.rmtree(aside, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _size(path: Path, seen: set[tuple[int, int]], new: set[tuple[int, int]]) -> int:
    """Bytes of the files under `path` not already in `seen`, whose inodes go into `new`: a
    file hard-linked into several kept entries counts once, in the most recently used one."""
    def one(p: str) -> int:
        try:
            st = os.lstat(p)
        except OSError:
            return 0
        if (st.st_dev, st.st_ino) in seen or (st.st_dev, st.st_ino) in new:
            return 0
        new.add((st.st_dev, st.st_ino))
        return st.st_size
    if not path.is_dir():
        return one(str(path))
    total = 0
    for top, _, files in os.walk(path):
        total += sum(one(os.path.join(top, f)) for f in files)
    return total


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as e:   # EPERM: a process of another user holds the PID
        return e.errno == errno.EPERM
    return True


def _debris(parent: Path, now: float) -> list[Path]:
    """Temp files beside the entries in `parent` whose writer is dead and that are old enough
    that no live writer in another PID namespace could still own them."""
    found = []
    try:
        children = list(parent.iterdir())
    except OSError:
        return found
    for p in children:
        if not p.name.startswith(".") or not (m := _DEBRIS.search(p.name)):
            continue
        try:
            old = now - p.lstat().st_mtime >= DEBRIS_AGE
        except OSError:
            continue
        if old and not _alive(int(m.group(1))):
            found.append(p)
    return found


def _drop_orphan_locks(parent: Path) -> None:
    """Remove the lock files of entries that no longer exist and that nobody holds."""
    for lk in parent.glob(".*.lock"):
        entry = parent / lk.name[1:-len(".lock")]
        if entry.exists() or (held := try_lock(entry, wait=False)) is None:
            continue
        with held:
            if not entry.exists():
                lk.unlink(missing_ok=True)


def _children(d: Path, *, dirs_only: bool) -> list[Path]:
    try:
        return [p for p in d.iterdir() if not p.name.startswith(".")
                and (not dirs_only or p.is_dir())]
    except OSError:
        return []


def _level(root: Path, depth: int) -> list[Path]:
    """The directories `depth` levels under `root` (root itself at 0), never dot-names."""
    level = [root]
    for _ in range(depth):
        level = [p for d in level for p in _children(d, dirs_only=True)]
    return level


def entries(root: Path, depth: int) -> list[Path]:
    """The entries `depth` levels under `root`: files or directories, never dot-names."""
    return [p for d in _level(root, depth - 1) for p in _children(d, dirs_only=False)]


@dataclass
class Removal:
    path: Path
    bytes: int
    why: str   # "age" or "size"


@dataclass
class Report:
    root: Path
    dry_run: bool
    kept: int = 0
    kept_bytes: int = 0
    pinned: list[Path] = field(default_factory=list)   # would go, but in use: kept
    removed: list[Removal] = field(default_factory=list)
    debris: list[Removal] = field(default_factory=list)

    def summary(self) -> str:
        gib = 1 << 30
        verb = "would remove" if self.dry_run else "removed"
        gone = sum(r.bytes for r in self.removed) + sum(r.bytes for r in self.debris)
        return (f"{self.root}: kept {self.kept} entries ({self.kept_bytes / gib:.2f} GiB, "
                f"{len(self.pinned)} in use); {verb} {len(self.removed)} entries and "
                f"{len(self.debris)} leftover temp files ({gone / gib:.2f} GiB)"
                + (". Dry run: the first eviction of a cache only reports; the next one "
                   f"removes. Report: {self.root / REPORT}" if self.dry_run else ""))

    def as_json(self) -> dict:
        def rows(rs):
            return [{"path": str(r.path.relative_to(self.root)), "bytes": r.bytes, "why": r.why}
                    for r in rs]
        return {"dry_run": self.dry_run, "at": time.time(), "kept": self.kept,
                "kept_bytes": self.kept_bytes,
                "pinned": [str(p.relative_to(self.root)) for p in self.pinned],
                "removed": rows(self.removed), "debris": rows(self.debris)}


def evict(root: Path, depth: int, *, budget: int, max_age: float,
          dry_run: bool | None = None, now: float | None = None,
          min_idle: float = MIN_IDLE) -> Report | None:
    """Keep the most recently used entries `depth` levels under `root` (last use is the mtime,
    which readers touch) that fit in `budget` bytes, and remove the rest and any not used for
    `max_age` seconds. An entry another process has locked (pinned or being rebuilt) is kept,
    and so is one used in the last `min_idle` seconds, even over the budget: a reader that
    does not pin (an older checkout's harness) still touches what it reads.
    Also sweeps the temp files of dead writers (`DEBRIS_AGE`). `dry_run` None means a dry run
    unless an earlier run left its report in `root`: the first eviction of a cache only says
    what it would remove. The report is written to `root/REPORT` either way. None when another
    process is evicting this root."""
    if not root.is_dir():
        return None
    gate = try_lock(root / "evict", wait=False)
    if gate is None:
        return None
    with gate:
        if dry_run is None:
            dry_run = not (root / REPORT).exists()
        now = time.time() if now is None else now
        report = Report(root, dry_run)
        found = []
        for e in entries(root, depth):
            try:
                found.append((e.stat().st_mtime, e))
            except OSError:
                continue
        found.sort(key=lambda t: t[0], reverse=True)
        seen: set[tuple[int, int]] = set()   # the files of the entries kept
        total = 0
        for used, e in found:
            size = _size(e, seen, new := set())
            why = "age" if now - used > max_age else "size" if total + size > budget else None
            if why == "size" and now - used < min_idle:
                why = None
            if why is None:
                total += size
                seen |= new
                report.kept += 1
                continue
            held = try_lock(e, wait=False)
            try:
                if held is None or e.stat().st_mtime != used:   # in use, or used just now
                    report.pinned.append(e)
                    total += size
                    seen |= new
                    report.kept += 1
                    continue
                report.removed.append(Removal(e, size, why))
                if not dry_run:
                    remove(e)
                    lock_path(e).unlink(missing_ok=True)   # still held: see `lock`
            except OSError:
                continue
            finally:
                if held is not None:
                    held.release()
        report.kept_bytes = total
        for parent in _level(root, depth - 1):   # temp files sit beside the entries
            for d in _debris(parent, now):
                report.debris.append(Removal(d, _size(d, seen, set()), "dead writer"))
                if not dry_run:
                    remove(d)
            if not dry_run:
                _drop_orphan_locks(parent)
        try:
            (root / REPORT).write_text(json.dumps(report.as_json(), indent=1), encoding="utf-8")
        except OSError:
            pass
        return report
