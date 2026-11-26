"""The tag tree and the bookmarks as plain records and as witnesses."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pikepdf
import pytest
from conftest import make_pdf, make_tagged_pdf

from reflow.extract import pagecontent, tags
from reflow.extract.claims import Claim
from reflow.extract.tags import (
    Bookmark,
    BookmarkWitness,
    TagElement,
    TagPage,
    TagWitness,
    read_bookmarks,
    read_tags,
)
from reflow.model import Role, Source

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
FW9 = SAMPLES / "fw9.pdf"
P17 = SAMPLES / "p17.pdf"


def flatten(elements):
    for element in elements:
        yield element
        yield from flatten(element.children)


def near(box, expected, tolerance=1.0):
    return all(abs(a - b) <= tolerance for a, b in zip(box, expected))


@pytest.fixture(scope="module")
def tagged(tmp_path_factory):
    return make_tagged_pdf(tmp_path_factory.mktemp("tagged") / "tagged.pdf")


@pytest.fixture(scope="module")
def tree(tagged):
    return read_tags(tagged, jobs=1)


def element(tree, page, tag):
    return next(e for e in flatten(tree[page].elements) if e.tag == tag)


class TestReadTags:
    def test_pages_and_share(self, tree):
        assert sorted(tree) == [1, 2]
        assert tree[1].tagged_share == 1.0
        assert tree[2].tagged_share == 1.0

    def test_text_and_box_of_a_heading(self, tree):
        h1 = element(tree, 1, "/H1")
        assert h1.text == "Chapter One"
        assert h1.page == 1
        # Courier is 0.6 em per glyph: 11 glyphs of 24 pt from x=72, a 24 pt tall box
        assert near(h1.bbox, (72, 76.7, 72 + 11 * 14.4, 100.7))
        assert h1.mcids == [0]

    def test_paragraph(self, tree):
        first = [e for e in tree[1].elements[0].children if e.tag == "/P"][0]
        assert first.text == "First paragraph."
        assert near(first.bbox, (72, 122.3, 72 + 16 * 7.2, 134.3))

    def test_figure_has_alt_and_the_box_of_its_image(self, tree):
        figure = element(tree, 1, "/Figure")
        assert figure.alt == "A grey box"
        assert figure.text == ""
        assert near(figure.bbox, (72, 242, 172, 292))

    def test_role_map_resolves_custom_types(self, tree):
        heading = element(tree, 2, "/H2")
        assert heading.text == "Details"
        assert near(heading.bbox, (72, 123.5, 72 + 7 * 10.8, 141.5))

    def test_table_header_scope(self, tree):
        th = element(tree, 2, "/TH")
        td = element(tree, 2, "/TD")
        assert (th.text, th.scope) == ("Name", "col")
        assert (td.text, td.scope) == ("Value", None)
        assert near(td.bbox, (200, 202.3, 236, 214.3))

    def test_wrappers_hold_the_union_of_their_children(self, tree):
        table = element(tree, 2, "/Table")
        assert table.text == "Name Value"
        assert table.mcids == [2, 3]
        assert [c.tag for c in table.children] == ["/TR"]
        assert near(table.bbox, (72, 202.3, 236, 214.3))
        assert tree[2].elements[0].tag == "/Document"
        assert tree[2].elements[0].depth == 0
        assert table.depth == 1

    def test_element_on_two_pages_yields_two_elements(self, tree):
        first = [e for e in tree[1].elements[0].children if e.text.startswith("A paragraph")]
        second = [e for e in tree[2].elements[0].children if e.text.startswith("continues")]
        assert [(e.tag, e.page) for e in first + second] == [("/P", 1), ("/P", 2)]

    def test_pages_argument_limits_the_work(self, tagged):
        only = read_tags(tagged, [2], jobs=1)
        assert list(only) == [2]
        assert only[2].elements[0].text == "continues here. Details Name Value"

    def test_parallel_read_equals_serial(self, tagged, monkeypatch):
        monkeypatch.setattr(pagecontent, "PAGES_PER_WORKER", 1)
        assert read_tags(tagged, jobs=2) == read_tags(tagged, jobs=1)

    def test_executor_is_used_instead_of_a_pool(self, tagged, monkeypatch):
        """A conversion hands read_tags its own pool (reflow.run.Workers); a thread pool
        stands in here, so no process is spawned and none may be."""
        monkeypatch.setattr(pagecontent, "PAGES_PER_WORKER", 1)

        def no_pool(*args, **kwargs):
            raise AssertionError("read_tags made its own process pool")

        monkeypatch.setattr(pagecontent, "ProcessPoolExecutor", no_pool)
        with ThreadPoolExecutor(max_workers=2) as pool:
            mapped = []
            original = pool.map

            def counting_map(fn, *iterables):
                mapped.append(fn)
                return original(fn, *iterables)

            pool.map = counting_map
            assert read_tags(tagged, jobs=2, executor=pool) == read_tags(tagged, jobs=1)
            assert mapped == [pagecontent.read_pages]
            witness = TagWitness.open(tagged, jobs=2, executor=pool)
        assert witness is not None and witness.pages() == {1, 2}

    def test_short_document_stays_in_process_even_with_an_executor(self, tagged):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.map = None  # type: ignore[assignment]  # would raise if used
            assert read_tags(tagged, jobs=2, executor=pool) == read_tags(tagged, jobs=1)

    def test_values_are_plain_python(self, tree):
        for page in tree.values():
            for e in flatten(page.elements):
                assert type(e.tag) is str and type(e.text) is str
                assert all(type(v) is float for v in e.bbox)
                assert all(type(m) is int for m in e.mcids)
                assert e.alt is None or type(e.alt) is str

    def test_untagged_pdf(self, tmp_path):
        path = make_pdf(tmp_path / "plain.pdf", ["text"])
        assert read_tags(path) == {}
        assert TagWitness.open(path) is None

    def test_div_soup_is_not_trusted(self, tagged, tmp_path):
        path = tmp_path / "soup.pdf"
        with pikepdf.open(tagged) as pdf:
            for obj in pdf.objects:
                if isinstance(obj, pikepdf.Dictionary) and obj.get("/Type") == "/StructElem":
                    obj.S = pikepdf.Name.Div
            pdf.Root.StructTreeRoot.RoleMap = pikepdf.Dictionary()
            pdf.save(path)
        soup = read_tags(path, jobs=1)
        assert soup[1].tagged_share == 0.0
        assert TagWitness(soup).claims(1).trusted is False


class TestReadBookmarks:
    def test_titles_levels_and_pages(self, tagged):
        marks = read_bookmarks(tagged)
        assert [(b.title, b.level, b.page) for b in marks] == [
            ("Chapter One", 1, 1), ("Details", 2, 2), ("Table", 1, 2)]

    def test_destination_tops_are_top_left_origin(self, tagged):
        # /XYZ 0 720 0, /FitH 700 (name tree) and /FitH 600 (old /Dests) on 792 pt pages
        assert [b.top for b in read_bookmarks(tagged)] == [72.0, 92.0, 192.0]
        assert {b.width for b in read_bookmarks(tagged)} == {612.0}

    def test_destination_tops_follow_the_crop_box(self, tagged, tmp_path):
        path = tmp_path / "cropped.pdf"
        with pikepdf.open(tagged) as pdf:
            pdf.pages[1].obj.CropBox = pikepdf.Array([0, 100, 612, 692])
            pdf.save(path)
        # /FitH 700 and /FitH 600 on page 2, whose crop box starts 100 pt above the bottom
        # edge and ends 100 pt below the top: 692 - 700 is above the page, 692 - 600 is 92.
        assert [b.top for b in read_bookmarks(path)] == [72.0, -8.0, 92.0]

    def test_destination_tops_and_widths_follow_the_rotation(self, tagged, tmp_path):
        path = tmp_path / "rotated.pdf"
        with pikepdf.open(tagged) as pdf:
            pdf.pages[1].obj.Rotate = 90
            pdf.save(path)
        marks = read_bookmarks(path)
        # A page turned a quarter shows its height as its width, and the destination's
        # distance from its left edge as the distance from the top.
        assert [b.width for b in marks] == [612.0, 792.0, 792.0]
        assert [b.top for b in marks] == [72.0, 0.0, 0.0]

    def test_no_outline(self, tmp_path):
        assert read_bookmarks(make_pdf(tmp_path / "plain.pdf", ["text"])) == []

    def test_unresolvable_destination(self, tagged, tmp_path):
        path = tmp_path / "broken.pdf"
        with pikepdf.open(tagged) as pdf:
            pdf.Root.Outlines.First.Dest = pikepdf.String("nowhere")
            pdf.save(path)
        first = read_bookmarks(path)[0]
        assert (first.title, first.page, first.top) == ("Chapter One", None, None)


class TestTagWitness:
    def test_open_and_pages(self, tagged):
        witness = TagWitness.open(tagged)
        assert witness.source is Source.TAGS
        assert witness.pages() == {1, 2}
        assert witness.claims(3) is None

    def test_claims_in_tree_order_without_wrappers(self, tagged):
        page = TagWitness.open(tagged).claims(1)
        assert page.trusted
        assert [c.role for c in page.order] == [Role.H1, Role.P, Role.FIGURE, Role.P]
        assert page.order[0].attrs == {"level": 1}
        assert page.order[0].text == "Chapter One"
        assert page.order[2].attrs == {"alt": "A grey box"}
        assert page.order[2].text is None

    def test_table_claims(self, tagged):
        page = TagWitness.open(tagged).claims(2)
        assert [c.role for c in page.order] == [
            Role.P, Role.H2, Role.TABLE, Role.TR, Role.TH, Role.TD]
        assert page.order[1].attrs == {"level": 2}
        assert page.order[4].attrs == {"scope": "col"}

    def test_untrusted_below_the_share(self):
        el = TagElement("/P", 1, (0, 0, 10, 10), "x", 0)
        assert TagWitness({1: TagPage(1, [el], 0.5)}).claims(1).trusted is False
        assert TagWitness({1: TagPage(1, [el], tags.TRUSTED_SHARE)}).claims(1).trusted

    def test_generic_heading_takes_its_level_from_sect_nesting(self):
        h = TagElement("/H", 1, (0, 0, 10, 10), "Deep", 3)
        inner = TagElement("/Sect", 1, (0, 0, 10, 10), "Deep", 2, children=[h])
        outer = TagElement("/Sect", 1, (0, 0, 10, 10), "Deep", 1, children=[inner])
        alone = TagElement("/H", 1, (0, 0, 10, 10), "Top", 0)
        claims = TagWitness({1: TagPage(1, [outer, alone], 1.0)}).claims(1).order
        assert [(c.role, c.attrs) for c in claims] == [
            (Role.H2, {"level": 2, "generic": True}), (Role.H1, {"level": 1, "generic": True})]

    def test_bare_blocks_count_but_inline_spans_do_not(self):
        span = TagElement("/Span", 1, (0, 0, 5, 5), "in", 2)
        para = TagElement("/P", 1, (0, 0, 10, 10), "in a paragraph", 1, children=[span])
        bare = TagElement("/Div", 1, (0, 20, 10, 30), "bare text", 1)
        formula = TagElement("/Formula", 1, (0, 40, 10, 50), "x = 1", 1)
        wrapper = TagElement("/Div", 1, (0, 0, 10, 50), "in a paragraph bare text", 0,
                             children=[para, bare, formula])
        order = TagWitness({1: TagPage(1, [wrapper], 1.0)}).claims(1).order
        assert [(c.role, c.text) for c in order] == [
            (Role.P, "in a paragraph"), (None, "bare text"), (None, "x = 1")]

    def test_long_text_is_keyed_by_its_ends(self):
        text = "a" * 40 + "b" * 30 + "c" * 20
        el = TagElement("/P", 1, (0, 0, 10, 10), text, 0)
        claim = TagWitness({1: TagPage(1, [el], 1.0)}).claims(1).order[0]
        assert claim.text == "a" * 40 + "…" + "c" * 20
        assert isinstance(claim, Claim)

    def test_a_page_is_built_once_and_a_missing_page_stays_missing(self):
        el = TagElement("/P", 1, (0, 0, 10, 10), "x", 0)
        witness = TagWitness({1: TagPage(1, [el], 1.0)})
        assert witness.claims(1) is witness.claims(1)
        assert witness.claims(1) == TagWitness({1: TagPage(1, [el], 1.0)}).claims(1)
        assert witness.claims(2) is None and witness.claims(2) is None

    def test_actual_text_is_carried(self):
        el = TagElement("/Span", 1, (0, 0, 10, 10), "f f", 0, actual_text="ff")
        claim = TagWitness({1: TagPage(1, [el], 1.0)}).claims(1).order[0]
        assert claim.attrs == {"actual_text": "ff"}


def outline(*entries, width=612.0):
    return [Bookmark(title, level, page, top, width) for title, level, page, top in entries]


class TestBookmarkWitness:
    MARKS = outline(("Chapter 1  Filing", 1, 2, 100.0), ("Part A", 2, 2, 300.0),
                    ("Missing", 1, 2, 500.0), ("Elsewhere", 1, 3, 40.0))
    TEXTS = {2: ["Chapter 1 Filing Information", "body", "PART A", "tail"],
             3: ["other"]}

    def test_title_that_prefixes_a_line_claims_it(self):
        witness = BookmarkWitness(self.MARKS, self.TEXTS)
        page = witness.claims(2)
        assert [(c.role, c.attrs["level"]) for c in page.order] == [(Role.H1, 1), (Role.H2, 2)]
        assert page.order[0].text == "Chapter 1  Filing"

    def test_box_is_the_page_width_and_a_band_at_the_destination(self):
        claim = BookmarkWitness(self.MARKS, self.TEXTS).claims(2).order[0]
        assert claim.bbox == (0.0, 98.0, 612.0, 114.0)

    def test_pages_and_pages_without_a_match(self):
        witness = BookmarkWitness(self.MARKS, self.TEXTS)
        assert witness.pages() == {2, 3}
        assert witness.claims(3) is None
        assert witness.claims(9) is None
        assert witness.source is Source.TAGS

    def test_no_destination_y_skips_the_bookmark(self):
        marks = outline(("Intro", 1, 1, None))
        assert BookmarkWitness(marks, {1: ["Intro"]}).claims(1) is None
        assert BookmarkWitness(marks, {1: ["Intro"]}).pages() == set()

    def test_trust_needs_enough_resolved_bookmarks(self):
        texts = {1: ["A", "B", "C"]}
        few = outline(("A", 1, 1, 10.0), ("B", 1, 1, 50.0))
        assert BookmarkWitness(few, texts).claims(1).trusted is False
        enough = outline(("A", 1, 1, 10.0), ("B", 1, 1, 50.0), ("C", 1, 1, 90.0))
        assert BookmarkWitness(enough, texts).claims(1).trusted is True
        unresolved = enough + outline(("D", 1, None, None), ("E", 1, None, None))
        assert BookmarkWitness(unresolved, texts).claims(1).trusted is False

    def test_deep_levels_cap_at_h6(self):
        marks = outline(("Deep", 9, 1, 10.0))
        assert BookmarkWitness(marks, {1: ["Deep"]}).claims(1).order[0].role is Role.H6

    def test_from_a_real_outline(self, tagged):
        witness = BookmarkWitness(read_bookmarks(tagged),
                                  {1: ["Chapter One", "First paragraph."], 2: ["Details"]})
        assert witness.claims(1).order[0].role is Role.H1
        assert witness.claims(2).order[0].role is Role.H2
        assert witness.claims(2).trusted


@pytest.fixture(scope="module")
def fw9():
    return read_tags(FW9, jobs=1)


@pytest.mark.skipif(not FW9.exists(), reason="samples/fw9.pdf is not checked out")
class TestFw9:
    def test_headings_and_th_cells(self, fw9):
        # the plan said page 1 holds H2-H4; it holds H1-H3 and the H4s are on pages 3 and 4
        assert {"/H1", "/H2", "/H3"} <= {e.tag for e in flatten(fw9[1].elements)}
        counts = {}
        for page in fw9.values():
            for e in flatten(page.elements):
                counts[e.tag] = counts.get(e.tag, 0) + 1
        assert (counts["/H2"], counts["/H3"], counts["/H4"], counts["/TH"]) == (6, 12, 8, 8)
        assert {n for n, page in fw9.items()
                if any(e.tag == "/H4" for e in flatten(page.elements))} == {3, 4}

    def test_page_one_is_trusted(self, fw9):
        assert fw9[1].tagged_share >= tags.TRUSTED_SHARE
        assert TagWitness(fw9).claims(1).trusted

    def test_headings_carry_text_and_boxes(self, fw9):
        h2 = next(e for e in flatten(fw9[1].elements) if e.tag == "/H2")
        assert h2.text
        x0, top, x1, bottom = h2.bbox
        assert 0 <= x0 < x1 <= 612 and 0 <= top < bottom <= 792

    def test_no_bookmarks(self):
        assert read_bookmarks(FW9) == []


@pytest.mark.skipif(not P17.exists(), reason="samples/p17.pdf is not checked out")
class TestP17:
    def test_bookmarks(self):
        marks = read_bookmarks(P17)
        assert len(marks) == 122
        assert sum(b.page is not None for b in marks) >= 0.9 * len(marks)
        assert {b.level for b in marks} >= {1, 2}

    def test_a_few_pages(self):
        pages = read_tags(P17, [8, 9], jobs=1)
        assert sorted(pages) == [8, 9]
        roles = {e.tag for page in pages.values() for e in flatten(page.elements)}
        assert {"/H1", "/P"} <= roles
