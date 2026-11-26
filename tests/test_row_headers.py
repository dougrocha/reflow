from reflow.analyze.row_headers import apply, link_header_bands, mark_row_headers
from reflow.model import Node, Role


def cell(role, text, **attrs):
    return Node(role, text=text, attrs=attrs)


def table(rows, **attrs):
    return Node(Role.TABLE, id="tbl", attrs=attrs,
                children=[Node(Role.TR, children=row) for row in rows])


def label_table():
    return table([
        [cell(Role.TH, "Model"), cell(Role.TH, "Top-1"), cell(Role.TH, "Top-5")],
        [cell(Role.TD, "ResNet-18"), cell(Role.TD, "69.8"), cell(Role.TD, "89.1")],
        [cell(Role.TD, "ResNet-34"), cell(Role.TD, "73.3"), cell(Role.TD, "91.4")],
        [cell(Role.TD, "ResNet-50"), cell(Role.TD, "76.1"), cell(Role.TD, "-")],
    ], header_rows=1)


def test_label_plus_numbers_table_gets_row_headers_and_keeps_column_header():
    tbl = label_table()

    assert mark_row_headers(tbl) is True

    body = tbl.children[1:]
    assert [row.children[0].role for row in body] == [Role.TH, Role.TH, Role.TH]
    assert all(row.children[0].attrs["scope"] == "row" for row in body)
    assert [row.children[1].role for row in body] == [Role.TD, Role.TD, Role.TD]
    header = tbl.children[0].children
    assert [c.role for c in header] == [Role.TH, Role.TH, Role.TH]
    assert all(c.attrs.get("scope") != "row" for c in header)


def test_words_only_table_is_untouched():
    tbl = table([
        [cell(Role.TH, "Feature"), cell(Role.TH, "Status")],
        [cell(Role.TD, "Captions"), cell(Role.TD, "Done")],
        [cell(Role.TD, "Tables"), cell(Role.TD, "Partial")],
    ], header_rows=1)
    before = tbl.to_dict()

    assert mark_row_headers(tbl) is False
    assert link_header_bands(tbl) is False
    assert tbl.to_dict() == before


def test_docling_table_with_row_scope_is_untouched():
    tbl = table([
        [cell(Role.TH, "Model"), cell(Role.TH, "Top-1")],
        [cell(Role.TH, "ResNet-18", scope="row"), cell(Role.TD, "69.8")],
        [cell(Role.TH, "ResNet-34", scope="row"), cell(Role.TD, "73.3")],
    ], header_rows=1)
    before = tbl.to_dict()

    assert mark_row_headers(tbl) is False
    assert tbl.to_dict() == before


def test_two_row_header_band_links_data_cells_to_both_levels():
    tbl = table([
        [cell(Role.TH, "Model"), cell(Role.TH, "Accuracy", colspan=2)],
        [cell(Role.TH, ""), cell(Role.TH, "2024"), cell(Role.TH, "2025")],
        [cell(Role.TD, "A"), cell(Role.TD, "1"), cell(Role.TD, "2")],
        [cell(Role.TD, "B"), cell(Role.TD, "3"), cell(Role.TD, "4")],
    ], header_rows=2)

    assert link_header_bands(tbl) is True

    head_row, sub_row = tbl.children[0].children, tbl.children[1].children
    assert [c.attrs["id"] for c in head_row] == ["tbl-h0-0", "tbl-h0-1"]
    assert [c.attrs["id"] for c in sub_row] == ["tbl-h1-0", "tbl-h1-1", "tbl-h1-2"]
    for row in tbl.children[2:]:
        assert row.children[1].attrs["headers"] == ["tbl-h0-1", "tbl-h1-1"]
        assert row.children[2].attrs["headers"] == ["tbl-h0-1", "tbl-h1-2"]
        assert row.children[0].attrs["headers"] == ["tbl-h0-0", "tbl-h1-0"]


def test_apply_counts_tables_and_skips_already_marked_ones():
    root = Node(Role.DOC, children=[label_table()])

    assert apply(root) == {"row_headers": 1, "bands": 0}
    assert apply(root) == {"row_headers": 0, "bands": 0}
