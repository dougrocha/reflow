import json
import threading
import time

import pytest

from conftest import make_pdf
from reflow import convert as convert_module
from reflow import output
from reflow.analyze import review
from reflow.convert import Summary, convert, convert_many
from reflow.model import Document
from reflow.names import ALT_CACHE
from testkit import digests


def test_reconvert_removes_the_old_trace(tmp_path):
    pdf = make_pdf(tmp_path / "one.pdf", ["text"])
    out = tmp_path / "out"
    # Left over from an earlier conversion: a single-file trace and a page the new run lacks.
    (out / "trace").mkdir(parents=True)
    (out / "trace.json").write_text("{}")
    (out / "trace" / "0099.json").write_text("{}")

    convert(pdf, out, page_images=False)

    assert not (out / "trace.json").exists()
    assert not (out / "trace" / "0099.json").exists()
    assert (out / "trace" / "0001.json").exists()


def test_bad_options_leave_the_previous_output_alone(tmp_path):
    out = tmp_path / "out"
    convert(make_pdf(tmp_path / "one.pdf", ["first"]), out, page_images=False)
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}

    with pytest.raises(ValueError, match="unknown engine"):
        convert(make_pdf(tmp_path / "two.pdf", ["second"]), out, engine="invalid")

    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_a_failed_conversion_keeps_the_previous_output(tmp_path, monkeypatch):
    out = tmp_path / "out"
    convert(make_pdf(tmp_path / "one.pdf", ["first"]), out, page_images=False)
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}

    def broken(*args, **kwargs):
        raise RuntimeError("layout failed")
    monkeypatch.setattr("reflow.analyze.engine.analyze", broken)
    with pytest.raises(RuntimeError):
        convert(make_pdf(tmp_path / "two.pdf", ["second"]), out, page_images=False)

    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_reconvert_drops_files_the_new_output_does_not_have(tmp_path):
    out = tmp_path / "out"
    convert(make_pdf(tmp_path / "two.pdf", ["first", "second"]), out)
    assert (out / "pages" / "0002.webp").exists()
    (out / "figures").mkdir()
    (out / "figures" / "p0001-0.webp").write_bytes(b"old figure")
    (out / ALT_CACHE).write_text('{"kept": true}')

    convert(make_pdf(tmp_path / "one.pdf", ["only"]), out, page_images=False)

    assert not (out / "pages").exists()
    assert not (out / "figures" / "p0001-0.webp").exists()
    assert (out / ALT_CACHE).exists()


def test_refuses_an_output_holding_other_files(tmp_path):
    out = tmp_path / "mine"
    out.mkdir()
    (out / "notes.txt").write_text("not a conversion")

    with pytest.raises(ValueError, match="notes.txt"):
        convert(make_pdf(tmp_path / "one.pdf", ["text"]), out, page_images=False)

    assert [p.name for p in out.iterdir()] == ["notes.txt"]


def test_convert_returns_a_summary_derived_from_the_document(tmp_path):
    pdf = make_pdf(tmp_path / "one.pdf", ["text"], title="Hello Title")
    summary = convert(pdf, tmp_path / "out", page_images=False, ocr="none")
    assert isinstance(summary, Summary)
    assert (summary.title, summary.title_origin, summary.title_suspect) == ("Hello Title", "meta", False)
    assert (summary.pages, summary.figures, summary.tables, summary.engine) == (1, 0, 0, "heuristic")
    assert set(summary.review) == {"errors", "warnings", "pages"}
    assert "prior" in summary.facts and summary.ocr.runner == "none" and summary.ocr_missing == 0
    assert summary.alt_text["provider"] == "none" and summary.alt_text_errors == []
    # every pass is timed, so the timing file adds up to the run
    assert list(summary.seconds) == [
        "ingest", "text_trust", "font_map", "ocr", "extract", "layout", "ocr_marks", "prefetch", "evidence", "order",
        "structure", "scan_noisy", "figures", "alt_text", "empty_pages", "missing_words", "review", "model", "publish"]
    untitled = convert(make_pdf(tmp_path / "plain.pdf", ["text"]), tmp_path / "out2",
                       page_images=False, ocr="none")
    assert (untitled.title, untitled.title_origin) == ("plain", "filename")


def test_publish_writes_every_derived_file_once(tmp_path):
    pdf = make_pdf(tmp_path / "one.pdf", ["text"])
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)

    assert (out / "layout" / "0001.json").is_file()
    assert (out / "working.pdf").is_file()
    assert (out / "original.pdf").read_bytes() == pdf.read_bytes()
    body = (out / "document.json").read_bytes()
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["v"]) == 12 and int(manifest["v"], 16) >= 0
    doc = Document.from_json(body.decode("utf-8"))
    assert json.loads((out / "review.json").read_text()) == review.items(doc)
    assert [p.label for p in doc.pages] == ["1"]
    assert doc.root.attrs["title"] == "one"


def _published(out):
    """The version, and every file that carries it, of a published output."""
    v = json.loads((out / "manifest.json").read_text())["v"]
    stamped = [p for p in out.rglob("*") if p.is_file() and output.VERSION_SLOT.encode() in p.read_bytes()]
    assert not stamped, stamped
    return v


def _also_write(change):
    """render_html, followed by `change(out_dir)`: rendered output that differs, and a model that doesn't."""
    real = output.render_html

    def render(doc, title, out_dir, **kw):
        site = real(doc, title, out_dir, **kw)
        change(out_dir)
        return site
    return render


@pytest.mark.parametrize("change", [
    lambda out: (out / "assets" / "viewer.css").write_text("p { color: red }"),
    lambda out: [c.write_text(c.read_text().replace("</main>", "<p>new</p></main>"))
                 for c in (out / "chunks").glob("*.html")],
], ids=["stylesheet", "renderer"])
def test_version_follows_the_rendered_output_not_only_the_model(tmp_path, monkeypatch, change):
    pdf = make_pdf(tmp_path / "one.pdf", ["text", "text"])
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    first, body = _published(out), (out / "document.json").read_bytes()
    convert(pdf, out, page_images=False)
    assert _published(out) == first  # nothing rendered changed: the cached files stay valid

    monkeypatch.setattr(output, "render_html", _also_write(change))
    convert(pdf, out, page_images=False)
    second = _published(out)
    assert (out / "document.json").read_bytes() == body
    assert second != first
    manifest_chunks = json.loads((out / "manifest.json").read_text())["chunks"]
    index = (out / "index.html").read_text()
    assert f"viewer.css?v={second}" in index and first not in index
    assert all(f"{c['href']}?v={second}" in index for c in manifest_chunks)
    header = json.loads((out / "passages.jsonl").read_text().splitlines()[0])
    assert header["v"] == second


def test_bad_ocr_mode_is_rejected_before_any_work(tmp_path):
    with pytest.raises(ValueError, match="unknown OCR mode"):
        convert(make_pdf(tmp_path / "one.pdf", ["text"]), tmp_path / "out", ocr="tesseract")
    assert not (tmp_path / "out").exists()


def test_convert_many_on_one_pool_matches_serial_output(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", ["text", "text", "text"])
    b = make_pdf(tmp_path / "b.pdf", ["text", "empty"], title="Bee")
    convert(a, tmp_path / "ref-a", jobs=1, page_images=False)
    convert(b, tmp_path / "ref-b", jobs=1, page_images=False)
    results = convert_many([(a, tmp_path / "a"), (b, tmp_path / "b")], jobs=2, chunk_pages=1,
                           page_images=False)
    assert [r.ok for r in results] == [True, True]
    assert results[1].summary.title == "Bee"
    for name in ("a", "b"):
        assert digests(tmp_path / name) == digests(tmp_path / f"ref-{name}")


@pytest.mark.parametrize("options, serial", [
    ({"assist": "local", "device": "cpu"}, True),
    ({"alt_text": "local", "device": "cpu"}, True),
    ({"engine": "hybrid", "device": "cpu"}, True),
    ({"engine": "direct"}, True),
    ({"alt_text": "ollama"}, True),
    ({"assist": "bogus"}, False),
    ({"assist_labels_max_calls": None}, False),
    ({}, False),
])
def test_convert_many_runs_one_document_at_a_time_when_it_uses_a_model(
        tmp_path, monkeypatch, options, serial):
    """A model in this process, or a model server every page asks, is not made for two
    documents at once; an option that does not validate is `convert`'s to report."""
    active, most, lock = 0, 0, threading.Lock()

    def fake(pdf, out_dir, **_):
        nonlocal active, most
        with lock:
            active += 1
            most = max(most, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    monkeypatch.setattr(convert_module, "convert", fake)
    docs = [(tmp_path / f"{i}.pdf", tmp_path / f"{i}") for i in range(3)]
    convert_many(docs, jobs=2, max_docs=3, **options)
    assert (most == 1) is serial


def _review(out):
    """review.json without the title item: make_pdf files have no metadata title, so every
    one of them falls back to its file name and is flagged (see test_title.py)."""
    items = json.loads((out / "review.json").read_text())
    return [i for i in items if i["needs"] != "title"]


def test_scanned_pages_without_ocr_are_flagged_and_both_pdfs_are_written(tmp_path):
    pdf = make_pdf(tmp_path / "mixed.pdf", ["text", "scan"])
    out = tmp_path / "out"
    summary = convert(pdf, out, ocr="none", page_images=False)

    assert (out / "working.pdf").is_file()
    assert (out / "original.pdf").read_bytes() == pdf.read_bytes()
    assert [(i["role"], i["page"], i["severity"], i["needs"], i["label"]) for i in _review(out)] == [
        ("doc", None, "error", None, "Document has image-only pages"),      # ocr-doc
        ("page", 2, "error", "ocr", "Image-only page without OCR")]        # ocr-missing
    assert (summary.ocr.runner, summary.ocr.attempted, summary.ocr.done) == ("none", [2], [])
    assert summary.ocr_missing == 1
    assert "ocr" in summary.seconds


def test_a_runner_recognizes_scanned_pages_and_the_page_describes_the_ocrd_copy(tmp_path, monkeypatch):
    from test_ocr import FakeRunner
    runner = FakeRunner()
    monkeypatch.setattr("reflow.prepare.make_runner", lambda name: runner)
    pdf = make_pdf(tmp_path / "mixed.pdf", ["text", "scan", "empty"])
    out = tmp_path / "out"
    summary = convert(pdf, out, page_images=False)

    assert runner.calls == [([2], ("eng",))]
    assert (summary.ocr.runner, summary.ocr.done, summary.ocr.failed) == ("fake", [2], {})
    assert (summary.ocr_missing, summary.facts.get("ocr-page")) == (0, 1)
    doc = Document.from_json((out / "document.json").read_text())
    page = doc.pages[1]
    assert [f.code for f in page.facts] == ["ocr-page"]
    assert page.kind.value == "mixed" and page.char_count > 0   # the OCR'd working copy
    assert doc.pages[0].facts == [] and doc.pages[2].facts == []
    assert "Recognized words" in (out / "index.html").read_text()
    assert not any(i["role"] in ("doc", "page") for i in _review(out))   # no ocr-missing, no ocr-doc
    assert (out / "original.pdf").read_bytes() == pdf.read_bytes()   # the original is never OCR'd


def test_an_owner_locked_pdf_is_refused_and_leaves_no_output(tmp_path):
    from reflow.prepare import RestrictedPdf
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], owner_only=True)
    out = tmp_path / "out"
    with pytest.raises(RestrictedPdf, match="forbids text extraction"):
        convert(pdf, out, page_images=False)
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_allow_restricted_converts_and_flags_the_document(tmp_path):
    pdf = make_pdf(tmp_path / "locked.pdf", ["text"], owner_only=True)
    out = tmp_path / "out"
    summary = convert(pdf, out, allow_restricted=True, page_images=False)
    assert summary.facts["extract-forbidden"] == 1
    assert [(i["role"], i["severity"], i["needs"], i["label"]) for i in _review(out) if i["role"] == "doc"] == [
        ("doc", "error", "extract", "The PDF forbids text extraction")]
    assert (out / "original.pdf").read_bytes() == pdf.read_bytes()
    assert summary.review["errors"] >= 1


def test_the_files_declared_page_labels_reach_document_json(tmp_path):
    import pikepdf
    from pikepdf import Array, Dictionary, Name
    pdf = make_pdf(tmp_path / "labelled.pdf", ["text", "text", "text"])
    with pikepdf.open(pdf, allow_overwriting_input=True) as doc:
        doc.Root.PageLabels = Dictionary(Nums=Array([
            0, Dictionary(S=Name.r), 2, Dictionary(S=Name.D, St=1)]))
        doc.save(pdf)
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    doc = Document.from_json((out / "document.json").read_text())
    assert [p.label for p in doc.pages] == ["i", "ii", "1"]
