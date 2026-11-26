"""`reflow audit` (checks.source) and `reflow doctor` (checks.doctor)."""

import importlib
import json

import pikepdf
import pytest
from pikepdf import Dictionary, Name

from conftest import make_pdf, make_tagged_pdf
from reflow.checks import doctor
from reflow.checks.source import CHECKS, audit

META = ("SRC-META-001", "SRC-META-002", "SRC-META-003", "SRC-META-004")


def test_every_check_reported_once_in_spec_order(tmp_path):
    report = audit(make_pdf(tmp_path / "a.pdf", ["text"]))
    assert [f.check.id for f in report.findings] == [c.id for c in CHECKS]
    assert set(report.by_id) == {c.id for c in CHECKS}


def test_meta_checks_pass_on_a_tagged_titled_pdf(tmp_path):
    path = make_pdf(tmp_path / "good.pdf", ["text"], title="Report", lang="en", tagged=True)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.ViewerPreferences = Dictionary(DisplayDocTitle=True)
        pdf.Root.StructTreeRoot = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
        pdf.save(path)
    report = audit(path)
    by = report.by_id
    assert [by[i].status for i in META] == ["pass"] * 4, [by[i].detail for i in META]
    assert by["SRC-TXT-001"].status == "pass"


def test_meta_checks_fail_on_a_bare_pdf(tmp_path):
    by = audit(make_pdf(tmp_path / "bare.pdf", ["text"])).by_id
    assert by["SRC-META-001"].status == "fail"
    assert by["SRC-META-002"].status == "fail"
    assert by["SRC-META-003"].status == "fail"
    assert by["SRC-META-004"].status == "pass"


def test_title_without_display_doc_title_warns(tmp_path):
    by = audit(make_pdf(tmp_path / "t.pdf", ["text"], title="Report")).by_id
    assert by["SRC-META-002"].status == "warn"
    assert "DisplayDocTitle" in by["SRC-META-002"].detail


def test_scanned_page_fails_txt_001_with_the_page(tmp_path):
    report = audit(make_pdf(tmp_path / "scan.pdf", ["text", "scan", "text"]))
    f = report.by_id["SRC-TXT-001"]
    assert f.status == "fail" and f.pages == [2]
    assert "1 of 3 pages" in f.detail and "(2)" in f.detail
    assert report.failed


def test_unembedded_standard_font_fails_txt_002(tmp_path):
    f = audit(make_pdf(tmp_path / "f.pdf", ["text"])).by_id["SRC-TXT-002"]
    assert f.status == "fail" and "Helvetica" in f.detail and f.pages == [1]


def test_embedded_font_passes_txt_002(tmp_path):
    path = make_pdf(tmp_path / "f.pdf", ["text"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        font.FontDescriptor = Dictionary(Type=Name.FontDescriptor, Flags=32,
                                         FontFile=pdf.make_stream(b"\x00"))
        pdf.save(path)
    assert audit(path).by_id["SRC-TXT-002"].status == "pass"


def test_symbolic_font_without_tounicode_warns(tmp_path):
    path = make_pdf(tmp_path / "f.pdf", ["text"])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        font = pdf.pages[0].Resources.Font.F1
        font.FontDescriptor = Dictionary(Type=Name.FontDescriptor, Flags=4,
                                         FontFile=pdf.make_stream(b"\x00"))
        pdf.save(path)
    f = audit(path).by_id["SRC-TXT-002"]
    assert f.status == "warn" and "ToUnicode" in f.detail


def test_untagged_pdf_skips_fig_and_tbl(tmp_path):
    by = audit(make_pdf(tmp_path / "u.pdf", ["text"])).by_id
    assert by["SRC-FIG-001"].status == "skip" and by["SRC-FIG-001"].detail == "untagged"
    assert by["SRC-TBL-001"].status == "skip"


def test_tagged_pdf_checks_figures_and_tables(tmp_path):
    # The fixture's /Figure has /Alt and its /Table has a /TH: both pass.
    by = audit(make_tagged_pdf(tmp_path / "tagged.pdf")).by_id
    assert by["SRC-FIG-001"].status == "pass", by["SRC-FIG-001"].detail
    assert by["SRC-TBL-001"].status == "pass", by["SRC-TBL-001"].detail


def test_figure_without_alt_and_table_without_th_fail(tmp_path):
    path = make_tagged_pdf(tmp_path / "tagged.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for elem in pdf.Root.StructTreeRoot.K.K:
            if elem.S == "/Figure":
                del elem["/Alt"]
            if elem.S == "/Table":
                elem.K[0].K[0].S = Name.TD  # the header cell becomes data
        pdf.save(path)
    by = audit(path).by_id
    assert by["SRC-FIG-001"].status == "fail" and by["SRC-FIG-001"].pages == [1]
    assert by["SRC-TBL-001"].status == "fail" and by["SRC-TBL-001"].pages == [2]


def test_the_tag_tree_parsed_on_a_pool_gives_the_same_report(tmp_path, monkeypatch):
    from reflow.extract import pagecontent, parallel
    path = make_tagged_pdf(tmp_path / "tagged.pdf")
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        for elem in pdf.Root.StructTreeRoot.K.K:
            if elem.S == "/Figure":
                del elem["/Alt"]
        pdf.save(path)
    serial = audit(path, jobs=1).to_dict()
    monkeypatch.setenv("REFLOW_WORKER_BUDGET", "0")            # every slot asked for is free
    monkeypatch.setattr(pagecontent, "PAGES_PER_WORKER", 1)   # a pool even for two pages
    pools = []

    class Pool(parallel.ProcessPoolExecutor):
        def __init__(self, *args, **kwargs):
            pools.append(kwargs.get("max_workers", args[0] if args else None))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(parallel, "ProcessPoolExecutor", Pool)
    assert audit(path, jobs=2).to_dict() == serial
    assert pools == [2]


def test_no_form_fields_passes_frm_001(tmp_path):
    assert audit(make_pdf(tmp_path / "n.pdf", ["text"])).by_id["SRC-FRM-001"].status == "pass"


def test_widgets_without_tooltip_fail_frm_001(tmp_path):
    from formpdf import make_form_pdf
    path = make_form_pdf(tmp_path / "form.pdf")
    f = audit(path).by_id["SRC-FRM-001"]
    # Only "first" carries a /TU; the notes, state, radios and checkbox do not.
    assert f.status == "fail" and f.pages == [1], f.detail
    assert "have no tooltip" in f.detail


def test_not_a_pdf_is_a_failing_report_not_an_exception(tmp_path):
    junk = tmp_path / "junk.pdf"
    junk.write_text("not a pdf")
    report = audit(junk)
    assert report.failed and report.filename == "junk.pdf"
    assert report.by_id["SRC-TXT-001"].status == "fail"
    assert all(f.status == "skip" for f in report.findings if f.check.id != "SRC-TXT-001")


def test_missing_file_is_a_failing_report(tmp_path):
    assert audit(tmp_path / "nope.pdf").failed


def test_encrypted_pdf_needs_the_password(tmp_path):
    path = make_pdf(tmp_path / "locked.pdf", ["text"], password="pw")
    assert audit(path).by_id["SRC-TXT-001"].detail == "encrypted; pass --password"
    report = audit(path, "pw")
    assert report.by_id["SRC-TXT-001"].status == "pass"
    assert report.by_id["SRC-META-004"].status == "pass"


def test_to_dict_round_trips_through_json(tmp_path):
    report = audit(make_pdf(tmp_path / "s.pdf", ["scan"], title="T"))
    data = json.loads(json.dumps(report.to_dict()))
    assert set(data) == {"filename", "passed", "findings"}
    assert data["passed"] is False
    assert set(data["findings"][0]) == {"id", "title", "wcag", "matterhorn", "status", "detail",
                                        "pages"}
    txt = next(f for f in data["findings"] if f["id"] == "SRC-TXT-001")
    assert txt["status"] == "fail" and txt["pages"] == [1]


# ---- doctor -----------------------------------------------------------------------------------

def test_doctor_runs_every_probe():
    probes = doctor.probes()
    assert [p.name for p in probes] == [name for name, _, _ in doctor.PROBES]
    qpdf = probes[0]
    assert qpdf.status == "ok" and "libqpdf" in qpdf.detail


def test_doctor_survives_a_raising_import(monkeypatch):
    def boom(name, package=None):
        raise RuntimeError(f"cannot import {name}")

    monkeypatch.setattr(importlib, "import_module", boom)
    result = doctor.probes()
    assert [p.name for p in result] == [name for name, _, _ in doctor.PROBES]
    by = {p.name: p for p in result}
    for name in ("ocrmypdf", "docling", "cuda"):
        assert by[name].status == "broken" and "RuntimeError" in by[name].detail
    assert by["qpdf"].status == "ok"


@pytest.mark.parametrize("name", ["ocrmypdf", "docling"])
def test_doctor_reports_a_missing_module(monkeypatch, name):
    real = importlib.import_module

    def missing(mod, package=None):
        if mod == name:
            raise ImportError(f"No module named '{name}'")
        return real(mod, package)

    monkeypatch.setattr(importlib, "import_module", missing)
    by = {p.name: p for p in doctor.probes()}
    assert by[name].status == "missing"
