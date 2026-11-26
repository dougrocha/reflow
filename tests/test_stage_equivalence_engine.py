"""The stage-cache equivalence engine (tests/stage_equivalence.py) on fake outputs and fake stage
roots: no conversions. The real check runs under -m pages (tests/pages/test_stage_equivalence.py)."""

from __future__ import annotations

import io
import json
import os

import pikepdf
import pytest

from stage_equivalence import (EDITED_MODULE, OCR_NOISE, Exclusion, PlantError, Stage,
                               StageEvents, arm, compare_outputs, edited_tree, noise_of,
                               plant_stale, snapshot)


def _write(root, files: dict[str, bytes | str]):
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


def test_identical_outputs_compare_equal(tmp_path):
    files = {"document.json": "{}", "working.pdf": b"%PDF-1", "chunks/0001.html": "<p>"}
    got = compare_outputs(_write(tmp_path / "a", files), _write(tmp_path / "b", files))
    assert got.ok and got.compared == 3 and got.applied == ()


def test_a_changed_file_and_a_missing_file_differ(tmp_path):
    a = _write(tmp_path / "a", {"document.json": '{"x": 1}', "toc.json": "[]"})
    b = _write(tmp_path / "b", {"document.json": '{"x": 2}'})
    got = compare_outputs(a, b)
    assert {(d.path, d.kind) for d in got.differences} == {("document.json", "content"),
                                                           ("toc.json", "only-left")}
    assert '-{"x": 1}' in got.render() and '+{"x": 2}' in got.render()


def test_without_ocr_working_pdf_is_compared_byte_for_byte(tmp_path):
    a = _write(tmp_path / "a", {"working.pdf": b"%PDF-1 a"})
    b = _write(tmp_path / "b", {"working.pdf": b"%PDF-1 b"})
    assert [d.path for d in compare_outputs(a, b, noise_of(a)).differences] == ["working.pdf"]


def _ocr_pdf(run: str, text: bytes = b"(words) Tj") -> bytes:
    """A PDF stamped as ocrmypdf stamps each run's copy: its own /ID, dates and XObject name."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page()
    name = f"/OCR-{run}"
    layer = pikepdf.Stream(pdf, b"BT " + text + b" ET", Type=pikepdf.Name.XObject,
                           Subtype=pikepdf.Name.Form, BBox=[0, 0, 612, 792])
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary({name: layer}))
    page.Contents = pdf.make_stream(f"q {name} Do Q".encode())
    pdf.docinfo["/ModDate"] = f"D:2026110218{len(run):02d}00"
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        meta["xmp:ModifyDate"] = f"2026-11-02T18:{len(run):02d}:00"
    pdf.trailer.ID = pikepdf.Array([b"same", run.encode()])
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def test_ocr_noise_is_excluded_only_where_it_is_noise(tmp_path):
    def out(name, run, text, pdf_text=b"(words) Tj"):
        return _write(tmp_path / name, {
            "working.pdf": _ocr_pdf(run, pdf_text),
            "ocr-words.json": json.dumps({"pdf_sha256": run, "pages": [text]}),
        })
    a, b = out("a", "aaaa", "words"), out("b", "bbbbbb", "words")
    assert noise_of(a) == OCR_NOISE
    got = compare_outputs(a, b, noise_of(a))
    assert got.ok and {e.path for e in got.applied} == {"working.pdf", "ocr-words.json"}
    # Everything but the stamps is still compared: the words, and the PDF's own content.
    c = out("c", "cccc", "other words", b"(other words) Tj")
    assert [d.path for d in compare_outputs(a, c, noise_of(a)).differences] == [
        "ocr-words.json", "working.pdf"]


def test_an_exclusion_needs_a_reason():
    with pytest.raises(ValueError):
        Exclusion("working.pdf", "  ", bytes)


def _entry(root, rel, data=b"x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    (path.parent / f".{path.name}.lock").write_bytes(b"")   # cachedir's lock: not an entry
    return path


def test_snapshot_sees_entries_by_layout_and_not_locks(tmp_path):
    _entry(tmp_path, "ingest/src1/k1")
    _entry(tmp_path, "pages-words/src1/k2")
    assert set(snapshot(tmp_path)) == {"ingest/src1/k1", "pages-words/src1/k2"}


def test_an_unknown_stage_kind_is_an_error_not_a_guess(tmp_path):
    _entry(tmp_path, "fonts/src1/k1")
    with pytest.raises(ValueError, match="fonts"):
        StageEvents.between({}, snapshot(tmp_path))


def test_a_read_is_a_hit_and_a_new_entry_is_a_miss(tmp_path):
    read = _entry(tmp_path, "ingest/s/k1")
    _entry(tmp_path, "extract/s/k2")
    arm(tmp_path)
    before = snapshot(tmp_path)
    os.utime(read)                        # what stagecache._try_read does on a hit
    _entry(tmp_path, "pages/s/k3")        # what a miss writes
    events = StageEvents.between(before, snapshot(tmp_path))
    assert events.hit == {Stage.INGEST} and events.miss == {Stage.PAGES}


def test_an_entry_built_again_in_its_own_place_is_a_miss(tmp_path):
    entry = _entry(tmp_path, "extract/s/k1", b"old")
    arm(tmp_path)
    before = snapshot(tmp_path)
    # stagecache._cached on an unreadable entry: remove it, build, rename a new one into place
    entry.unlink()
    rebuilt = _entry(tmp_path, "extract/s/.k1.tmp", b"new")
    rebuilt.rename(entry)
    events = StageEvents.between(before, snapshot(tmp_path))
    assert events.miss == {Stage.EXTRACT} and events.hit == frozenset()


def test_plant_puts_the_donors_entry_at_the_targets_path(tmp_path):
    target = _entry(tmp_path / "t", "extract/s/key", b"right")
    _entry(tmp_path / "d", "extract/other/key2", b"wrong")
    pairs = plant_stale(tmp_path / "t", Stage.EXTRACT, tmp_path / "d")
    assert target.read_bytes() == b"wrong" and pairs == [("extract/s/key", "extract/other/key2")]


def test_a_plant_that_changes_nothing_is_a_setup_error(tmp_path):
    _entry(tmp_path / "t", "extract/s/key", b"same")
    _entry(tmp_path / "d", "extract/o/key", b"same")
    with pytest.raises(PlantError):
        plant_stale(tmp_path / "t", Stage.EXTRACT, tmp_path / "d")
    with pytest.raises(PlantError):
        plant_stale(tmp_path / "t", Stage.OCR, tmp_path / "d")   # nothing to plant


def test_the_edited_tree_changes_only_the_stages_module(tmp_path):
    tree = edited_tree(Stage.EXTRACT, tmp_path)
    live = EDITED_MODULE[Stage.EXTRACT]
    edited = tree / "reflow" / live.relative_to(live.parents[1])
    assert edited.read_bytes().startswith(live.read_bytes()) and edited.read_bytes() != live.read_bytes()
    other = tree / "reflow" / "convert.py"
    assert other.read_bytes() == (live.parents[1] / "convert.py").read_bytes()
    assert not list(tree.rglob("__pycache__"))
