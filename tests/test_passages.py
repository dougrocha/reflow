import json
import re
from pathlib import Path

import pytest

from reflow.convert import convert
from reflow.model import Document, Node, PageInfo, Role
from reflow.names import PASSAGES_JSONL
from reflow.output import check_replaceable
from reflow.render.passages import (
    LEAD_CHARS, TABLE_GROUP_CHARS, TRAIL_CHARS, passages, table_groups,
    write_passages,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
BOX = (10.0, 20.0, 200.0, 40.0)


def node(role, text=None, *, page=1, bbox=BOX, children=(), **attrs):
    return Node(role, page=page, bbox=bbox, text=text, attrs=attrs, children=list(children))


def row(*cells, th=False):
    return node(Role.TR, children=[node(Role.TH if th else Role.TD, c) for c in cells])


def document(*children):
    return Document(root=Node(Role.DOC, children=list(children)),
                    pages=[PageInfo(1, 612, 792), PageInfo(2, 612, 792)])


def test_a_hand_built_document_yields_passages_with_trails_leads_and_table_groups():
    intro = node(Role.P, "Bring these items:")
    items = node(Role.LIST, children=[
        node(Role.LI, "A photo ID", children=[node(Role.LIST, children=[node(Role.LI, "Not expired")])]),
        node(Role.LI, "A signed form"),
    ])
    table = node(Role.TABLE, bbox=(5.0, 100.0, 300.0, 400.0), children=[
        node(Role.CAPTION, "Table 1. Fees."),
        row("Item", "Fee", th=True),
        row("Copy", "$5"),
        row("Seal", "$10"),
    ])
    figure = node(Role.FIGURE, alt="A bar chart of fees by year", children=[node(Role.CAPTION, "Fees.")])
    inner = node(Role.SECTION, children=[node(Role.H3, "Costs"), table, figure])
    outer = node(Role.SECTION, children=[
        node(Role.H2, "How to apply"), intro, items, node(Role.NOTE, "Fees may change."),
        node(Role.CAPTION, "Photo 2"), inner])
    doc = document(node(Role.P, "Front matter"), outer)

    got = [(p.id, p.kind, p.text, p.trail, p.lead) for p in passages(doc)]

    assert got == [
        (doc.root.children[0].id, "p", "Front matter", "", ""),
        (outer.children[0].id, "h", "How to apply", "", ""),
        (intro.id, "p", "Bring these items:", "How to apply", ""),
        (items.children[0].id, "li", "A photo ID", "How to apply", "Bring these items:"),
        (items.children[0].children[0].children[0].id, "li", "Not expired", "How to apply",
         "Bring these items:"),
        (items.children[1].id, "li", "A signed form", "How to apply", "Bring these items:"),
        (outer.children[3].id, "p", "Fees may change.", "How to apply", ""),
        (outer.children[4].id, "cap", "Photo 2", "How to apply", ""),
        (inner.children[0].id, "h", "Costs", "How to apply", ""),
        (f"{table.id}#r0", "tbl", "Item | Fee\nCopy | $5\nSeal | $10", "How to apply > Costs",
         "Table 1. Fees."),
        (figure.id, "fig", "A bar chart of fees by year", "How to apply > Costs", "Fees."),
    ]


def test_a_table_passage_has_the_tables_box_and_no_row_has_one():
    table = node(Role.TABLE, bbox=(5.0, 100.0, 300.0, 400.0), page=2,
                 children=[row("a", "b", th=True), row("c", "d")])
    (found,) = passages(document(table))
    assert (found.page, found.bbox) == (2, (5.0, 100.0, 300.0, 400.0))


def test_the_excluded_roles_and_empty_text_yield_nothing():
    drafted = node(Role.FIGURE, alt="Model-written words", provenance="ai-drafted")
    same_as_caption = node(Role.FIGURE, alt="fig.  1", children=[node(Role.CAPTION, "Fig. 1")])
    doc = document(
        node(Role.ARTIFACT, "Page 3 of 9"),
        node(Role.H2, "A heading", provenance="ai-drafted"),
        node(Role.H2, "No box", bbox=None),
        node(Role.H2, "--"),
        node(Role.FIELD, "Name", kind="text"),
        node(Role.GROUP, "Who are you?", children=[node(Role.FIELD, "x")]),
        node(Role.P, "No page", page=None),
        node(Role.P, "No box", bbox=None),
        node(Role.P, " .. -- "),
        node(Role.P, None),
        drafted,
        same_as_caption,
        node(Role.FIGURE, alt="Decoration", decorative=True),
        node(Role.TABLE, bbox=None, children=[row("a")]),
        node(Role.TABLE, children=[row("", ""), row("--", "")]),
    )
    assert list(passages(doc)) == []


def test_ai_drafted_or_unreviewed_text_is_not_indexed_as_the_documents_words():
    drafted = node(Role.P, "A handwritten note the model transcribed.", provenance="ai-drafted", reviewed=False)
    unreviewed = node(Role.P, "Another model transcription.", reviewed=False)
    drafted_item = node(Role.LI, "A drafted list item", provenance="ai-drafted")
    drafted_heading = node(Role.H2, "A heading the model wrote", provenance="ai-drafted")
    unreviewed_heading = node(Role.H3, "Another model heading", reviewed=False)
    own = node(Role.P, "Printed text from the document.")
    doc = document(drafted, unreviewed, node(Role.LIST, children=[drafted_item]),
                   node(Role.SECTION, children=[drafted_heading, own]),
                   node(Role.SECTION, children=[unreviewed_heading]))
    assert [p.text for p in passages(doc)] == ["Printed text from the document."]


def test_a_heading_is_a_passage_with_its_own_box_and_the_headings_above_it_as_trail():
    heading = node(Role.H2, "FY 2024-25 revenue and financing $18.6 million",
                   bbox=(30.0, 50.0, 400.0, 70.0))
    body = node(Role.P, "Barbara Buikema, General Manager")
    doc = document(node(Role.SECTION, children=[
        node(Role.H1, "Budget-in-Brief"),
        node(Role.SECTION, children=[heading, body]),
    ]))

    got = [(p.id, p.kind, p.text, p.trail, p.lead, p.bbox) for p in passages(doc)]

    assert got == [
        (doc.root.children[0].children[0].id, "h", "Budget-in-Brief", "", "", BOX),
        (heading.id, "h", "FY 2024-25 revenue and financing $18.6 million", "Budget-in-Brief", "",
         (30.0, 50.0, 400.0, 70.0)),
        (body.id, "p", "Barbara Buikema, General Manager",
         "Budget-in-Brief > FY 2024-25 revenue and financing $18.6 million", "", BOX),
    ]


def test_a_heading_outside_a_section_is_a_passage_too():
    heading = node(Role.H3, "Loose heading")
    (found,) = passages(document(heading))
    assert (found.id, found.kind, found.text, found.trail) == (heading.id, "h", "Loose heading", "")


def test_a_heading_trail_keeps_the_innermost_headings_within_its_limit():
    long = "W" * 100
    inner = node(Role.SECTION, children=[node(Role.H3, long + "3"), node(Role.P, "Body text")])
    middle = node(Role.SECTION, children=[node(Role.H2, long + "2"), inner])
    outer = node(Role.SECTION, children=[node(Role.H1, long + "1"), middle])
    *_, found = passages(document(outer))
    # Three headings do not fit; the outer two go until the innermost does.
    assert found.text == "Body text"
    assert found.trail == long + "3" and len(found.trail) <= TRAIL_CHARS


def test_a_long_lead_in_keeps_its_end_and_a_long_caption_its_start():
    words = " ".join(f"word{i}" for i in range(100)) + " end:"
    caption = "Table 9. " + "very long " * 40
    doc = document(
        node(Role.P, words),
        node(Role.LIST, children=[node(Role.LI, "first")]),
        node(Role.TABLE, children=[node(Role.CAPTION, caption), row("a", "b")]),
    )
    _, li, tbl = list(passages(doc))
    assert len(li.lead) <= LEAD_CHARS and li.lead.endswith("end:")
    assert len(tbl.lead) <= LEAD_CHARS and tbl.lead.startswith("Table 9. ")


def test_table_groups_repeat_the_header_and_stay_within_the_limit():
    body = [row(f"row{i}", "x" * 80) for i in range(30)]
    table = node(Role.TABLE, children=[row("Name", "Value", th=True), *body])
    groups = list(table_groups(table))
    assert [k for k, _ in groups] == list(range(len(groups))) and len(groups) > 1
    for _, text in groups:
        assert text.startswith("Name | Value\n")
        assert len(text) <= TABLE_GROUP_CHARS
    assert [line for _, t in groups for line in t.split("\n")[1:]] == \
        [f"row{i} | {'x' * 80}" for i in range(30)]


def test_a_row_longer_than_the_limit_is_its_own_group_and_never_cut():
    huge = "y" * (TABLE_GROUP_CHARS + 50)
    table = node(Role.TABLE, children=[row("H", th=True), row("a"), row(huge), row("b")])
    assert [t for _, t in table_groups(table)] == ["H\na", f"H\n{huge}", "H\nb"]


def test_a_table_with_only_header_rows_is_one_group():
    table = node(Role.TABLE, children=[row("A", "B", th=True)])
    assert list(table_groups(table)) == [(0, "A | B")]


def test_to_line_is_compact_utf8_in_a_fixed_key_order():
    (found,) = passages(document(node(Role.P, "Café \"quoted\"")))
    line = found.to_line()
    assert "é" in line and '"n":1,"b":[10.0,20.0,200.0,40.0],"k":"p"' in line
    assert list(json.loads(line)) == ["id", "n", "b", "k", "t", "h", "c"]


def test_write_passages_writes_the_header_then_one_line_each(tmp_path):
    doc = document(node(Role.P, "One"), node(Role.P, "Two"))
    count = write_passages(doc, tmp_path, "abc123")
    lines = (tmp_path / PASSAGES_JSONL).read_text(encoding="utf-8").splitlines()
    assert count == 2 and len(lines) == 3
    assert json.loads(lines[0]) == {"schema": 1, "v": "abc123", "pages": [[612, 792], [612, 792]]}
    assert lines[0] == '{"schema":1,"v":"abc123","pages":[[612,792],[612,792]]}'


@pytest.fixture(scope="module")
def resnet(tmp_path_factory):
    pdf = SAMPLES / "resnet.pdf"
    if not pdf.exists():
        pytest.skip("samples/resnet.pdf is not here")
    base = tmp_path_factory.mktemp("passages")
    for jobs in (1, 2):
        convert(pdf, base / f"jobs{jobs}", jobs=jobs, page_images=False)
    return base


def test_every_passage_is_in_its_pages_chunk_and_inside_its_page(resnet):
    out = resnet / "jobs1"
    header, *rest = [json.loads(line) for line in
                     (out / PASSAGES_JSONL).read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    chunks = [(c["first"], c["last"], (out / c["href"]).read_text(encoding="utf-8"))
              for c in manifest["chunks"]]
    assert rest
    for p in rest:
        page = p["n"]
        width, height = header["pages"][page - 1]
        x0, top, x1, bottom = p["b"]
        assert 0 <= x0 < x1 <= width and 0 <= top < bottom <= height, p["id"]
        (html,) = [h for first, last, h in chunks if first <= page <= last]
        assert f'data-node="{p["id"].split("#")[0]}"' in html, p["id"]
    assert {"p", "tbl"} <= {p["k"] for p in rest}
    assert all(re.fullmatch(r".+#r\d+", p["id"]) for p in rest if p["k"] == "tbl")


def test_the_header_version_is_the_manifests(resnet):
    out = resnet / "jobs1"
    header = json.loads((out / PASSAGES_JSONL).read_text(encoding="utf-8").split("\n", 1)[0])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert header["v"] == manifest["v"]
    assert header["schema"] == 1 and len(header["pages"]) == manifest["pageCount"]


def test_jobs_one_and_two_write_the_same_bytes(resnet):
    assert (resnet / "jobs1" / PASSAGES_JSONL).read_bytes() == \
        (resnet / "jobs2" / PASSAGES_JSONL).read_bytes()


def test_a_converted_folder_is_still_replaceable(resnet):
    check_replaceable(resnet / "jobs1")


DAY = {"status": [
    {"label": "Closed / Holiday", "label_boxes": [[410.8, 872.9, 495.8, 884.2]],
     "swatch": [387.1, 870.0, 405.5, 888.5], "evidence": [33.0, 236.0, 60.0, 249.0]},
    {"label": "Teacher PD or Work Day/ Student Holiday",
     "label_boxes": [[586.5, 872.4, 719.6, 883.6], [586.5, 884.4, 670.9, 895.6]],
     "swatch": [563.5, 870.0, 581.7, 888.5], "evidence": [33.0, 236.0, 60.0, 249.0]}],
    "notes": [{"text": "Labor Day", "box": [39.9, 1042.9, 146.7, 1053.4]}]}


def test_a_calendar_day_reads_with_its_statuses_and_notes():
    table = node(Role.TABLE, children=[
        row("S", "M", th=True),
        node(Role.TR, children=[node(Role.TD, "01"), node(Role.TD, "02", day=DAY)])])
    (found,) = passages(document(table))
    assert found.text == ("S | M\n01 | 02 — Closed / Holiday — Teacher PD or Work Day/ Student "
                          "Holiday — Labor Day")


def test_an_icon_legend_status_joins_its_passage():
    (found,) = passages(document(node(Role.P, "Dyer, Kim", status="Not Accepting New Patients")))
    assert found.text == "Dyer, Kim — Not Accepting New Patients"


def test_an_unread_figure_yields_no_passage():
    fig = node(Role.FIGURE, unread=True, alt="Handwriting, not transcribed.", ocr_guess="Sxa")
    assert list(passages(document(fig))) == []


def test_only_an_alt_the_author_wrote_is_quoted():
    printed = node(Role.P, "Fees by year")
    alts = [node(Role.FIGURE, alt="Fees by year", provenance="ai-drafted", reviewed=False),
            node(Role.FIGURE, alt="Signature, not transcribed.", unread=True),
            node(Role.FIGURE, alt="Checked", provenance="reviewer"),
            node(Role.FIGURE, children=[node(Role.CAPTION, "Fig. 2")]),
            node(Role.FIGURE),
            node(Role.FIGURE, alt="A bar chart of fees")]
    doc = document(printed, *alts)
    assert [(p.kind, p.text) for p in passages(doc)] == [("p", "Fees by year"),
                                                         ("fig", "A bar chart of fees")]
