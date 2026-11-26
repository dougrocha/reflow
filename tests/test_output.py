import pytest

from reflow import names
from reflow.names import INTERNAL_NAMES, OUTPUT_NAMES, PUBLIC_NAMES
from reflow.output import check_replaceable

DIRS = {names.CHUNKS_DIR, names.PAGES_DIR, names.FIGURES_DIR, names.ASSETS_DIR, names.TRACE_DIR,
        names.LAYOUT_DIR}


def test_public_and_internal_sets_are_disjoint():
    assert PUBLIC_NAMES.isdisjoint(INTERNAL_NAMES)


def test_the_ocr_word_grades_are_never_published():
    from reflow.ingest.ocr_trust import SIDECAR_NAME
    assert names.OCR_WORDS == SIDECAR_NAME and SIDECAR_NAME in INTERNAL_NAMES


def test_check_replaceable_accepts_a_folder_of_output_names(tmp_path):
    out = tmp_path / "out"
    for name in OUTPUT_NAMES:
        if name in DIRS:
            (out / name).mkdir(parents=True)
        else:
            (out / name).parent.mkdir(parents=True, exist_ok=True)
            (out / name).write_bytes(b"")
    check_replaceable(out)  # no raise


def test_check_replaceable_rejects_a_foreign_file(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.txt").write_text("mine")
    with pytest.raises(ValueError, match="notes.txt"):
        check_replaceable(out)


# What a killed conversion leaves beside its output (issue #294), and `recover`.

import os
import subprocess
import sys
import textwrap

from reflow import cachedir, output


def _kill_mid_swap(out, *, at):
    """Run `staged` into `out` in a child process that dies (os._exit, so no cleanup runs)
    `at` "write" (while writing the stage) or "swap" (between the swap's two renames)."""
    code = textwrap.dedent(f"""
        import os, sys
        from pathlib import Path
        from reflow import output
        out = Path({str(out)!r})
        rename = Path.rename
        def dying_rename(self, target):   # dies once the old output is aside
            moved = rename(self, target)
            if Path(target).name.endswith(".old"):
                os._exit(9)
            return moved
        Path.rename = dying_rename
        with output.staged(out) as stage:
            (stage / "index.html").write_text("new")
            if {at!r} == "write":
                os._exit(9)
    """)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 9


def _published(tmp_path, text="old"):
    out = tmp_path / "out"
    out.mkdir()
    (out / "index.html").write_text(text)
    return out


def test_a_kill_between_the_swap_renames_is_recovered(tmp_path):
    out = _published(tmp_path)
    _kill_mid_swap(out, at="swap")
    assert not out.exists()   # the window issue #294 describes
    output.recover(out)
    assert (out / "index.html").read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]   # stage, .old and locks gone


def test_a_killed_runs_stage_is_removed(tmp_path):
    out = _published(tmp_path)
    _kill_mid_swap(out, at="write")
    assert len(list(tmp_path.glob(".out.tmp-*"))) == 1
    output.recover(out)
    assert (out / "index.html").read_text() == "old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_a_live_runs_stage_is_kept(tmp_path):
    out = _published(tmp_path)
    with output.staged(out) as stage:
        (stage / "index.html").write_text("new")
        output.recover(out)   # as a second conversion into `out` would, on start
        assert (stage / "index.html").read_text() == "new"
    assert (out / "index.html").read_text() == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]   # no lock file left


def test_a_failed_run_leaves_nothing_beside_the_output(tmp_path):
    out = _published(tmp_path)
    with pytest.raises(RuntimeError), output.staged(out):
        raise RuntimeError
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_an_unlocked_stage_is_removed_only_once_old(tmp_path):
    """A stage with no lock file may be an older reflow's, still running."""
    out = _published(tmp_path)
    stage = tmp_path / ".out.tmp-0123456789ab"
    stage.mkdir()
    output.recover(out)
    assert stage.is_dir() and not cachedir.lock_path(stage).exists()
    output.recover(out, now=stage.stat().st_mtime + output.UNLOCKED_STAGE_AGE)
    assert not stage.exists()


def test_a_leftover_old_is_removed_when_the_output_exists(tmp_path):
    """A kill after the second rename leaves the new output in place and the old one aside."""
    out = _published(tmp_path, "new")
    old = tmp_path / ".out.tmp-0123456789ab.old"
    old.mkdir()
    (old / "index.html").write_text("old")
    output.recover(out)
    assert (out / "index.html").read_text() == "new" and not old.exists()


def test_a_sibling_outputs_leftovers_are_not_this_ones(tmp_path):
    """`out.tmp-report` is another output; its `.old` and stage must survive recover("out")."""
    out = _published(tmp_path)
    sibling = [tmp_path / ".out.tmp-report.tmp-0123456789ab.old",
               tmp_path / ".out.tmp-report.tmp-0123456789ab", tmp_path / ".out.tmp-notes.old"]
    for p in sibling:
        p.mkdir()
    output.recover(out, now=1e12)   # every unlocked stage is old enough to go
    assert all(p.is_dir() for p in sibling)


def test_a_bracketed_output_name_is_matched_literally(tmp_path):
    out = tmp_path / "report[1]"
    out.mkdir()
    lock = tmp_path / "..report[1].tmp-0123456789ab.lock"
    lock.touch()
    output.recover(out)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report[1]"]


def test_a_swap_clears_an_earlier_swaps_old_first(tmp_path):
    """Two `.old` folders with the output missing would leave recover guessing by mtime."""
    out = _published(tmp_path, "new")
    leftover = tmp_path / ".out.tmp-0123456789ab.old"
    leftover.mkdir()   # a kill while an earlier swap deleted its `.old`
    _kill_mid_swap(out, at="swap")
    assert not leftover.exists()
    output.recover(out)
    assert (out / "index.html").read_text() == "new"
