"""Shared caches under contention: concurrent writers on one key, readers during eviction,
killed writers, and eviction by size and age (reflow.cachedir, reflow.stagecache)."""

import multiprocessing
import os
import threading
import time
from pathlib import Path

from reflow import cachedir, stagecache
from testkit import blank_pdf

ENTRIES = ("reflow.extract.pagecontent",)


def _build_in_process(root: str, pdf: str, log: str, start: str) -> None:
    """One writer: wait for the start signal, then ask the stage cache for the key."""
    while not Path(start).exists():
        time.sleep(0.005)

    def build():
        with open(log, "a") as f:
            f.write(f"{os.getpid()}\n")
        time.sleep(0.5)   # long enough for the other writer to arrive while this one builds
        return {"built by": os.getpid()}
    value, _ = stagecache.cached(Path(root), "pages", ENTRIES, Path(pdf), build, "part")
    with open(log + ".seen", "a") as f:
        f.write(f"{value['built by']}\n")


def _stored(root, pdf):
    """The value saved for the key, which must not be built again."""
    value, hit = stagecache.cached(root, "pages", ENTRIES, pdf, lambda: None, "part")
    assert hit
    return value


def _entry_file(root):
    """The one saved entry under `root` (beside its lock file and any temp)."""
    [entry] = [p for p in root.glob("pages/*/*") if not p.name.startswith(".")]
    return entry


def test_two_processes_writing_one_key_build_it_once(tmp_path):
    root, pdf = tmp_path / "cache", blank_pdf(tmp_path / "a.pdf")
    log, start = tmp_path / "builds", tmp_path / "go"
    ctx = multiprocessing.get_context("spawn")
    writers = [ctx.Process(target=_build_in_process, args=(str(root), str(pdf), str(log),
                                                           str(start))) for _ in range(2)]
    for w in writers:
        w.start()
    start.touch()
    for w in writers:
        w.join(60)
        assert w.exitcode == 0
    builders = log.read_text().split()
    assert len(builders) == 1                       # the second writer waited, then read
    seen = (tmp_path / "builds.seen").read_text().split()
    assert seen == builders * 2                     # both got the one value that was built
    entry = _entry_file(root)
    assert _stored(root, pdf) == {"built by": int(builders[0])}
    assert not [p for p in entry.parent.iterdir() if ".tmp-" in p.name]   # no temp left


def test_two_threads_writing_one_key_build_it_once(tmp_path):
    root, pdf = tmp_path / "cache", blank_pdf(tmp_path / "a.pdf")
    builds, results = [], []
    barrier = threading.Barrier(2)

    def writer():
        barrier.wait()

        def build():
            builds.append(threading.get_ident())
            time.sleep(0.3)
            return "value"
        results.append(stagecache.cached(root, "pages", ENTRIES, pdf, build, "part"))
    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(builds) == 1
    assert sorted(results) == [("value", False), ("value", True)]


def test_every_writer_gets_its_own_temp_name(tmp_path):
    dest = tmp_path / "key"
    names = {cachedir.temp_path(dest) for _ in range(50)}
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(cachedir.temp_path(dest)))
               for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    names |= set(seen)
    assert len(names) == 58
    assert all(n.name.startswith(".key.tmp-") for n in names)


def _entry(root: Path, rel: str, size: int, used: float) -> Path:
    path = root / rel
    path.mkdir(parents=True)
    (path / "data").write_bytes(b"x" * size)
    os.utime(path, (used, used))
    return path


def test_the_first_eviction_only_reports(tmp_path):
    now = time.time()
    old = _entry(tmp_path, "doc/old", 10, now - 100 * 86400)
    first = cachedir.evict(tmp_path, 2, budget=1000, max_age=86400, now=now)
    assert first.dry_run and [r.path for r in first.removed] == [old]
    assert old.exists()                                    # reported, not removed
    assert (tmp_path / cachedir.REPORT).exists()
    assert "would remove 1 entries" in first.summary()
    second = cachedir.evict(tmp_path, 2, budget=1000, max_age=86400, now=now)
    assert not second.dry_run and [r.path for r in second.removed] == [old]
    assert not old.exists()


def test_eviction_keeps_the_most_recently_used_within_the_budget(tmp_path):
    now = time.time()
    newest = _entry(tmp_path, "a/1", 100, now - 10)
    middle = _entry(tmp_path, "b/1", 100, now - 20)
    oldest = _entry(tmp_path, "a/2", 100, now - 30)
    stale = _entry(tmp_path, "c/1", 1, now - 30 * 86400)
    report = cachedir.evict(tmp_path, 2, budget=250, max_age=14 * 86400, dry_run=False, now=now,
                            min_idle=0)
    assert {(r.path, r.why) for r in report.removed} == {(oldest, "size"), (stale, "age")}
    assert newest.exists() and middle.exists() and not oldest.exists() and not stale.exists()
    assert report.kept == 2 and report.kept_bytes == 200


def test_a_file_hard_linked_into_two_entries_counts_once(tmp_path):
    now = time.time()
    a = _entry(tmp_path, "a/1", 100, now - 10)
    b = tmp_path / "b" / "1"
    b.mkdir(parents=True)
    os.link(a / "data", b / "data")
    os.utime(b, (now - 20, now - 20))
    report = cachedir.evict(tmp_path, 2, budget=150, max_age=86400, dry_run=False, now=now,
                            min_idle=0)
    assert report.removed == [] and report.kept_bytes == 100


def test_eviction_never_removes_an_entry_a_reader_holds(tmp_path):
    now = time.time()
    entry = _entry(tmp_path, "doc/key", 100, now - 10)
    pin = cachedir.lock(entry, shared=True)
    report = cachedir.evict(tmp_path, 2, budget=0, max_age=86400, dry_run=False, now=now,
                            min_idle=0)
    assert report.pinned == [entry] and entry.exists() and (entry / "data").exists()
    pin.release()
    report = cachedir.evict(tmp_path, 2, budget=0, max_age=86400, dry_run=False, now=now,
                            min_idle=0)
    assert [r.path for r in report.removed] == [entry] and not entry.exists()
    assert not cachedir.lock_path(entry).exists()


def test_a_reader_waiting_on_an_evicted_lock_file_locks_the_new_one(tmp_path):
    entry = _entry(tmp_path, "doc/key", 1, time.time())
    evicting = cachedir.lock(entry)
    got = []
    reader = threading.Thread(target=lambda: got.append(cachedir.lock(entry, shared=True)))
    reader.start()
    time.sleep(0.1)   # the reader has opened the lock file and waits on it
    cachedir.remove(entry)
    cachedir.lock_path(entry).unlink()
    evicting.release()
    reader.join(5)
    held = got[0]
    # The reader holds the lock at the path now, so a writer of the key must wait for it.
    assert cachedir.lock(entry, wait=False) is None
    held.release()
    assert cachedir.lock(entry, wait=False) is not None


def _leave_a_temp_and_die(dest: str) -> None:
    tmp = cachedir.temp_path(Path(dest))
    tmp.mkdir()
    (tmp / "partial").write_bytes(b"x" * 10)
    os._exit(1)   # killed mid-write: nothing cleans up


def test_a_killed_writers_temp_files_are_swept(tmp_path):
    now = time.time()
    _entry(tmp_path, "doc/live", 1, now)
    ctx = multiprocessing.get_context("spawn")
    writer = ctx.Process(target=_leave_a_temp_and_die, args=(str(tmp_path / "doc" / "key"),))
    writer.start()
    writer.join(30)
    dead = next((tmp_path / "doc").glob(".key.tmp-*"))
    mine = cachedir.temp_path(tmp_path / "doc" / "other")   # this process is alive
    mine.mkdir()
    long_ago = now - cachedir.DEBRIS_AGE - 60
    for p in (dead, mine):
        os.utime(p, (long_ago, long_ago))
    young = tmp_path / "doc" / f".young.tmp-{writer.pid}"
    young.mkdir()   # a dead PID, but too recent: another PID namespace may own it

    report = cachedir.evict(tmp_path, 2, budget=1 << 30, max_age=86400, dry_run=False, now=now)
    assert [r.path for r in report.debris] == [dead]
    assert not dead.exists() and mine.exists() and young.exists()


def test_a_corrupt_stage_entry_is_rebuilt_under_the_lock(tmp_path):
    root, pdf = tmp_path / "cache", blank_pdf(tmp_path / "a.pdf")
    stagecache.cached(root, "pages", ENTRIES, pdf, lambda: "old", "part")
    entry = _entry_file(root)
    entry.write_bytes(b"not a pickle")
    assert stagecache.cached(root, "pages", ENTRIES, pdf, lambda: "new", "part") == ("new", False)
    assert _stored(root, pdf) == "new"


def test_stage_eviction_walks_stage_pdf_key(tmp_path):
    root, pdf = tmp_path / "cache", blank_pdf(tmp_path / "a.pdf")
    stagecache.cached(root, "pages", ENTRIES, pdf, lambda: "v", "part")
    entry = _entry_file(root)
    report = cachedir.evict(root, 3, budget=0, max_age=86400, dry_run=False)
    assert report.removed == [] and entry.exists()   # used just now: kept over the budget
    old = time.time() - cachedir.MIN_IDLE - 60
    os.utime(entry, (old, old))
    report = cachedir.evict(root, 3, budget=0, max_age=86400, dry_run=False)
    assert [r.path for r in report.removed] == [entry] and not entry.exists()


def test_an_entry_used_in_the_last_hour_is_not_evicted_for_size(tmp_path):
    now = time.time()
    recent = _entry(tmp_path, "a/1", 100, now - 60)
    idle = _entry(tmp_path, "a/2", 100, now - 2 * cachedir.MIN_IDLE)
    report = cachedir.evict(tmp_path, 2, budget=0, max_age=86400, dry_run=False, now=now)
    assert [r.path for r in report.removed] == [idle] and recent.exists()


def test_a_removed_entry_does_not_hide_the_files_it_shares_with_a_kept_one(tmp_path):
    now = time.time()
    a = _entry(tmp_path, "a/1", 100, now - 10)
    b = _entry(tmp_path, "b/1", 100, now - 20)
    c = tmp_path / "c" / "1"
    c.mkdir(parents=True)
    os.link(b / "data", c / "data")
    os.utime(c, (now - 30, now - 30))
    report = cachedir.evict(tmp_path, 2, budget=150, max_age=86400, dry_run=False, now=now,
                            min_idle=0)
    # b goes over the budget; c's file is b's, so it is counted (and goes) too.
    assert [r.path for r in report.removed] == [b, c] and a.exists()
    assert report.kept_bytes == 100
