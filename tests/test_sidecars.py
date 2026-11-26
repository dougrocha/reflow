import json

from reflow.model import Document, Fact, Node, PageInfo, Role, Source
from reflow.render.sidecars import TEXT_LIMIT, page_sidecar, sidecar_name, write_page_layouts


def _doc() -> Document:
    root = Node(Role.DOC, children=[
        Node(Role.H1, page=1, bbox=(72, 80, 300, 100), text="Title",
             attrs={"why": "large type", "secret": "x"}),
        Node(Role.TABLE, page=1, bbox=(72, 120, 500, 300), children=[
            Node(Role.TD, page=1, bbox=(72, 120, 200, 140), text="cell",
                 facts=[Fact("table-giant-cell", Source.HEURISTIC, "one cell")]),
        ]),
        Node(Role.P, page=2, bbox=(72, 80, 500, 120), text="Second page",
             facts=[Fact("role-agreement", Source.DOCLING)]),
    ])
    return Document(root=root, pages=[PageInfo(number=1, width=612, height=792),
                                      PageInfo(number=2, width=612, height=792)])


def test_two_page_document_writes_one_file_per_page(tmp_path):
    assert write_page_layouts(_doc(), tmp_path) == 2
    assert (tmp_path / "layout" / "0001.json").is_file()
    assert (tmp_path / "layout" / "0002.json").is_file()
    assert sidecar_name(2) == "layout/0002.json"


def test_sidecar_shape_keys():
    side = page_sidecar(_doc(), 1)
    assert set(side) == {"n", "w", "h", "sections", "nodes", "labels", "review"}
    assert (side["n"], side["w"], side["h"]) == (1, 612, 792)
    assert side["sections"] == []
    assert set(side["nodes"][0]) == {"id", "role", "bbox", "source", "confidence", "text",
                                     "attrs", "facts"}


def test_nodes_in_walk_order_including_table_cells():
    side = page_sidecar(_doc(), 1)
    assert [n["role"] for n in side["nodes"]] == ["h1", "table", "td"]
    assert all(n["bbox"] for n in side["nodes"])


def test_text_is_cut_at_limit():
    root = Node(Role.DOC, children=[
        Node(Role.P, page=1, bbox=(0, 0, 10, 10), text="x" * 500)])
    doc = Document(root=root, pages=[PageInfo(number=1, width=10, height=10)])
    text = page_sidecar(doc, 1)["nodes"][0]["text"]
    assert len(text) == TEXT_LIMIT


def test_attrs_are_filtered_to_inspector_keys():
    node = page_sidecar(_doc(), 1)["nodes"][0]
    assert node["attrs"] == {"why": "large type"}


def test_facts_serialized_with_to_dict():
    td = page_sidecar(_doc(), 1)["nodes"][2]
    assert td["facts"] == [Fact("table-giant-cell", Source.HEURISTIC, "one cell").to_dict()]


def test_review_lists_only_nodes_with_a_severity_fact():
    side = page_sidecar(_doc(), 1)
    assert side["review"] == [side["nodes"][2]["id"]]
    assert page_sidecar(_doc(), 2)["review"] == []


def test_page_without_nodes_still_gets_a_file(tmp_path):
    root = Node(Role.DOC, children=[Node(Role.P, page=1, bbox=(0, 0, 10, 10), text="a")])
    doc = Document(root=root, pages=[PageInfo(number=1, width=10, height=10),
                                     PageInfo(number=2, width=10, height=10)])
    assert write_page_layouts(doc, tmp_path) == 2
    empty = json.loads((tmp_path / "layout" / "0002.json").read_text(encoding="utf-8"))
    assert empty["nodes"] == []


def test_file_is_compact_and_keeps_unicode(tmp_path):
    root = Node(Role.DOC, children=[
        Node(Role.P, page=1, bbox=(0, 0, 10, 10), text="café")])
    doc = Document(root=root, pages=[PageInfo(number=1, width=10, height=10)])
    write_page_layouts(doc, tmp_path)
    raw = (tmp_path / "layout" / "0001.json").read_text(encoding="utf-8")
    assert "café" in raw
    assert '": ' not in raw and ", " not in raw


def test_labels_cover_the_fact_codes_on_the_page():
    from reflow.analyze.confidence import CODES
    assert page_sidecar(_doc(), 1)["labels"] == {
        "table-giant-cell": CODES["table-giant-cell"].label}
    assert page_sidecar(_doc(), 2)["labels"] == {"role-agreement": CODES["role-agreement"].label}


def test_the_ocr_guess_of_an_unread_figure_is_for_the_inspector():
    fig = Node(Role.FIGURE, page=1, bbox=(0, 0, 10, 10),
               attrs={"unread": True, "alt": "Handwriting, not transcribed.", "ocr_guess": "Sxa"})
    doc = Document(root=Node(Role.DOC, children=[fig]),
                   pages=[PageInfo(number=1, width=612, height=792)])
    assert page_sidecar(doc, 1)["nodes"][0]["attrs"]["ocr_guess"] == "Sxa"
