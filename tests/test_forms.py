from pathlib import Path

import pytest
from conftest import make_pdf
from formpdf import make_form_pdf
from reflow.extract.pages import extract_pages
from reflow.extract.forms import read_widgets
from reflow.extract.parallel import extract_document

SAMPLES = Path(__file__).parent.parent / "samples"


@pytest.fixture
def form_pdf(tmp_path):
    return make_form_pdf(tmp_path / "form.pdf")


def test_reads_each_widget_kind(form_pdf):
    widgets = read_widgets(form_pdf)[1]
    by_name = {}
    for w in widgets:
        by_name.setdefault(w.name, []).append(w)
    assert [w.kind for w in widgets] == ["text", "text", "combo", "radio", "radio", "checkbox"]
    first = by_name["first"][0]
    assert (first.tooltip, first.maxlen, first.multiline) == ("Applicant first name", 20, False)
    assert first.bbox == (72.0, 104.0, 292.0, 122.0)
    assert by_name["notes"][0].multiline and by_name["notes"][0].maxlen is None
    state = by_name["state"][0]
    assert state.options == ["AZ", "CA", "NY"] and state.selected == [1]
    assert [(w.export, w.checked) for w in by_name["Contact"]] == [("email", True), ("phone", False)]
    assert not by_name["agree"][0].checked


def test_a_document_with_no_form_has_no_widgets(tmp_path):
    pdf = make_pdf(tmp_path / "plain.pdf", ["text"])
    assert read_widgets(pdf) == {}
    assert all(p.widgets == [] for p in extract_pages(pdf))


def test_widgets_survive_the_worker_pool_in_the_same_order(form_pdf):
    serial, _ = extract_document(form_pdf, 1)
    pooled, _ = extract_document(form_pdf, 2, chunk_pages=1)
    assert serial[0].widgets and pooled[0].widgets == serial[0].widgets


def test_i9_widget_counts():
    widgets = [w for ws in read_widgets(SAMPLES / "corpus" / "uscis-i9.pdf").values() for w in ws]
    kinds = [w.kind for w in widgets]
    assert (kinds.count("text"), kinds.count("combo"), kinds.count("checkbox")) == (117, 5, 8)
    assert next(w for w in widgets if w.name == "US Social Security Number").maxlen == 9


def test_a_password_field_is_read_as_one_and_keeps_its_value(tmp_path):
    pdf = make_form_pdf(tmp_path / "pw.pdf", password=True)
    [pw] = [w for w in read_widgets(pdf)[1] if w.name == "pw"]
    assert (pw.kind, pw.password, pw.value) == ("text", True, "hunter2")


def test_an_ordinary_text_field_is_not_a_password(form_pdf):
    [first] = [w for w in read_widgets(form_pdf)[1] if w.name == "first"]
    assert first.password is False
