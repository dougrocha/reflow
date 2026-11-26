import pikepdf

from conftest import make_pdf, make_text_pdf
from reflow.analyze.page_integrity import flag_empty
from reflow.analyze.review import queue
from reflow.ingest.pages import ingest
from reflow.ingest.ocr import needs_ocr
from reflow.model import Node, Role


def test_nonblank_empty_transcript_is_an_error_but_blank_page_is_not(tmp_path):
    path = make_pdf(tmp_path / "pages.pdf", ["text", "scan", "empty"])
    doc = ingest(path)
    doc.meta["lang"] = "en"
    # A figure is not a text transcript.
    doc.root.children = [Node(Role.FIGURE, page=2)]
    flag_empty(doc, path)
    flag_empty(doc, path)
    items = queue(doc)
    assert [(i["page"], i["severity"], i["label"]) for i in items] == [
        (1, "error", "Non-blank page has an empty transcript"),
        (2, "error", "Non-blank page has an empty transcript")]


def test_readable_transcript_needs_no_empty_page_error(tmp_path):
    path = make_pdf(tmp_path / "text.pdf", ["text"])
    doc = ingest(path)
    doc.root.children = [Node(Role.P, page=1, text="Hello accessible world")]
    flag_empty(doc, path)
    assert doc.pages[0].facts == []


def test_sparse_page_with_outlined_content_gets_ocr_despite_page_number(tmp_path):
    path = make_text_pdf(tmp_path / "outlined.pdf", [[(300, 760, 9, "15")]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        # Outlined glyphs are paths, just as on the audit's page 30.
        ops = " ".join(f"{50+i*4} 680 m {52+i*4} 686 l {54+i*4} 680 l h f"
                       for i in range(40))
        pdf.pages[0].Contents = pdf.make_stream(pdf.pages[0].Contents.read_bytes()
                                              + ops.encode())
        pdf.save(path)
    assert sorted(needs_ocr(ingest(path), path).pages) == [1]


def test_decorative_rule_with_page_number_does_not_request_ocr(tmp_path):
    path = make_text_pdf(tmp_path / "rule.pdf", [[(300, 760, 9, "15")]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].Contents = pdf.make_stream(pdf.pages[0].Contents.read_bytes()
                                              + b" 50 680 m 550 680 l S")
        pdf.save(path)
    assert needs_ocr(ingest(path), path).pages == {}


def test_internal_table_row_labels_do_not_hide_an_empty_transcript(tmp_path):
    path = make_pdf(tmp_path / "table.pdf", ["scan"])
    doc = ingest(path)
    doc.root.children = [Node(Role.TABLE, page=1, children=[
        Node(Role.TR, page=1, text="row 0", children=[Node(Role.TD, page=1)])])]
    flag_empty(doc, path)
    assert [f.code for f in doc.pages[0].facts] == ["empty-transcript"]
