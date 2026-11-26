"""The defect-injection harness's own logic (tests/robustness/defects.py and inject.py), without
converting or rendering anything: each defect on a small hand-built document, and the rule for
which review items catch a defect."""

from __future__ import annotations

import random

import pytest
from robustness.defects import DEFECTS
from robustness.inject import review_catches, signals
from robustness.kinds import Damage, Unsupported
from robustness.oracle import Fact

from reflow.analyze.sections import nest_sections
from reflow.model import Document, Node, PageInfo, Role, make_id

DEFECT = {d.id: d for d in DEFECTS}


def _node(role: Role, text: str, y: float, page: int = 1) -> Node:
    bbox = (72.0, y, 500.0, y + 12)
    return Node(role, id=make_id(page, bbox, text), page=page, bbox=bbox, text=text)


def _doc() -> Document:
    """Two sections of prose and a table on page 1, one more section on page 2."""
    rows = [Node(Role.TR, id=f"r{i}", page=1, children=[_node(Role.TD, a, 300 + 20 * i),
                                                         _node(Role.TD, b, 300 + 20 * i)])
            for i, (a, b) in enumerate([("Model", "Error"), ("Plain 18", "27.94"),
                                        ("ResNet 34", "25.03"), ("ResNet 50", "22.85")])]
    flat = [
        _node(Role.H1, "Annual report", 40),
        _node(Role.H2, "First section", 70),
        _node(Role.P, "The staff filed five official files after the check.", 90),
        _node(Role.P, "Our office flew the flag of the field team in 2024.", 110),
        _node(Role.H3, "A smaller part", 140),
        _node(Role.P, "Each fluent reader will find it in the right order.", 160),
        _node(Role.H2, "Second section", 200),
        Node(Role.TABLE, id="t1", page=1, bbox=(72.0, 300.0, 500.0, 380.0), children=rows,
             attrs={"rows": 4, "header_rows": 1}),
        _node(Role.H2, "Third section", 60, page=2),
        _node(Role.P, "Nothing on this page is tested by a spec at all.", 80, page=2),
    ]
    root = Node(Role.DOC, id="doc", children=nest_sections(flat), attrs={"title": "Annual report"})
    return Document(root, [PageInfo(1, 612, 792), PageInfo(2, 612, 792)])


def _texts(doc: Document) -> list[tuple[str, str]]:
    return [(n.role.value, n.text) for n in doc.flow() if n.text]


@pytest.mark.parametrize("defect", DEFECT)
def test_every_defect_changes_page_one_and_round_trips(defect):
    doc = _doc()
    before = doc.to_json(indent=None)
    damage = DEFECT[defect].mutate(doc, random.Random(1), frozenset({1}))
    after = doc.to_json(indent=None)
    assert after != before
    assert damage.pages == {1} and damage.boxes.get(1) and damage.note
    assert Document.from_json(after).to_json(indent=None) == after


@pytest.mark.parametrize("defect", DEFECT)
def test_a_defect_with_nothing_to_change_on_the_tested_pages_is_unsupported(defect):
    doc = _doc()
    for node in list(doc.root.walk()):   # page 3 has nothing
        assert node.page != 3
    with pytest.raises(Unsupported):
        DEFECT[defect].mutate(doc, random.Random(1), frozenset({3}))


def test_a_demoted_heading_joins_the_section_before_and_the_others_keep_their_ids():
    doc = _doc()
    ids = {n.children[0].text: n.id for n in doc.root.walk() if n.role is Role.SECTION}
    for seed in range(20):   # the seed that picks "A smaller part"
        doc = _doc()
        damage = DEFECT["demote-heading"].mutate(doc, random.Random(seed), frozenset({1}))
        if "A smaller part" in damage.note:
            break
    sections = {n.children[0].text: n for n in doc.root.walk() if n.role is Role.SECTION}
    assert set(sections) == {"First section", "Second section", "Third section"}
    assert all(sections[t].id == ids[t] for t in sections)
    first = [c.text for c in sections["First section"].children]
    assert first[-2:] == ["A smaller part", "Each fluent reader will find it in the right order."]
    assert ("p", "A smaller part") in _texts(doc)


def test_swapped_paragraphs_keep_their_words():
    doc = _doc()
    words = sorted(w for _, t in _texts(doc) for w in t.split())
    DEFECT["swap-paragraphs"].mutate(doc, random.Random(1), frozenset({1}))
    assert sorted(w for _, t in _texts(doc) for w in t.split()) == words
    paras = [t for r, t in _texts(doc) if r == "p"]
    assert paras[:2] == ["Our office flew the flag of the field team in 2024.",
                         "The staff filed five official files after the check."]


def test_a_dropped_row_is_a_body_row_and_the_count_follows():
    for seed in range(10):
        doc = _doc()
        DEFECT["drop-table-row"].mutate(doc, random.Random(seed), frozenset({1}))
        table = next(n for n in doc.root.walk() if n.role is Role.TABLE)
        assert table.children[0].children[0].text == "Model" and len(table.children) == 3
        assert table.attrs["rows"] == 3


def test_header_rows_spanned_rows_and_named_headers_are_never_dropped():
    doc = _doc()
    table = next(n for n in doc.root.walk() if n.role is Role.TABLE)
    table.attrs["header_rows"] = 2                       # row 1 is a second header row
    table.children[2].children[0].attrs["rowspan"] = 2   # row 2 spans into row 3
    with pytest.raises(Unsupported):
        DEFECT["drop-table-row"].mutate(doc, random.Random(1), frozenset({1}))
    table.children[2].children[0].attrs.pop("rowspan")
    table.children[3].children[0].attrs["id"] = "h-resnet-50"   # a header other cells name
    with pytest.raises(Unsupported):                     # one droppable row is not two
        DEFECT["drop-table-row"].mutate(doc, random.Random(1), frozenset({1}))


def test_a_changed_number_changes_one_digit():
    doc = _doc()
    texts = lambda: [n.text for n in doc.root.walk() if n.text]   # noqa: E731 - table cells too
    before = texts()
    DEFECT["change-number"].mutate(doc, random.Random(1), frozenset({1}))
    changed = [(a, b) for a, b in zip(before, texts(), strict=True) if a != b]
    assert len(changed) == 1
    a, b = changed[0]
    assert len(a) == len(b) and sum(x != y for x, y in zip(a, b, strict=True)) == 1


def test_a_review_item_catches_a_defect_only_when_new_over_its_region_or_of_its_codes():
    damage = Damage(frozenset({1}), {1: (72, 90, 500, 122)})
    swap = DEFECT["swap-paragraphs"]
    old_order = Fact("order-disagreement", 1, (72, 80, 500, 130))
    old_other = Fact("unfinished-sentence", 1, (72, 90, 500, 100))
    elsewhere = Fact("literal-bullets", 1, (72, 600, 500, 620))
    new_here = Fact("low-confidence", 1, (72, 100, 500, 110))
    clean = [old_order, old_other]
    assert review_catches(swap, clean, clean, damage) == ["order-disagreement (already there)"]
    assert review_catches(swap, clean, [*clean, elsewhere, new_here], damage) == [
        "low-confidence", "order-disagreement (already there)"]
    assert review_catches(DEFECT["change-number"], clean, clean, damage) == []


class _Out:
    def __init__(self, items, nodes):
        self._items, self._nodes = items, nodes

    def review(self):
        return self._items

    def document(self):
        return {"root": {"id": "doc", "children": self._nodes}}


def test_a_review_code_points_at_its_own_targets_then_its_nodes_then_the_page():
    nodes = [{"id": "a", "bbox": [0, 0, 10, 10]}, {"id": "b", "bbox": [90, 90, 100, 100]}]
    page_item = {"id": "page:1", "page": 1, "warn": "order-disagreement",
                 "findings": [{"code": "ruled-box-doubt"}, {"code": "scan-noisy"}],
                 "targets": [{"code": "order-disagreement", "box": [0, 0, 10, 10]},
                             {"code": "ruled-box-doubt", "box": [90, 90, 100, 100]}]}
    collapsed = {"id": "a", "page": 1, "warn": "literal-bullets", "targets": [],
                 "findings": [{"code": "literal-bullets"}], "group": {"nodeIds": ["a", "b"]}}
    got = set(signals(_Out([page_item, collapsed], nodes)))
    assert got == {Fact("order-disagreement", 1, (0, 0, 10, 10)),
                   Fact("ruled-box-doubt", 1, (90, 90, 100, 100)),
                   Fact("scan-noisy", 1, None),
                   Fact("literal-bullets", 1, (0, 0, 10, 10)),
                   Fact("literal-bullets", 1, (90, 90, 100, 100))}
    between = Damage(frozenset({1}), {1: (45, 45, 55, 55)})
    clean = signals(_Out([], nodes))
    swap = DEFECT["swap-paragraphs"]
    assert review_catches(swap, clean, list(got), between) == ["scan-noisy"]


def test_the_defect_scorecard_says_where_each_conversion_came_from():
    from robustness.inject import _currency
    record = {"documents": {"fw9": {"from": "page-check cache fw9/0123456789abcdef"},
                            "resnet": {"from": "converted in this run"},
                            "uscis-i9": {"from": "page-check cache uscis-i9/fedcba9876543210"}}}
    (line,) = _currency(record)
    assert line.startswith("The conversions the cells read: fw9, uscis-i9 from the page-check "
                           "cache; resnet converted in this run.")
    assert "docs:check" in line
