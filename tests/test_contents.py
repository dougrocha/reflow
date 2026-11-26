"""A printed table of contents becomes one list, nested by its printed sections."""

from reflow.analyze.contents import fold_contents
from reflow.model import Node, Role


def entry(title, ref, top, x=50.0):
    return Node(Role.LI, page=3, bbox=(x, top, 561.0, top + 12), text=title,
                attrs={"contents": True, "page_ref": ref, "size": 11.0, "x": x})


def para(text, top, x=50.0, role=Role.P, width=200.0):
    return Node(role, page=3, bbox=(x, top, x + width, top + 12), text=text,
                attrs={"font_size": 11.0} if role != Role.P else {})


def texts(lst):
    """The list as (text, page label, children) tuples."""
    return [(li.text, li.attrs.get("page_ref", ""),
             [texts(c) for c in li.children if c.role == Role.LIST]) for li in lst.children]


def test_sections_labels_and_entries_become_one_nested_list():
    nodes = [para("Table of Contents", 93, role=Role.H4),
             para("Page", 140, x=540.0, role=Role.H4, width=34.0),
             para("INTRODUCTORY SECTION", 153, role=Role.H4),
             entry("Transmittal Letter", "iii", 180), entry("Board of Trustees", "xi", 194),
             para("FINANCIAL SECTION", 277, role=Role.H4),
             entry("Independent Auditors’ Report", "1", 304),
             para("Basic Financial Statements:", 356),
             para("Government-Wide Financial Statements:", 379, x=68.0),
             entry("Statement of Net Position", "21", 395, x=77.0),
             entry("Notes to Basic Financial Statements", "33", 589, x=68.0)]
    out = fold_contents(nodes)

    assert [n.text for n in out[:2]] == ["Table of Contents", "Page"]
    assert [n.role for n in out[:2]] == [Role.H4, Role.P]   # the column label is no heading
    (lst,) = out[2:]
    assert lst.role == Role.LIST
    assert texts(lst) == [
        ("INTRODUCTORY SECTION", "", [[("Transmittal Letter", "iii", []),
                                       ("Board of Trustees", "xi", [])]]),
        ("FINANCIAL SECTION", "", [[
            ("Independent Auditors’ Report", "1", []),
            ("Basic Financial Statements:", "", [[
                ("Government-Wide Financial Statements:", "", [[
                    ("Statement of Net Position", "21", [])]]),
                ("Notes to Basic Financial Statements", "33", [])]])]])]


def test_a_wrapped_entry_that_still_holds_its_leader_loses_it_and_keeps_its_page():
    wrapped = para("Reconciliation of the Funds Balance Sheet to the Statement of Net Position "
                   + "." * 40 + " 27", 468, x=86.0, width=475.0)
    nodes = [entry("Balance Sheet", "26", 440, x=86.0), wrapped,
             entry("Statement of Activities", "22", 500, x=86.0),
             entry("Notes", "33", 520, x=86.0)]
    (lst,) = fold_contents(nodes)
    assert [(li.text, li.attrs["page_ref"]) for li in lst.children] == [
        ("Balance Sheet", "26"),
        ("Reconciliation of the Funds Balance Sheet to the Statement of Net Position", "27"),
        ("Statement of Activities", "22"), ("Notes", "33")]


def test_a_page_with_few_entries_is_left_alone():
    nodes = [para("Chapter 1", 90, role=Role.H2), entry("One", "1", 120), entry("Two", "2", 140)]
    assert fold_contents(nodes) is nodes


def test_a_page_of_prose_between_its_entries_is_left_alone():
    prose = para(" ".join(["word"] * 30), 200)
    nodes = [entry("A", "1", 100), entry("B", "2", 120), prose, entry("C", "3", 300),
             entry("D", "4", 320)]
    assert fold_contents(nodes) is nodes


def test_an_index_page_with_letter_headings_is_left_alone():
    letter = para("A", 150, role=Role.H3)
    letter.attrs["index_letter"] = True
    nodes = [entry("Alpha", "1", 100), entry("Beta", "2", 120), letter,
             entry("Gamma", "3", 160), entry("Delta", "4", 180)]
    assert fold_contents(nodes) is nodes


def test_section_headings_over_lists_of_entries_are_left_as_they_are():
    # budget-appendix p5: "Detailed Budget Estimates by Agency:" over its entries.
    nodes = [para("TABLE OF CONTENTS", 90, role=Role.H2),
             para("Detailed Budget Estimates by Agency:", 120, role=Role.H3),
             entry("Legislative Branch", "13", 150), entry("Judicial Branch", "47", 165),
             para("Other Materials:", 200, role=Role.H3),
             entry("Advance Appropriations", "1247", 230), entry("Index", "1255", 245)]
    assert fold_contents(nodes) is nodes
    assert [n.role for n in nodes[:2]] == [Role.H2, Role.H3]


def test_a_group_label_above_the_first_entry_heads_the_entries_under_it():
    nodes = [para("Financial Statements:", 100),
             entry("Statement of Net Position", "21", 120, x=68.0),
             entry("Statement of Activities", "22", 134, x=68.0),
             para("Notes:", 150),
             entry("Note 1", "30", 170, x=68.0), entry("Note 2", "31", 184, x=68.0)]
    (lst,) = fold_contents(nodes)
    assert texts(lst) == [
        ("Financial Statements:", "", [[("Statement of Net Position", "21", []),
                                        ("Statement of Activities", "22", [])]]),
        ("Notes:", "", [[("Note 1", "30", []), ("Note 2", "31", [])]])]


def test_a_sentence_above_the_entries_stays_a_paragraph():
    nodes = [para("See the notes.", 100),
             *(entry(f"Entry {i}", str(i), 120 + 14 * i, x=68.0) for i in range(4))]
    out = fold_contents(nodes)
    assert out[0].role == Role.P and out[0].text == "See the notes."
