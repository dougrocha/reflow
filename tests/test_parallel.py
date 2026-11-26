from pathlib import Path

import pytest

from conftest import make_pdf
from reflow.convert import convert, convert_many
from reflow.extract.pages import extract_pages
from reflow.extract.parallel import chunk_ranges, extract_chunk, extract_document
from reflow.extract.weights import page_weights, weighted_chunks
from testkit import digests

SAMPLES = Path(__file__).parent.parent / "samples"


def test_chunk_ranges_cover_every_page_once_in_order():
    for count in (0, 1, 19, 20, 21, 40, 41, 1298):
        ranges = chunk_ranges(count)
        pages = [n for a, b in ranges for n in range(a, b + 1)]
        assert pages == list(range(1, count + 1))
        assert all(b - a + 1 <= 20 for a, b in ranges)
    assert chunk_ranges(45) == [(1, 20), (21, 40), (41, 45)]
    assert chunk_ranges(5, size=2) == [(1, 2), (3, 4), (5, 5)]


def test_chunk_ranges_size_follows_the_job_count():
    def sizes(count, **kw):
        return {b - a + 1 for a, b in chunk_ranges(count, **kw)}

    # Four chunks per worker: ceil(pages / (jobs * 4)) pages each, and at least one.
    assert chunk_ranges(142, jobs=8)[:2] == [(1, 5), (6, 10)]
    assert chunk_ranges(142, jobs=8)[-1] == (141, 142)
    assert len(chunk_ranges(142, jobs=8)) == 29
    assert max(sizes(1298, jobs=8)) == 41
    assert chunk_ranges(7, jobs=8) == [(n, n) for n in range(1, 8)]  # fewer pages than 4 per job
    assert chunk_ranges(0, jobs=4) == []
    assert chunk_ranges(10, jobs=1) == [(1, 3), (4, 6), (7, 9), (10, 10)]
    # An explicit size wins; with neither, the fixed default of 20 pages applies.
    assert chunk_ranges(142, size=50, jobs=8) == [(1, 50), (51, 100), (101, 142)]
    assert chunk_ranges(45, jobs=None) == [(1, 20), (21, 40), (41, 45)]
    for count, jobs in ((1, 8), (31, 3), (142, 8), (1298, 16)):
        pages = [n for a, b in chunk_ranges(count, jobs=jobs) for n in range(a, b + 1)]
        assert pages == list(range(1, count + 1))


def _pdf_with_text_lengths(path: Path, lengths: list[int]) -> Path:
    import pikepdf
    from pikepdf import Dictionary, Name

    pdf = pikepdf.new()
    font = pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica))
    for n in lengths:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = Dictionary(Font=Dictionary(F1=font))
        page.Contents = pdf.make_stream(
            b"BT /F1 12 Tf 72 720 Td (" + b"word " * n + b") Tj ET")
    pdf.save(path)
    return path


def test_weighted_chunks_cover_every_page_once_in_order_and_close_near_their_share():
    weights = [10] * 20 + [100] * 6 + [10] * 20
    ranges = weighted_chunks(weights, 8)
    assert [n for a, b in ranges for n in range(a, b + 1)] == list(range(1, 47))
    share = sum(weights) / 8
    for a, b in ranges:   # a run closes at its share: it overshoots by at most its last page
        assert sum(weights[a - 1:b - 1]) < share
    assert sum(1 for a, b in ranges if a <= 26 and b >= 21) >= 4   # the heavy pages are spread out
    assert weighted_chunks([5, 5, 5], 1) == [(1, 3)]
    assert weighted_chunks([5, 5, 5], 99) == [(1, 1), (2, 2), (3, 3)]
    assert weighted_chunks([], 4) == []


def test_page_weights_are_the_declared_content_sizes(tmp_path):
    pdf = _pdf_with_text_lengths(tmp_path / "p.pdf", [1, 300, 1])
    weights = page_weights(pdf, 3)
    assert weights is not None and weights[1] > weights[0] == weights[2] > 0
    assert page_weights(pdf, 4) is None                      # not the page count the caller expects
    assert page_weights(tmp_path / "missing.pdf", 3) is None
    assert page_weights(make_pdf(tmp_path / "blank.pdf", ["empty", "empty"]), 2) is None


def test_extract_document_cuts_by_weight_and_returns_pages_in_order(tmp_path):
    pdf = _pdf_with_text_lengths(tmp_path / "p.pdf", [1, 1, 1, 400, 400, 1, 1, 1])
    seen = []
    weights = page_weights(pdf, 8)
    pages, tables = extract_document(pdf, 8, jobs=2, weights=weights, on_pages=seen.append)
    assert [p.number for p in pages] == list(range(1, 9))
    assert sum(seen) == 8 and len(seen) > 2
    assert (pages, tables) == extract_document(pdf, 8, jobs=1)


def test_extract_document_sizes_its_chunks_from_the_job_count(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [f"page {i}" for i in range(1, 8)])
    seen = []
    pages, tables = extract_document(pdf, 7, jobs=2, on_pages=seen.append)  # ceil(7 / 8) = 1
    assert seen == [1] * 7
    assert (pages, tables) == extract_document(pdf, 7, jobs=1)


def test_extract_pages_range_matches_the_whole_run(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [f"page {i}" for i in range(1, 8)])
    whole = list(extract_pages(pdf))
    assert [p.number for p in extract_pages(pdf, 3, 5)] == [3, 4, 5]
    assert list(extract_pages(pdf, 3, 5)) == whole[2:5]
    assert [p.number for p in extract_pages(pdf, 6, 99)] == [6, 7]


def test_chunks_reassemble_in_page_order(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [f"page {i}" for i in range(1, 8)])
    serial_pages, serial_tables = extract_chunk(pdf, 1, 7)
    parts = [extract_chunk(pdf, a, b) for a, b in chunk_ranges(7, 3)]
    assert [p for part, _ in parts for p in part] == serial_pages
    assert {n: t for _, tables in parts for n, t in tables.items()} == serial_tables


def test_extract_document_in_a_pool_matches_serial(tmp_path):
    pdf = make_pdf(tmp_path / "p.pdf", [f"page {i}" for i in range(1, 8)])
    seen = []
    pages, tables = extract_document(pdf, 7, jobs=2, chunk_pages=2, on_pages=seen.append)
    assert (pages, tables) == extract_document(pdf, 7, jobs=1)
    assert [p.number for p in pages] == list(range(1, 8))
    assert seen == [2, 2, 2, 1]


def test_jobs_2_output_is_identical_to_jobs_1_on_fw9(tmp_path):
    pdf = SAMPLES / "fw9.pdf"
    convert(pdf, tmp_path / "one", page_images=False, jobs=1)
    convert(pdf, tmp_path / "two", page_images=False, jobs=2, chunk_pages=2)
    one, two = digests(tmp_path / "one"), digests(tmp_path / "two")
    assert one.keys() == two.keys()
    assert {k for k in one if one[k] != two[k]} == set()
    assert "document.json" in one


def test_convert_many_matches_convert_per_document(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", [f"alpha {i}" for i in range(1, 6)])
    b = SAMPLES / "fw9.pdf"
    convert(a, tmp_path / "ref-a", jobs=1)  # page images included: they must match too
    convert(b, tmp_path / "ref-b", jobs=1)
    events = []
    results = convert_many([(a, tmp_path / "a"), (b, tmp_path / "b")], jobs=2, chunk_pages=2,
                           on_done=lambda i, r: events.append(i))
    assert [r.ok for r in results] == [True, True]
    assert sorted(events) == [0, 1]
    for name in ("a", "b"):
        got, ref = digests(tmp_path / name), digests(tmp_path / f"ref-{name}")
        assert got == ref
        assert any(k.startswith("pages/") for k in got)


def test_convert_many_one_failure_does_not_stop_the_other(tmp_path):
    good = make_pdf(tmp_path / "good.pdf", ["one", "two", "three"])
    bad = tmp_path / "bad.pdf"
    bad.write_text("this is not a pdf")
    results = convert_many([(bad, tmp_path / "bad"), (good, tmp_path / "good")], jobs=2,
                           chunk_pages=1, page_images=False)
    assert not results[0].ok and results[0].error is not None
    assert results[1].ok and results[1].summary.pages == 3
    assert (tmp_path / "good" / "manifest.json").exists()


def test_convert_many_cli(tmp_path):
    from typer.testing import CliRunner

    from reflow.cli import app
    good = make_pdf(tmp_path / "good.pdf", ["one", "two"])
    bad = tmp_path / "bad.pdf"
    bad.write_text("nope")
    runner = CliRunner()
    r = runner.invoke(app, ["convert-many", str(good), str(tmp_path / "g"), str(bad),
                            str(tmp_path / "b"), "-j", "2", "--no-page-images"])
    assert r.exit_code == 1, r.output
    lines = [ln.split("\t") for ln in r.output.splitlines() if "\t" in ln]
    assert any(ln[:2] == ["ok", str(tmp_path / "g")] for ln in lines)
    assert any(ln[0] == "FAILED" and ln[1] == str(tmp_path / "b") for ln in lines)
    assert runner.invoke(app, ["convert-many", str(good)]).exit_code == 2


def test_a_crashed_worker_gets_the_later_documents_a_fresh_pool():
    import os
    from concurrent.futures.process import BrokenProcessPool

    import pytest

    from reflow.convert import _PoolHolder
    holder = _PoolHolder(1)
    try:
        broken = holder.get()
        with pytest.raises(BrokenProcessPool):
            broken.submit(os._exit, 1).result()
        holder.replace(broken)
        holder.replace(broken)  # a second document reporting the same pool is harmless
        fresh = holder.get()
        assert fresh is not broken
        assert fresh.submit(int, "3").result() == 3
    finally:
        holder.close()


@pytest.mark.parametrize("isolate", [False, True])
def test_a_failed_document_carries_its_traceback_as_text(tmp_path, isolate):
    from reflow.prepare import SourceUnreadable
    bad = tmp_path / "bad.pdf"
    bad.write_text("this is not a pdf")
    [result] = convert_many([(bad, tmp_path / "bad")], jobs=1, isolate=isolate, page_images=False)
    assert not result.ok
    assert isinstance(result.error, SourceUnreadable) and result.error.__traceback__ is None
    assert result.traceback_text.startswith("Traceback (most recent call last):")
    assert "in prepare" in result.traceback_text      # the frames that raised it
    assert "SourceUnreadable: " in result.traceback_text


def test_convert_many_cli_prints_the_traceback_of_a_failed_document(tmp_path):
    from typer.testing import CliRunner

    from reflow.cli import app
    bad = tmp_path / "bad.pdf"
    bad.write_text("nope")
    r = CliRunner().invoke(app, ["convert-many", str(bad), str(tmp_path / "b"), "-j", "1",
                                 "--no-page-images"])
    assert r.exit_code == 1, r.output
    assert "Traceback (most recent call last):" in r.output
    assert "in prepare" in r.output
