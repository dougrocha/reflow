import copy
import json
import pickle

import pytest

from reflow.model import (Document, Mark, Node, PageInfo, PageKind, Role, Source, Span, Why,
                          code_of, make_id)
from testkit import mark_at


def test_a_why_is_its_prose_to_everything_but_code_of():
    why = Why("a grid of names", "row-twin")
    node = Node(Role.P, page=1, bbox=(0, 0, 10, 10), text="x", attrs={"why": [why, "plain"]})
    assert why == "a grid of names" and f"({why})" == "(a grid of names)"
    assert json.loads(json.dumps(node.to_dict()))["attrs"]["why"] == ["a grid of names", "plain"]
    assert "row-twin" not in json.dumps(node.to_dict())
    assert [code_of(w) for w in node.attrs["why"]] == ["row-twin", None]
    for same in (pickle.loads(pickle.dumps(why)), copy.deepcopy(why), copy.copy(why)):
        assert code_of(same) == "row-twin" and same == why
    back = Node.from_dict(json.loads(json.dumps(node.to_dict())))
    assert [code_of(w) for w in back.attrs["why"]] == [None, None]   # JSON keeps the words only


def test_id_is_stable_across_float_noise():
    a = make_id(3, (72.0, 100.2, 300.4, 112.0), "Hello")
    b = make_id(3, (72.1, 99.9, 300.3, 111.8), "Hello")
    assert a == b
    assert a.startswith("3:")


def test_id_ignores_role():
    p = Node(Role.P, page=1, bbox=(0, 0, 10, 10), text="Intro")
    h = Node(Role.H2, page=1, bbox=(0, 0, 10, 10), text="Intro")
    assert p.id == h.id


def test_id_changes_with_text_or_page():
    base = make_id(1, (0, 0, 10, 10), "a")
    assert make_id(1, (0, 0, 10, 10), "b") != base
    assert make_id(2, (0, 0, 10, 10), "a") != base


def test_walk_is_document_order():
    root = Node(Role.DOC, children=[
        Node(Role.H1, page=1, text="T"),
        Node(Role.LIST, page=1, children=[Node(Role.LI, page=1, text="x")]),
        Node(Role.P, page=1, text="end"),
    ])
    assert [n.role for n in root.walk()] == [Role.DOC, Role.H1, Role.LIST, Role.LI, Role.P]


def test_json_round_trip():
    doc = Document(
        root=Node(Role.DOC, children=[
            Node(Role.FIGURE, page=2, bbox=(1, 2, 3, 4), attrs={"alt": "Chart"},
                 confidence=0.4, source=Source.LLM),
        ]),
        pages=[PageInfo(1, 612, 792), PageInfo(2, 612, 792, kind=PageKind.SCANNED)],
        meta={"title": "Report"},
    )
    again = Document.from_json(doc.to_json())
    assert again == doc



def test_a_calendar_days_annotations_round_trip():
    day = {"status": [{"label": "Closed / Holiday", "label_boxes": [[410.8, 872.9, 495.8, 884.2]],
                       "swatch": [387.1, 870.0, 405.5, 888.5], "evidence": [33.0, 236.0, 60.0, 249.0]}],
           "notes": [{"text": "Labor Day", "box": [39.9, 1042.9, 146.7, 1053.4]}]}
    doc = Document(root=Node(Role.DOC, children=[
        Node(Role.TD, page=1, bbox=(40, 238, 50, 247), text="02", attrs={"day": day})]),
        pages=[PageInfo(1, 612, 792)])
    again = Document.from_json(doc.to_json())
    assert again == doc and again.root.children[0].attrs["day"] == day

def test_ensure_unique_ids():
    dup = dict(page=1, bbox=(0, 0, 10, 10), text="Page 1")
    doc = Document(root=Node(Role.DOC, children=[Node(Role.ARTIFACT, **dup),
                                                 Node(Role.ARTIFACT, **dup)]))
    doc.ensure_unique_ids()
    ids = [n.id for n in doc.root.walk()]
    assert len(ids) == len(set(ids))


# --- inline marks ---------------------------------------------------------------------------

def test_marks_round_trip_with_and_without():
    plain = Node(Role.P, page=1, text="abc")
    assert "marks" not in plain.to_dict()
    text = "Do not attach it"
    node = Node(Role.P, page=1, text=text, marks=[
        mark_at(text, "not", "strong"), mark_at(text, "attach", "doubt", alt="attack")])
    d = node.to_dict()
    assert d["marks"] == [[3, 6, "strong", "not"], [7, 13, "doubt", "attach", "attack"]]
    assert Node.from_dict(json.loads(json.dumps(d))).marks == node.marks


def test_old_json_without_marks_loads():
    node = Node.from_dict({"id": "1:a", "role": "p", "text": "hi"})
    assert node.marks == []


@pytest.mark.parametrize("args", [
    (0, 3, "bold", "abc"),            # unknown kind
    (2, 2, "strong", ""),             # empty range
    (3, 2, "em", ""),                 # reversed
    (-1, 2, "em", "abc"),             # negative start
    (0, 3, "del-unread", "abc"),      # a point has no range
    (2, 2, "del-unread", "x"),        # a point has no text
    (0, 3, "strong", "ab"),           # text of the wrong length
])
def test_invalid_mark_raises(args):
    with pytest.raises(ValueError):
        Mark(*args)


def test_alt_belongs_to_doubt_only():
    with pytest.raises(ValueError):
        Mark(0, 3, "strong", "abc", alt="x")
    assert Mark(0, 3, "doubt", "abc", alt="abd").alt == "abd"
    assert Mark(1, 1, "del-unread", "").end == 1


def test_set_text_shifts_a_mark_after_a_cut():
    text = "....... See the Note here"
    node = Node(Role.P, page=1, text=text, marks=[mark_at(text, "Note", "strong")],
                spans=[Span(text.index("here"), len(text), "link", "https://x.test")])
    node.set_text("See the Note here")
    assert node.marks == [Mark(8, 12, "strong", "Note")]
    assert node.spans == [Span(13, 17, "link", "https://x.test")]
    assert node.facts == []


def test_set_text_keeps_a_doubt_over_an_edited_word():
    text = "the cat sat"
    node = Node(Role.P, page=1, text=text, marks=[mark_at(text, "cat", "doubt", alt="cut")])
    node.set_text("the cut sat")
    assert node.marks == [Mark(4, 7, "doubt", "cut")]    # alt equals the new text: cleared
    node.set_text("the cart sat")
    assert node.marks == [Mark(4, 8, "doubt", "cart")]
    assert node.facts == []


def test_set_text_reports_a_lost_semantic_mark_only():
    text = "keep struck words"
    node = Node(Role.P, page=1, text=text,
                marks=[mark_at(text, "struck", "del"), mark_at(text, "words", "strong")])
    node.set_text("keep ")
    assert node.marks == []
    assert [(f.code, f.data) for f in node.facts] == [
        ("ocr-mark-lost", {"kind": "del", "text": "struck"})]


def test_a_lost_strong_adds_no_fact():
    node = Node(Role.P, page=1, text="a bold b", marks=[Mark(2, 6, "strong", "bold")])
    node.set_text("a  b")
    assert node.marks == [] and node.facts == []


def test_del_unread_between_brackets_follows_a_cut_before_it():
    node = Node(Role.P, page=1, text="xx [] end", marks=[Mark(4, 4, "del-unread", "")])
    node.set_text("[] end")
    assert node.marks == [Mark(1, 1, "del-unread", "")]
    assert node.facts == []


def test_del_unread_is_lost_when_both_neighbours_go():
    node = Node(Role.P, page=1, text="a [] b", marks=[Mark(3, 3, "del-unread", "")])
    node.set_text("a  b")
    assert node.marks == []
    assert node.facts[0].code == "ocr-mark-lost" and node.facts[0].data["kind"] == "del-unread"


def test_join_inline_shifts_the_second_part():
    a = Node(Role.P, text="Prior Authorization", marks=[Mark(0, 5, "strong", "Prior")],
             spans=[Span(6, 19, "link", "#p2")])
    b = Node(Role.P, text="Request", marks=[Mark(0, 7, "em", "Request")])
    a.join_inline([a, b])
    assert a.text == "Prior Authorization Request"
    assert a.spans == [Span(6, 19, "link", "#p2")]
    assert a.marks == [Mark(0, 5, "strong", "Prior"), Mark(20, 27, "em", "Request")]
    assert all(a.text[m.start:m.end] == m.text for m in a.marks)


def test_tracked_change_marks_round_trip():
    text = "a [] court, or the business court"
    node = Node(Role.P, page=1, text=text, marks=[
        Mark(3, 3, "del-unread", ""), mark_at(text, ", or the business court", "ins"),
        mark_at(text, "business", "doubt")])
    d = node.to_dict()
    assert d["marks"][0] == [3, 3, "del-unread", ""]
    assert Node.from_dict(json.loads(json.dumps(d))).marks == node.marks


def _loaded(spans=(), marks=(), text="Deep nets win"):
    d = {"id": "1:a", "role": "p", "text": text}
    if spans:
        d["spans"] = list(spans)
    if marks:
        d["marks"] = list(marks)
    return Node.from_dict(d)


@pytest.mark.parametrize("node, problem", [
    ({"spans": [[5, 20, "link", "https://x.test"]]}, "past the end"),          # out of range
    ({"spans": [[5, 5, "link", "https://x.test"]]}, "start < end"),            # empty
    ({"spans": [[9, 5, "link", "https://x.test"]]}, "start < end"),            # reversed
    ({"spans": [[0, 4, "anchor", "#p2"]]}, "span kind"),                       # unknown kind
    ({"spans": [[0, 9, "link", "https://x.test"], [5, 13, "noteref", "n1"]]},
     "overlaps"),                                                              # spans cross
    ({"marks": [[10, 15, "strong", "win  "]]}, "past the end"),                # out of range
    ({"marks": [[14, 14, "del-unread", ""]]}, "past the end"),                 # point past end
    ({"marks": [[5, 9, "strong", "Deep"]]}, "not the text"),                   # stale text
    ({"marks": [[0, 4, "bold", "Deep"]]}, "mark kind"),                        # unknown kind
    ({"marks": [[4, 0, "em", ""]]}, "start < end"),                            # reversed
    ({"marks": [[0, 4, "em", "Deep"]], "text": None}, "no text"),               # marks, no text
])
def test_a_bad_span_or_mark_fails_the_load_and_names_its_node(node, problem):
    with pytest.raises(ValueError, match=problem) as error:
        _loaded(**node)
    assert "node 1:a" in str(error.value)


def test_marks_that_cross_and_a_mark_across_a_link_load():
    """Marks may cross each other and a span: the renderer splits the lower-ranked element."""
    node = _loaded(spans=[[5, 13, "link", "https://x.test"]],
                   marks=[[0, 9, "strong", "Deep nets"], [5, 13, "em", "nets win"],
                          [13, 13, "del-unread", ""]])
    assert len(node.marks) == 3 and len(node.spans) == 1


def test_a_bad_mark_in_a_document_fails_its_load():
    doc = Document(root=Node(Role.DOC, children=[Node(Role.P, page=1, text="abc")]),
                   pages=[PageInfo(number=1, width=612, height=792)])
    d = doc.to_dict()
    d["root"]["children"][0]["marks"] = [[0, 3, "em", "xyz"]]
    with pytest.raises(ValueError, match="not the text"):
        Document.from_dict(d)


# -- the fix mark: a reviewer's correction ----------------------------------------------


def test_a_fix_mark_needs_its_reading_and_a_doubt_may_have_one():
    with pytest.raises(ValueError):
        Mark(0, 3, "fix", "abc")
    with pytest.raises(ValueError):
        Mark(0, 3, "strong", "abc", alt="x")
    fix = Mark(0, 5, "fix", "80110", alt="8011O")
    assert fix.to_dict() == [0, 5, "fix", "80110", "8011O"]
    assert Mark.from_dict(fix.to_dict()) == fix


def test_set_text_keeps_a_fix_while_its_text_is_unchanged():
    text = "General rules"
    node = Node(Role.P, page=1, text=text, marks=[Mark(0, 7, "fix", "General", alt="Ge neral")])
    node.set_text("The General rules")
    assert node.marks == [Mark(4, 11, "fix", "General", alt="Ge neral")]
    assert node.facts == []


def test_set_text_drops_a_changed_fix_with_a_fact_and_never_clears_its_alt():
    text = "General rules"
    node = Node(Role.P, page=1, text=text, marks=[Mark(0, 7, "fix", "General", alt="Ge neral")])
    node.set_text("Genxral rules")
    assert node.marks == []
    assert [(f.code, f.data["kind"]) for f in node.facts] == [("ocr-mark-lost", "fix")]
    # an alt equal to the new text does not make the mark vanish quietly either
    other = Node(Role.P, page=1, text="a b", marks=[Mark(0, 1, "fix", "a", alt="b")])
    other.set_text("b b")
    assert other.marks == [] and other.facts[0].data["kind"] == "fix"
