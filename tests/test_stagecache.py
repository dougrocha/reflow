"""The opt-in stage cache: hits, invalidation by stage code, and corrupt entries."""

import json
import shutil
from pathlib import Path

import pikepdf
from conftest import extract_cached

from reflow import identity, stagecache
from reflow.ingest.ocr import OcrPage, OcrPlan
from reflow.stagecache import CachedRunner
from testkit import blank_pdf


class FakeRunner:
    name = "fake"
    grades = False

    def __init__(self) -> None:
        self.calls = 0

    def environment(self, langs):
        return "fake engine data"

    def run(self, src, dst, plan, langs, evidence) -> None:
        self.calls += 1
        blank_pdf(Path(dst), 2)
        for n in plan.pages:   # word evidence, as the grading plugin leaves it
            (Path(evidence) / f"{n}.json").write_text(f'{{"page": {n}, "run": {self.calls}}}')


def test_extract_is_keyed_by_the_working_copy_it_parsed(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    working = blank_pdf(tmp_path / "working.pdf")
    assert extract_cached(root, pdf, working)[1] is False
    assert extract_cached(root, pdf, working)[1] is True
    restored = tmp_path / "restored.pdf"   # from the ingest or OCR entry: the same bytes
    shutil.copyfile(working, restored)
    assert extract_cached(root, pdf, restored)[1] is True
    redone = tmp_path / "redone.pdf"       # OCR or qpdf run again: other bytes
    redone.write_bytes(working.read_bytes() + b"\n% another run\n")
    assert extract_cached(root, pdf, redone)[1] is False
    other = blank_pdf(tmp_path / "b.pdf", 2)
    assert extract_cached(root, other, working)[1] is False   # other source PDF


def test_extract_invalidated_by_extract_code(tmp_path, monkeypatch):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    extract_cached(root, pdf, pdf)
    monkeypatch.setattr(stagecache, "code_hash", lambda entries, root=None: "changed")
    assert extract_cached(root, pdf, pdf)[1] is False


def _kept(root: Path, kind: str) -> list[Path]:
    """The entries of a stage (its folders also hold the writers' locks and temp files)."""
    return [p for p in (root / kind).glob("*/*") if not p.name.startswith(".")]


def _code_changes_after(monkeypatch, hashes: int) -> None:
    """The code hash is the same for the first `hashes` calls, then another: an edit made while a
    stage builds, after its key was computed and before the check that follows the build."""
    calls = iter(range(hashes))
    monkeypatch.setattr(stagecache, "code_hash",
                        lambda entries, root=None: "before" if next(calls, None) is not None else "after")


def test_a_source_edit_during_a_build_keeps_its_result_out_of_the_cache(tmp_path, monkeypatch):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    entries = stagecache.EXTRACT_ENTRIES
    _code_changes_after(monkeypatch, 2)   # the key, then the check: no edit
    assert stagecache.cached(root, "extract", entries, pdf, lambda: "built") == ("built", False)
    assert len(_kept(root, "extract")) == 1
    monkeypatch.setattr(stagecache, "code_hash", lambda entries, root=None: "before")
    assert stagecache.cached(root, "extract", entries, pdf, lambda: "again")[1] is True

    other = blank_pdf(tmp_path / "b.pdf", 2)
    _code_changes_after(monkeypatch, 1)   # the key, then an edit before the check
    assert stagecache.cached(root, "extract", entries, other, lambda: "new") == ("new", False)
    assert len(_kept(root, "extract")) == 1   # the result is returned, and nothing is stored


def test_a_source_edit_during_ocr_keeps_its_output_out_of_the_cache(tmp_path, monkeypatch):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = FakeRunner()
    runner = CachedRunner(inner, root, pdf)
    _code_changes_after(monkeypatch, 1)
    runner.run(pdf, tmp_path / "o1.pdf", PLAN, ("eng",), _evidence(tmp_path, "e1"))
    assert inner.calls == 1 and (tmp_path / "o1.pdf").exists()   # the run's output is used
    assert not _kept(root, "ocr")
    monkeypatch.setattr(stagecache, "code_hash", lambda entries, root=None: "after")
    runner.run(pdf, tmp_path / "o2.pdf", PLAN, ("eng",), _evidence(tmp_path, "e2"))
    runner.run(pdf, tmp_path / "o3.pdf", PLAN, ("eng",), _evidence(tmp_path, "e3"))
    assert inner.calls == 2   # the first of these was a miss, and kept: the second hit


def _extract_entry(root: Path) -> Path:
    """The one entry file of the extract stage (its folder also holds the writer's lock)."""
    (entry,) = [p for p in (root / "extract").rglob("*") if p.is_file() and not p.name.startswith(".")]
    return entry


def test_corrupt_extract_entry_is_ignored(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    extract_cached(root, pdf, pdf)
    entry = _extract_entry(root)
    entry.write_bytes(entry.read_bytes()[:5])   # truncated
    assert extract_cached(root, pdf, pdf)[1] is False   # read again, entry rewritten
    assert extract_cached(root, pdf, pdf)[1] is True
    entry.write_bytes(b"not a pickle")
    assert extract_cached(root, pdf, pdf)[1] is False


def test_partial_temp_files_are_not_entries(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    extract_cached(root, pdf, pdf)
    entry = _extract_entry(root)
    assert not [p for p in entry.parent.iterdir() if p.name.startswith(".") and p.suffix != ".lock"]


PLAN = OcrPlan({1: OcrPage("no-text", "redo")})


def _evidence(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return path


def test_ocr_runner_hit_and_corrupt_entry(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = FakeRunner()
    runner = CachedRunner(inner, root, pdf)
    runner.run(pdf, tmp_path / "o1.pdf", PLAN, ("eng",), _evidence(tmp_path, "e1"))
    runner.run(pdf, tmp_path / "o2.pdf", PLAN, ("eng",), _evidence(tmp_path, "e2"))
    assert inner.calls == 1
    with pikepdf.open(tmp_path / "o2.pdf") as out:
        assert len(out.pages) == 2
    runner.run(pdf, tmp_path / "o3.pdf", PLAN, ("deu",), _evidence(tmp_path, "e3"))   # other languages
    assert inner.calls == 2
    for entry in [p for p in (root / "ocr").rglob("working.pdf")]:
        entry.write_bytes(b"garbage")
    runner.run(pdf, tmp_path / "o4.pdf", PLAN, ("eng",), _evidence(tmp_path, "e4"))   # corrupt: redone
    assert inner.calls == 3
    with pikepdf.open(tmp_path / "o4.pdf"):
        pass


def test_an_ocr_hit_restores_the_pdf_and_its_word_evidence(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = FakeRunner()
    runner = CachedRunner(inner, root, pdf)
    cold, warm = tmp_path / "cold", tmp_path / "warm"
    cold.mkdir()
    warm.mkdir()
    runner.run(pdf, tmp_path / "o1.pdf", PLAN, ("eng",), cold)
    runner.run(pdf, tmp_path / "o2.pdf", PLAN, ("eng",), warm)
    assert inner.calls == 1
    assert (tmp_path / "o1.pdf").read_bytes() == (tmp_path / "o2.pdf").read_bytes()
    assert (cold / "1.json").read_bytes() == (warm / "1.json").read_bytes()
    # An entry holding the PDF without its evidence is a miss.
    for entry in (root / "ocr").rglob("evidence"):
        shutil.rmtree(entry)
    again = tmp_path / "again"
    again.mkdir()
    runner.run(pdf, tmp_path / "o3.pdf", PLAN, ("eng",), again)
    assert inner.calls == 2 and (again / "1.json").exists()


def test_the_ocr_key_changes_with_a_page_s_mode_or_reason(tmp_path):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    inner = FakeRunner()
    runner = CachedRunner(inner, root, pdf)
    band = OcrPlan({1: OcrPage("textless-band", "redo", ((0, 10, 612, 200),))})
    for i, plan in enumerate((PLAN, OcrPlan({1: OcrPage("no-text", "force")}), band,
                              OcrPlan({1: OcrPage("textless-band", "redo", ((0, 50, 612, 90),))}))):
        runner.run(pdf, tmp_path / "out.pdf", plan, ("eng",), _evidence(tmp_path, f"e{i}"))
    assert inner.calls == 3   # the bands are not part of the key


def _prepare(pdf: Path, stage: Path, root: Path | None, **extra):
    from reflow.prepare import prepare
    from reflow.run import Options, Workers
    stage.mkdir(parents=True, exist_ok=True)
    return prepare(pdf, stage, Options(ocr="none", stage_cache=root, **extra),
                          Workers.serial())


def test_ingest_hit_reuses_working_copy_and_document(tmp_path, monkeypatch):
    from conftest import make_pdf

    from reflow.ingest import pages as ingest_pages
    pdf, root = make_pdf(tmp_path / "a.pdf", ["text", "text"]), tmp_path / "cache"
    first = _prepare(pdf, tmp_path / "s1", root)
    calls = []
    real = ingest_pages.ingest
    monkeypatch.setattr("reflow.ingest.pages.ingest", lambda *a, **k: calls.append(1) or real(*a, **k))
    second = _prepare(pdf, tmp_path / "s2", root)
    assert not calls   # not read again
    assert second.doc == first.doc
    assert (tmp_path / "s2" / "working.pdf").is_file()
    with pikepdf.open(tmp_path / "s2" / "working.pdf") as out:
        assert len(out.pages) == 2


def test_ingest_off_by_default(tmp_path):
    from conftest import make_pdf
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    _prepare(pdf, tmp_path / "s1", None)
    assert not (tmp_path / "cache").exists()


def test_ingest_keyed_on_bytes_and_name(tmp_path):
    from conftest import make_pdf
    pdf, root = make_pdf(tmp_path / "a.pdf", ["text"]), tmp_path / "cache"
    other = make_pdf(tmp_path / "b.pdf", ["text", "text"])
    assert _prepare(pdf, tmp_path / "s1", root).doc.meta["page_count"] == 1
    assert _prepare(other, tmp_path / "s2", root).doc.meta["page_count"] == 2
    copy = tmp_path / "renamed.pdf"
    copy.write_bytes(pdf.read_bytes())
    assert _prepare(copy, tmp_path / "s3", root).doc.meta["filename"] == "renamed.pdf"
    assert _prepare(pdf, tmp_path / "s4", root).doc.meta["filename"] == "a.pdf"


def test_corrupt_ingest_entries_are_misses(tmp_path):
    from conftest import make_pdf
    pdf, root = make_pdf(tmp_path / "a.pdf", ["text"]), tmp_path / "cache"
    _prepare(pdf, tmp_path / "s1", root)
    (entry,) = [d for d in (root / "ingest").rglob("*") if d.is_dir()
                and (d / "doc.pkl").exists()]
    for damage in (lambda: (entry / "doc.pkl").write_bytes(b"junk"),
                   lambda: (entry / "working.pdf").write_bytes(b"junk"),
                   lambda: (entry / "doc.pkl").unlink()):
        damage()
        run = _prepare(pdf, tmp_path / "s2", root)   # read again, entry rewritten
        assert run.doc.meta["page_count"] == 1
        with pikepdf.open(tmp_path / "s2" / "working.pdf"):
            pass
        assert (entry / "doc.pkl").exists()
        assert _prepare(pdf, tmp_path / "s3", root).doc.meta["page_count"] == 1


def test_ingest_partial_temp_dirs_are_not_entries(tmp_path):
    from conftest import make_pdf
    pdf, root = make_pdf(tmp_path / "a.pdf", ["text"]), tmp_path / "cache"
    _prepare(pdf, tmp_path / "s1", root)
    parent = next((root / "ingest").iterdir())
    entry = next(d for d in parent.iterdir() if not d.name.startswith("."))
    # Only the entry and its lock: the writer's temp dir was renamed into place.
    assert sorted(p.name for p in parent.iterdir()) == sorted([entry.name, f".{entry.name}.lock"])


def test_ingest_not_cached_with_a_password(tmp_path):
    from conftest import make_pdf
    pdf = make_pdf(tmp_path / "a.pdf", ["text"], password="pw")
    root = tmp_path / "cache"
    _prepare(pdf, tmp_path / "s1", root, password="pw")
    assert not (root / "ingest").exists()


def test_page_reader_shares_pages_between_tags_and_links_and_caches(tmp_path, monkeypatch):
    from conftest import make_tagged_pdf

    from reflow.extract import pagecontent
    from reflow.extract.pagecontent import PageReader
    pdf, root = make_tagged_pdf(tmp_path / "t.pdf"), tmp_path / "cache"
    parsed = []
    real = pagecontent.read_pages
    monkeypatch.setattr(pagecontent, "read_pages",
                        lambda p, numbers, words=False: parsed.append((tuple(numbers), words))
                        or real(p, numbers, words))
    stage = (root, pdf)
    reader = PageReader(pdf, jobs=1, stage=stage)
    first = reader.get([1, 2], words=True)
    assert reader.get([1, 2]) is not None and reader.get([2]).keys() == {2}
    assert parsed == [((1, 2), True)]            # the second ask is served from memory
    assert all(c.words is not None for c in first.values())
    again = PageReader(pdf, jobs=1, stage=stage).get([1, 2], words=True)   # new run: from disk
    assert parsed == [((1, 2), True)]
    assert {n: c.chars for n, c in again.items()} == {n: c.chars for n, c in first.items()}
    PageReader(pdf, jobs=1, stage=stage).get([1])                           # other pages: parsed
    assert parsed[-1] == ((1,), False)
    for entry in [p for p in (root / "pages-words").rglob("*") if p.is_file()]:
        entry.write_bytes(b"garbage")
    PageReader(pdf, jobs=1, stage=stage).get([1, 2], words=True)           # corrupt: a miss
    assert parsed[-1] == ((1, 2), True) and len(parsed) == 3


def test_extract_invalidated_by_a_package_upgrade(tmp_path, monkeypatch):
    pdf, root = blank_pdf(tmp_path / "a.pdf"), tmp_path / "cache"
    extract_cached(root, pdf, pdf)
    monkeypatch.setattr(identity, "tool_key", lambda: "pdfminer.six=newer")
    assert extract_cached(root, pdf, pdf)[1] is False


def test_page_reads_are_keyed_by_the_working_copy_they_parse(tmp_path, monkeypatch):
    from conftest import make_tagged_pdf

    from reflow.extract import pagecontent
    from reflow.extract.pagecontent import PageReader
    source, root = make_tagged_pdf(tmp_path / "t.pdf"), tmp_path / "cache"
    parsed = []
    real = pagecontent.read_pages
    monkeypatch.setattr(pagecontent, "read_pages",
                        lambda p, numbers, words=False: parsed.append(Path(p).name)
                        or real(p, numbers, words))
    first = tmp_path / "first.pdf"
    shutil.copyfile(source, first)
    PageReader(first, jobs=1, stage=(root, source)).get([1])
    restored = tmp_path / "restored.pdf"   # from the ingest or OCR entry: the same bytes
    shutil.copyfile(first, restored)
    PageReader(restored, jobs=1, stage=(root, source)).get([1])
    assert parsed == ["first.pdf"]
    redone = tmp_path / "redone.pdf"       # written again: qpdf gives it a fresh /ID
    with pikepdf.open(first) as pdf:
        pdf.save(redone)
    assert redone.read_bytes() != first.read_bytes()
    PageReader(redone, jobs=1, stage=(root, source)).get([1])
    assert parsed == ["first.pdf", "redone.pdf"]


def _published(out: Path) -> tuple[str, str]:
    """(document.json, manifest "v") of a conversion."""
    return (out / "document.json").read_text(), json.loads((out / "manifest.json").read_text())["v"]


def test_an_ocr_run_again_is_never_read_through_an_old_extraction(tmp_path, monkeypatch):
    # OCR output differs from run to run. With the OCR entry gone (eviction goes entry by entry)
    # OCR runs again with the same runner on the same pages; extraction and the page reads must
    # parse the copy it wrote, not hit with the reading of the copy the evicted entry held.
    from conftest import make_pdf
    from test_ocr import FakeRunner as StampingRunner

    from reflow.convert import convert
    pdf, root = make_pdf(tmp_path / "scan.pdf", ["text", "scan"]), tmp_path / "cache"
    runner = StampingRunner("first reading")
    monkeypatch.setattr("reflow.prepare.make_runner", lambda _: runner)
    convert(pdf, tmp_path / "one", page_images=False, stage_cache=root)
    assert "first reading" in _published(tmp_path / "one")[0]
    shutil.rmtree(root / "ocr")
    shutil.rmtree(root / "model")   # else the model it built is rendered again (reflow.saved)
    runner.text = "second reading"
    convert(pdf, tmp_path / "two", page_images=False, stage_cache=root)
    text, _ = _published(tmp_path / "two")
    assert "second reading" in text and "first reading" not in text
    assert runner.calls == [([2], ("eng",))] * 2


def test_cached_and_uncached_conversions_publish_the_same_version(tmp_path, monkeypatch):
    from conftest import make_pdf
    from test_ocr import FakeRunner as StampingRunner

    from reflow.analyze import engine
    from reflow.convert import convert
    pdf, root = make_pdf(tmp_path / "doc.pdf", ["text", "scan", "text"]), tmp_path / "cache"
    monkeypatch.setattr("reflow.prepare.make_runner", lambda _: StampingRunner())
    extracted = []
    real = engine.extract_document
    monkeypatch.setattr(engine, "extract_document",
                        lambda *a, **k: extracted.append(1) or real(*a, **k))
    published = {name: (convert(pdf, tmp_path / name, page_images=False, stage_cache=cache),
                        _published(tmp_path / name))[1]
                 for name, cache in (("off", None), ("cold", root), ("warm", root))}
    assert len(extracted) == 2   # the warm conversion read every stage from the cache
    assert published["off"] == published["cold"] == published["warm"]


def test_the_command_line_converts_through_the_stage_cache_only_when_asked(tmp_path):
    from conftest import make_pdf
    from typer.testing import CliRunner

    from reflow.cli import app
    pdf = make_pdf(tmp_path / "a.pdf", ["text"])
    for command in ("convert", "convert-many"):
        root, out = tmp_path / f"cache-{command}", tmp_path / f"out-{command}"
        target = ["-o", str(out)] if command == "convert" else [str(out)]
        common = [command, str(pdf), *target, "--jobs", "1", "--no-page-images", "--ocr", "none"]
        assert CliRunner().invoke(app, common).exit_code == 0
        assert not root.exists()
        result = CliRunner().invoke(app, [*common, "--stage-cache", str(root)])
        assert result.exit_code == 0, result.output
        assert {p.name for p in root.iterdir()} >= {"ingest", "extract"}
