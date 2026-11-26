"""Groups of check boxes: which become radios, which share a fieldset, and which have none.
A synthetic page: lines and widgets at set positions, planned by analyze.forms."""

from __future__ import annotations

import pytest

from reflow.analyze.forms import CHOOSE_ONE, plan_forms
from reflow.extract.forms import Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import Node, Role
from reflow.render.html import NodeRenderer


def _line(x0: float, top: float, text: str, bold: bool = False, size: float = 10.0) -> Line:
    return Line(1, (x0, top, x0 + 5 * len(text), top + size * 1.2), text, size, bold, False, "Helvetica")


def _box(index: int, x: float, top: float, name: str | None = None, checked: bool = False) -> Widget:
    return Widget(1, index, "checkbox", (x, top, x + 9, top + 9), name=name or f"Box{index}",
                  export="On", checked=checked)


def _plan(lines: list[Line], widgets: list[Widget]) -> list[Node]:
    page = PageContent(1, 612, 792, lines, [], [], widgets=widgets)
    return plan_forms(page, lines, lines, []).flow


def _groups(nodes: list[Node]) -> list[Node]:
    return [n for n in nodes if n.role == Role.GROUP]


def _kinds(group: Node) -> list[str]:
    return [k.attrs["kind"] for k in group.children]


@pytest.mark.parametrize("text, expected", [
    ("Check only one. See instructions", True), ("check one of the following boxes", True),
    ("Select one:", True), ("Mark 1", True),
    ("This request applies to (check one or both):", False), ("Check one or more", False),
    ("Check one and attach a copy", False), ("check the box", False),
])
def test_choose_one_wants_a_one_and_no_or(text, expected):
    assert bool(CHOOSE_ONE.search(text)) is expected


def test_a_yes_no_pair_of_separate_fields_becomes_radios_of_one_group():
    lines = [_line(40, 100, "Is the owner here?"), _line(66, 120, "Yes"), _line(126, 120, "No")]
    [group] = _groups(_plan(lines, [_box(0, 50, 120), _box(1, 110, 120)]))
    assert _kinds(group) == ["radio", "radio"] and group.attrs["radio"]
    assert group.text == "Is the owner here?"
    assert {k.attrs["group"] for k in group.children} == {group.attrs["name"]}
    # each radio still names its own PDF checkbox field
    assert [k.attrs["name"] for k in group.children] == ["Box0", "Box1"]


def test_a_y_n_pair_on_a_row_of_other_options_is_a_group_of_its_own():
    lines = [_line(66, 120, "Pathways"), _line(166, 120, "Health Choice"), _line(266, 120, "Y"),
             _line(326, 120, "N"), _line(10, 120, "Other Health Insurance:")]
    boxes = [_box(0, 50, 120), _box(1, 150, 120), _box(2, 250, 120), _box(3, 310, 120)]
    groups = _groups(_plan(lines, boxes))
    pair = [g for g in groups if _kinds(g) == ["radio", "radio"]]
    assert len(pair) == 1 and [k.text for k in pair[0].children] == ["Y", "N"]
    assert [k.text for g in groups if g is not pair[0] for k in g.children] == ["Pathways", "Health Choice"]
    assert all(_kinds(g) == ["checkbox", "checkbox"] for g in groups if g is not pair[0])


def test_options_under_a_printed_check_only_one_are_radios_but_not_under_check_one_or_both():
    options = [_line(66, 120, "Alpha"), _line(66, 140, "Beta"), _line(66, 160, "Gamma")]
    boxes = [_box(0, 50, 120), _box(1, 50, 140), _box(2, 50, 160)]
    [one] = _groups(_plan([_line(40, 100, "Check only one:"), *options], boxes))
    assert _kinds(one) == ["radio"] * 3
    [both] = _groups(_plan([_line(40, 100, "Check one or both:"), *options], boxes))
    assert _kinds(both) == ["checkbox"] * 3
    [plain] = _groups(_plan([_line(40, 100, "Services:"), *options], boxes))
    assert _kinds(plain) == ["checkbox"] * 3


def test_a_cue_with_a_control_between_it_and_the_group_is_not_the_groups():
    options = [_line(66, 160, "Alpha"), _line(66, 180, "Beta")]
    other = Widget(1, 9, "text", (50, 125, 200, 145), name="Name")
    lines = [_line(40, 100, "Check only one:"), *options]
    [group] = _groups(_plan(lines, [_box(0, 50, 160), _box(1, 50, 180), other]))
    assert _kinds(group) == ["checkbox", "checkbox"]


def test_boxes_that_share_a_pdf_field_or_are_ticked_twice_stay_checkboxes():
    lines = [_line(40, 100, "Check only one:"), _line(66, 120, "Alpha"), _line(66, 140, "Beta")]
    same = [_box(0, 50, 120, name="Pick"), _box(1, 50, 140, name="Pick")]
    assert _kinds(_groups(_plan(lines, same))[0]) == ["checkbox", "checkbox"]
    both = [_box(0, 50, 120, checked=True), _box(1, 50, 140, checked=True)]
    assert _kinds(_groups(_plan(lines, both))[0]) == ["checkbox", "checkbox"]
    one = [_box(0, 50, 120, checked=True), _box(1, 50, 140)]
    assert _kinds(_groups(_plan(lines, one))[0]) == ["radio", "radio"]


def test_two_options_that_differ_by_a_not_are_radios_but_unrelated_ones_are_not():
    def pair(first, second):
        lines = [_line(66, 400, first), _line(66, 420, second)]
        [group] = _groups(_plan(lines, [_box(0, 50, 400), _box(1, 50, 420)]))
        return _kinds(group)

    assert pair("Are requested.", "Are not requested.") == ["radio", "radio"]
    assert pair("Are not requested", "Are requested") == ["radio", "radio"]
    assert pair("Alpha", "Not beta") == ["checkbox", "checkbox"]


def test_a_group_nothing_names_has_no_fieldset_and_no_empty_legend():
    lines = [_line(66, 400, "Alpha"), _line(66, 420, "Beta")]
    [group] = _groups(_plan(lines, [_box(0, 50, 400), _box(1, 50, 420)]))
    assert group.attrs["label"] == "none" and not group.text
    html = str(NodeRenderer({}, {1: "1"}).render(group))
    assert "<fieldset" not in html and "<legend" not in html and html.count("<input") == 2


def test_a_group_with_no_legend_joins_the_group_the_same_title_names():
    lines = [_line(40, 100, "PROPERTY TYPE", bold=True), _line(66, 120, "Alpha"), _line(66, 140, "Beta"),
             _line(416, 120, "Gamma"), _line(416, 140, "Delta")]
    boxes = [_box(0, 50, 120), _box(1, 50, 140), _box(2, 400, 120), _box(3, 400, 140)]
    [group] = _groups(_plan(lines, boxes))
    assert [k.text for k in group.children] == ["Alpha", "Beta", "Gamma", "Delta"]
    assert group.attrs["aria"] == "PROPERTY TYPE"
    html = str(NodeRenderer({}, {1: "1"}).render(group))
    assert '<legend class="sr-only">PROPERTY TYPE</legend>' in html


def _section(*kids: Node) -> Node:
    heading = Node(Role.H2, page=1, text="1. APPLICANT INFORMATION")
    return Node(Role.SECTION, page=1, attrs={"level": 2}, children=[heading, *kids])


def _text(i: int, label: str) -> Node:
    return Node(Role.FIELD, page=1, text=label, attrs={"kind": "text", "fid": f"f1-{i}", "name": f"T{i}",
                                                        "label": "printed"})


def test_the_text_boxes_under_a_heading_form_a_fieldset_named_by_it():
    para = Node(Role.P, page=1, text="Please print.")
    html = str(NodeRenderer({}, {1: "1"}).render(_section(_text(0, "NAME"), para, _text(1, "EMAIL"))))
    assert html.count("<fieldset") == 1
    assert '<legend class="sr-only">1. APPLICANT INFORMATION</legend>' in html
    inside = html[html.index("<fieldset"):html.index("</fieldset>")]
    assert inside.count("<input") == 2 and "Please print." in inside


def test_one_text_box_or_a_group_first_makes_no_section_fieldset():
    html = str(NodeRenderer({}, {1: "1"}).render(_section(_text(0, "NAME"))))
    assert "<fieldset" not in html
    group = Node(Role.GROUP, page=1, text="Plan", attrs={"kind": "group"},
                 children=[Node(Role.FIELD, page=1, text="A", attrs={"kind": "checkbox", "fid": "f1-9"})])
    html = str(NodeRenderer({}, {1: "1"}).render(_section(group, _text(0, "NAME"), _text(1, "EMAIL"))))
    assert html.count("<fieldset") == 1   # the group's own; the boxes after it are not wrapped again
