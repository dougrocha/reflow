"""The manifest of real badly made PDFs (samples/robustness/MANIFEST.tsv) and how the robustness
scorecard reports them, without converting anything."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from robustness import harness, real
from robustness.kinds import KINDS
from robustness.real import MANIFEST, REPO, RealResult, family_of, judge_real, load, problems

KIND_IDS = {k.id for k in KINDS}
ROWS = load(MANIFEST)


def test_the_committed_manifest_has_no_problems():
    assert ROWS
    assert problems(ROWS, KIND_IDS) == []


def test_a_made_up_damage_id_is_a_problem():
    made_up = dataclasses.replace(ROWS[0], damage_id="tounicode-sideways")
    assert problems([made_up], KIND_IDS) == [
        f"{made_up.id}: damage_id 'tounicode-sideways' is not a kind in KINDS"]


@pytest.mark.parametrize("damage_id", sorted({r.damage_id for r in ROWS if r.damage_id}))
def test_removing_a_kind_a_row_names_is_a_problem(damage_id):
    """A twin kind can't be renamed or dropped while a manifest row still names it."""
    named = [r.id for r in ROWS if r.damage_id == damage_id]
    assert problems(ROWS, KIND_IDS - {damage_id}) == [
        f"{row}: damage_id {damage_id!r} is not a kind in KINDS" for row in named]


def test_the_first_rows_are_the_samples_the_issue_names():
    by_path = {r.path: r for r in ROWS}
    assert "samples/azblue/site/hca-nondiscrim-multilang.pdf" in by_path   # the Navajo file (#124)
    assert any(r.family == "Adobe PDF Library" and r.path.startswith("samples/corpus/") for r in ROWS)
    assert any("no tag tree" in r.quirk for r in ROWS)


def test_no_file_that_may_not_be_redistributed_is_committed():
    tracked = set(subprocess.run(["git", "ls-files", "samples"], cwd=REPO, capture_output=True,
                                 text=True, check=True).stdout.splitlines())
    for r in ROWS:
        if r.redistribute == "no":
            assert r.path not in tracked, r.id
            ignored = subprocess.run(["git", "check-ignore", "-q", r.path], cwd=REPO)
            assert ignored.returncode == 0, f"{r.id}: git does not ignore {r.path}"
        elif r.committed:
            assert (REPO / r.path).is_file(), f"{r.id}: {r.path} is not in the checkout"
            ignored = subprocess.run(["git", "check-ignore", "-q", r.path], cwd=REPO)
            assert ignored.returncode == 1, f"{r.id}: git ignores {r.path}, so it is not committed"


def test_each_committed_file_is_the_one_the_manifest_hashes():
    import hashlib
    for r in ROWS:
        if r.committed:
            assert hashlib.sha256((REPO / r.path).read_bytes()).hexdigest() == r.sha256, r.id
        if r.transcript:
            assert (REPO / r.transcript).is_file(), r.id


@pytest.mark.parametrize(("producer", "creator", "family"), [
    ("Adobe PDF Library 17.0", "Adobe InDesign 18.1 (Macintosh)", "Adobe PDF Library"),
    ("Adobe PDF Library 25.1.108", "Acrobat PDFMaker 25 for Word", "Adobe PDF Library"),
    ("Microsoft® Word for Microsoft 365", "Microsoft® Word for Microsoft 365", "Microsoft Word"),
    ("Microsoft: Print To PDF", None, "Microsoft Print to PDF"),
    ("Acrobat Distiller 19.0 (Windows)", "Canva", "Canva"),
    ("PDF-XChange 4.0.163.0 [ABBYY] (Windows Seven)", None, "ABBYY"),
    ("GPL Ghostscript 8.54", "dvips(k) 5.86e Copyright 2001 Radical Eye Software", "dvips"),
    ("Acrobat Distiller 20.0 (Windows); modified using iText® Core 9.4.0", "ACOMP.exe", "Acrobat Distiller"),
    ("iText® 7.1.12 ©2000-2020 iText Group NV", None, "iText"),
    ("pdfTeX-1.40.25", "LaTeX with hyperref", "pdfTeX"),
    ("Skia/PDF m53", "Chromium", "Skia"),
    ("macOS Version 14.6.1 (Build 23G93) Quartz PDFContext", "Aspose Ltd.", "Quartz"),
    ("Designer 6.5", "Designer 6.5", "Adobe Designer"),
    ("typeset.sh", None, "other"),
    (None, "", "unknown"),
])
def test_family_of(producer, creator, family):
    assert family_of(producer, creator) == family


def _converted(tmp_path: Path, text: str, meta: dict, codes: list[str]) -> Path:
    """A conversion folder with one page of `text`, and `codes` raised on page 1."""
    out = tmp_path / "out"
    (out / "chunks").mkdir(parents=True)
    (out / "chunks/1.html").write_text(f'<div class="pg" data-page="1"><p>{text}</p></div>',
                                       encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"lang": "en", "chunks": [{"href": "chunks/1.html"}]}),
                                       encoding="utf-8")
    doc = {"meta": meta, "pages": [{"number": 1, "facts": [{"code": c} for c in codes]}],
           "root": {"children": []}}
    (out / "document.json").write_text(json.dumps(doc), encoding="utf-8")
    return out


def test_a_transcribed_row_is_scored_and_the_family_comes_from_the_document(tmp_path):
    row = next(r for r in ROWS if r.transcript)
    meta = {"producer": "Acrobat Distiller 19.0 (Windows)", "creator": "Canva"}
    want = {1: ("hello", "world")}
    same = judge_real(row, _converted(tmp_path / "a", "Hello World", meta, []), want, "text-layer-suspect")
    assert (same.outcome, same.met, same.family) == ("preserved", True, "Canva")
    lost = judge_real(row, _converted(tmp_path / "b", "(ELLO7ORLD", meta, []), want, "text-layer-suspect")
    assert (lost.outcome, lost.met) == ("silent loss", False)
    assert lost.detail.startswith("text-layer-suspect not raised; page 1 text:")
    caught = judge_real(row, _converted(tmp_path / "c", "(ELLO7ORLD", meta, ["text-layer-suspect"]),
                        want, "text-layer-suspect")
    assert (caught.outcome, caught.met) == ("caught", True)


def test_a_row_without_a_transcript_is_inventory(tmp_path):
    row = next(r for r in ROWS if not r.transcript)
    res = judge_real(row, _converted(tmp_path, "anything", {"producer": None, "creator": None},
                                     ["unicode-errors", "unicode-errors"]), None, "unicode-errors")
    assert (res.outcome, res.met, res.family) == ("inventory", None, "unknown")
    assert res.codes == Counter({"unicode-errors": 2})


def test_a_crash_on_a_transcribed_file_is_a_scored_miss():
    transcribed = next(r for r in ROWS if r.transcript)
    other = next(r for r in ROWS if not r.transcript)
    assert (RealResult(transcribed, "crashed").scored, RealResult(transcribed, "crashed").met) == (True, False)
    assert RealResult(other, "crashed").met is None
    assert RealResult(transcribed, "absent").met is None


def test_read_transcript_splits_pages_on_form_feeds(tmp_path):
    t = tmp_path / "t.txt"
    t.write_text("Hello World\n\fSecond page", encoding="utf-8")
    assert real.read_transcript(t) == {1: ("hello", "world"), 2: ("second", "page")}


def test_the_scorecard_groups_real_files_by_family_with_denominators():
    rows = [RealResult(ROWS[0], "inventory", "Microsoft: Print To PDF", None, "Microsoft Print to PDF",
                       Counter({"ocr-page": 3})),
            RealResult(ROWS[1], "silent loss", "Canva", "Canva", "Canva", Counter(), "words differ"),
            RealResult(ROWS[2], "absent", detail="not here")]
    record = [{"id": r.row.id, "manifest_family": r.row.family, "family": r.family,
               "producer": r.producer, "creator": r.creator, "sha256": r.row.sha256,
               "damage_id": r.row.damage_id, "quirk": r.row.quirk, "transcript": bool(r.row.transcript),
               "outcome": r.outcome, "met": r.met, "codes": dict(r.codes), "detail": r.detail,
               "seconds": 0} for r in rows]
    text = "\n".join(harness.real_scorecard(record))
    assert "| Microsoft Print to PDF | 1 | 1 | 0 | 0 | 1/1 | 0 | 0/0 |" in text
    assert "| Canva | 1 | 1 | 0 | 0 | 0/1 | 1 | 0/1 |" in text
    assert f"| {ROWS[2].family} | 1 | 0 | 0 | 1 | 0/0 | 0 | 0/0 |" in text
    assert "not how common a quirk is" in text
    assert "| all | 3 | 2 | 0 | 1 | 1/2 | 1 | 0/1 |" in text
    # a family with no file is named as unmeasured, never given a count
    assert "No file here comes from " in text and "Skia" in text.split("No file here comes from ")[1]
    assert "| Skia |" not in text
    # The Document's family wins over the manifest's, and the difference is shown.
    assert f"Microsoft Print to PDF (manifest: {ROWS[0].family})" in text
