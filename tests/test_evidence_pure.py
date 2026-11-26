"""The decisions of reflow.analyze.evidence on plain data: they return records and change no
node; one function applies them, and the result is what the old in-place passes made."""

import copy

from reflow.analyze import evidence, title
from reflow.analyze.evidence import (Annotation, Demote, Note, Promote, Retag, apply_fusion,
                                     apply_outline, decide_title, plan_fusion, plan_tag_headings)
from reflow.extract.claims import Claim, PageClaims, WitnessKind
from reflow.model import Document, Node, PageInfo, Role, Source
from test_tag_headings import FakeTags, believed_tree, block, claim, doc_of
from testkit import heading


def snapshot(nodes):
    """Everything a decision must leave alone, for comparing before and after."""
    return [(id(n), n.role, n.text, n.bbox, list(n.facts), dict(n.attrs), list(n.marks), list(n.spans))
            for n in nodes]


# -- the title ------------------------------------------------------------------------------


def joined_title():
    lead = heading("Prior Authorization Request for", 21.0)
    top = heading("Medications, DME, and Medical Devices", 27.0, role=Role.H1)
    nodes = [lead, top, heading("Optional Information", 13.0)]
    return nodes, lead, top, title.choose_title(nodes, None, None, "prior-auth")


def test_deciding_the_title_names_the_heading_and_the_joined_text_and_changes_nothing():
    nodes, lead, top, choice = joined_title()
    before = snapshot(nodes)
    promotion = decide_title(nodes, choice)
    assert promotion == Promote(top, lead, "Prior Authorization Request for Medications, DME, and Medical Devices")
    assert snapshot(nodes) == before and len(nodes) == 3


def test_a_title_from_no_heading_or_a_section_heading_is_not_promoted():
    nodes = [heading("Other", 18.0)]
    assert decide_title(nodes, title.TitleChoice(text="x", origin="meta", score=0.9)) is None
    stray = Node(Role.H2, page=1, text="Not in the tree")
    assert decide_title(nodes, title.TitleChoice(text="x", origin="heading", score=0.5,
                                                 nodes=(stray,))) is None


def test_the_promotion_is_made_by_apply_outline_and_levels_what_is_beneath():
    nodes, lead, top, choice = joined_title()
    nodes[2].role = Role.H4
    apply_outline(nodes, [decide_title(nodes, choice)], relevel=True)
    assert nodes == [top, nodes[1]] and lead not in nodes
    assert top.role == Role.H1 and top.text == choice.text
    assert nodes[1].role == Role.H2


def test_title_apply_demotes_a_title_page_behind_the_cover():
    nodes = [heading("Annual Report", 30, page=1),
             heading("ANNUAL REPORT", 14, page=2, role=Role.H3),
             Node(Role.P, page=2, text="BRIAN WEBER"),
             heading("Introduction", 14, page=3, role=Role.H3)]
    choice = title.choose_title(nodes, None, None, "doc")
    assert evidence.title_apply(nodes, choice) is nodes[0]
    assert [n.role for n in nodes] == [Role.H1, Role.P, Role.P, Role.H2]
    assert nodes[1].attrs["why"] == [f"paragraph: {title.TITLE_PAGE_WHY}"]


# -- the tag tree's headings ------------------------------------------------------------------


def test_the_tag_plan_changes_no_node_and_apply_makes_what_tag_headings_makes():
    nodes, tags = believed_tree()
    twin, twin_tags = believed_tree()
    before = snapshot(nodes)
    plan = plan_tag_headings(nodes, tags)
    assert snapshot(nodes) == before
    assert plan.report.trusted and plan.report.promoted == 1 and plan.report.demoted == 1
    assert [(e.node.text, e.role) for e in plan.edits if isinstance(e, Retag) and e.fact] == [
        ("Methods", Role.H2), ("Stray line", Role.P), ("Results", Role.H3)]

    apply_outline(nodes, plan.edits, relevel=plan.report.trusted)
    evidence.tag_headings(doc_of(twin), twin_tags)
    assert [(n.role, n.text, [f.code for f in n.facts]) for n in nodes] == [
        (n.role, n.text, [f.code for f in n.facts]) for n in twin]


def test_a_decided_change_carries_its_fact():
    nodes, tags = believed_tree()
    (methods,) = [e for e in plan_tag_headings(nodes, tags).edits
                  if isinstance(e, Retag) and e.node.text == "Methods"]
    assert methods.plain and methods.fact.code == "tag-heading"
    assert methods.fact.data == {"was": "p", "now": "h2"}


def test_a_tree_not_believed_still_decides_its_list_item_demotions_but_asks_for_no_levelling():
    nodes = [block(Role.H2, f"Head {i}", 30 * i) for i in range(4)] + [
        block(Role.H4, "different from 2019 (Figure 9).", 200)]
    tags = FakeTags({1: [claim(Role.P, 30 * i) for i in range(4)] + [claim(Role.LI, 200)]})
    plan = plan_tag_headings(nodes, tags)
    assert not plan.report.trusted and plan.report.demoted == 1
    assert [(e.node.text, e.role) for e in plan.edits] == [("different from 2019 (Figure 9).", Role.P)]
    assert nodes[4].role == Role.H4          # decided, not made


def test_a_demotion_counts_as_a_paragraph_when_the_tree_is_weighed():
    # The stray list line is a heading as the layout left it and a paragraph as the plan has it:
    # the tree agrees with all five tagged blocks only after that decision.
    nodes = [block(Role.P, "Intro", 10), block(Role.H2, "Part", 40), block(Role.P, "Body", 70),
             block(Role.H4, "continues the list", 100), block(Role.P, "Tail", 130)]
    tags = FakeTags({1: [claim(Role.P, 10), claim(Role.H2, 40, 2), claim(Role.P, 70),
                         claim(Role.LI, 100), claim(Role.P, 130)]})
    plan = plan_tag_headings(nodes, tags)
    assert plan.report.trusted and "agrees with 5 of 5" in plan.report.why


def test_a_paragraph_the_tags_call_a_sentence_heading_is_noted_not_promoted():
    nodes = [block(Role.H2, "Part", 10), block(Role.P, "We will pay the claim in full on the day we receive it.", 40),
             block(Role.P, "Body", 70), block(Role.P, "More", 100)]
    tags = FakeTags({1: [claim(Role.H2, 10, 2), claim(Role.H3, 40, 3), claim(Role.P, 70),
                         claim(Role.P, 100)]})
    plan = plan_tag_headings(nodes, tags)
    assert Note(nodes[1], "paragraph: a sentence (the tag tree has it as a heading)") in plan.edits
    assert all(not (isinstance(e, Retag) and e.node is nodes[1]) for e in plan.edits)


def test_a_joined_heading_removes_its_later_pieces_only_when_applied():
    nodes = [block(Role.P, "Intro", 10), block(Role.P, "A long", 40), block(Role.H3, "title", 54),
             block(Role.P, "Body", 90), block(Role.H2, "Next", 120), block(Role.P, "Tail", 150)]
    tags = FakeTags({1: [claim(Role.P, 10), Claim((50.0, 40.0, 400.0, 68.0), Role.H2, None, {"level": 2}),
                         claim(Role.P, 90), claim(Role.H3, 120, 3), claim(Role.P, 150)]})
    plan = plan_tag_headings(nodes, tags)
    (joined,) = [e for e in plan.edits if isinstance(e, Retag) and e.join]
    assert joined.join == (nodes[1], nodes[2]) and len(nodes) == 6 and nodes[1].text == "A long"
    apply_outline(nodes, plan.edits, relevel=True)
    assert [n.text for n in nodes] == ["Intro", "A long title", "Body", "Next", "Tail"]


def test_outline_edits_apply_in_the_order_they_were_decided():
    a, b = heading("Alpha", 12.0), heading("Beta", 12.0)
    fact = evidence.Fact("tag-heading", Source.TAGS, "first")
    again = evidence.Fact("tag-heading", Source.TAGS, "second")
    apply_outline([a, b], [Retag(a, Role.P, fact=fact), Retag(a, Role.H3, plain=True, fact=again),
                           Demote(b, "because"), Note(b, "and so")], relevel=False)
    assert a.role == Role.H3 and [f.detail for f in a.facts] == ["first", "second"]
    assert b.role == Role.P and b.attrs["why"] == ["paragraph: because", "and so"]


# -- fusion ---------------------------------------------------------------------------------------


class Fake:
    source = Source.TAGS

    def __init__(self, pages, kind=WitnessKind.TAG_TREE):
        self.kind = kind
        self._pages = pages

    def pages(self):
        return set(self._pages)

    def claims(self, page):
        return self._pages.get(page)


def fused_doc():
    fig = Node(Role.FIGURE, page=1, bbox=(50, 100, 550, 300))
    head = Node(Role.H2, page=1, bbox=(50, 400, 550, 420), text="Head")
    para = Node(Role.P, page=1, bbox=(50, 500, 550, 520), text="Body")
    stray = Node(Role.P, page=1, bbox=(50, 700, 550, 720), text="Nobody tagged me")
    doc = Document(root=Node(Role.DOC, children=[fig, head, para, stray]), pages=[PageInfo(1, 600, 800)])
    claims = [Claim((50, 100, 550, 300), Role.FIGURE, None, {"alt": "A tagged chart"}),
              Claim((50, 400, 550, 420), Role.P, None, {}),
              Claim((50, 500, 550, 520), Role.P, None, {})]
    return doc, {1: PageClaims(1, claims, True)}


def test_the_fusion_plan_changes_no_node_and_apply_fusion_makes_it_true():
    doc, pages = fused_doc()
    fig, head, para, stray = doc.root.children
    before = snapshot([doc.root, *doc.root.children])
    plan = plan_fusion(doc, [Fake(pages)])
    assert snapshot([doc.root, *doc.root.children]) == before
    assert [(a.node, a.fact.code) for a in plan.annotations] == [
        (stray, "untagged"), (fig, "tag-alt"), (fig, "role-agreement"),
        (head, "role-disagreement"), (para, "role-agreement")]
    assert plan.pages_compared == 1
    assert all(isinstance(a, Annotation) for a in plan.annotations)

    report = apply_fusion(plan)
    assert fig.attrs["alt"] == "A tagged chart" and fig.attrs["alt-source"] == "tags"
    assert report.filled == {"alt": 1} and report.pages_compared == 1
    assert report.facts == {"untagged": 1, "tag-alt": 1, "role-agreement": 2, "role-disagreement": 1}


def test_a_second_witness_does_not_fill_what_the_first_planned_to_fill():
    doc, pages = fused_doc()
    plan = plan_fusion(doc, [Fake(pages), Fake(copy.deepcopy(pages))])
    assert [a.fact.code for a in plan.annotations].count("tag-alt") == 1
    assert plan.pages_compared == 2


def test_fuse_is_the_plan_applied():
    doc, pages = fused_doc()
    twin, twin_pages = fused_doc()
    report = evidence.fuse(doc, [Fake(pages)])
    assert report == apply_fusion(plan_fusion(twin, [Fake(twin_pages)]))
    assert [[f.code for f in n.facts] for n in doc.root.children] == [
        [f.code for f in n.facts] for n in twin.root.children]


def test_a_header_row_cell_takes_no_row_scope_in_the_plan():
    corner = Node(Role.TH, page=1, bbox=(60, 110, 100, 130), text="Directory for:")
    row = Node(Role.TH, page=1, bbox=(60, 150, 100, 170), text="Wages")
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 200), attrs={"header_rows": 1}, children=[
        Node(Role.TR, children=[corner]), Node(Role.TR, children=[row])])
    doc = Document(root=Node(Role.DOC, children=[table]), pages=[PageInfo(1, 600, 800)])
    claims = {1: PageClaims(1, [Claim((60, 110, 100, 130), Role.TH, "Directory for:", {"scope": "row"}),
                                Claim((60, 150, 100, 170), Role.TH, "Wages", {"scope": "row"})], True)}
    plan = plan_fusion(doc, [Fake(claims)])
    assert [(a.node, a.filled) for a in plan.annotations if a.filled] == [(row, "scope")]
    assert "scope" not in corner.attrs and "scope" not in row.attrs      # planned, not made
