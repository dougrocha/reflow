from types import SimpleNamespace

import pytest

pytest.importorskip("docling_core")

from docling_core.types.doc import DocItemLabel  # noqa: E402

from conftest import make_text_pdf
from reflow.analyze import docling_layout, hybrid
from reflow.analyze.hybrid import analyze_hybrid, join_across_pages, merge, route
from reflow.analyze.constants import SPLIT_COLUMNS, SPLIT_SOFT
from reflow.extract.pages import extract_pages
from reflow.model import Node, PageInfo, PageKind, Role, Source


def signals(**kw):
    base = {"split": 0, "figures": 0, "tables": 0, "rules": 0, "blocks": 5, "short_blocks": 0}
    return {**base, **kw}


def test_routing_rules():
    info = {"pages": {
        1: signals(),                                   # plain text
        2: signals(split=SPLIT_COLUMNS),                # clean columns: heuristics are fine
        3: signals(split=SPLIT_SOFT),
        4: signals(tables=1),
        5: signals(rules=200),                          # rules alone: not hard
        6: signals(figures=2, vector_figures=1),
        7: signals(blocks=20, short_blocks=12),
        8: signals(tables=1),                           # but scanned
    }}
    pages = [PageInfo(n, 612, 792, kind=PageKind.SCANNED if n == 8 else PageKind.BORN_DIGITAL)
             for n in range(1, 9)]
    r = route(info, pages)
    assert r.hard == {3: ["soft-columns"], 4: ["tables"], 6: ["vector-figures"],
                      7: ["fragments"]}


TBL = (72, 300, 300, 400)
CELLS = [["alpha", "beta"], ["gamma", "delta"]]


def _table_node(bbox, cells, *, source=Source.HEURISTIC, caption=None):
    rows = [Node(Role.TR, page=2, text=f"row {r}",
                 children=[Node(Role.TD, page=2, text=c) for c in row])
            for r, row in enumerate(cells)]
    return Node(Role.TABLE, page=2, bbox=bbox, source=source,
                children=([caption] if caption else []) + rows)


def _docling_table(bbox, cells, caption=None):
    return _table_node(bbox, cells, source=Source.DOCLING, caption=caption)


def _figure(bbox):
    return Node(Role.FIGURE, page=2, bbox=bbox, attrs={"needs": "alt"}, source=Source.DOCLING)


def _merge(heuristic, docling):
    decisions = []
    return merge(heuristic, docling, {2}, decisions), decisions


def test_merge_keeps_the_heuristic_text_of_hard_pages():
    h = [Node(Role.P, page=p, text=f"h{p}") for p in (1, 2, 3)]
    d = [Node(Role.P, page=2, text="d2", source=Source.DOCLING)]
    assert [n.text for n in merge(h, d, {2, 3})] == ["h1", "h2", "h3"]


def test_hybrid_keeps_heuristic_headings_and_levels_on_hard_pages(tmp_path, monkeypatch):
    body = [(72, 160 + 14 * i, 10, f"Body text line {i} of the section.") for i in range(3)]
    pdf = make_text_pdf(tmp_path / "t.pdf", [
        [(72, 100, 16, "Chapter One", "B"), *body],
        [(72, 100, 16, "Chapter Two", "B"), (72, 125, 12, "A subsection", "B"), *body],
    ])
    pages = list(extract_pages(pdf))

    # Docling handles page 2; its headings and text are not used, only its tables and figures.
    def fake_docling(path, numbers, converter):
        assert numbers == [2]
        return [Node(Role.H2, page=2, bbox=(72, 100, 200, 116), text="Docling title",
                     source=Source.DOCLING),
                Node(Role.P, page=2, text="Body", source=Source.DOCLING)]

    monkeypatch.setattr(hybrid, "docling_nodes", fake_docling)
    monkeypatch.setattr(hybrid, "route", lambda info, infos: hybrid.Routing({2: ["test"]}))
    nodes, routing = analyze_hybrid(pdf, pages, {}, [PageInfo(1, 612, 792), PageInfo(2, 612, 792)],
                                    converter=object())
    heads = [(n.text, n.role) for n in nodes if n.role.value.startswith("h")]
    # Same 16pt bold style -> same level on both pages; the 12pt subsection nests below it.
    assert heads == [("Chapter One", Role.H2), ("Chapter Two", Role.H2),
                     ("A subsection", Role.H3)]
    assert {n.source for n in nodes if n.page == 2} == {Source.HEURISTIC}
    assert "Docling title" not in [n.text for n in nodes]


def test_docling_table_replaces_the_heuristic_table_it_covers_and_keeps_its_caption():
    cap = Node(Role.CAPTION, page=2, bbox=(72, 280, 300, 296), text="Table 1: Results")
    heur = [_table_node(TBL, CELLS, caption=cap)]
    dl = [_docling_table(TBL, CELLS, caption=Node(Role.CAPTION, page=2, text="Docling caption"))]
    out, dec = _merge(heur, dl)
    assert [n.role for n in out] == [Role.TABLE]
    table = out[0]
    assert table.source == Source.DOCLING
    assert [c.text for c in table.children if c.role == Role.CAPTION] == ["Table 1: Results"]
    assert (table.attrs["rows"], table.attrs["columns"]) == (2, 2)
    assert dec[0]["adopted"] is True and dec[0]["recall"] == 1.0
    assert table.attrs["why"][0].startswith("docling table 2x2")


def test_docling_table_is_rejected_when_it_loses_text():
    heur = [_table_node(TBL, CELLS)]
    dl = [_docling_table(TBL, [["alpha", "beta"], ["gamma", "xx"]])]  # "delta" lost
    out, dec = _merge(heur, dl)
    assert out[0].source == Source.HEURISTIC
    assert dec[0]["adopted"] is False and "recall" in dec[0]["reason"]
    assert any("not adopted" in w for w in out[0].attrs["why"])


def _score_table(rows, *, source=Source.HEURISTIC):
    return Node(Role.TABLE, page=2, bbox=TBL, source=source, children=[
        Node(Role.TR, page=2, children=[Node(Role.TD, page=2, text=value) for value in row])
        for row in rows
    ])


def test_changed_numbers_are_rejected_even_when_their_digits_match():
    heuristic = _score_table([["Item", "Score"], ["alpha", "12"], ["beta", "34"]])
    docling = _score_table([["Item", "Score"], ["alpha", "21"], ["beta", "43"]],
                           source=Source.DOCLING)

    out, decisions = _merge([heuristic], [docling])

    assert out[0].source == Source.HEURISTIC
    assert decisions[0]["adopted"] is False
    assert decisions[0]["missing_numbers"] == ["12", "34"]
    assert "numeric values missing" in decisions[0]["reason"]
    assert "numeric values missing" in out[0].attrs["why"][0]


def test_numbers_moved_between_rows_are_rejected():
    heuristic = _score_table([["Item", "Score"], ["alpha", "12"], ["beta", "34"]])
    docling = _score_table([["Item", "Score"], ["alpha", "34"], ["beta", "12"]],
                           source=Source.DOCLING)

    out, decisions = _merge([heuristic], [docling])

    assert out[0].source == Source.HEURISTIC
    assert decisions[0]["adopted"] is False
    assert decisions[0]["missing_numbers"] == []
    assert decisions[0]["split_rows"] == 2


def test_faithful_numeric_table_is_adopted():
    heuristic = _score_table([["Item", "Score"], ["alpha", "12"], ["beta", "34"]])
    docling = _score_table([["Item", "Score"], ["alpha", "12"], ["beta", "34"]],
                           source=Source.DOCLING)

    out, decisions = _merge([heuristic], [docling])

    assert out[0].source == Source.DOCLING
    assert decisions[0]["adopted"] is True
    assert decisions[0]["missing_numbers"] == []
    assert decisions[0]["split_rows"] == 0


def test_text_block_replacement_uses_complete_values():
    text = Node(Role.P, page=2, bbox=TBL, text="Result was 12, then 34.")
    docling = _score_table([["Result", "was 12"], ["then", "34"]], source=Source.DOCLING)

    out, decisions = _merge([text], [docling])

    assert out[0].source == Source.DOCLING
    assert decisions[0]["adopted"] is True


def test_docling_table_smaller_than_the_heuristic_table_is_rejected():
    heur = [_table_node(TBL, [["alpha", "beta"], ["gamma", "delta"], ["eps", "zeta"]])]
    dl = [_docling_table(TBL, [["alpha beta", "gamma"], ["delta eps zeta", ""]])]
    out, dec = _merge(heur, dl)
    assert out[0].source == Source.HEURISTIC
    assert dec[0]["adopted"] is False and "smaller" in dec[0]["reason"]


def test_docling_figure_takes_over_the_chart_labels_it_covers():
    labels = [Node(Role.P, page=2, bbox=(80, 120, 120, 130), text="Accuracy"),
              Node(Role.P, page=2, bbox=(200, 230, 260, 240), text="Epochs")]
    body = Node(Role.P, page=2, bbox=(72, 300, 300, 330), text="Body text")
    out, dec = _merge([*labels, body], [_figure((72, 100, 300, 260))])
    figures = [n for n in out if n.role == Role.FIGURE]
    assert len(figures) == 1 and [c.text for c in figures[0].children
                                    if c.attrs.get("figure_label")] == ["Accuracy", "Epochs"]
    assert [c.bbox for c in figures[0].children] == [labels[0].bbox, labels[1].bbox]
    assert [n.text for n in out if n.role == Role.P] == ["Body text"]
    assert dec[0]["adopted"] is True


def test_docling_figure_that_takes_ocr_labels_keeps_the_ocr_in_picture_warning():
    label = Node(Role.P, page=2, bbox=(80, 120, 120, 130), text="T2 — -", source=Source.OCR,
                 attrs={"figure_label": True})
    photo = Node(Role.FIGURE, page=2, bbox=(80, 110, 290, 250), children=[label])
    photo.fact("ocr-in-picture", Source.OCR, "1 lines read inside the picture are listed under it")
    out, dec = _merge([photo], [_figure((72, 100, 300, 260))])
    [figure] = [n for n in out if n.role == Role.FIGURE]
    assert dec[0]["adopted"] is True and figure.source == Source.DOCLING
    assert [c.text for c in figure.children] == ["T2 — -"]
    assert "ocr-in-picture" in {f.code for f in figure.facts}


def test_docling_figure_does_not_absorb_body_prose():
    prose = " ".join(["word"] * 30)
    heur = [Node(Role.P, page=2, bbox=(80, 120, 280, 200), text=prose)]
    out, dec = _merge(heur, [_figure((72, 100, 300, 260))])
    assert [n.text for n in out] == [prose]
    assert dec[0]["adopted"] is False and "body text" in dec[0]["reason"]


def test_a_heuristic_block_only_partly_inside_blocks_the_adoption():
    heur = [Node(Role.P, page=2, bbox=(72, 220, 300, 340), text="Half in the table")]
    out, dec = _merge(heur, [_docling_table(TBL, CELLS)])
    assert [n.text for n in out] == ["Half in the table"]
    assert dec[0]["adopted"] is False and "overlaps a p block" in dec[0]["reason"]


def test_the_heuristic_caption_is_reattached_and_docling_s_caption_is_dropped():
    cap = Node(Role.CAPTION, page=2, bbox=(72, 270, 300, 290), text="Table 2: Scores")
    heur = [cap, _table_node(TBL, CELLS)]
    dl = [_docling_table(TBL, CELLS, caption=Node(Role.CAPTION, page=2,
                                                  text="Table 2 Scores (docling)"))]
    out, dec = _merge(heur, dl)
    assert [n.role for n in out] == [Role.TABLE]
    assert [c.text for c in out[0].children if c.role == Role.CAPTION] == ["Table 2: Scores"]
    assert all("docling" not in (c.text or "").lower() for c in out[0].children)


def test_an_adopted_table_is_never_claimed_twice():
    heur = [_table_node(TBL, CELLS)]
    out, dec = _merge(heur, [_docling_table(TBL, CELLS), _docling_table(TBL, CELLS)])
    assert [d["adopted"] for d in dec] == [True, False]
    assert len([n for n in out if n.role == Role.TABLE]) == 1


def test_adopted_objects_keep_their_place_in_reading_order():
    above = Node(Role.P, page=2, bbox=(72, 100, 300, 120), text="Above")
    below = Node(Role.P, page=2, bbox=(72, 420, 300, 440), text="Below")
    out, _ = _merge([above, _table_node(TBL, CELLS), below], [_docling_table(TBL, CELLS)])
    assert [n.role for n in out] == [Role.P, Role.TABLE, Role.P]


def test_a_new_figure_is_placed_before_the_first_node_below_it():
    above = Node(Role.P, page=2, bbox=(72, 100, 300, 120), text="Above")
    below = Node(Role.P, page=2, bbox=(72, 300, 300, 320), text="Below")
    out, dec = _merge([above, below], [_figure((72, 150, 300, 260))])
    assert [n.role for n in out] == [Role.P, Role.FIGURE, Role.P]
    assert dec[0]["adopted"] is True


def test_docling_converts_each_hard_page_alone_and_page_numbers_map_back(mixed_pdf, monkeypatch):
    import pikepdf
    from reflow.analyze import docling_layout
    seen = []  # page count of each sub-PDF Docling was given

    class FakeConverter:
        def convert(self, path):
            with pikepdf.open(path) as pdf:
                seen.append(len(pdf.pages))
            return type("R", (), {"document": None})()

    monkeypatch.setattr(docling_layout, "to_nodes", lambda doc: [
        Node(Role.P, page=1, text="a"), Node(Role.LIST, page=1, children=[
            Node(Role.LI, page=1, text="b")])])
    nodes = hybrid.docling_nodes(mixed_pdf, [4, 2], FakeConverter())
    assert seen == [1, 1]  # never two pages in one conversion, so Docling can't join them
    assert [n.page for n in nodes] == [2, 2, 4, 4]
    assert nodes[1].children[0].page == 2 and nodes[3].children[0].page == 4


# Fake Docling items: just enough of ProvenanceItem / DocItem for reflow.analyze.docling_layout.
class _Box:
    def __init__(self, *xyxy):
        self.xyxy = xyxy

    def to_top_left_origin(self, page_height):
        return self

    def as_tuple(self):
        return self.xyxy


def _prov(page, span, box=(10, 10, 90, 20)):
    return SimpleNamespace(page_no=page, charspan=span, bbox=_Box(*box))


def _doc(*items, pages=(1, 2, 3, 4)):
    sizes = {p: SimpleNamespace(size=SimpleNamespace(height=100)) for p in pages}
    return SimpleNamespace(pages=sizes, iterate_items=lambda: [(item, 0) for item in items])


def _item(label, text, provs, **attrs):
    return SimpleNamespace(self_ref="#/texts/0", label=label, text=text, prov=provs, **attrs)


def _text_item(text, provs):
    return _item(DocItemLabel.TEXT, text, provs)


TEXT = "the first part of the paragraph carries on the next page with the rest of it"


def test_text_merged_across_a_page_break_splits_at_the_second_provenance():
    # Docling's span for the page-3 run starts 2 chars before "next", inside "on the".
    start = TEXT.index("next") - 2
    item = _text_item(TEXT, [_prov(2, (0, start)), _prov(3, (start, len(TEXT)), (5, 30, 95, 80))])
    nodes = docling_layout.to_nodes(_doc(item))
    assert [(n.page, n.text) for n in nodes] == [
        (2, TEXT[:TEXT.index("next") - 1]), (3, TEXT[TEXT.index("next"):])]
    assert nodes[0].bbox == (10, 10, 90, 20) and nodes[1].bbox == (5, 30, 95, 80)


def test_split_boundary_snaps_to_the_space_when_the_span_overshoots():
    # Docling's span for the page-3 run starts 2 chars after "next", inside "next page".
    start = TEXT.index("next") + 2
    item = _text_item(TEXT, [_prov(2, (0, start)), _prov(3, (start, len(TEXT)))])
    nodes = docling_layout.to_nodes(_doc(item))
    assert [n.page for n in nodes] == [2, 3]
    assert nodes[0].text + " " + nodes[1].text == TEXT


def test_split_falls_back_to_the_whole_text_on_the_first_page_when_spans_do_not_fit():
    item = _text_item(TEXT, [_prov(2, (0, 40)), _prov(3, (500, len(TEXT)))])
    nodes = docling_layout.to_nodes(_doc(item))
    assert [(n.page, n.bbox, n.text) for n in nodes] == [(2, (10, 10, 90, 20), TEXT)]


def test_item_with_one_page_or_no_text_is_not_split():
    one = _text_item(TEXT, [_prov(2, (0, 10)), _prov(2, (10, len(TEXT)))])
    assert [(n.page, n.text) for n in docling_layout.to_nodes(_doc(one))] == [(2, TEXT)]
    picture = _item(DocItemLabel.PICTURE, "", [_prov(2, (0, 0)), _prov(3, (0, 0))])
    figures = docling_layout.to_nodes(_doc(picture))
    assert [(n.role, n.page) for n in figures] == [(Role.FIGURE, 2)]


def test_list_items_split_into_a_new_list_at_a_page_break():
    parent = SimpleNamespace(cref="#/groups/0")
    items = [_item(DocItemLabel.LIST_ITEM, text, [_prov(page, (0, len(text)))],
                   enumerated=True, marker=marker, parent=parent)
             for page, marker, text in [(1, "9.", "Ninth item"), (1, "9.", "Tenth item"),
                                        (2, "10.", "Eleventh item")]]
    nodes = docling_layout.to_nodes(_doc(*items))
    # One list per page; the continuation keeps its printed marker, so it still numbers 10.
    assert [(n.role, n.page, len(n.children)) for n in nodes] == [(Role.LIST, 1, 2),
                                                                  (Role.LIST, 2, 1)]
    assert nodes[1].children[0].page == 2 and nodes[1].children[0].attrs["marker"] == "10."


def test_paragraph_continued_on_the_next_page_is_joined():
    first = Node(Role.P, page=1, bbox=(72, 600, 540, 612), text="The form asks for the infor-")
    second = Node(Role.P, page=2, bbox=(72, 90, 540, 102), text="mation about your income.")
    joined = join_across_pages([first, second], {1, 2})
    assert [n.text for n in joined] == [
        "The form asks for the information about your income."]
    node = joined[0]
    assert node.id == first.id and node.page == 1 and node.bbox == first.bbox
    assert node.attrs["pages"] == [1, 2]
    assert node.attrs["why"] == ["joined with the paragraph that continues on the next page"]


def test_sentence_ending_with_a_full_stop_is_not_joined():
    first = Node(Role.P, page=1, text="The deadline is 30 April.")
    second = Node(Role.P, page=2, text="applies to every filer in the table.")
    assert [n.text for n in join_across_pages([first, second], {1, 2})] == [
        "The deadline is 30 April.", "applies to every filer in the table."]


def test_heading_between_pages_prevents_the_join():
    first = Node(Role.P, page=1, text="Continued from the previous")
    heading = Node(Role.H2, page=2, text="Next section")
    second = Node(Role.P, page=2, text="page of the text.")
    out = join_across_pages([first, heading, second], {1, 2})
    assert [n.role for n in out] == [Role.P, Role.H2, Role.P]
    assert out[0].text == "Continued from the previous"
