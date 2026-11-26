"""The inline-mark contract: Node.text has no setter, so every text edit goes through
Node.set_text or Node.join_inline, and a mark stays on its word and a lost one is reported."""

import pytest

from reflow.analyze import evidence, notes
from reflow.analyze.hybrid import join_across_pages
from reflow.extract.claims import Claim, PageClaims
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.leaders import normalize_document
from reflow.model import Document, Mark, Node, PageInfo, Role, Span
from testkit import mark_at


class FakeTags:
    def __init__(self, pages):
        self._pages = pages

    def pages(self):
        return set(self._pages)

    def claims(self, page):
        return self._pages[page]


def test_dot_leaders_removed_before_a_doubt_mark_leave_it_on_its_word():
    text = "Legislative ��� Branch"
    node = Node(Role.LI, page=1, text=text, marks=[mark_at(text, "Branch", "doubt")])
    normalize_document(Document(root=Node(Role.DOC, children=[node]),
                                pages=[PageInfo(number=1, width=612, height=792)]))
    assert node.text != text
    assert [node.text[m.start:m.end] for m in node.marks] == ["Branch"]
    assert node.facts == []


def test_a_footnote_marker_cut_keeps_a_mark_on_the_same_word():
    marker_line = Line(1, (50, 720, 280, 727), "1 See the Guide here", 7.0, False, False, "F")
    body_text = "Deep nets win competitions1 , where we also won."
    at = body_text.index("1")
    body_line = Line(1, (50, 100, 280, 110), body_text, 10.0, False, False, "F", sup=((at, at + 1),))
    body = Node(Role.P, page=1, bbox=(50, 100, 280, 110), text=body_text)
    foot_text = "1 See the Guide here"
    foot = Node(Role.P, page=1, bbox=(50, 720, 280, 727), text=foot_text,
                marks=[mark_at(foot_text, "Guide", "strong")],
                spans=[Span(foot_text.index("here"), len(foot_text), "link", "https://x.test")])
    page = PageContent(1, 612, 792, [body_line, marker_line], [])
    assert notes.attach_footnotes([body, foot], [page], 10.0) == 1
    assert foot.text == "See the Guide here"
    assert [foot.text[m.start:m.end] for m in foot.marks] == ["Guide"]
    assert [foot.text[s.start:s.end] for s in foot.spans] == ["here"]


def test_joining_blocks_keeps_a_mark_on_its_word():
    a_text, b_text = "Rates  rise", "in  March"
    a = Node(Role.P, page=1, text=a_text, marks=[mark_at(a_text, "rise", "ins")])
    b = Node(Role.P, page=1, text=b_text, marks=[mark_at(b_text, "March", "del")])
    for node, top in ((a, 100), (b, 120)):
        node.bbox = (50, top, 400, top + 14)
        node.attrs["font_size"] = 10.0
    intro = Node(Role.H2, page=1, text="Intro", bbox=(50, 10, 400, 24), attrs={"font_size": 10.0})
    body = [Node(Role.P, page=1, text=f"Body {i}", bbox=(50, 150 + 30 * i, 400, 164 + 30 * i),
                 attrs={"font_size": 10.0}) for i in range(5)]
    nodes = [intro, a, b, *body]
    tags = FakeTags({1: PageClaims(1, [Claim(intro.bbox, Role.H2, None, {"level": 2}),
                                       Claim((50, 100, 400, 134), Role.H2, None, {"level": 2}),
                                       *[Claim(n.bbox, Role.P) for n in body]], True)})
    doc = Document(root=Node(Role.DOC, children=nodes), pages=[PageInfo(number=1, width=600, height=800)])
    assert evidence.tag_headings(doc, tags).merged == 1
    assert b not in doc.root.children
    assert a.text == "Rates rise in March"
    assert [a.text[m.start:m.end] for m in a.marks] == ["rise", "March"]
    assert [f.code for f in a.facts] == ["tag-heading"]


def test_a_paragraph_joined_across_pages_keeps_its_marks():
    a_text, b_text = "The form asks for the infor-", "mation about your income."
    a = Node(Role.P, page=1, text=a_text, marks=[mark_at(a_text, "form", "strong")])
    b = Node(Role.P, page=2, text=b_text, marks=[mark_at(b_text, "income", "em")])
    (joined,) = join_across_pages([a, b], {1, 2})
    assert joined.text == "The form asks for the information about your income."
    assert [joined.text[m.start:m.end] for m in joined.marks] == ["form", "income"]


def test_a_stage_that_leaves_a_stale_mark_fails_the_conversion():
    """The end of analysis checks every node, so a stale mark fails instead of being dropped."""
    node = Node(Role.P, page=1, text="The same words", marks=[Mark(9, 13, "em", "grid")])
    with pytest.raises(ValueError, match="not the text"):
        node.check_inline()
