"""Evidence fusion: tag-tree and bookmark claims matched to nodes, as facts and fills."""

from reflow.analyze.evidence import fuse, match_claims
from reflow.extract.claims import Claim, PageClaims, WitnessKind
from reflow.model import Document, Node, PageInfo, Role, Source


class Fake:
    """A witness with canned claims."""
    source = Source.TAGS

    def __init__(self, pages: dict[int, PageClaims], kind=WitnessKind.TAG_TREE):
        self.kind = kind
        self._pages = pages

    def pages(self):
        return set(self._pages)

    def claims(self, page):
        return self._pages.get(page)


def doc(*nodes: Node) -> Document:
    return Document(root=Node(Role.DOC, children=list(nodes)), pages=[PageInfo(1, 600, 800)])


def block(role, text, top, page=1):
    return Node(role, page=page, bbox=(50, top, 550, top + 20), text=text)


def claim(role, top, text=None, **attrs):
    return Claim((50, top, 550, top + 20), role, text, attrs)


def codes(node):
    return [f.code for f in node.facts]


def page(*claims, trusted=True):
    return {1: PageClaims(1, list(claims), trusted)}


def test_match_pairs_nodes_with_the_claims_that_cover_them():
    a, b = block(Role.P, "a", 100), block(Role.P, "b", 300)
    hit = claim(Role.P, 102)
    far = claim(Role.P, 600)
    matches = match_claims([a, b], [far, hit])
    assert [(m.node, m.claims) for m in matches] == [(a, [hit])]


def test_the_best_overlap_comes_first():
    a = block(Role.P, "a", 100)
    near, rough = claim(Role.P, 100), Claim((50, 100, 550, 160), Role.P)
    assert match_claims([a], [rough, near])[0].claims == [near, rough]


def test_a_trusted_tag_confirms_or_disputes_the_role():
    ok, wrong = block(Role.P, "x", 100), block(Role.H2, "y", 200)
    report = fuse(doc(ok, wrong), [Fake(page(claim(Role.P, 100), claim(Role.P, 200)))])
    assert codes(ok) == ["role-agreement"]
    assert codes(wrong) == ["role-disagreement"]
    assert wrong.facts[0].data == {"ours": "h2", "theirs": "p"}
    assert report.facts == {"role-agreement": 1, "role-disagreement": 1}


def test_a_claim_without_a_role_and_an_untrusted_page_say_nothing():
    a, b = block(Role.P, "x", 100), block(Role.P, "y", 200)
    fuse(doc(a, b), [Fake(page(claim(None, 100), claim(Role.TABLE, 200), trusted=False))])
    assert codes(a) == [] and codes(b) == []


def test_text_kinds_agree_with_each_other():
    item = block(Role.LI, "x", 100)
    fuse(doc(item), [Fake(page(claim(Role.NOTE, 100)))])
    assert codes(item) == ["role-agreement"]


def test_heading_levels_are_compared_after_the_usual_offset():
    # A promoted title moved every heading one level down: the tags' H1s are our H2s.
    heads = [block(Role.H2, f"h{i}", 100 + 40 * i) for i in range(3)]
    odd = block(Role.H4, "odd", 300)    # nests two levels under its sibling here, one level in the tags' outline
    claims = [claim(Role.H1, 100 + 40 * i, level=1) for i in range(3)] + [claim(Role.H1, 300, level=1)]
    fuse(doc(*heads, odd), [Fake(page(*claims))])
    assert all(codes(h) == ["role-agreement"] for h in heads)
    assert codes(odd) == ["level-disagreement"]
    assert odd.facts[0].data == {"ours": 4, "theirs": 1, "offset": 1}


def test_a_bookmark_that_names_the_level_is_recorded():
    head = block(Role.H2, "Chapter", 100)
    para = block(Role.P, "Body text that is really a heading", 200)
    report = fuse(doc(head, para), [Fake(page(claim(Role.H2, 100, level=2),
                                              claim(Role.H2, 200, level=2)), kind=WitnessKind.BOOKMARKS)])
    assert codes(head) == ["bookmark-level"]
    assert codes(para) == ["role-disagreement"]
    assert report.facts["bookmark-level"] == 1


def test_a_bookmark_band_over_a_table_is_ignored():
    table = Node(Role.TABLE, page=1, bbox=(50, 90, 550, 300))
    fuse(doc(table), [Fake(page(claim(Role.H2, 100, level=2)), kind=WitnessKind.BOOKMARKS)])
    assert codes(table) == []


def test_blocks_no_trusted_tag_covers_are_untagged():
    covered, bare = block(Role.P, "x", 100), block(Role.P, "y", 400)
    fuse(doc(covered, bare), [Fake(page(claim(Role.P, 100)))])
    assert codes(bare) == ["untagged"] and codes(covered) == ["role-agreement"]
    other = block(Role.P, "z", 400)
    fuse(doc(other), [Fake(page(claim(Role.P, 100), trusted=False))])
    assert codes(other) == []


def test_the_witness_is_known_by_its_kind_and_its_facts_name_it_by_the_kind():
    bare = block(Role.P, "y", 400)
    odd = block(Role.P, "z", 100)
    fuse(doc(bare, odd), [Fake(page(claim(Role.H2, 100)))])
    assert codes(bare) == ["untagged"]
    assert odd.facts[0].detail == "the tag tree says h2, not p"
    other = block(Role.P, "w", 400)
    fuse(doc(other), [Fake(page(claim(Role.P, 100)), kind=WitnessKind.BOOKMARKS)])
    assert codes(other) == []    # only a tag tree says which blocks no tag covers


def test_a_figure_takes_the_tags_alt_text():
    fig = Node(Role.FIGURE, page=1, bbox=(50, 100, 550, 300))
    kept = Node(Role.FIGURE, page=1, bbox=(50, 400, 550, 500), attrs={"alt": "mine"})
    claims = page(Claim((50, 100, 550, 300), Role.FIGURE, None, {"alt": "A tagged chart"}),
                  Claim((50, 400, 550, 500), Role.FIGURE, None, {"alt": "theirs"}))
    report = fuse(doc(fig, kept), [Fake(claims)])
    assert fig.attrs["alt"] == "A tagged chart" and "tag-alt" in codes(fig)
    assert kept.attrs["alt"] == "mine" and "tag-alt" not in codes(kept)
    assert report.filled == {"alt": 1}


def test_an_untrusted_tree_does_not_fill_alt():
    fig = Node(Role.FIGURE, page=1, bbox=(50, 100, 550, 300))
    fuse(doc(fig), [Fake(page(Claim((50, 100, 550, 300), Role.FIGURE, None, {"alt": "x"}),
                             trusted=False))])
    assert "alt" not in fig.attrs


def test_a_row_header_takes_its_scope_from_the_tags():
    row = Node(Role.TH, text="Wages")
    col = Node(Role.TH, text="Amount")
    data = Node(Role.TD, text="12")
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 300), children=[
        Node(Role.TR, children=[col]), Node(Role.TR, children=[row, data])])
    claims = page(Claim((60, 150, 100, 170), Role.TH, "Wages", {"scope": "row"}),
                  Claim((60, 110, 100, 130), Role.TH, "Amount", {"scope": "col"}))
    report = fuse(doc(table), [Fake(claims)])
    assert row.attrs["scope"] == "row" and "tag-scope" in codes(row)
    assert "scope" not in col.attrs and "scope" not in data.attrs
    assert report.filled == {"scope": 1}


def test_a_cell_of_the_header_rows_is_no_row_header_whatever_the_tags_say():
    # The tagger calls the first column its row headers, the corner cell "Directory for:" too;
    # in the header row it names a column, and a header row with a row header is no thead.
    corner = Node(Role.TH, text="Directory for:")
    name = Node(Role.TH, text="Network")
    stub = Node(Role.TH, text="1. Stand-alone")
    data = Node(Role.TD, text="Prime")
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 300), attrs={"header_rows": 1}, children=[
        Node(Role.TR, children=[corner, name]), Node(Role.TR, children=[stub, data])])
    claims = page(Claim((60, 110, 130, 130), Role.TH, "Directory for:", {"scope": "row"}),
                  Claim((60, 150, 130, 170), Role.TH, "1. Stand-alone", {"scope": "row"}))
    report = fuse(doc(table), [Fake(claims)])
    assert "scope" not in corner.attrs and "tag-scope" not in codes(corner)
    assert stub.attrs["scope"] == "row"
    assert report.filled == {"scope": 1}


def test_pages_limits_the_work():
    a = block(Role.P, "x", 100)
    report = fuse(doc(a), [Fake(page(claim(Role.P, 100)))], pages=[2])
    assert codes(a) == [] and report.pages_compared == 0


def test_a_tagged_item_inside_our_list_is_not_a_disagreement():
    box = Node(Role.LIST, page=1, bbox=(50, 100, 550, 300))
    fuse(doc(box), [Fake(page(Claim((50, 100, 550, 130), Role.LI), Claim((50, 140, 550, 170), Role.LI)))])
    assert codes(box) == []
    whole = Node(Role.LIST, page=1, bbox=(50, 100, 550, 300))
    fuse(doc(whole), [Fake(page(Claim((50, 100, 550, 300), Role.TABLE)))])
    assert codes(whole) == ["role-disagreement"]


# -- level comparison that survives differently outlined sections -------------------------


def test_a_constant_gap_in_part_of_the_document_does_not_flood_the_queue():
    # fw9: the tags put every "Line N" at level 4 where we have 3, the parts agree at 1 and 2.
    nodes, claims = [], []
    top = 100
    for i, (ours, theirs) in enumerate([(1, 1), (2, 2)] + [(3, 4)] * 12 + [(2, 2)]):
        nodes.append(block({1: Role.H1, 2: Role.H2, 3: Role.H3}[ours], f"h{i}", top))
        claims.append(claim(Role.H1, top, level=theirs))
        top += 30
    claims = [Claim(c.bbox, {1: Role.H1, 2: Role.H2, 4: Role.H4}[c.attrs["level"]], None, c.attrs)
              for c in claims]
    fuse(doc(*nodes), [Fake(page(*claims))])
    assert not any("level-disagreement" in codes(n) for n in nodes)


def test_mostly_disagreeing_levels_make_one_document_fact():
    # nesting disagrees almost everywhere: ours alternates, theirs is flat
    nodes = [block(Role.H2 if i % 2 else Role.H3, f"h{i}", 100 + 30 * i) for i in range(12)]
    claims = [claim(Role.H1, 100 + 30 * i, level=1) for i in range(12)]
    d = doc(*nodes)
    report = fuse(d, [Fake(page(*claims))])
    assert not any("level-disagreement" in codes(n) for n in nodes)
    assert [f.code for f in d.root.facts] == ["levels-unreliable"]
    assert report.facts["levels-unreliable"] == 1


# -- alt text ------------------------------------------------------------------------------


def alt_figure(alt):
    fig = Node(Role.FIGURE, page=1, bbox=(50, 100, 550, 300))
    fuse(doc(fig), [Fake(page(Claim((50, 100, 550, 300), Role.FIGURE, None, {"alt": alt})))])
    return fig


def test_junk_alt_is_not_copied():
    for junk in ("", "  ", "Image", "picture", "Figure 3", "graphic", "Photo", "IMG_0042.JPG",
                 "chart.png", "untitled", "Untitled", "image_12"):
        fig = alt_figure(junk)
        assert "alt" not in fig.attrs and "tag-alt" not in codes(fig), junk


def test_a_real_alt_is_copied_with_its_source():
    fig = alt_figure("Bar chart of 2023 revenue by region")
    assert fig.attrs["alt"] == "Bar chart of 2023 revenue by region"
    assert fig.attrs["alt-source"] == "tags"


# -- header scopes with duplicate labels ---------------------------------------------------


def test_duplicate_header_labels_take_their_own_scope_by_position():
    row_total = Node(Role.TH, text="Total", bbox=(60, 200, 100, 220))
    col_total = Node(Role.TH, text="Total", bbox=(300, 110, 340, 130))
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 300), children=[
        Node(Role.TR, children=[col_total]), Node(Role.TR, children=[row_total])])
    claims = page(Claim((60, 200, 100, 220), Role.TH, "Total", {"scope": "row"}),
                  Claim((300, 110, 340, 130), Role.TH, "Total", {"scope": "col"}))
    fuse(doc(table), [Fake(claims)])
    assert row_total.attrs["scope"] == "row"
    assert "scope" not in col_total.attrs


def test_a_header_cell_without_a_box_and_with_an_ambiguous_label_gets_no_scope():
    a, b = Node(Role.TH, text="Total"), Node(Role.TH, text="Total")
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 300), children=[
        Node(Role.TR, children=[a]), Node(Role.TR, children=[b])])
    claims = page(Claim((60, 200, 100, 220), Role.TH, "Total", {"scope": "row"}),
                  Claim((300, 110, 340, 130), Role.TH, "Total", {"scope": "both"}))
    fuse(doc(table), [Fake(claims)])
    assert "scope" not in a.attrs and "scope" not in b.attrs


def test_a_box_that_matches_but_a_different_text_gets_no_scope():
    cell = Node(Role.TH, text="Wages", bbox=(60, 150, 100, 170))
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 300), children=[Node(Role.TR, children=[cell])])
    fuse(doc(table), [Fake(page(Claim((60, 150, 100, 170), Role.TH, "Tax", {"scope": "row"})))])
    assert "scope" not in cell.attrs


def test_a_tagged_paragraph_over_our_list_is_not_a_disagreement():
    lst = Node(Role.LIST, page=1, bbox=(50, 100, 550, 300))
    fuse(doc(lst), [Fake(page(Claim((50, 100, 550, 300), Role.P)))])
    assert "role-disagreement" not in codes(lst)


def test_text_inside_a_tagged_figure_is_not_a_disagreement():
    label = block(Role.P, "axis label", 150)
    fuse(doc(label), [Fake(page(Claim((50, 100, 550, 500), Role.FIGURE)))])
    assert "role-disagreement" not in codes(label)


def test_match_claims_scores_as_share_and_iou_do():
    """The one-overlap form must pair, score and order exactly as geometry.share and iou say."""
    import random

    from reflow import geometry
    from reflow.analyze.evidence import MATCH_SHARE

    def box(rng):
        x0, x1 = sorted((rng.uniform(0, 100), rng.uniform(0, 100)))
        top, bottom = sorted((rng.uniform(0, 100), rng.uniform(0, 100)))
        return (x0, top, x1, bottom) if rng.random() < 0.9 else (x1, bottom, x0, top)

    rng = random.Random(5)
    for _ in range(300):
        nodes = [Node(Role.P, page=1, bbox=box(rng), text="t") for _ in range(rng.randint(1, 5))]
        claims = [Claim(box(rng), Role.P, None, {}) for _ in range(rng.randint(1, 7))]
        expected = []
        for node in nodes:
            scored = [(geometry.iou(node.bbox, c.bbox), c) for c in claims
                      if max(geometry.share(node.bbox, c.bbox),
                             geometry.share(c.bbox, node.bbox)) >= MATCH_SHARE
                      and c.bbox[2] > node.bbox[0] and c.bbox[0] < node.bbox[2]
                      and c.bbox[3] > node.bbox[1] and c.bbox[1] < node.bbox[3]]
            scored.sort(key=lambda pair: -pair[0])
            if scored:
                expected.append((node, [c for _, c in scored], scored[0][0]))
        got = [(m.node, m.claims, m.share) for m in match_claims(nodes, claims)]
        assert got == expected
