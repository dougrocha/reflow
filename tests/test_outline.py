"""Heading levels from printed section numbers."""

import pytest

from reflow.analyze.outline import relevel_numbered, section_number
from reflow.model import Node, Role

RFP = [
    ("Section 1 – Project Description", Role.H2),
    ("1.1 Purpose", Role.H3),
    ("2.3.2 Information about Kitsap", Role.H3),
    ("3 SECTION 3 – EXISTING SYSTEM DESCRIPTION", Role.H2),
    ("3.1 CAD SYSTEM", Role.H3),
    ("3.1.1 Database Servers", Role.H4),
    ("3.1.2 Workstations", Role.H4),
    ("3.2 INTERFACES", Role.H4),
    ("3.2.1 Fire Station Alerting", Role.H4),
    ("3.2.2 Mobile Data", Role.H4),
]


def headings(texts_and_roles):
    return [Node(role, page=1, text=text) for text, role in texts_and_roles]


def test_sibling_sections_get_one_level_in_the_rfp_sequence():
    nodes = headings(RFP)
    relevel_numbered(nodes)
    assert [(n.text, n.role) for n in nodes] == [
        ("Section 1 – Project Description", Role.H2),
        ("1.1 Purpose", Role.H3),
        ("2.3.2 Information about Kitsap", Role.H2),  # no "Section 2" or "2.3" printed: top level
        ("3 SECTION 3 – EXISTING SYSTEM DESCRIPTION", Role.H2),
        ("3.1 CAD SYSTEM", Role.H3),
        ("3.1.1 Database Servers", Role.H4),
        ("3.1.2 Workstations", Role.H4),
        ("3.2 INTERFACES", Role.H3),
        ("3.2.1 Fire Station Alerting", Role.H4),
        ("3.2.2 Mobile Data", Role.H4),
    ]


def test_unnumbered_headings_keep_their_style_level():
    nodes = headings([("Overview", Role.H2), ("1.1 A", Role.H3), ("1.2 B", Role.H4),
                      ("1.3 C", Role.H4), ("Appendix", Role.H5)])
    relevel_numbered(nodes)
    assert [n.role for n in nodes] == [Role.H2, Role.H2, Role.H2, Role.H2, Role.H5]


def test_two_numbered_headings_of_ten_are_left_alone():
    nodes = headings([("Intro", Role.H2)] * 8 + [("1.1 A", Role.H3), ("1.2 B", Role.H4)])
    relevel_numbered(nodes)
    assert nodes[-2].role == Role.H3 and nodes[-1].role == Role.H4


def test_a_document_where_few_number_starts_parse_is_left_alone():
    nodes = headings([("1.1 A", Role.H3), ("1.2 B", Role.H4), ("1.3 C", Role.H4),
                      ("2024 Annual Report", Role.H2), ("2025 Plan", Role.H2),
                      ("1999 Results", Role.H2)])
    relevel_numbered(nodes)
    assert [n.role for n in nodes] == [Role.H3, Role.H4, Role.H4, Role.H2, Role.H2, Role.H2]


def test_section_n_headings_do_not_hide_a_digit_led_heading_that_is_no_number():
    nodes = headings([("Section 1 – A", Role.H3), ("Section 2 – B", Role.H4),
                      ("Section 3 – C", Role.H4), ("2024 Annual Report", Role.H2)])
    relevel_numbered(nodes)
    assert [n.role for n in nodes] == [Role.H3, Role.H4, Role.H4, Role.H2]


def test_section_n_headings_alone_are_numbered():
    nodes = headings([("Section 1 – A", Role.H3), ("Section 2 – B", Role.H4),
                      ("Section 3 – C", Role.H4)])
    relevel_numbered(nodes)
    assert [n.role for n in nodes] == [Role.H2, Role.H2, Role.H2]


def test_the_title_is_not_touched():
    title = Node(Role.H1, page=1, text="1.1 A")
    nodes = [title, *headings([("1.1 A", Role.H4), ("1.2 B", Role.H4), ("1.3 C", Role.H4)])]
    relevel_numbered(nodes)
    assert title.role == Role.H1


@pytest.mark.parametrize("text,number", [
    ("3.1 CAD SYSTEM", (3, 1)), ("3.1.2 Servers", (3, 1, 2)), ("3.1", (3, 1)),
    ("Section 1 – Project", (1,)), ("SECTION 12: Terms", (12,)), ("Section 2 - Scope", (2,)),
    ("3 SECTION 3 – EXISTING", (3,)), ("4 – Budget", (4,)), ("5: Notes", (5,)),
    ("2024 Annual Report", None), ("3 months", None), ("100 SECTION", None),
    ("Section 1 Overview", None), ("3.1.2.4 Deep", None), ("3.1.", None),
    ("Rule 12(b)(6)", None), ("12(b) Defenses", None), ("Overview", None),
])
def test_section_number(text, number):
    assert section_number(text) == number
