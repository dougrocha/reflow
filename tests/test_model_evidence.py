"""Fact, Span and Node.fact: validation, the JSON round trip, and old JSON still loads."""

import json

import pytest

from reflow.analyze import confidence
from reflow.model import Document, Fact, Node, PageInfo, Role, Source, Span


def test_facts_and_spans_round_trip():
    node = Node(Role.P, page=1, bbox=(0, 0, 10, 10), text="see page 12",
                facts=[Fact("order-disagreement", Source.DOCLING, "tau 0.4", {"tau": 0.4})],
                spans=[Span(4, 11, "link", "#p12")])
    page = PageInfo(1, 612, 792, facts=[Fact("ocr-page", Source.OCR)])
    doc = Document(root=Node(Role.DOC, children=[node]), pages=[page])
    again = Document.from_json(doc.to_json())
    assert again.root.children[0].facts == node.facts
    assert again.root.children[0].spans == node.spans
    assert again.pages[0].facts == page.facts


def test_empty_facts_and_spans_add_no_keys():
    d = Node(Role.P, page=1, text="x").to_dict()
    assert "facts" not in d and "spans" not in d
    assert "facts" not in PageInfo(1, 612, 792).to_dict()


def test_old_json_without_facts_still_loads():
    raw = {"schema": 1, "meta": {}, "pages": [{"number": 1, "width": 612, "height": 792,
                                                 "rotation": 0, "kind": "born_digital",
                                                 "char_count": 3, "image_coverage": 0.0}],
           "root": {"id": "0:a", "role": "doc", "confidence": 1.0, "source": "heuristic"}}
    doc = Document.from_dict(json.loads(json.dumps(raw)))
    assert doc.root.facts == [] and doc.pages[0].facts == []


def test_code_registry_is_well_formed():
    for code, spec in confidence.CODES.items():
        assert code == code.lower() and " " not in code
        assert -1.0 <= spec.weight <= 1.0
        assert spec.severity in (None, "error", "warning")
        assert spec.scope in ("node", "page", "doc")
    for role, threshold in confidence.ROLE_THRESHOLD.items():
        assert isinstance(role, Role) and 0 < threshold < 1


def test_flow_opens_sections_and_keeps_other_containers_whole():
    para = Node(Role.P, page=1, text="a")
    lst = Node(Role.LIST, page=1, children=[Node(Role.LI, page=1, text="x")])
    head = Node(Role.H2, page=1, text="H")
    inner = Node(Role.SECTION, children=[Node(Role.H3, page=1, text="h3")])
    section = Node(Role.SECTION, children=[head, lst, inner])
    doc = Document(root=Node(Role.DOC, children=[para, section]))
    assert [n.role for n in doc.flow()] == [Role.P, Role.H2, Role.LIST, Role.H3]


@pytest.mark.parametrize("start, end, kind", [
    (4, 4, "link"),           # empty
    (5, 4, "link"),           # reversed
    (-1, 4, "link"),          # negative start
    (0, 4, "footnote"),       # unknown kind
])
def test_invalid_span_raises(start, end, kind):
    with pytest.raises(ValueError):
        Span(start, end, kind, "#p12")


def test_span_from_dict_is_validated():
    with pytest.raises(ValueError):
        Span.from_dict([3, 3, "link", "#p1"])


@pytest.mark.parametrize("code", ["Bad Code", "Upper", "has space", ""])
def test_invalid_fact_code_raises(code):
    with pytest.raises(ValueError):
        Fact(code, Source.HEURISTIC)


@pytest.mark.parametrize("value", [(1, 2), {"a": 1}, [[1]], [(1, 2)], {1, 2}])
def test_non_json_fact_data_raises(value):
    with pytest.raises(TypeError):
        Fact("alt-missing", Source.HEURISTIC, data={"x": value})


def test_json_scalar_fact_data_is_accepted():
    fact = Fact("alt-missing", Source.HEURISTIC,
                data={"n": 3, "f": 0.5, "s": "x", "b": True, "z": None, "l": [1, "a", None]})
    assert fact.data["l"] == [1, "a", None]


def test_node_fact_appends_and_returns_the_same_object():
    node = Node(Role.P, page=1, text="x")
    returned = node.fact("order-disagreement", Source.DOCLING, "tau 0.4", tau=0.4)
    assert node.facts == [returned]
    assert returned == Fact("order-disagreement", Source.DOCLING, "tau 0.4", {"tau": 0.4})


def test_node_fact_validates_data():
    node = Node(Role.P, page=1, text="x")
    with pytest.raises(TypeError):
        node.fact("alt-missing", Source.HEURISTIC, x=(1, 2))
    assert node.facts == []


def test_tags_source_round_trips():
    assert Source("tags") is Source.TAGS
    node = Node(Role.H1, page=1, text="Title", source=Source.TAGS)
    again = Document.from_json(Document(root=Node(Role.DOC, children=[node])).to_json())
    assert again.root.children[0].source is Source.TAGS
