"""Unit tests for the page check helpers. Fast and unmarked, so the default run includes them."""

from __future__ import annotations

import json
from html import escape
from pathlib import Path
from types import SimpleNamespace

import pytest

from checks import CHECKS, SHELL_CHECKS, Assertion, CheckFailed, parse_html, recall, run_check
from corpus import DOCUMENTS, source_text
from specs import PageSpec, load_specs


def test_every_corpus_document_has_a_page_spec():
    assert set(DOCUMENTS) == {spec.doc for spec in load_specs()}


@pytest.mark.parametrize("keyword, slugs", [
    ("resnet", ["resnet"]),
    ("counted", ["docaccess-assessment-appeal", "docaccess-rfp", "p17"]),
    ("", None),
])
def test_mark_layer_prepares_only_the_selected_documents(keyword, slugs):
    """`-k resnet` converts resnet alone, the counted test its three documents, a run without
    -k every document of the module."""
    import test_mark_layer

    class Collected:
        def pytest_collection_finish(self, session):
            self.slugs = test_mark_layer.selected_slugs(session.items)

    collected = Collected()
    args = ["--collect-only", "-q", "-m", "pages", "-p", "no:cacheprovider",
            str(Path(test_mark_layer.__file__))]
    assert pytest.main(args + (["-k", keyword] if keyword else []), plugins=[collected]) == 0
    assert collected.slugs == (slugs or test_mark_layer.SLUGS)


@pytest.mark.parametrize("source", ["", " \r\n\t", "--- …"])
def test_recall_rejects_a_source_without_word_tokens(source):
    with pytest.raises(CheckFailed, match="^no source text on this page$"):
        recall(source, "Recovered text")


def test_numeric_recall_floor_is_unchanged():
    pg = parse_html("<p>alpha beta</p>")
    run_check(Assertion("min_recall", 50), pg, lambda: "alpha beta gamma delta")
    with pytest.raises(CheckFailed, match="word recall 50.0 < floor 51"):
        run_check(Assertion("min_recall", 51), pg, lambda: "alpha beta gamma delta")


def test_reference_recall_uses_its_floor():
    pg = parse_html("<p>alpha beta</p>")
    value = {"floor": 50, "reference": {"doc": "fw9", "page": 1}}
    run_check(Assertion("min_recall", value), pg, lambda: "alpha beta gamma delta")
    with pytest.raises(CheckFailed, match="word recall 50.0 < floor 51"):
        run_check(Assertion("min_recall", {**value, "floor": 51}), pg,
                  lambda: "alpha beta gamma delta")


def test_source_text_reads_a_pdf_text_layer():
    assert "Request for Taxpayer" in source_text("fw9", 1)
    assert source_text("scan", 1) == ""


def test_page_runner_reads_the_reference_without_converting_it(monkeypatch):
    import test_pages as page_tests

    a = Assertion("min_recall", {"floor": 100, "reference": {"doc": "fw9", "page": 1}})
    spec = PageSpec("reference-1", "scan", 1, "Reference recall", [a])
    monkeypatch.setattr(page_tests, "SPECS", {spec.id: spec})
    pg = parse_html(f"<p>{escape(source_text('fw9', 1))}</p>")

    def doc(slug):
        assert slug == "scan", "the reference document must not be converted"
        return SimpleNamespace(page=lambda number: pg, pdf_text=lambda number: "", shell=None,
                               page_map=None)

    page_tests.test_page(SimpleNamespace(doc=doc), spec.id, 0)


def test_report_evaluates_reference_recall_on_a_scan(monkeypatch, capsys, tmp_path):
    import report

    a = Assertion("min_recall", {"floor": 60, "reference": {"doc": "fw9", "page": 1}},
                  xfail="No OCR runs in this environment")
    spec = PageSpec("scan-1", "scan", 1, "Scanned form", [a])
    doc = SimpleNamespace(page=lambda number: parse_html("<p></p>"),
                          pdf_text=lambda number: "", shell=None, page_map=None,
                          out_dir=tmp_path)
    monkeypatch.setattr(report, "Corpus", lambda: SimpleNamespace(
        doc=lambda slug: doc, prepare=lambda *args, **kwargs: None, quick=False))
    monkeypatch.setattr(report, "load_specs", lambda only: [spec])

    assert report.main(["-v", "scan"]) == 0
    output = capsys.readouterr().out
    assert "word recall 0.0 < floor 60" in output
    assert "ok (known open)" in output
    assert "ERROR" not in output


@pytest.mark.parametrize(("value", "message"), [
    ({"floor": 60, "reference": {"doc": "missing", "page": 1}}, "unknown reference doc"),
    ({"floor": 60, "reference": {"doc": "fw9", "page": 0}}, "positive integer"),
    ({"floor": 60, "reference": {"doc": "fw9", "page": 1.5}}, "positive integer"),
    ({"floor": "sixty", "reference": {"doc": "fw9", "page": 1}}, "floor must be a number"),
])
def test_load_specs_rejects_an_invalid_reference(tmp_path, monkeypatch, value, message):
    monkeypatch.setattr("specs.SPEC_DIR", tmp_path)
    (tmp_path / "scan-1.json").write_text(json.dumps({
        "doc": "scan", "page": 1, "assertions": [{"min_recall": value}],
    }))
    with pytest.raises(ValueError, match=message):
        load_specs()


@pytest.mark.parametrize("has_ocr", [False, True])
def test_load_specs_only_expects_missing_ocr_failures_without_a_runner(tmp_path, monkeypatch,
                                                                     has_ocr):
    monkeypatch.setattr("specs.SPEC_DIR", tmp_path)
    monkeypatch.setattr("specs.make_runner", lambda mode: object() if has_ocr else None)
    (tmp_path / "scan-1.json").write_text(json.dumps({
        "doc": "scan", "page": 1, "assertions": [
            {"xfail_without_ocr": "No OCR runner", "contains": ["Recovered text"]},
            {"xfail": "Known structure defect", "heading": {"text": "Title"}},
        ],
    }))
    conditional, unconditional = load_specs()[0].assertions
    assert conditional.check == "contains"
    assert conditional.value == ["Recovered text"]
    assert conditional.xfail == (None if has_ocr else "No OCR runner")
    assert unconditional.xfail == "Known structure defect"


def _counting_convert_many(monkeypatch, calls, failed=None):
    """Replace the conversion with a fake that writes one cacheable entry per document."""
    import reflow.convert

    def fake_convert_many(docs, **kwargs):
        results = []
        for _, out_dir in docs:
            calls.append(out_dir)
            _entry(out_dir, "converted")
            ocr = SimpleNamespace(runner="tesseract", failed=failed or {})
            summary = SimpleNamespace(ocr=ocr, seconds={}, pages=1, model="")
            results.append(SimpleNamespace(ok=True, error=None, summary=summary))
        return results

    monkeypatch.setattr(reflow.convert, "convert_many", fake_convert_many)


def test_corpus_cache_separates_ocr_availability(tmp_path, monkeypatch):
    import corpus as corpus_module

    monkeypatch.setattr(corpus_module, "CACHE_ROOT", tmp_path)
    calls = []
    _counting_convert_many(monkeypatch, calls)
    for has_ocr, converted in ((False, 1), (True, 2), (False, 2)):   # the last is a hit
        monkeypatch.setattr("corpus.make_runner", lambda mode, on=has_ocr: object() if on else None)
        corpus_module.Corpus(quick=False).prepare(["scan"])
        assert len(calls) == converted


@pytest.mark.parametrize("digital_warnings", [0, 1, 2])
def test_queue_cap_excludes_ocr_without_diluting_born_digital_warnings(tmp_path, digital_warnings):
    import test_calibration as calibration

    # Twenty born-digital nodes including the root, alongside a much larger OCR page.
    digital = [{"source": "heuristic", "page": 2} for _ in range(19)]
    ocr = [{"source": "heuristic", "page": 1} for _ in range(100)]
    ocr.append({"source": "ocr", "page": 2})
    (tmp_path / "document.json").write_text(json.dumps({
        "root": {"source": "heuristic", "children": digital + ocr},
        "pages": [{"number": 1, "facts": [{"code": "ocr-page", "source": "ocr"}]},
                  {"number": 2}],
    }))
    review = [{**n, "severity": "warning"} for n in ocr + digital[:digital_warnings]]
    review.append({"source": "heuristic", "page": 2, "severity": "error"})
    (tmp_path / "review.json").write_text(json.dumps(review))
    corpus = SimpleNamespace(doc=lambda slug: SimpleNamespace(out_dir=tmp_path))

    over = calibration.queue_over_cap(corpus, ["mixed"])
    if digital_warnings <= 1:
        assert over == []
    else:
        assert len(over) == 1 and "2 items for 20 nodes (10.0%)" in over[0]


def _table(spec: dict, html: str) -> None:
    CHECKS["table"](spec, parse_html(html), lambda: "")


@pytest.mark.parametrize("html,passes", [
    ("<tr><td>56×56</td><td>pool then block</td></tr>", True),
    ("<tr><td>56×56</td><td>block</td></tr>"
     "<tr><td>28×28</td><td>pool then block</td></tr>", False),
    ("<tr><td>56×56 pool</td><td>block</td></tr>", False),
    ("<tr><td>56×56 extra</td><td>pool then block</td></tr>", False),
    ("<tr><td colspan='2'>56×56</td><td>pool then block</td></tr>", True),
    ("<tr><td>56×56</td><td colspan='2'>pool then block</td></tr>", True),
    ("<tr><td>56×56</td></tr><tr><td>pool then block</td></tr>", False),
    ("<tr><td>56×56</td><td>block</td><td>block</td><td>pool</td></tr>", False),
    ("<tr><td rowspan='2'>56×56</td><td>block</td></tr>"
     "<tr><td>pool then block</td></tr>", True),
])
def test_cell_contains_stays_in_selected_columns_of_exact_anchored_row(html, passes):
    spec = {"cell_contains": [{"row": {"col": 0, "text": "56×56"},
                              "cols": [1, 2], "text": "pool"}]}
    if passes:
        _table(spec, f"<table>{html}</table>")
    else:
        with pytest.raises(CheckFailed, match="cell_contains"):
            _table(spec, f"<table>{html}</table>")


def test_cell_contains_does_not_relax_whole_cell_or_shape_requirements():
    html = "<table><tr><td>56×56</td><td>pool then block</td></tr></table>"
    spec = {"rows": 1, "cols": 2, "exact": True,
            "cell_contains": [{"row": {"col": 0, "text": "56×56"},
                               "cols": [1], "text": "pool"}]}
    _table(spec, html)
    with pytest.raises(CheckFailed, match="missing cells.*pool"):
        _table({**spec, "cells": ["pool"]}, html)
    with pytest.raises(CheckFailed, match="row widths"):
        _table({**spec, "cols": 3}, html)
    with pytest.raises(CheckFailed):
        _table({**spec, "rows": 2}, html)


def _wide(columns: int) -> str:
    return "<table>" + "".join(f"<tr>{'<td>x</td>' * columns}</tr>" for _ in range(2)) + "</table>"


def test_max_table_cols_fails_a_table_wider_than_the_limit():
    pg = parse_html(_wide(12) + _wide(30))
    with pytest.raises(CheckFailed, match="table 2 is 30 columns wide, at most 12 expected"):
        CHECKS["max_table_cols"](12, pg, lambda: "")
    CHECKS["max_table_cols"](30, pg, lambda: "")


def test_max_table_cols_counts_colspan_and_rowspan():
    spanning = ("<table><tr><th colspan='20'>one cell</th></tr>"
                "<tr><td>a</td><td>b</td></tr></table>")
    with pytest.raises(CheckFailed, match="20 columns wide"):
        CHECKS["max_table_cols"](12, parse_html(spanning), lambda: "")
    down = ("<table><tr><td rowspan='2'>a</td><td>b</td></tr>"
            "<tr><td>c</td></tr></table>")
    CHECKS["max_table_cols"](2, parse_html(down), lambda: "")


def test_max_table_cols_passes_a_page_with_no_table():
    CHECKS["max_table_cols"](1, parse_html("<p>no table</p>"), lambda: "")


def test_exact_counts_colspan_in_the_row_width():
    html = ("<table><tr><th colspan='2'>Method</th></tr>"
            "<tr><td>a</td><td>b</td></tr></table>")
    _table({"rows": 2, "cols": 2, "exact": True}, html)
    # Without exact, the minimum counts raw cells, so a single colspan=2 cell is only 1 column.
    with pytest.raises(CheckFailed):
        _table({"rows": 1, "cols": 2}, "<table><tr><th colspan='2'>M</th></tr></table>")


def test_exact_counts_rowspan_in_the_rows_below():
    # Row 2 has two cells, but the rowspan from row 1 fills column 0, so it covers 3 columns.
    html = ("<table><tr><th rowspan='2'>Method</th><th>top-1</th><th>top-5</th></tr>"
            "<tr><td>28.5</td><td>9.9</td></tr></table>")
    _table({"rows": 2, "cols": 3, "exact": True}, html)


def test_exact_requires_every_row_to_span_all_columns():
    # The raw count is 3 at the top, so the minimum passes; exact sees the 2-wide second row.
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>1</td><td>2</td></tr></table>"
    _table({"rows": 2, "cols": 3}, html)
    with pytest.raises(CheckFailed, match=r"row widths \[3, 2\]"):
        _table({"rows": 2, "cols": 3, "exact": True}, html)


def test_exact_requires_the_row_count_to_match():
    html = "<table><tr><td>a</td></tr><tr><td>b</td></tr></table>"
    _table({"rows": 1, "cols": 1}, html)  # a minimum: two rows is enough
    with pytest.raises(CheckFailed, match="2 rows"):
        _table({"rows": 1, "cols": 1, "exact": True}, html)


def test_exact_still_checks_cells():
    html = "<table><tr><td>a</td><td>b</td></tr></table>"
    with pytest.raises(CheckFailed, match="missing cells \\['c'\\]"):
        _table({"rows": 1, "cols": 2, "exact": True, "cells": ["a", "c"]}, html)


def test_exact_passes_when_any_table_on_the_page_matches():
    chart = "<table><tr><td>axis</td></tr></table>"
    real = "<table><tr><td>x</td><td>y</td></tr><tr><td>1</td><td>2</td></tr></table>"
    _table({"rows": 2, "cols": 2, "exact": True, "cells": ["y", "2"]}, chart + real)


def test_non_exact_minimums_are_unchanged():
    html = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>1</td></tr></table>"
    _table({"rows": 2, "cols": 3, "cells": ["a", "1"]}, html)


def test_table_row_headers_checks_body_without_requiring_row_scope_in_thead():
    html = ("<table><thead><tr><th scope='col'>Name</th><th>Score</th></tr></thead>"
            "<tbody><tr><th scope='row'>Alice</th><td>10</td></tr>"
            "<tr><th scope='row'>Bob</th><td>20</td></tr></tbody></table>")
    _table({"rows": 3, "cols": 2, "exact": True, "row_headers": True}, html)


@pytest.mark.parametrize("body", [
    "<tr><td>Alice</td><td>10</td></tr>",
    "<tr><th>Alice</th><td>10</td></tr>",
    "<tr><th scope='col'>Alice</th><td>10</td></tr>",
    "<tr><td>10</td><th scope='row'>Alice</th></tr>",
    "<tr></tr>",
    "",
])
def test_table_row_headers_rejects_missing_or_misplaced_headers(body):
    html = ("<table><thead><tr><th>Name</th><th>Score</th></tr></thead>"
            f"<tbody><tr><th scope='row'>Bob</th><td>20</td></tr>{body}</tbody></table>")
    if not body:
        html = "<table><thead><tr><th>Name</th></tr></thead></table>"
    with pytest.raises(CheckFailed, match="first cell is th"):
        _table({"row_headers": True}, html)


def test_table_row_headers_accepts_a_matching_table_among_others():
    html = ("<table><tbody><tr><td>10</td></tr></tbody></table>"
            "<table><tbody><tr><th scope='row'>Alice</th><td>10</td></tr></tbody></table>")
    _table({"cells": ["Alice", "10"], "row_headers": True}, html)


def test_shell_contains_reads_the_document_shell():
    shell = parse_html("<header><h1>Deep Residual Learning</h1></header>"
                       "<div class='pg'>Body text</div>")
    SHELL_CHECKS["shell_contains"](["Deep Residual Learning"], shell)
    with pytest.raises(CheckFailed):
        SHELL_CHECKS["shell_contains"](["Body text is not the title"], shell)


def test_doc_lang_reads_the_html_language():
    shell = parse_html('<html lang="zh-Hant"><body><header>x</header></body></html>')
    SHELL_CHECKS["doc_lang"]("zh-Hant", shell)
    with pytest.raises(CheckFailed, match="en-US"):
        SHELL_CHECKS["doc_lang"]("zh-Hant", parse_html('<html lang="en-US"></html>'))
    with pytest.raises(CheckFailed, match="None"):
        SHELL_CHECKS["doc_lang"]("zh-Hant", parse_html("<html><body></body></html>"))


def test_doc_title_reads_the_shell_title_exactly():
    shell = parse_html("<html><head><title>CICP Provider Directory</title></head>"
                       "<body><header>x</header></body></html>")
    SHELL_CHECKS["doc_title"]("CICP Provider Directory", shell)
    with pytest.raises(CheckFailed, match="02-building-permit"):
        SHELL_CHECKS["doc_title"]("BUILDING PERMIT APPLICATIONS", parse_html(
            "<html><head><title>02-building-permit-applications</title></head></html>"))
    with pytest.raises(CheckFailed, match="None"):
        SHELL_CHECKS["doc_title"]("Anything", parse_html("<html><body></body></html>"))


def test_block_lang_wants_the_language_on_the_block_or_around_it():
    pg = parse_html('<div class="pg"><p lang="es">Hola a todos los miembros</p>'
                    '<section lang="fr"><p>Bonjour tout le monde</p></section>'
                    '<p>Plain English</p></div>')
    CHECKS["block_lang"]([{"text": "Hola a todos", "lang": "es"},
                          {"text": "Bonjour", "lang": "fr"}], pg, lambda: "")
    with pytest.raises(CheckFailed, match="Plain English"):
        CHECKS["block_lang"]([{"text": "Plain English", "lang": "es"}], pg, lambda: "")


def test_block_lang_respects_overrides_and_the_page_language():
    pg = parse_html('<div lang="es"><p>Hola <em>amigos</em></p>'
                    '<section lang="fr"><p lang="en">Plain English</p></section></div>')
    CHECKS["block_lang"]([{"text": "Hola amigos", "lang": "es"},
                          {"text": "Plain English", "lang": "en"}], pg, lambda: "")
    for lang in ("es", "fr"):
        with pytest.raises(CheckFailed, match="Plain English"):
            CHECKS["block_lang"]([{"text": "Plain English", "lang": lang}], pg, lambda: "")


def test_block_lang_null_wants_no_language_on_the_block():
    pg = parse_html('<div class="pg"><p lang="es">Hola a todos</p><p>Plain English</p></div>')
    CHECKS["block_lang"]([{"text": "Plain English", "lang": None}], pg, lambda: "")
    with pytest.raises(CheckFailed, match="Hola a todos"):
        CHECKS["block_lang"]([{"text": "Hola a todos", "lang": None}], pg, lambda: "")


def test_report_scorecard_summarises_a_document(monkeypatch, capsys, tmp_path):
    import report

    (tmp_path / "review.json").write_text(json.dumps(
        [{"severity": "error"}, {"severity": "warning"}, {"severity": "warning"}]))
    (tmp_path / "page-checks-timing.json").write_text(json.dumps(
        {"pages": 10, "seconds": {"extract": 2.0, "layout": 3.0}}))
    passing = Assertion("min_recall", 90)
    xfailing = Assertion("contains", ["absent"], xfail="known open")
    spec = PageSpec("doc-1", "doc", 1, "page", [passing, xfailing])
    doc = SimpleNamespace(page=lambda number: parse_html("<p>alpha beta</p>"),
                          pdf_text=lambda number: "alpha beta", shell=None, page_map=None,
                          out_dir=tmp_path)
    monkeypatch.setattr(report, "Corpus", lambda: SimpleNamespace(
        doc=lambda slug: doc, prepare=lambda *args, **kwargs: None, quick=False))
    monkeypatch.setattr(report, "load_specs", lambda only: [spec])
    monkeypatch.setattr(report, "REPO", tmp_path)

    assert report.main(["--write", "card.md"]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("doc "))
    assert row.split() == ["doc", "1", "1", "1", "0", "0", "100.0", "90.0", "1", "2", "5.0", "2.0"]
    assert "| doc | 1 | 1 | 1 | 0 | 0 | 100.0 | 90.0 | 1 | 2 | 5.0 | 2.0 |" in (
        tmp_path / "card.md").read_text()


def _entry(path, marker):
    path.mkdir(parents=True)
    (path / "manifest.json").write_text("{}")
    (path / "marker").write_text(marker)
    return path


def test_publish_keeps_an_existing_entry_unless_replacing(tmp_path):
    from corpus import publish

    out = _entry(tmp_path / "key", "live")
    publish(_entry(tmp_path / "tmp1", "ours"), out)
    assert (out / "marker").read_text() == "live"
    assert not (tmp_path / "tmp1").exists()

    publish(_entry(tmp_path / "tmp2", "new"), out, replace=True)
    assert (out / "marker").read_text() == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["key"]  # nothing left aside


def test_prepare_does_not_cache_a_conversion_with_failed_ocr(tmp_path, monkeypatch, capsys):
    import corpus as corpus_module

    monkeypatch.setattr(corpus_module, "CACHE_ROOT", tmp_path)
    calls = []
    _counting_convert_many(monkeypatch, calls, failed={1: "timeout"})
    c = corpus_module.Corpus(quick=False)
    c.prepare(["scan"])

    assert "OCR failed on pages [1]" in capsys.readouterr().err
    assert (c.doc("scan").out_dir / "marker").read_text() == "converted"  # usable this run
    corpus_module.Corpus(quick=False).prepare(["scan"])
    assert len(calls) == 2   # but not cached: the next run converts again

    _counting_convert_many(monkeypatch, calls)   # OCR works now
    corpus_module.Corpus(quick=False).prepare(["scan"])
    corpus_module.Corpus(quick=False).prepare(["scan"])
    assert len(calls) == 3   # a clean conversion is cached


NOTE_PAGE = (
    '<p>See chapter 1, later.<a role="doc-noteref" href="chunks/0001.html#n-1:a" id="r-1:a">1</a>'
    ' <a href="chunks/0001.html#p8">chapter 1</a> and <a href="https://irs.gov/x">IRS.gov/x</a></p>'
    '<aside role="doc-footnote" id="n-1:a"><a role="doc-backlink" href="chunks/0001.html#r-1:a">1</a>'
    ' The note.</aside>')


def _run(check: str, value, html: str) -> None:
    CHECKS[check](value, parse_html(html), lambda: "")


def test_footnotes_and_noterefs_count_the_aria_roles():
    _run("footnotes", 1, NOTE_PAGE)
    _run("noterefs", 1, NOTE_PAGE)
    with pytest.raises(CheckFailed, match="0 footnotes"):
        _run("footnotes", 1, "<p>none</p>")
    with pytest.raises(CheckFailed, match="note references"):
        _run("noterefs", 2, NOTE_PAGE)


def test_noterefs_need_a_footnote_with_a_backlink():
    orphan = NOTE_PAGE.replace('id="n-1:a"', 'id="n-9:z"')
    with pytest.raises(CheckFailed, match="points at no footnote"):
        _run("noterefs", 1, orphan)
    no_back = NOTE_PAGE.replace('role="doc-backlink"', "")
    with pytest.raises(CheckFailed, match="no backlink"):
        _run("noterefs", 1, no_back)


LINKS_HTML = ('<p>See <a href="chunks/0001.html#p8">chapter 1,</a> and <a '
              'href="https://www.irs.gov/payments">IRS.gov/Payments</a>.</p>')


def test_links_check_matches_text_with_a_url_or_a_page():
    pg = parse_html(LINKS_HTML)
    run_check(Assertion("links", [{"text": "IRS.gov/Payments",
                                   "href": "https://www.irs.gov/payments"},
                                  {"text": "chapter 1,", "page": 8},
                                  {"text": "chapter 1,", "internal": True}]),
              pg, lambda: "")


@pytest.mark.parametrize("want, message", [
    ({"text": "chapter 1,", "page": 80}, r"no link 'chapter 1,' to page 80 \(it goes to"),
    ({"text": "chapter 1,", "href": "https://x.test"}, "to https://x.test"),
    ({"text": "IRS.gov/Payments", "internal": True}, "to a page of the document"),
    ({"text": "absent", "internal": True}, "no link 'absent'"),
])
def test_links_check_rejects_a_wrong_or_missing_link(want, message):
    with pytest.raises(CheckFailed, match=message):
        run_check(Assertion("links", [want]), parse_html(LINKS_HTML), lambda: "")


EMPHASIS_HTML = ('<p>Read this. <strong>Do not attach <a href="#x">hearing</a> evidence.</strong> '
                 'See <em>Furnished  System</em> and <b>bold</b>.</p>')


def test_emphasis_check_matches_the_text_of_a_strong_or_em_element():
    pg = parse_html(EMPHASIS_HTML)
    run_check(Assertion("emphasis", [{"text": "Do not attach hearing evidence.", "kind": "strong"},
                                     {"text": "Furnished System", "kind": "em"}]),
              pg, lambda: "")


@pytest.mark.parametrize("want, message", [
    ({"text": "Do not attach", "kind": "strong"}, "no <strong> with the text 'Do not attach'"),
    ({"text": "Furnished System", "kind": "strong"}, "no <strong>"),
    ({"text": "bold", "kind": "strong"}, "no <strong> with the text 'bold'"),   # <b> is not <strong>
])
def test_emphasis_check_rejects_text_that_is_not_the_whole_element(want, message):
    with pytest.raises(CheckFailed, match=message):
        run_check(Assertion("emphasis", [want]), parse_html(EMPHASIS_HTML), lambda: "")


ACCENT_HTML = ('<p><strong>FEE: <span class="ink-red">$65.75</span></strong> per parcel, '
               '<span class="ink-green">see below</span>.</p>')


def test_emphasis_check_matches_the_text_of_an_accent_span():
    run_check(Assertion("emphasis", [{"text": "$65.75", "kind": "red"},
                                     {"text": "see below", "kind": "green"}]),
              parse_html(ACCENT_HTML), lambda: "")


@pytest.mark.parametrize("want, message", [
    ({"text": "$65.75", "kind": "green"}, "no green span with the text '\\$65.75'"),
    ({"text": "FEE: $65.75", "kind": "red"}, "no red span with the text 'FEE: \\$65.75'"),
])
def test_emphasis_check_rejects_the_wrong_accent_or_a_longer_text(want, message):
    with pytest.raises(CheckFailed, match=message):
        run_check(Assertion("emphasis", [want]), parse_html(ACCENT_HTML), lambda: "")


FILLED_HTML = ('<aside class="filled-box" aria-label="REMEMBER"><p class="note">REMEMBER</p>'
               '<p>Out-of-network claims must be submitted within a year.</p></aside>'
               '<section class="filled-box" role="group" aria-label="Tip"><p>Tip</p><p>Two.</p></section>'
               '<section class="ruled-box" role="group" aria-label="Ruled"><p>One</p><p>Two</p></section>')


def test_ruled_box_check_finds_a_filled_aside_and_a_filled_group():
    pg = parse_html(FILLED_HTML)
    run_check(Assertion("ruled_box", {"name": "REMEMBER", "filled": True, "tag": "aside",
                                      "texts": ["REMEMBER", "Out-of-network claims"]}),
              pg, lambda: "")
    run_check(Assertion("ruled_box", {"name": "Tip", "filled": True, "texts": ["Tip", "Two."]}),
              pg, lambda: "")


@pytest.mark.parametrize("spec, message", [
    ({"name": "REMEMBER", "texts": ["REMEMBER"]}, "no ruled section box named 'REMEMBER'"),
    ({"name": "REMEMBER", "filled": True, "texts": ["REMEMBER"]}, "no filled section box"),
    ({"name": "Ruled", "filled": True, "texts": ["One"]}, "no filled section box named 'Ruled'"),
])
def test_ruled_box_check_tells_a_filled_box_from_a_ruled_one(spec, message):
    with pytest.raises(CheckFailed, match=message):
        run_check(Assertion("ruled_box", spec), parse_html(FILLED_HTML), lambda: "")


SUP_HTML = ('<p>Visits 42.2 million times each year.<sup>1</sup></p>'
            '<p>won competitions<sup><a role="doc-noteref" href="#n-1">2</a></sup> here</p>'
            '<p>Struggle in an emergency.3</p>')


def test_superscripts_check_finds_a_sup_after_its_words():
    run_check(Assertion("superscripts", [{"text": "1", "after": "each year."},
                                         {"text": "2", "after": "won competitions"}]),
              parse_html(SUP_HTML), lambda: "")


def test_superscripts_check_rejects_a_marker_left_on_the_line():
    with pytest.raises(CheckFailed, match="no superscript '3' after 'an emergency.'"):
        run_check(Assertion("superscripts", [{"text": "3", "after": "an emergency."}]),
                  parse_html(SUP_HTML), lambda: "")


# The unruled "Claim Totals:" label of the remittance scan spans grid columns 0 to 2: a recovery
# may place it by its left edge, by its centre or across all three.
LABEL_LEFT = "<td>Claim Totals:</td><td></td><td></td>"
LABEL_CENTRE = "<td></td><td>Claim Totals:</td><td></td>"
LABEL_ACROSS = '<td colspan="3">Claim Totals:</td>'


def _remittance_table(claim_totals: list[str], label: str = LABEL_LEFT, band: bool = False) -> str:
    """The scan's ruled line-item grid as printed: a header, the member and provider band (with
    `band`), three items and the claim totals, whose columns 0 to 2 are the HTML `label`."""
    headers = ["Service Dates From/To", "Service Code", "# Units", "Amount Billed",
               "Excluded/Deductible", "Not Allowed", "Allowed Amount", "C.O.B. Insurance",
               "Co-Pay Amount", "Withhold Amount", "Paid Amount", "Adjustment-Reason/code"]
    items = [["08/11/22-08/11/22", "J0702", "2", "20.00", "0.00", "6.89", "13.11", "10.49",
              "0.00", "0.00", "2.62", "R1"],
             ["08/11/22-08/11/22", "20553", "1", "168.00", "0.00", "106.83", "61.17", "48.94",
              "0.00", "0.00", "12.23", "R1"],
             ["08/11/22-08/11/22", "76942", "1", "80.00", "0.00", "22.42", "57.58", "46.06",
              "0.00", "0.00", "11.52", "R1"]]

    def cells(row):
        return "".join(f"<td>{escape(c)}</td>" for c in row)

    rows = [cells(headers)]
    if band:
        rows.append('<td colspan="12">Member: Provider: Attending NPI #: Member #: Account No: '
                    "Claim #: Plan: Paid DRG:</td>")
    rows += [cells(item) for item in items]
    rows.append(label + cells([*claim_totals, ""]))
    return "<table>" + "".join(f"<tr>{row}</tr>" for row in rows) + "</table>"


def test_the_remittance_table_target_is_the_twelve_column_source_grid():
    from decimal import Decimal as D
    spec = next(a for s in load_specs() if s.id == "az-remittance-advice-scan-1"
                for a in s.assertions if a.check == "table")
    assert spec.xfail and (spec.value["rows"], spec.value["cols"]) == (5, 12)
    totals = ["268.00", "0.00", "136.14", "131.86", "105.49", "0.00", "0.00", "26.37"]
    # The source's own arithmetic: each total is its column's sum, and allowed less
    # other-insurance payment is the amount paid.
    items = {"billed": ["20.00", "168.00", "80.00"], "not_allowed": ["6.89", "106.83", "22.42"],
             "allowed": ["13.11", "61.17", "57.58"], "cob": ["10.49", "48.94", "46.06"],
             "paid": ["2.62", "12.23", "11.52"]}
    assert {k: sum(map(D, v)) for k, v in items.items()} == {
        "billed": D(totals[0]), "not_allowed": D(totals[2]), "allowed": D(totals[3]),
        "cob": D(totals[4]), "paid": D(totals[7])}
    assert D(totals[0]) - D(totals[2]) == D(totals[3])
    assert D(totals[3]) - D(totals[4]) == D(totals[7])
    # Wherever the label lands under its three columns, and with or without the member band
    # as a row of its own (six rows), the source's table meets the target.
    for label in (LABEL_LEFT, LABEL_CENTRE, LABEL_ACROSS):
        for band in (False, True):
            CHECKS["table"](spec.value, parse_html(_remittance_table(totals, label, band)),
                            lambda: "")
    # A claim total in the wrong column or with a wrong value, the totals one column off, or a
    # totals row without its label (or with the statement's) is not the source's table.
    wrong = [([totals[2], totals[1], totals[0], *totals[3:]], LABEL_LEFT),
             ([*totals[:3], totals[4], totals[3], *totals[5:]], LABEL_LEFT),
             ([*totals[:7], "26.73"], LABEL_LEFT),
             (totals, LABEL_LEFT + "<td></td>"),
             (totals, "<td></td><td></td><td></td>"),
             (totals, "<td>Statement Totals:</td><td></td><td></td>")]
    for values, label in wrong:
        with pytest.raises(CheckFailed, match="cell_contains"):
            CHECKS["table"](spec.value, parse_html(_remittance_table(values, label)), lambda: "")


# --- calendar_month and the ledger checks ------------------------------------------------

def _month_html(caption="JULY 2024", year=2024, month=7, edit=None):
    import calendar as cal

    first = (cal.monthrange(year, month)[0] + 1) % 7
    days = [None] * first + list(range(1, cal.monthrange(year, month)[1] + 1))
    days += [None] * (-len(days) % 7)
    if edit:
        days = edit(days)
    head = "".join(f"<th scope='col'>{c}</th>" for c in "SMTWTFS")
    rows = "".join(
        "<tr>" + "".join(f"<td>{d:02d}</td>" if d else "<td></td>" for d in days[i:i + 7]) + "</tr>"
        for i in range(0, len(days), 7))
    return f"<table><caption>{caption}</caption><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"


def _calendar(html, caption="JULY 2024", year=2024, month=7):
    CHECKS["calendar_month"]({"caption": caption, "year": year, "month": month},
                             parse_html(html), lambda: "")


def test_calendar_month_passes_the_right_grid():
    _calendar(_month_html())


def test_calendar_month_fails_a_displaced_duplicated_or_missing_date():
    def displaced(days):
        i = days.index(15)
        days[i], days[i + 1] = days[i + 1], days[i]  # 15 and 16 swap
        return days

    def duplicated(days):
        days[days.index(16)] = 15
        return days

    def missing(days):
        days[days.index(31)] = None
        return days

    for edit, message in [(displaced, "expected date 15"), (duplicated, "expected date 16"),
                          (missing, "stop at 30")]:
        with pytest.raises(CheckFailed, match=message):
            _calendar(_month_html(edit=edit))


def test_calendar_month_fails_a_grid_shifted_a_column():
    with pytest.raises(CheckFailed, match="date 1 is in column 3, expected 2"):
        _calendar(_month_html(edit=lambda days: [None] + days[:-1] if days[-1] is None else days))


def test_calendar_month_fails_the_same_caption_twice_and_a_missing_caption():
    with pytest.raises(CheckFailed, match="2 tables captioned"):
        _calendar(_month_html() + _month_html())
    with pytest.raises(CheckFailed, match="0 tables captioned"):
        _calendar(_month_html(), caption="AUGUST 2024")


def test_calendar_month_wants_a_header_row_of_seven_th():
    html = _month_html().replace("<th scope='col'>S</th>", "<td>S</td>", 1)
    with pytest.raises(CheckFailed, match="not 7 th"):
        _calendar(html)



KEY_SPAN = ' — <span class="day-status" data-from="key" data-bbox="[[1,2,3,4]]">{}</span>'
NOTE_SPAN = ' — <span class="day-note" data-from="list" data-bbox="[[5,6,7,8]]">{}</span>'


def _annotated(day, spans):
    """July 2024 with `spans` after the number of `day`."""
    return _month_html().replace(f"<td>{day:02d}</td>", f"<td>{day:02d}{spans}</td>", 1)


def _day_spec(**more):
    return {"caption": "JULY 2024", "year": 2024, "month": 7, **more}


def test_calendar_month_reads_a_days_own_number_beside_its_copied_words():
    html = _annotated(2, KEY_SPAN.format("Closed / Holiday") + NOTE_SPAN.format("Staff Holiday"))
    CHECKS["calendar_month"](_day_spec(status={"2": ["Closed / Holiday"]},
                                       notes={"2": ["Staff Holiday"]}, plain=[1, 3]),
                             parse_html(html), lambda: "")


def test_calendar_month_fails_a_wrong_or_missing_status_or_note():
    html = _annotated(2, KEY_SPAN.format("Closed / Holiday"))
    for spec, message in [(_day_spec(status={"2": ["Student Holiday"]}), "date 2 has status"),
                          (_day_spec(notes={"2": ["Staff Holiday"]}), "date 2 has notes"),
                          (_day_spec(status={"3": ["Closed / Holiday"]}), "date 3 has status"),
                          (_day_spec(plain=[2]), "date 2 should copy no words")]:
        with pytest.raises(CheckFailed, match=message):
            CHECKS["calendar_month"](spec, parse_html(html), lambda: "")


def test_calendar_month_fails_copied_words_without_their_source():
    html = _annotated(2, ' — <span class="day-status">Closed / Holiday</span>')
    with pytest.raises(CheckFailed, match="without its source"):
        CHECKS["calendar_month"](_day_spec(), parse_html(html), lambda: "")


def test_calendar_month_fails_a_label_written_into_the_cell_text():
    html = _month_html().replace("<td>02</td>", "<td>02 Closed / Holiday</td>", 1)
    with pytest.raises(CheckFailed, match="not a date"):
        CHECKS["calendar_month"](_day_spec(), parse_html(html), lambda: "")

def _ledger_doc(tmp_path, page_map=None):
    import json

    from corpus import DocOutput

    def node(role, page, facts=(), children=()):
        return {"role": role, "page": page, "facts": [{"code": c} for c in facts],
                "children": list(children)}

    table = node("table", 2, ["calendar-grid-mismatch", "prior"], [node("td", 2, ["ocr-image-text"])])
    document = {"root": node("doc", None, children=[table, node("p", 1, ["order-disagreement"])]),
                "pages": [{"number": 2, "facts": [{"code": "ocr-page"}]}]}
    (tmp_path / "document.json").write_text(json.dumps(document))
    (tmp_path / "manifest.json").write_text(json.dumps({"chunks": []}))
    return DocOutput("x", tmp_path, page_map)


def test_review_checks_read_the_ledger_of_the_page_and_its_nested_nodes(tmp_path):
    doc = _ledger_doc(tmp_path)
    pg = parse_html("")
    run_check(Assertion("review_has", ["calendar-grid-mismatch", "ocr-image-text"]), pg,
              lambda: "", doc=doc, page=2)
    run_check(Assertion("review_lacks", ["table-giant-cell", "order-disagreement"]), pg,
              lambda: "", doc=doc, page=2)
    with pytest.raises(CheckFailed, match="calendar-grid-mismatch"):
        run_check(Assertion("review_lacks", ["calendar-grid-mismatch"]), pg, lambda: "",
                  doc=doc, page=2)
    with pytest.raises(ValueError, match="needs the converted document"):
        run_check(Assertion("review_lacks", []), pg, lambda: "")


def test_review_checks_translate_a_quick_excerpts_page(tmp_path):
    doc = _ledger_doc(tmp_path, page_map={30: 2})
    run_check(Assertion("review_has", ["calendar-grid-mismatch"]), parse_html(""), lambda: "",
              doc=doc, page=30)
    with pytest.raises(KeyError):
        run_check(Assertion("review_has", []), parse_html(""), lambda: "", doc=doc, page=31)


def test_calendar_month_fails_a_date_past_the_end_of_the_month():
    html = _month_html().replace("<td>31</td><td></td>", "<td>31</td><td>32</td>")
    with pytest.raises(CheckFailed, match="date 32 is past the end"):
        _calendar(html)


def _converted(tmp_path, *, page_map=None, pages=None, nodes=(), review=(), toc=()):
    """A DocOutput over a converted-looking folder: manifest, document.json, review.json."""
    from corpus import DocOutput

    out = tmp_path / "out"
    out.mkdir(parents=True)
    pages = pages or [{"number": 1, "width": 612.0, "height": 792.0, "facts": []}]
    (out / "manifest.json").write_text(json.dumps({"toc": list(toc), "chunks": []}))
    (out / "document.json").write_text(json.dumps({
        "pages": pages, "root": {"role": "doc", "children": list(nodes)}}))
    (out / "review.json").write_text(json.dumps(list(review)))
    return DocOutput("scan", out, page_map)


def _doc_check(check, value, doc, page=1):
    run_check(Assertion(check, value), parse_html(""), lambda: "", doc=doc, page=page)


def test_doc_checks_read_the_excerpt_page_of_a_source_page(tmp_path):
    # Quick mode: source page 30 is page 2 of the excerpt.
    pages = [{"number": 1, "width": 612.0, "height": 792.0, "facts": []},
             {"number": 2, "width": 612.0, "height": 792.0,
              "facts": [{"code": "scan-noisy", "source": "heuristic"}]}]
    doc = _converted(tmp_path, page_map={1: 1, 30: 2}, pages=pages)
    _doc_check("review_has", ["scan-noisy"], doc, page=30)
    _doc_check("review_lacks", ["scan-noisy"], doc, page=1)
    with pytest.raises(KeyError, match="quick excerpt"):
        _doc_check("review_has", ["scan-noisy"], doc, page=2)


def test_review_codes_come_from_every_fact_of_a_nested_node(tmp_path):
    label = {"role": "p", "page": 1, "facts": [
        {"code": "prior", "source": "ocr"},
        {"code": "ocr-in-picture", "source": "ocr"},
        {"code": "alt-missing", "source": "heuristic"}]}
    figure = {"role": "figure", "page": 1, "bbox": [0, 0, 100, 100], "facts": [],
              "children": [label]}
    doc = _converted(tmp_path, nodes=[{"role": "section", "children": [figure]}])
    _doc_check("review_has", ["ocr-in-picture", "alt-missing"], doc)
    _doc_check("review_lacks", ["scan-noisy"], doc)
    with pytest.raises(CheckFailed, match="alt-missing"):
        _doc_check("review_lacks", ["scan-noisy", "alt-missing"], doc)
    with pytest.raises(CheckFailed):
        _doc_check("review_has", ["prior"], doc)   # no severity: never a review item


def test_doc_check_without_the_document_is_an_error():
    with pytest.raises(ValueError, match="needs the converted document"):
        run_check(Assertion("review_has", ["x"]), parse_html(""), lambda: "")


def test_figure_share_and_count_read_the_page_figures(tmp_path):
    whole = {"role": "figure", "page": 1, "bbox": [0, 0, 612, 792]}
    photo = {"role": "figure", "page": 1, "bbox": [72, 72, 300, 300]}
    other = {"role": "figure", "page": 2, "bbox": [0, 0, 612, 792]}
    pages = [{"number": n, "width": 612.0, "height": 792.0, "facts": []} for n in (1, 2, 3)]
    doc = _converted(tmp_path, pages=pages, nodes=[photo, other])
    _doc_check("max_figure_share", 0.3, doc)
    _doc_check("min_figures", 1, doc)
    with pytest.raises(CheckFailed, match="at least 2"):
        _doc_check("min_figures", 2, doc)
    _doc_check("min_figures", 0, doc, page=3)
    doc = _converted(tmp_path / "b", pages=pages, nodes=[photo, whole])
    with pytest.raises(CheckFailed, match="100%"):
        _doc_check("max_figure_share", 0.6, doc)


def test_toc_lacks_reads_the_whole_contents(tmp_path):
    # The entry is on page 40; the check is asked about page 1. The Contents is the document's.
    toc = [{"level": 2, "text": "Budget in Brief", "page": 1},
           {"level": 2, "text": "CROSBY", "page": 40}]
    doc = _converted(tmp_path, toc=toc)
    _doc_check("toc_lacks", ["Gletzen"], doc)
    with pytest.raises(CheckFailed, match="CROSBY"):
        _doc_check("toc_lacks", ["CROSBY"], doc)


def test_max_review_items_counts_the_page_items(tmp_path):
    review = [{"page": 1}, {"page": 1}, {"page": 2}, {"page": None}]
    doc = _converted(tmp_path, review=review)
    _doc_check("max_review_items", 2, doc)
    with pytest.raises(CheckFailed, match="2 review items"):
        _doc_check("max_review_items", 1, doc)


def test_signature_items_count_the_page_items_that_point_at_a_signature(tmp_path):
    sig = {"code": "unread-signature", "box": [0, 0, 10, 10]}
    review = [{"page": 1, "targets": [sig]}, {"page": 1, "targets": [sig]},
              {"page": 1, "targets": [{"code": "unread-text", "box": [0, 0, 10, 10]}]},
              {"page": 2, "targets": [sig]}]
    doc = _converted(tmp_path, review=review)
    _doc_check("signature_items", 2, doc)
    with pytest.raises(CheckFailed, match="2 signature review items"):
        _doc_check("signature_items", 1, doc)


def test_figure_order_wants_a_signature_directly_before_the_line_it_signs(tmp_path):
    def p(text):
        return {"role": "p", "page": 1, "text": text}
    sig = {"role": "figure", "page": 1, "attrs": {"kind": "signature"},
           "children": [p("President")]}   # a figure's children are not the page's order
    nodes = [p("Signed by:"), sig, p("President"), p("ATTEST:"), sig, p("Footer")]
    doc = _converted(tmp_path, nodes=[{"role": "section", "children": nodes}])
    _doc_check("figure_order", ["Signed by:", "<figure kind=signature>", "President", "ATTEST:",
                                "<figure kind=signature>"], doc)
    with pytest.raises(CheckFailed, match="'Signed by:' not found"):
        _doc_check("figure_order", ["ATTEST:", "Signed by:"], doc)
    for apart in (["President", "<figure kind=signature>", "ATTEST:"],
                  ["<figure kind=signature>", "ATTEST:"]):
        with pytest.raises(CheckFailed, match="'<figure kind=signature>' not found"):
            _doc_check("figure_order", apart, doc)


def test_alt_checks_read_the_page_images():
    pg = parse_html('<div class="pg"><figure><img alt="Photo of a river under a bridge">'
                    '</figure><figure><img alt="Figure on page 4"></figure></div>')
    CHECKS["alt_has"](["river", "^Figure on page \\d+$"], pg, lambda: "")
    with pytest.raises(CheckFailed, match="truck"):
        CHECKS["alt_has"](["truck"], pg, lambda: "")
    with pytest.raises(CheckFailed, match="Figure on page"):
        CHECKS["alt_not"](["^Figure on page"], pg, lambda: "")
    empty = parse_html('<div class="pg"><p>No pictures</p></div>')
    with pytest.raises(CheckFailed, match="no <img>"):
        CHECKS["alt_has"](["river"], empty, lambda: "")
    CHECKS["alt_not"](["^Figure on page"], empty, lambda: "")


def test_load_specs_rejects_alt_not_without_a_figure_count(tmp_path, monkeypatch):
    monkeypatch.setattr("specs.SPEC_DIR", tmp_path)
    (tmp_path / "scan-1.json").write_text(json.dumps({
        "doc": "scan", "page": 1, "assertions": [{"alt_not": ["^Figure on page"]}]}))
    with pytest.raises(ValueError, match="alt_not needs a figures or min_figures check"):
        load_specs()
    (tmp_path / "scan-1.json").write_text(json.dumps({
        "doc": "scan", "page": 1, "assertions": [{"alt_not": ["^Figure on page"]},
                                                  {"min_figures": 1}]}))
    assert [a.check for a in load_specs()[0].assertions] == ["alt_not", "min_figures"]


def test_max_blocks_counts_the_text_blocks_of_the_page():
    pg = parse_html('<div class="pg"><p class="pg-label">Page 5</p><h2>Title</h2><p>one</p>'
                    '<ul><li>two</li><li>three</li></ul></div>')
    run_check(Assertion("max_blocks", 4), pg, lambda: "")
    with pytest.raises(CheckFailed, match="4 text blocks, at most 3 expected"):
        run_check(Assertion("max_blocks", 3), pg, lambda: "")


def test_same_item_needs_both_texts_in_one_list_item():
    pg = parse_html("<ul><li>the last three numbers of the driver's license number; and</li>"
                    "<li>the social security number</li></ul>"
                    "<p>split from its item</p>")
    run_check(Assertion("same_item", [["the last three numbers", "license number; and"]]), pg,
              lambda: "")
    with pytest.raises(CheckFailed, match="not in one list item"):
        run_check(Assertion("same_item", [["the social security", "split from its item"]]), pg,
                  lambda: "")


def test_same_item_does_not_count_a_nested_list():
    pg = parse_html("<ul><li>(a) A claim against a judge<ol><li>must be made under oath;</li>"
                    "</ol></li></ul>")
    with pytest.raises(CheckFailed, match="not in one list item"):
        run_check(Assertion("same_item", [["A claim against", "made under oath"]]), pg,
                  lambda: "")


def test_unread_figures_counts_the_unread_figures_of_the_page():
    pg = parse_html('<figure class="unread"><img alt="a"></figure><figure><img alt="b"></figure>')
    CHECKS["unread_figures"](1, pg, lambda: "")
    CHECKS["unread_figures"]({"min": 1}, pg, lambda: "")
    with pytest.raises(CheckFailed, match="1 unread figures"):
        CHECKS["unread_figures"](0, pg, lambda: "")
    with pytest.raises(CheckFailed):
        CHECKS["unread_figures"]({"min": 2}, pg, lambda: "")


def test_unread_over_text_fails_a_crop_over_a_block_of_print(tmp_path):
    crop = {"role": "figure", "page": 1, "bbox": [100, 100, 300, 200], "attrs": {"unread": True}}
    title = {"role": "h1", "page": 1, "bbox": [50, 150, 400, 170], "text": "NATIONAL ADVISORY"}
    beside = {"role": "p", "page": 1, "bbox": [50, 300, 400, 320], "text": "SCOTT H. SIMPKINSON"}
    doc = _converted(tmp_path, nodes=[crop, title, beside])
    _doc_check("unread_over_text", ["SCOTT H. SIMPKINSON"], doc)
    with pytest.raises(CheckFailed, match="NATIONAL ADVISORY"):
        _doc_check("unread_over_text", ["NATIONAL ADVISORY", "SCOTT H. SIMPKINSON"], doc)


def test_inserted_and_deleted_letters_stay_one_word_and_the_tracked_checks():
    pg = parse_html('<div class="pg"><p>wo<ins>rd</ins> a [<del class="unread-word" '
                    'data-generated><span role="img" aria-label="struck word, unreadable"></span>'
                    '</del>] statutory court<ins>, or the business court</ins>.</p></div>')
    from checks import page_text
    assert page_text(pg) == "word a [] statutory court, or the business court."
    run_check(Assertion("del_count", 1), pg, lambda: "")
    run_check(Assertion("ins_count", 2), pg, lambda: "")
    run_check(Assertion("ins_texts", {"exactly": ["rd", ", or the business court"]}), pg,
              lambda: "")
    run_check(Assertion("ins_texts", {"includes": [", or the business court"]}), pg, lambda: "")
    with pytest.raises(CheckFailed, match="no insertion"):
        run_check(Assertion("ins_texts", {"includes": ["or the business"]}), pg, lambda: "")
    run_check(Assertion("unmarked", ["statutory court"]), pg, lambda: "")
    with pytest.raises(CheckFailed, match="outside the insertions"):
        run_check(Assertion("unmarked", ["court, or"]), pg, lambda: "")


def test_toc_on_page_names_exactly_the_entries_of_the_page():
    doc = SimpleNamespace(excerpt_page=lambda n: n, manifest={"toc": [
        {"text": "SECTION 44", "page": 24}, {"text": "Elsewhere", "page": 25}]})
    run_check(Assertion("toc_on_page", ["SECTION 44"]), None, lambda: "", doc=doc, page=24)
    with pytest.raises(CheckFailed, match="Contents entries of page 25"):
        run_check(Assertion("toc_on_page", []), None, lambda: "", doc=doc, page=25)


def test_two_runs_preparing_one_document_at_once_convert_it_once(tmp_path, monkeypatch):
    import threading
    import time

    import corpus as corpus_module
    import reflow.convert

    monkeypatch.setattr(corpus_module, "CACHE_ROOT", tmp_path)
    converted = []

    def fake_convert_many(docs, **kwargs):
        converted.append([out.name for _, out in docs])
        time.sleep(0.3)   # the other run arrives while this one converts
        results = []
        for _, out_dir in docs:
            _entry(out_dir, "converted")
            summary = SimpleNamespace(ocr=SimpleNamespace(runner="none", failed={}),
                                      seconds={}, pages=1, model="")
            results.append(SimpleNamespace(ok=True, error=None, summary=summary))
        return results

    monkeypatch.setattr(reflow.convert, "convert_many", fake_convert_many)
    runs = [corpus_module.Corpus(quick=False) for _ in range(2)]
    barrier = threading.Barrier(2)

    def run(c):
        barrier.wait()
        c.prepare(["scan", "fw9"])
    threads = [threading.Thread(target=run, args=(c,)) for c in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = sorted(name.split(".")[1] for batch in converted for name in batch)   # .<key>.tmp-…
    assert keys == sorted([runs[0]._key("scan"), runs[0]._key("fw9")])   # each converted once
    for c in runs:
        assert (c.doc("scan").out_dir / "marker").read_text() == "converted"
        assert (c.doc("fw9").out_dir / "marker").read_text() == "converted"
    assert sorted(p.name for p in (tmp_path / "scan").iterdir() if ".tmp-" in p.name) == []


def test_prune_never_removes_what_a_run_is_reading(tmp_path, monkeypatch):
    import corpus as corpus_module
    import reflow.convert

    for name in ("CACHE_ROOT", "QUICK_CACHE_ROOT", "STAGE_ROOT"):
        monkeypatch.setattr(corpus_module, name, tmp_path / name)
    monkeypatch.setattr(corpus_module, "FULL_BUDGET", 0)

    def fake_convert_many(docs, **kwargs):
        summary = SimpleNamespace(ocr=SimpleNamespace(runner="none", failed={}), seconds={},
                                  pages=1, model="")
        return [(_entry(out, "converted"), SimpleNamespace(ok=True, error=None,
                                                           summary=summary))[1]
                for _, out in docs]

    monkeypatch.setattr(reflow.convert, "convert_many", fake_convert_many)
    monkeypatch.setattr(corpus_module.cachedir, "MIN_IDLE", 0)   # only the pin keeps it
    reader = corpus_module.Corpus(quick=False)
    out = reader.doc("scan").out_dir
    assert corpus_module.prune()[0].dry_run   # the first prune of a cache only reports
    report = corpus_module.prune()[0]
    assert report.pinned == [out] and (out / "manifest.json").exists()


def test_a_session_that_only_collects_the_page_checks_never_prunes(monkeypatch):
    """The fast suite collects the page checks in nested sessions (above); none may evict from
    the shared caches."""
    import corpus as corpus_module
    import test_mark_layer

    calls = []
    monkeypatch.setattr(corpus_module, "prune", lambda *a, **k: calls.append(1) or [])
    args = ["--collect-only", "-q", "-m", "pages", "-p", "no:cacheprovider",
            str(Path(test_mark_layer.__file__))]
    assert pytest.main(args) == 0
    assert calls == []


def _radio(i: int, name: str = "g1-1", field: str | None = "F") -> str:
    pdf = f' data-pdf-field="{field}{i}"' if field else ""
    return f'<div class="field field-check"><input type="radio" name="{name}" id="f1-{i}"{pdf}><label for="f1-{i}">Opt {i}</label></div>'


def _group(inner: str, legend: str = "Pick one:") -> str:
    return f'<fieldset class="fields"><legend>{legend}</legend>{inner}</fieldset>'


def test_field_group_type_wants_every_control_to_be_of_that_type():
    spec = {"legend": "Pick one:", "options": ["Opt 1", "Opt 2"], "type": "radio"}
    _run("field_group", spec, _group(_radio(1) + _radio(2)))
    boxes = _group(_radio(1).replace("radio", "checkbox") + _radio(2).replace("radio", "checkbox"))
    with pytest.raises(CheckFailed, match=r"type \['checkbox'\]"):
        _run("field_group", spec, boxes)
    _run("field_group", {**spec, "type": "checkbox"}, boxes)


def test_field_group_radios_must_share_a_name_and_name_their_pdf_field():
    spec = {"legend": "Pick one:", "options": ["Opt 1", "Opt 2"], "type": "radio"}
    with pytest.raises(CheckFailed, match="expected one"):
        _run("field_group", spec, _group(_radio(1) + _radio(2, name="g1-2")))
    with pytest.raises(CheckFailed, match="PDF field"):
        _run("field_group", spec, _group(_radio(1) + _radio(2, field=None)))


def test_field_group_without_a_type_is_unchanged():
    _run("field_group", {"legend": "Pick one:", "options": ["Opt 1"]}, _group(_radio(1)))


def test_fieldsets_named_rejects_an_empty_legend():
    _run("fieldsets_named", True, "<h2>Plan</h2>" + _group(_radio(1), legend="Plan"))
    with pytest.raises(CheckFailed, match="no legend words"):
        _run("fieldsets_named", True, _group(_radio(1), legend=""))
    with pytest.raises(CheckFailed, match="no legend words"):
        _run("fieldsets_named", True, '<fieldset><legend class="sr-only"></legend></fieldset>')


def test_fieldsets_named_wants_a_hidden_legend_printed_in_view_too():
    hidden = '<fieldset><legend class="sr-only">PROPERTY TYPE</legend><input type="checkbox" id="a"></fieldset>'
    _run("fieldsets_named", True, "<h3>PROPERTY TYPE</h3>" + hidden)
    with pytest.raises(CheckFailed, match="not printed anywhere else"):
        _run("fieldsets_named", True, hidden)
    # A visible legend needs no second copy.
    _run("fieldsets_named", True, _group(_radio(1)))


def _box(i: int, label: str, **attrs: str) -> str:
    written = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f'<div class="field"><label for="f1-{i}">{label}</label><input id="f1-{i}"{written}></div>'


def test_input_attrs_reads_the_attributes_of_the_box_with_a_label():
    html = (_box(1, "EMAIL", type="email", inputmode="email", autocomplete="email")
            + _box(2, "EMAIL", type="email", inputmode="email", autocomplete="off")
            + _box(3, "NOTES", autocomplete="off"))
    _run("input_attrs", [{"label": "EMAIL", "type": "email", "inputmode": "email", "autocomplete": "email"},
                         {"label": "EMAIL", "nth": 1, "autocomplete": "off"},
                         {"label": "NOTES", "type": "text", "inputmode": None}], html)


def test_input_attrs_fails_on_a_wrong_missing_or_extra_attribute():
    html = _box(1, "EMAIL", type="email", autocomplete="email")
    with pytest.raises(CheckFailed, match="autocomplete is 'email', expected 'off'"):
        _run("input_attrs", [{"label": "EMAIL", "autocomplete": "off"}], html)
    with pytest.raises(CheckFailed, match="inputmode is None, expected 'email'"):
        _run("input_attrs", [{"label": "EMAIL", "inputmode": "email"}], html)
    with pytest.raises(CheckFailed, match="type is 'email', expected 'text'"):
        _run("input_attrs", [{"label": "EMAIL", "type": "text"}], html)
    with pytest.raises(CheckFailed, match="inputmode is 'numeric', expected None"):
        _run("input_attrs", [{"label": "ZIP", "inputmode": None}], _box(1, "ZIP", inputmode="numeric"))
    with pytest.raises(CheckFailed, match="1 labels bound to an input, wanted number 1"):
        _run("input_attrs", [{"label": "EMAIL", "nth": 1, "type": "email"}], html)
    with pytest.raises(CheckFailed, match="0 labels"):
        _run("input_attrs", [{"label": "FAX", "type": "tel"}], html)


def test_fields_counts_email_and_tel_boxes_as_text():
    html = _box(1, "EMAIL", type="email") + _box(2, "PHONE", type="tel") + _box(3, "NOTES", type="text")
    _run("fields", {"text": 3}, html)
    with pytest.raises(CheckFailed, match="controls"):
        _run("fields", {"text": 2}, html)


def test_field_group_of_text_type_takes_email_and_tel_boxes_for_text_boxes():
    html = ('<fieldset class="fields"><legend>Contact</legend>' + _box(1, "EMAIL", type="email")
            + _box(2, "PHONE", type="tel") + _box(3, "NOTES", type="text") + "</fieldset>")
    spec = {"legend": "Contact", "options": ["EMAIL", "PHONE", "NOTES"], "type": "text"}
    _run("field_group", spec, html)
    with pytest.raises(CheckFailed, match="type"):
        _run("field_group", {**spec, "type": "radio"}, html)


def test_no_blank_runs_rejects_a_blank_left_as_underscores():
    _run("no_blank_runs", True, "<p>Name: </p><p>snake__case and a dash \u2014\u2014</p>")
    with pytest.raises(CheckFailed, match="1 blank lines left as underscores"):
        _run("no_blank_runs", True, "<p>Mother\u2019s Name:_______________ </p>")


def _controls(*controls: str) -> object:
    return parse_html("".join(controls))


def test_control_order_finds_the_names_in_document_order():
    pg = _controls('<input id="a" aria-label="First">', '<label for="b">Second</label><input id="b">',
                   '<input id="c" aria-label="Third">')
    run_check(Assertion("control_order", ["First", "Second", "Third"]), pg, lambda: "", None, 1)
    with pytest.raises(CheckFailed):
        run_check(Assertion("control_order", ["Third", "First"]), pg, lambda: "", None, 1)


def test_labels_in_names_fails_a_box_that_took_its_neighbours_caption():
    ok = _controls('<label for="a">Last Name</label><input id="a" aria-label="Last Name">')
    run_check(Assertion("labels_in_names", True), ok, lambda: "", None, 1)
    bad = _controls('<label for="a">Member ID</label><input id="a" aria-label="First Name">')
    with pytest.raises(CheckFailed):
        run_check(Assertion("labels_in_names", True), bad, lambda: "", None, 1)


_HEADED = ("<thead><tr><th scope='col'>Directory for:</th><th scope='col'>Network</th></tr></thead>"
           "<tbody><tr><th scope='row'>1. Stand-alone</th><td>Prime</td></tr></tbody>")


def test_col_headers_wants_th_col_cells_in_the_first_row_of_the_thead():
    spec = {"rows": 2, "cols": 2, "col_headers": ["Directory for:", "Network"]}
    _table(spec, f"<table>{_HEADED}</table>")
    # A name that is not in the first row, or only some of the names.
    with pytest.raises(CheckFailed, match="as th.scope=col. cells of the first row"):
        _table({**spec, "col_headers": ["Prime"]}, f"<table>{_HEADED}</table>")
    with pytest.raises(CheckFailed, match="as th.scope=col."):
        _table({**spec, "col_headers": ["Directory for:", "Absent"]}, f"<table>{_HEADED}</table>")


def test_col_headers_fails_a_header_row_in_the_tbody_with_a_row_header():
    # What bluedental p5 came out as: the names in the tbody, the first one a row header.
    body = ("<tbody><tr><th scope='row'>Directory for:</th><td>Network</td></tr>"
            "<tr><th scope='row'>1. Stand-alone</th><td>Prime</td></tr></tbody>")
    with pytest.raises(CheckFailed, match="as th.scope=col."):
        _table({"col_headers": ["Directory for:", "Network"]}, f"<table>{body}</table>")
    # A th scoped row in the thead, or td cells there, are not column headers either.
    row_scoped = ("<thead><tr><th scope='row'>Directory for:</th><th scope='col'>Network</th></tr>"
                  "</thead><tbody><tr><td>x</td><td>y</td></tr></tbody>")
    with pytest.raises(CheckFailed):
        _table({"col_headers": ["Directory for:"]}, f"<table>{row_scoped}</table>")
    cells = ("<thead><tr><td>Directory for:</td><td>Network</td></tr></thead>"
             "<tbody><tr><td>x</td><td>y</td></tr></tbody>")
    with pytest.raises(CheckFailed):
        _table({"col_headers": ["Directory for:"]}, f"<table>{cells}</table>")
