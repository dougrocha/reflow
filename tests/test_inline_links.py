"""Link annotations become inline link spans: placement on nodes and rendering."""

from reflow.analyze.confidence import CODES
from reflow.extract.links import Link, attach_links, offsets_of
from reflow.model import Node, Role, Span
from reflow.render.html import inline


def _para(text: str, box=(50, 100, 550, 130), page=1) -> Node:
    return Node(Role.P, page=page, bbox=box, text=text)


def _link(words, target="https://example.com", box=(100, 105, 160, 120), kind="uri", page=1):
    return Link(page, box, target, kind, tuple(words))


def test_a_link_becomes_a_span_over_its_words():
    para = _para("Visit the IRS site today")
    assert attach_links([para], {1: [_link(["the", "IRS"])]}) == 1
    assert para.spans == [Span(6, 13, "link", "https://example.com")]
    assert [f.code for f in para.facts] == ["link"]


def test_repeated_words_take_successive_occurrences():
    para = _para("see page 4 and see page 9")
    links = [_link(["see", "page"], "#p4", kind="page"), _link(["see", "page"], "#p9", kind="page")]
    attach_links([para], {1: links})
    assert [(s.start, s.target) for s in para.spans] == [(0, "#p4"), (15, "#p9")]


def test_offsets_of_skips_taken_places():
    assert offsets_of(("a",), "a a", taken=[Span(0, 1, "link", "x")]) == (2, 3)


def test_the_smallest_block_holding_the_rectangle_wins():
    outer = _para("alpha beta", box=(0, 0, 600, 800))
    inner = _para("alpha beta", box=(50, 100, 550, 130))
    attach_links([outer, inner], {1: [_link(["beta"])]})
    assert inner.spans and not outer.spans


def test_a_link_outside_every_block_is_dropped():
    para = _para("alpha beta", box=(50, 500, 550, 530))
    assert attach_links([para], {1: [_link(["beta"])]}) == 0
    assert not para.spans


def test_a_table_cell_without_a_box_takes_the_link():
    cell = Node(Role.TD, text="Worksheet 1-A")
    other = Node(Role.TD, text="Total")
    table = Node(Role.TABLE, page=1, bbox=(50, 100, 550, 200),
                 children=[Node(Role.TR, children=[other, cell])])
    attach_links([table], {1: [_link(["Worksheet", "1-A"], "#p2", kind="page")]})
    assert cell.spans == [Span(0, 13, "link", "#p2")] and not other.spans


def test_line_broken_paragraphs_render_the_span_on_its_line():
    from reflow.render.html import NodeRenderer
    para = _para("alpha beta")
    para.attrs["lines"] = ["alpha", "beta"]
    attach_links([para], {1: [_link(["beta"])]})
    html = str(NodeRenderer({}, {}).render(para))
    assert 'alpha<br><a href="https://example.com" rel="noopener">beta</a>' in html


def test_a_link_to_a_page_the_document_lacks_stays_plain_text_and_goes_to_review():
    para = _para("See Appendix B for the forms")
    links = {1: [_link(["Appendix", "B"], "#p40", kind="page")]}
    assert attach_links([para], links, pages=range(1, 31)) == 0
    assert para.spans == []
    [fact] = para.facts
    assert (fact.code, fact.detail, fact.data["box"]) == ("link-unresolved", "#p40",
                                                          [100, 105, 160, 120])
    assert CODES[fact.code].severity == "warning"
    assert str(inline(para)) == "See Appendix B for the forms"


def test_a_link_to_a_page_the_document_has_is_kept():
    para = _para("See Appendix B for the forms")
    links = {1: [_link(["Appendix", "B"], "#p20", kind="page")]}
    assert attach_links([para], links, pages=range(1, 31)) == 1
    assert para.spans == [Span(4, 14, "link", "#p20")]


def test_render_wraps_the_span_and_escapes_the_rest():
    para = _para("a < b, see chapter 4 or www.irs.gov.")
    para.spans = [Span(11, 20, "link", "#p12")]
    assert str(inline(para)) == ('a &lt; b, see <a href="<p12>">chapter 4</a> or '
                                 '<a href="https://www.irs.gov">www.irs.gov</a>.')


def test_render_escapes_an_external_target():
    para = _para("go there")
    para.spans = [Span(0, 2, "link", 'https://x.test/?a=1&b="2"')]
    assert str(inline(para)).startswith('<a href="https://x.test/?a=1&amp;b=&#34;2&#34;"')


def test_render_drops_an_unsafe_target():
    para = _para("go there")
    para.spans = [Span(0, 2, "link", "javascript:alert(1)")]
    assert str(inline(para)) == "go there"


def test_render_without_spans_is_linkify():
    assert str(inline(_para("mail me@example.com"))) == (
        'mail <a href="mailto:me@example.com">me@example.com</a>')
