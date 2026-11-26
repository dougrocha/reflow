"""Checks that filter the page-vision model's confidently wrong heading and row-header answers
before they are shown. The wrong cases are the page-vision report's ("Every confidently wrong
case"), with their text as reflow converts it; the right cases are headings and row headers the
page specs require, which no check may reject. Scored on real conversions by
tests/pages/vision_gates.py."""

import pytest

from reflow.analyze import vision_gates as vg
from reflow.model import Fact, Mark, Node, Role, Source


def para(text, *, bold=False, larger=False, tag=None, role=Role.P):
    node = Node(role, page=1, bbox=(72, 100, 400, 112), text=text)
    if bold:
        node.marks.append(Mark(0, len(text), "strong", text))
    if larger:
        node.attrs["set_apart"] = "larger"
    if tag:
        node.facts.append(Fact("role-disagreement", Source.TAGS, f"the tag tree says {tag}",
                               {"ours": role.value, "theirs": tag}))
    return node


def gates(text, before=None, **style):
    return vg.heading_gates(para(text, **style), before and para(before))


# -- continuation: the block goes on from a sentence -----------------------------------------------


@pytest.mark.parametrize("text", [
    "court.",                                                            # legislation p9
    "appeals;",                                                          # legislation p40
    "court, each party or the party's attorney shall include in its initial",          # p5
    "hearing on the application before the other business court judge.",              # p12
])
def test_a_piece_of_a_sentence_is_no_heading(text):
    assert vg.Gate.CONTINUATION in gates(text, bold=True)


def test_a_line_after_prose_that_stops_mid_sentence_runs_on():
    # legislation pp4, 8, 19: the second line of a SECTION sentence
    assert vg.Gate.RUNS_ON in gates("Code, is amended to read as follows:",
                                    "SECTION 3. Section 19.003(a), Civil Practice and Remedies",
                                    bold=True)


@pytest.mark.parametrize("text,before", [
    ("Requests for Information", "The District's financial statements are designed to give an "
     "overview of its finances."),                                       # audit p30
    ("CERTIFICATION", None),                                             # appeal p2
    ("Patient Information", "Insurance Company/Dental Benefit Plan Information"),
    ("SECTION 1. SHORT TITLE.", "AN ACT relating to the business court."),
])
def test_a_heading_after_a_finished_sentence_or_a_label_is_no_continuation(text, before):
    assert not {vg.Gate.CONTINUATION, vg.Gate.RUNS_ON} & set(gates(text, before, bold=True))


def test_a_paragraph_before_that_ends_mid_sentence_runs_on_only_when_it_is_prose():
    # a short label before a heading ends with no stop, and says nothing about what follows
    assert vg.Gate.RUNS_ON not in gates("Contact Us", "Effective 07/02/2025", bold=True)


def test_runs_on_is_off_since_an_address_before_a_directory_heading_ends_with_no_stop():
    # pima-directory p445: 35 required headings follow a paragraph like this one
    assert vg.Gate.RUNS_ON in gates("Diaz, Carlos E, MD (M)",
                                    "698 E Wetmore Rd, Ste 100 Tucson, AZ 85705 (520) 955-1000",
                                    bold=True)
    assert vg.Gate.RUNS_ON not in vg.ON


# -- form identifiers ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["BOE-305-AH (P1) REV. 12 (05-24)",
                                  "BOE-305-AH (P2) REV. 12 (05-24)",
                                  "BOE-305-AH (P3) REV. 12 (05-24)"])
def test_a_form_identifier_is_no_heading(text):
    assert vg.Gate.FORM_ID in gates(text, bold=True)


@pytest.mark.parametrize("text", ["5. TYPE OF ASSESSMENT BEING APPEALED",
                                  "Sec. 25A.021. ACTIONS COMMENCED BEFORE SEPTEMBER 1, 2024.",
                                  "Part I Taxpayer Identification Number (TIN)",
                                  "Schedule A (Form 1040)", "COVID-19 Testing"])
def test_a_heading_that_names_a_form_or_a_code_is_no_form_identifier(text):
    assert vg.Gate.FORM_ID not in gates(text, bold=True)


# -- a witness in the type or the tags -------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "SECTION 6. Section 30.015(a), Civil Practice and Remedies Code, is amended to read as "
    "follows:",                                                          # legislation p5
    "NOW, THEREFORE, BE IT RESOLVED BY THE BOARD OF DIRECTORS",          # budget p2
    "This page intentionally left blank.",                               # audit p19
    "DATE OF APPLICATION",                                               # permit p1
])
def test_a_line_set_like_the_body_text_with_no_tag_has_no_witness(text):
    assert vg.Gate.NO_WITNESS in gates(text)


@pytest.mark.parametrize("style", [{"bold": True}, {"larger": True}, {"tag": "h2"}])
def test_bold_larger_type_or_a_heading_tag_is_a_witness(style):
    assert vg.Gate.NO_WITNESS not in gates("CERTIFICATION", **style)


def test_bold_over_part_of_the_text_is_no_witness():
    node = para("Note: the fee is due")
    node.marks.append(Mark(0, 5, "strong", "Note:"))
    assert vg.Gate.NO_WITNESS in vg.heading_gates(node, None)


# -- row headers -----------------------------------------------------------------------------------


@pytest.mark.parametrize("cells", [
    ["", "3", "10", "17", "24", "31"],                     # a calendar month's Sunday column
    ["1,234", "$ 5,678", "(12)", "-", "4.5%", "874,711 $"],   # audit p62's numeric columns
    ["3/14/24", "03/21/2024", "4-2-24"],                    # ledger dates
    ["$45.00", "$1,200.00"],                               # ledger fees
    ["Sunday", "Monday", "Tuesday"],
])
def test_a_column_of_numbers_dates_or_weekdays_is_data(cells):
    assert vg.data_column(cells)


@pytest.mark.parametrize("cells", [
    ["VGG-16 [41]", "ResNet-152"],                         # resnet p6
    ["18 layers", "34 layers"],                            # resnet p5
    ["plain", "ResNet"],
    ["", ""],                                              # nothing to judge
])
def test_a_column_of_names_is_no_data_column(cells):
    assert not vg.data_column(cells)
