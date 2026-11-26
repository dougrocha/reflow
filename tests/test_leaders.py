"""Dot leaders on contents pages: detection, the text cleanup, breaks, lists and HTML."""

import re

from reflow.analyze.breaks import break_reason
from reflow.analyze.layout import analyze
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.leaders import normalize_document, normalize_text, parse_contents_entry
from reflow.model import Document, Node, PageInfo, Role, Why, code_of
from reflow.render.site import render_html

FFFD = "�"


def line(top, text, *, x0=72.0, size=10.0, width=200.0, page=1):
    return Line(page=page, bbox=(x0, top, x0 + width, top + 10), text=text, size=size,
                bold=False, italic=False, font="Helvetica")


# --- leader detection ----------------------------------------------------------------------

def test_leaders_made_of_replacement_characters_dots_and_spaced_dots():
    assert parse_contents_entry(f"Explanation of Estimates {FFFD * 12}1") == (
        "Explanation of Estimates", "1")
    assert parse_contents_entry("Legislative Branch .......... 13") == ("Legislative Branch", "13")
    assert parse_contents_entry("Primary Care Providers (PCPs). . . . . . 9") == (
        "Primary Care Providers (PCPs)", "9")
    assert parse_contents_entry(f"Department of Housing {FFFD * 3}527") == (
        "Department of Housing", "527")
    assert parse_contents_entry("Judicial Branch ……… 47") == ("Judicial Branch", "47")


def test_roman_page_labels_are_entries():
    assert parse_contents_entry("Preface ..... iv") == ("Preface", "iv")


def test_things_that_are_not_contents_entries():
    assert parse_contents_entry("1...2") is None  # no title letters
    assert parse_contents_entry("He waited... and then left.") is None  # no page label
    assert parse_contents_entry("Revenue ........ $1,234,567") is None  # currency value
    assert parse_contents_entry("Units ..... 1,234 2,345") is None  # two numeric columns
    assert parse_contents_entry("Total ..... 1,234") is None  # comma in the value
    assert parse_contents_entry("Wait... 3 days") is None  # the label is not at the end


# --- normalize_text / normalize_document ---------------------------------------------------

def test_normalize_text_turns_leaders_into_dots_and_drops_stray_replacements():
    text, leaders, stray = normalize_text(f"Title {FFFD * 4}12 and a stray {FFFD} here")
    assert text == "Title ....12 and a stray  here"
    assert (leaders, stray) == (4, 1)
    assert FFFD not in text


def test_normalize_document_counts_every_repair():
    entry = Node(Role.LI, page=2, text=f"Legislative Branch {FFFD * 5}", attrs={"page_ref": "13"})
    lines = Node(Role.P, page=2, text="Address", attrs={"lines": [f"Suite {FFFD}", "Room 4"]})
    doc = Document(root=Node(Role.DOC, children=[entry, lines]),
                   pages=[PageInfo(number=2, width=612, height=792)])

    totals = normalize_document(doc)

    assert totals == {"leaders": 5, "stray": 1}
    assert entry.text == "Legislative Branch ....."
    assert lines.attrs["lines"] == ["Suite ", "Room 4"]


def test_normalize_document_keeps_the_code_of_a_why_over_its_repaired_words():
    why = [Why(f"new block: a stray {FFFD}", "stray"), "plain"]
    node = Node(Role.P, page=1, text="x", attrs={"why": why})
    normalize_document(Document(root=Node(Role.DOC, children=[node]),
                                pages=[PageInfo(number=1, width=612, height=792)]))
    assert node.attrs["why"] == ["new block: a stray ", "plain"]
    assert [code_of(w) for w in node.attrs["why"]] == ["stray", None]


# --- breaks and lists ----------------------------------------------------------------------

def test_break_reason_separates_contents_entries():
    entry = line(100, f"Judicial Branch {FFFD * 8}47")
    assert break_reason(line(90, "Preface"), entry) == "contents entry"
    assert break_reason(entry, line(112, f"Legislative {FFFD * 8}13")) == "contents entry"
    assert break_reason(entry, line(112, "continues here")) == "after a contents entry"


def test_consecutive_entries_become_one_contents_list():
    rows = [line(100 + 15 * i, f"Department {i} {FFFD * 9}{10 + i}") for i in range(3)]
    page = PageContent(number=2, width=612, height=792, lines=rows, images=[])

    nodes = analyze([page])

    (contents,) = [n for n in nodes if n.role == Role.LIST]
    assert contents.attrs == {"ordered": False, "contents": True}
    assert [c.text for c in contents.children] == ["Department 0", "Department 1", "Department 2"]
    assert [c.attrs["page_ref"] for c in contents.children] == ["10", "11", "12"]
    assert all("contents entry: title, dot leader and page number" in c.attrs["why"][-1]
               for c in contents.children)


# --- HTML --------------------------------------------------------------------------------

def _contents_doc():
    entries = [Node(Role.LI, page=2, text="Judicial Branch",
                    attrs={"contents": True, "page_ref": "5"}),
               Node(Role.LI, page=2, text="Lost Pages",
                    attrs={"contents": True, "page_ref": "99"})]
    contents = Node(Role.LIST, page=2, attrs={"ordered": False, "contents": True},
                    children=entries)
    pages = [PageInfo(number=n, width=612, height=792) for n in range(1, 6)]
    return Document(root=Node(Role.DOC, children=[contents]), pages=pages)


def test_contents_entries_render_title_then_linked_page_number(tmp_path):
    render_html(_contents_doc(), "Budget", tmp_path, page_widths=())
    html = (tmp_path / "chunks" / "0001.html").read_text(encoding="utf-8")

    assert '<li>Judicial Branch <a href="chunks/0001.html#p5">5</a></li>' in re.sub(r' data-node="[^"]*"', "", html)  # page 5 is in this chunk
    assert "<li>Lost Pages 99</li>" in re.sub(r' data-node="[^"]*"', "", html)  # no page 99 in the document: plain text
    assert FFFD not in html and "\x00" not in html
    assert "....." not in html  # the dots are gone from the rendered entries


def test_page_links_point_at_the_chunk_that_holds_the_page(tmp_path):
    from reflow.render.chunks import Chunk, resolve_links

    own, other = Chunk(1, pages=[1]), Chunk(2, pages=[5])
    chunk_of_page = {1: own, 5: other}
    html = '<a href="<p5>">5</a>'
    # Always chunks/NNNN.html#pN: the chunk pages set <base href="../">, so a bare "#pN" from
    # chunks/0001.html would go to the output root.
    assert resolve_links(html, chunk_of_page) == '<a href="chunks/0002.html#p5">5</a>'
    same = '<a href="<p1>">1</a>'
    assert resolve_links(same, chunk_of_page) == '<a href="chunks/0001.html#p1">1</a>'


def test_a_budget_row_with_a_line_code_is_a_table_row_not_an_entry():
    # A row code opens a schedule's row, leaders or not; a year or a short number still opens an entry.
    assert parse_contents_entry("235999 Total guaranteed loan reestimates ........ 172") is None
    assert parse_contents_entry("3510 Budget authority ................ 412") is None
    assert parse_contents_entry("2024 Annual Report ........ 5") == ("2024 Annual Report", "5")
    assert parse_contents_entry("12 Appendix ........ 5") == ("12 Appendix", "5")
