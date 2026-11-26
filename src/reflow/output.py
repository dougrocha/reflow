"""Publishing a conversion: the one writer of the files the renderer derives from the model,
and the staging folder that is swapped in for the output (the names are reflow.names).

A conversion never writes into the output directory itself. A failed run leaves the previous
output as it was, and a successful one replaces it whole, so no page image, figure or chunk
of an earlier document stays reachable beside the new one."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from reflow import cachedir
from reflow.names import (
    CARRIED,
    DECISIONS_LEDGER,
    IMMUTABLE_DIRS,
    OUTPUT_NAMES,
    PAGES_DIR,
    VERSIONED,
)
from reflow.render.pages import render_pages
from reflow.render.pages import start_page_images as start_images
from reflow.render.passages import write_passages
from reflow.render.sidecars import write_page_layouts
from reflow.render.site import render_html
from reflow.run import OptionsError

if TYPE_CHECKING:
    from reflow.pipeline import Model
    from reflow.run import Run

def check_replaceable(out_dir: Path) -> None:
    """Raise OptionsError if `out_dir` holds anything a conversion didn't write. Publishing
    replaces the whole directory, so a folder of someone's own files must not be the output."""
    if not out_dir.exists():
        return
    if not out_dir.is_dir():
        raise OptionsError(f"output {out_dir} is not a directory")
    foreign = sorted(p.name for p in out_dir.iterdir() if p.name not in OUTPUT_NAMES)
    if foreign:
        shown = ", ".join(foreign[:5]) + (", ..." if len(foreign) > 5 else "")
        raise OptionsError(f"output {out_dir} holds files a conversion didn't write ({shown}); "
                         "choose an empty or new directory")


@contextmanager
def staged(out_dir: Path) -> Iterator[Path]:
    """Yield an empty folder beside `out_dir` to write the conversion into. On a clean exit it
    replaces `out_dir`; on an exception it is deleted and `out_dir` is left as it was. The
    folder's name is unique, so several conversions can run at once. The run holds a flock on
    the folder's lock file (`.<stage>.lock`, taken before the folder exists) until the folder
    is gone or swapped in, so `recover` can tell a live run's folder from a killed run's."""
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = out_dir.with_name(f".{out_dir.name}.tmp-{uuid.uuid4().hex[:12]}")
    owner = cachedir.lock(stage)
    try:
        stage.mkdir()
        try:
            for name in CARRIED:
                if (out_dir / name).is_file():
                    shutil.copyfile(out_dir / name, stage / name)
            yield stage
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        with _held(out_dir):
            _swap(stage, out_dir)
    finally:
        cachedir.lock_path(stage).unlink(missing_ok=True)   # still held: see cachedir.lock
        owner.release()


def _swap(stage: Path, out_dir: Path) -> None:
    """Put `stage` in place of `out_dir`. A rename can't replace a non-empty directory, so the
    old output moves aside first and comes back if the new one can't be moved in. A process
    killed between the two renames leaves `out_dir` missing and the old output in the `.old`
    folder; the next `recover` moves it back. The caller holds `out_dir`'s lock (`_held`).
    An earlier swap's `.old` (a kill while it was being deleted) goes first, so while
    `out_dir` is missing there is never more than one `.old` to choose from."""
    if not out_dir.exists():
        stage.rename(out_dir)
        return
    for leftover in _leftovers(out_dir).olds:
        shutil.rmtree(leftover, ignore_errors=True)
    old = out_dir.with_name(f"{stage.name}.old")
    out_dir.rename(old)
    try:
        stage.rename(out_dir)
    except BaseException:
        old.rename(out_dir)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


@contextmanager
def _held(out_dir: Path) -> Iterator[None]:
    """Hold `out_dir`'s exclusive lock (`.<name>.lock` beside it), which every swap and every
    `recover` takes, so a recovery never sees a swap half done. The lock file is removed
    while held (cachedir.lock takes a lock on a replaced file again), so none is left."""
    held = cachedir.lock(out_dir)
    try:
        yield
    finally:
        cachedir.lock_path(out_dir).unlink(missing_ok=True)
        held.release()


# A stage with no lock file may belong to a reflow from before stages were locked, still
# running; it is removed only once it is this old (seconds).
UNLOCKED_STAGE_AGE = 6 * 3600


@dataclass
class _Leftovers:
    olds: list[Path]
    stages: list[Path]
    locks: list[Path]   # the stages' lock files


def _leftovers(out_dir: Path) -> _Leftovers:
    """What `staged` and `_swap` put beside `out_dir`, matched by exact name, so a sibling
    output whose name starts with this one's (`out.tmp-x`) is never taken for it."""
    name = re.escape(out_dir.name)
    pattern = re.compile(rf"(\.?)\.{name}\.tmp-[0-9a-f]{{12}}(\.old|\.lock)?")
    found = _Leftovers([], [], [])
    for p in out_dir.parent.iterdir():
        if not (m := pattern.fullmatch(p.name)):
            continue
        dot, tail = m.groups()
        if dot and tail == ".lock":
            found.locks.append(p)
        elif not dot and tail != ".lock" and p.is_dir() and not p.is_symlink():
            (found.olds if tail else found.stages).append(p)
    return found


def recover(out_dir: Path, *, now: float | None = None) -> None:
    """Undo what a killed conversion into `out_dir` left beside it, before the next one reads
    `out_dir`: if `out_dir` is missing and a swap's `.old` folder is there (the kill came
    between `_swap`'s two renames), move the old output back; remove the other `.old`
    folders, and the staging folders whose run is gone (their lock is free). A live run's
    stage is never touched, so several conversions into one output can run at once."""
    if not out_dir.parent.is_dir():
        return
    now = time.time() if now is None else now
    with _held(out_dir):
        found = _leftovers(out_dir)
        # `_swap` keeps one `.old` at most while out_dir is missing; the newest wins only
        # among the ones a reflow from before it left.
        olds = sorted(found.olds, key=lambda p: p.stat().st_mtime, reverse=True)
        if olds and not out_dir.exists():
            olds.pop(0).rename(out_dir)
        for old in olds:
            shutil.rmtree(old, ignore_errors=True)
        for stage in found.stages:
            _drop_if_abandoned(stage, now)
        for lk in found.locks:   # a stage's lock left by a killed run
            stage = lk.with_name(lk.name[1:-len(".lock")])
            if not stage.exists():
                _drop_if_abandoned(stage, now)


def _drop_if_abandoned(stage: Path, now: float) -> None:
    """Remove `stage` and its lock file when no run holds its lock."""
    if not cachedir.lock_path(stage).exists():
        try:
            if now - stage.lstat().st_mtime < UNLOCKED_STAGE_AGE:
                return
        except FileNotFoundError:
            return
    held = cachedir.lock(stage, wait=False)
    if held is None:   # a live run's
        return
    try:
        shutil.rmtree(stage, ignore_errors=True)
        cachedir.lock_path(stage).unlink(missing_ok=True)
    finally:
        held.release()


@dataclass(frozen=True)
class SiteStats:
    """What the renderer measured: the chunk count, the contents length and the two sizes
    the first-load budget watches."""
    chunks: int
    headings: int
    index_bytes: int
    largest_chunk_bytes: int


def start_page_images(run: Run) -> None:
    """Start the page images on the run's pool, if it has one; `publish` collects them. They
    need only the working PDF, so the pool renders them while the parent does its serial passes."""
    if not run.options.page_images or run.workers.pool is None or run.state.pages_carried:
        return
    widths = run.options.written_widths(len(run.doc.pages))
    run.state.page_images = start_images(
        run.working, run.stage / PAGES_DIR, len(run.doc.pages), run.workers.pool,
        width=widths[0], extra_widths=widths[1:], workers=run.workers.jobs)


def publish(run: Run, model: Model) -> SiteStats:
    """Write every file the renderer derives from the finished `model` into run.stage, in one
    place (the model's own files, names.MODEL_NAMES, are already there: pipeline.Model.write):
      1. page images when options.page_images (on the run's workers); none are rendered when
         saved.Reuse.restore carried the previous output's, which a render could not change
      2. LAYOUT_DIR/NNNN.json, the per-page sidecars, and DECISIONS_LEDGER when the model has one
      3. the site: index.html, chunks/, toc.json, manifest.json, assets/, with the version
         (manifest.json["v"], see `stamp_version`) stamped in once it is all written
      4. PASSAGES_JSONL, whose header carries the same version
    The swap into out_dir stays in `staged`."""
    doc, stage = model.doc, run.stage
    with run.step("publish"):
        widths = run.options.written_widths(len(doc.pages))
        if run.options.page_images and not run.state.pages_carried:
            done = 0

            def page_done(k: int) -> None:
                nonlocal done
                done += k
                run.progress("Rendering page images", done, len(doc.pages))

            started, run.state.page_images = run.state.page_images, None
            if started is not None:   # begun by start_page_images, on the pool
                started.finish(page_done)
            else:   # no pool (jobs=1) renders in this process, as the CLI's --jobs help promises
                render_pages(run.working, stage / PAGES_DIR, len(doc.pages), width=widths[0],
                             extra_widths=widths[1:], on_done=page_done,
                             executor=run.workers.pool, workers=run.workers.jobs)
        write_page_layouts(doc, stage)
        if (ledger := doc.decisions_ledger) is not None:
            (stage / DECISIONS_LEDGER).write_text(
                json.dumps(ledger, ensure_ascii=False, indent=1), encoding="utf-8")
        run.progress("Writing HTML", 0, None)
        site = render_html(doc, doc.root.attrs["title"], stage, figures=model.figures,
                           labels={p.number: p.label for p in doc.pages},
                           page_widths=widths if run.options.page_images else (),
                           version=VERSION_SLOT)
        digest = stamp_version(stage, model.body)
        write_passages(doc, stage, digest)
    return SiteStats(**site)


# What the renderer writes in place of the version, before the version is known. It is not
# hex, so it is never a digest, and it has the digest's length, so the sizes render_html
# measures are those of the stamped files.
VERSION_SLOT = "reflow-vslot"


def stamp_version(stage: Path, body: str) -> str:
    """Digest the document JSON `body` and the published site in `stage` (the files in
    VERSIONED and IMMUTABLE_DIRS, as rendered with VERSION_SLOT), then write the digest over
    every VERSION_SLOT. Returns the digest (sha256, first 12 hex digits).

    The viewer appends it to every page, figure, chunk and asset URL, which are cached for a
    year. A change to the renderer, the stylesheet or an image changes it, so a warm browser
    fetches the new files; a rebuild that writes the same bytes keeps it, and the cache."""
    files = [stage / name for name in VERSIONED if (stage / name).is_file()]
    for name in IMMUTABLE_DIRS:
        if (stage / name).is_dir():
            files.extend(p for p in (stage / name).rglob("*") if p.is_file())
    files.sort(key=lambda p: p.relative_to(stage).as_posix())
    h = hashlib.sha256(body.encode("utf-8"))
    slot = VERSION_SLOT.encode()
    stamped = []
    for path in files:
        data = path.read_bytes()
        rel = path.relative_to(stage).as_posix().encode()
        h.update(b"\0%s\0%d\0" % (rel, len(data)))
        h.update(data)
        if slot in data:
            stamped.append((path, data))
    digest = h.hexdigest()[:12]
    for path, data in stamped:
        path.write_bytes(data.replace(slot, digest.encode()))
    return digest
