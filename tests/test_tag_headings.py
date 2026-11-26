"""Tagged-PDF headings and bookmarks as the contents."""

from reflow.analyze import evidence
from reflow.extract.claims import Claim, PageClaims
from reflow.extract.tags import Bookmark
from reflow.model import HEADING_ROLES, Document, Node, Role
from reflow.render.toc import toc_entries


class FakeTags:
    def __init__(self, claims):
        self._claims = claims          # page -> [Claim]

    def pages(self):
        return set(self._claims)

    def claims(self, page):
        return PageClaims(page, self._claims[page])


def box(top):
    return (50.0, top, 400.0, top + 14)


def block(role, text, top, page=1, size=10.0):
    return Node(role, page=page, text=text, bbox=box(top), attrs={"font_size": size})


def claim(role, top, level=None, **attrs):
    if level is not None:
        attrs["level"] = level
    return Claim(box(top), role, None, attrs)


def doc_of(nodes):
    return Document(root=Node(Role.DOC, children=nodes))


def believed_tree():
    """Two pages, headings at two levels, body text, and one miscalled block of each kind."""
    nodes = [block(Role.H2, "Intro", 10), block(Role.P, "Body one", 40),
             block(Role.P, "Methods", 70),                     # tagged /H2: promote
             block(Role.P, "Body two", 100), block(Role.H3, "Stray line", 130),   # tagged /P: demote
             block(Role.H2, "Results", 10, page=2), block(Role.P, "Body three", 40, page=2),
             block(Role.P, "Body four", 70, page=2), block(Role.P, "Body five", 100, page=2)]
    tags = FakeTags({
        1: [claim(Role.H2, 10, 2), claim(Role.P, 40), claim(Role.H2, 70, 2),
            claim(Role.P, 100), claim(Role.P, 130)],
        2: [claim(Role.H3, 10, 3), claim(Role.P, 40), claim(Role.P, 70),
            claim(Role.P, 100)]})
    return nodes, tags


def test_tagged_headings_set_role_and_level():
    nodes, tags = believed_tree()
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert report.trusted and report.promoted == 1 and report.demoted == 1
    assert [n.role for n in nodes] == [Role.H2, Role.P, Role.H2, Role.P, Role.P, Role.H3, Role.P, Role.P, Role.P]
    methods = nodes[2]
    assert methods.facts[-1].code == "tag-heading" and methods.facts[-1].data["was"] == "p"


def test_a_tree_with_one_heading_level_promotes_but_keeps_our_levels_and_demotes_nothing():
    nodes = [block(Role.H2, "Part", 10), block(Role.H3, "Sub one", 40), block(Role.P, "Sub two", 70),
             block(Role.H3, "Sub three", 100), block(Role.H3, "Sub four", 130),
             block(Role.H3, "Called P", 160), block(Role.P, "Body one", 190),
             block(Role.P, "Body two", 220), block(Role.P, "Body three", 250)]
    tags = FakeTags({1: [claim(Role.H1, 10, 1), claim(Role.H1, 40, 1), claim(Role.H1, 70, 1),
                         claim(Role.H1, 100, 1), claim(Role.H1, 130, 1), claim(Role.P, 160),
                         claim(Role.P, 190), claim(Role.P, 220), claim(Role.P, 250)]})
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert report.trusted and report.flat and report.promoted == 1 and report.demoted == 0
    assert [n.role for n in nodes[:6]] == [Role.H2, Role.H3, Role.H3, Role.H3, Role.H3, Role.H3]


def test_a_tree_that_tags_everything_h1_over_paragraphs_is_not_believed():
    nodes = [block(Role.P, f"Head {i}", 30 * i) for i in range(5)]
    tags = FakeTags({1: [claim(Role.H1, 30 * i, 1) for i in range(5)]})
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert not report.trusted and all(n.role == Role.P for n in nodes)


def test_a_line_that_continues_a_list_item_is_not_a_heading_even_in_a_tree_not_believed():
    nodes = [block(Role.H2, f"Head {i}", 30 * i) for i in range(4)] + [
        block(Role.H4, "different from 2019 (Figure 9).", 200),
        block(Role.H4, "Numbered Form Label", 230)]
    tags = FakeTags({1: [claim(Role.P, 30 * i) for i in range(4)] + [
        claim(Role.LI, 200), claim(Role.LI, 230)]})
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert not report.trusted and report.demoted == 1
    assert [n.role for n in nodes[4:]] == [Role.P, Role.H4]


def test_a_column_top_line_that_ends_a_tagged_paragraph_is_not_a_heading():
    text = "A long paragraph that runs on and on until it reaches the end, not statistically different."
    key = text[:40] + "…" + text[-20:]
    nodes = [block(Role.H2, "Head", 10), block(Role.H4, "not statistically different.", 300)]
    tags = FakeTags({1: [claim(Role.P, 10), Claim((50.0, 100.0, 400.0, 314.0), Role.P, key, {})]})
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert report.demoted == 1 and nodes[1].role == Role.P


def test_a_heading_split_in_blocks_is_joined_though_the_tag_box_sits_below_ours():
    # Our line boxes reach above the tag's glyph boxes: each block is only ~0.7 inside it.
    nodes = [block(Role.P, "Intro", 10), block(Role.H4, "Does the first", 90),
             block(Role.H4, "line wrap?", 108), block(Role.P, "Body", 140),
             block(Role.H2, "Next", 170), block(Role.P, "Tail", 200)]
    tags = FakeTags({1: [claim(Role.P, 10),
                         Claim((50.0, 94.0, 400.0, 126.0), Role.H2, None, {"level": 2}),
                         claim(Role.P, 140), claim(Role.H3, 170, 3), claim(Role.P, 200)]})
    doc = doc_of(nodes)
    assert evidence.tag_headings(doc, tags).merged == 1
    assert "Does the first line wrap?" in [n.text for n in doc.root.children]


def test_a_tree_that_tags_everything_p_names_no_heading():
    nodes = [block(Role.H2, "Intro", 10), block(Role.P, "Body", 40), block(Role.P, "More", 70)]
    tags = FakeTags({1: [claim(Role.P, 10), claim(Role.P, 40), claim(Role.P, 70)]})
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert not report.trusted and nodes[0].role == Role.H2


def test_a_tree_that_disagrees_with_most_blocks_is_not_believed():
    nodes = [block(Role.H2, f"Head {i}", 30 * i) for i in range(4)] + [block(Role.P, "x", 200)]
    tags = FakeTags({1: [claim(Role.P, 30 * i) for i in range(4)] + [claim(Role.H2, 200, 2)]})
    assert not evidence.tag_headings(doc_of(nodes), tags).trusted


def test_a_tagged_heading_the_layout_split_is_joined():
    nodes = [block(Role.P, "Intro", 10), block(Role.P, "A long", 40), block(Role.H3, "title", 54),
             block(Role.P, "Body", 90), block(Role.H2, "Next", 120), block(Role.P, "Tail", 150)]
    tags = FakeTags({1: [claim(Role.P, 10), Claim((50.0, 40.0, 400.0, 68.0), Role.H2, None, {"level": 2}),
                         claim(Role.P, 90), claim(Role.H3, 120, 3), claim(Role.P, 150)]})
    doc = doc_of(nodes)
    report = evidence.tag_headings(doc, tags)
    assert report.merged == 1
    assert [n.text for n in doc.root.children] == ["Intro", "A long title", "Body", "Next", "Tail"]


def test_a_cover_title_the_tags_call_p_stays_a_heading():
    nodes, tags = believed_tree()
    nodes[4].attrs["font_size"] = 24.0       # larger than the body text
    evidence.tag_headings(doc_of(nodes), tags)
    assert nodes[4].role in HEADING_ROLES


def test_bookmarks_that_name_the_headings_are_the_contents():
    nodes = [block(Role.H1, "Report", 10), block(Role.H2, "Introduction", 10, page=2),
             block(Role.H3, "Scope", 40, page=2), block(Role.H2, "Results", 10, page=3),
             block(Role.H2, "Unbookmarked", 40, page=3), block(Role.H3, "Unbookmarked part", 40, page=4)]
    doc = doc_of(nodes)
    marks = [Bookmark("Introduction  ", 1, 2, 10), Bookmark("Scope", 2, 2, 40),
             Bookmark("Results", 1, 3, 10)]
    assert evidence.outline_toc(doc, marks) == 3
    assert doc.root.attrs["toc"] == "bookmarks"

    class Chunk:
        pages = [1, 2, 3, 4]
        href = "chunks/0001.html"

    entries = toc_entries(doc, [Chunk()])
    assert [(e["text"], e["level"]) for e in entries] == [
        ("Introduction", 2), ("Scope", 3), ("Results", 2), ("Unbookmarked", 2),
        ("Unbookmarked part", 3)]


class OneChunk:
    pages = list(range(1, 10))
    href = "chunks/0001.html"


def test_headings_under_a_bookmark_with_none_under_it_nest_under_it():
    """One bookmark per chapter keeps the chapter's own headings in the contents, up to
    the next bookmark: two levels under it at most, or beside it at its level. The title's
    bookmark over the chapters does not push them down a level."""
    nodes = [block(Role.H1, "Handbook", 10), block(Role.H2, "Handbook 2026", 40),
             block(Role.H2, "Front matter", 70),
             block(Role.H2, "Getting Services", 10, page=2), block(Role.H3, "Member Services", 40, page=2),
             block(Role.H4, "When to call", 70, page=2), block(Role.H5, "Too deep", 100, page=2),
             block(Role.H3, "Crisis Hotline", 130, page=2),
             block(Role.H2, "Unbookmarked chapter", 10, page=3), block(Role.H3, "Its part", 40, page=3),
             block(Role.H2, "Appeals", 10, page=4), block(Role.H3, "Before the first part", 40, page=4),
             block(Role.H3, "Bookmarked part", 70, page=4),
             block(Role.H3, "Part the bookmarks skip", 100, page=4)]
    doc = doc_of(nodes)
    marks = [Bookmark("Handbook 2026", 1, 1, 40), Bookmark("Getting Services", 2, 2, 10),
             Bookmark("Appeals", 2, 4, 10), Bookmark("Bookmarked part", 3, 4, 70)]
    assert evidence.outline_toc(doc, marks) == 4
    assert [(e["text"], e["level"]) for e in toc_entries(doc, [OneChunk()])] == [
        ("Handbook 2026", 2), ("Getting Services", 2), ("Member Services", 3), ("When to call", 4),
        ("Crisis Hotline", 3), ("Unbookmarked chapter", 2), ("Its part", 3), ("Appeals", 2),
        ("Bookmarked part", 3), ("Part the bookmarks skip", 3)]


def test_a_bookmark_names_its_heading_across_spacing_and_dashes():
    nodes = [block(Role.H2, "Servicios e insumos de planificación familiar", 10, page=2),
             block(Role.H2, "Products and Networks – Quick Guide", 10, page=3),
             block(Role.H2, "Glosario", 10, page=4)]
    doc = doc_of(nodes)
    marks = [Bookmark("Servicios e insumos deplanificación familiar", 1, 2, 10),
             Bookmark("Products and Networks - Quick Guide", 1, 3, 10), Bookmark("Glosario", 1, 4, 10)]
    assert evidence.outline_toc(doc, marks) == 3
    # The contents prints the headings as the page does, not as the bookmarks spell them.
    assert [e["text"] for e in toc_entries(doc, [OneChunk()])] == [
        "Servicios e insumos de planificación familiar", "Products and Networks – Quick Guide",
        "Glosario"]


def test_bookmarks_that_do_not_match_leave_the_headings_as_the_contents():
    nodes = [block(Role.H2, "Introduction", 10, page=2)]
    doc = doc_of(nodes)
    marks = [Bookmark("Chapter A", 1, 2, 10), Bookmark("Chapter B", 1, 3, 10),
             Bookmark("Chapter C", 1, 4, 10)]
    assert evidence.outline_toc(doc, marks) == 0
    assert "toc" not in doc.root.attrs


def test_a_tag_does_not_promote_a_paragraph_ocr_is_unsure_of():
    """The tag tree cannot make doubtful OCR text navigation; the refusal says why."""
    from reflow.model import Mark
    nodes, tags = believed_tree()
    methods = nodes[2]
    methods.set_text("Methads")
    methods.attrs["ocr"] = "graded"
    methods.marks = [Mark(0, 7, "doubt", "Methads")]
    report = evidence.tag_headings(doc_of(nodes), tags)
    assert methods.role == Role.P and report.promoted == 0
    assert "OCR is unsure" in methods.attrs["why"][-1] and "Methads" in methods.attrs["why"][-1]
