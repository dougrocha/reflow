"""Which blocks are headings: fragments, bullets, index entries, single characters, folios."""

from reflow.analyze.blocks import Block
from reflow.analyze.headings import (
    HeadingWhy, demoted_by_twin, from_ink, heading_decision, heading_why, join_wrapped_headings, ranks,
)
from reflow.analyze.layout import analyze
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import Node, Role, Why, code_of
import pytest
from dataclasses import replace


def line(top, text, *, size=10.0, bold=False, x0=72.0, width=200.0):
    return Line(page=1, bbox=(x0, top, x0 + width, top + 10), text=text, size=size,
                bold=bold, italic=False, font="Helvetica")


def roles_on_a_page(*lines):
    """The roles analyze gives the lines of one page, artifacts left out."""
    page = PageContent(1, 612, 792, list(lines), [])
    return [(n.role, n.text) for n in analyze([page]) if n.role != Role.ARTIFACT]


def heading(blocks, index=0, body=10.0):
    return heading_decision(blocks[index], body, blocks)[0]


def test_a_bold_lead_in_split_mid_word_is_not_a_heading():
    # p17 p130: "Getting tax forms and publica" + "tions. Go to IRS.gov/Forms ..."
    lead = Block([line(100, "Getting tax forms and publica", bold=True)])
    rest = Block([line(112, "tions. Go to IRS.gov/Forms to view, download, or print.")])
    blocks = [lead, rest]
    assert heading(blocks) is None
    assert heading_decision(lead, 10.0, blocks)[1] == (
        "the next line goes on in lowercase: a run-in lead or a split sentence")


def test_a_bold_phrase_continued_by_regular_text_on_the_same_line_is_not_a_heading():
    lead = Block([line(100, "Main home was in the United", bold=True)])
    rest = Block([line(108, "States. If your main home was in the United States.")])
    blocks = [lead, rest]

    assert heading(blocks) is None


def test_a_short_bold_line_with_body_text_under_it_is_a_heading():
    # provider guide p2: "Types of audits" (11 pt, 15.8 tall) over a full-width paragraph whose
    # line box overlaps it by leading alone.
    title = Block([Line(page=1, bbox=(72.0, 233.5, 152.8, 249.3), text="Types of audits",
                        size=11.0, bold=True, italic=False, font="Helvetica")])
    body = Block([Line(page=1, bbox=(72.0, 246.3, 516.7, 261.3),
                       text="Under the terms of the provider's contract, AZ Blue may audit.",
                       size=11.0, bold=False, italic=False, font="Helvetica")])

    assert heading([title, body], body=11.0) == (11.0, True)


def test_a_bold_phrase_continued_right_after_it_on_the_same_line_is_not_a_heading():
    lead = Block([line(100, "Main home was in the United", bold=True, width=150)])
    rest = Block([line(100, "States. If your main home was", x0=72 + 150 + 3)])

    assert heading([lead, rest]) is None


def test_a_bold_heading_with_regular_text_in_another_column_stays_a_heading():
    title = Block([line(100, "Standard deduction amounts", bold=True, width=150)])
    other_column = Block([line(108, "States. If your main home was in the United States.",
                               x0=330)])

    assert heading([title, other_column]) == (10.0, True)


def test_a_bold_heading_with_regular_text_far_to_the_right_stays_a_heading():
    title = Block([line(100, "Standard deduction amounts", bold=True, width=150)])
    far = Block([line(108, "Regular text after a wide gap.", x0=72 + 150 + 40)])

    assert heading([title, far]) == (10.0, True)


def test_a_list_bullet_keeps_its_same_baseline_body_text():
    from collections import Counter

    from reflow.analyze.blocks import marker_of
    from reflow.analyze.lists import list_items

    item_line = line(100, "• No tax on car loan interest.", x0=100)
    item = Block([item_line], marker=marker_of(item_line.text))
    body = Block([line(108, "If you paid qualified loan interest.", x0=110)])
    blocks = list_items([item, body], Counter({(100, 10.0): 2}))

    assert len(blocks) == 1
    assert blocks[0].item
    assert "If you paid qualified loan interest." in blocks[0].text


def _bullet_and(body):
    from collections import Counter

    from reflow.analyze.blocks import marker_of
    from reflow.analyze.lists import list_items

    item_line = line(100, "• No tax on car loan interest.", x0=100, width=150)
    item = Block([item_line], marker=marker_of(item_line.text))
    return list_items([item, body], Counter({(100, 10.0): 2}))


def test_a_bullet_does_not_absorb_aligned_text_from_another_column():
    body = Block([line(108, "If you paid qualified loan interest.", x0=110, width=40)])
    far = Block([line(108, "If you paid qualified loan interest.", x0=310)])

    assert len(_bullet_and(far)) == 2
    assert len(_bullet_and(body)) == 1


def test_a_bullet_does_not_absorb_a_heading_on_its_baseline():
    heading_block = Block([line(108, "Another heading", x0=110, bold=True, width=100)])

    blocks = _bullet_and(heading_block)

    assert len(blocks) == 2


def test_a_heading_followed_by_a_sentence_is_still_a_heading():
    title = Block([line(100, "Getting Tax Forms", bold=True)])
    body = Block([line(112, "The IRS publishes forms and publications every year.")])
    assert heading([title, body]) == (10.0, True)


@pytest.mark.parametrize("text", [
    "Section 1 - Project Description",
    "SECTION 2 – Scope of Work",
    "Section 3 — Requirements",
    "Section 4: Evaluation",
])
def test_numbered_section_heading_is_not_emitted_as_the_document_title(text):
    body = [line(130 + 12 * i, "Proposals are scored on the criteria in this section.") for i in range(6)]

    assert roles_on_a_page(line(100, text, size=15.0), *body)[0] == (Role.H2, text)


def test_overlapping_small_brand_text_does_not_split_a_wrapped_display_title():
    title = Block([Line(1, (35.46, 104, 288.51, 151.44), "How accident", 39, True, False,
                        "Univers-Bold")])
    brand = Block([Line(1, (499.5, 111, 585.54, 142), "ACCIDENT INSURANCE", 14, True, False,
                        "Univers-Bold")])
    continuation = Block([Line(1, (35.46, 145, 548.24, 192.43),
                              "insurance can help enhance", 39, True, False, "Univers-Bold")])

    joined = join_wrapped_headings([title, brand, continuation], body=9)

    assert [block.text for block in joined] == [
        "How accident insurance can help enhance", "ACCIDENT INSURANCE"]


def test_oversized_wrapped_display_sentence_remains_a_heading():
    title = Block([line(100, "How accident", size=24, bold=True),
                   line(121, "insurance can help", size=24, bold=True),
                   line(142, "enhance your benefits", size=24, bold=True)])

    assert heading([title], body=10) == (24, True)


def test_a_block_ending_in_a_hyphenated_split_is_not_a_heading():
    lead = Block([line(100, "Standard deduc-", bold=True)])
    rest = Block([line(112, "tion amounts for 2025", bold=False)])
    assert heading([lead, rest]) is None


def test_a_bullet_line_is_never_a_heading():
    # p17 p94: "• Schedule 1, Additional Income ..." at 10.5 pt, italic
    bullet = Block([line(100, "• Schedule 1, Additional Income and Adjustments to Income;",
                         size=10.5)])
    assert heading([bullet]) is None


def test_index_entries_bold_and_smaller_than_body_are_not_headings():
    # p17 p138: "Passive activity:" and "Payment of estimated tax 43" at 7.5 pt in an index
    body = Block([line(100, "A paragraph of ordinary body text, at the usual size.")])
    colon = Block([line(120, "Passive activity:", size=7.5, bold=True)])
    refs = Block([line(140, "Payment of estimated tax 43", size=7.5, bold=True)])
    assert heading([body, colon, refs], index=1) is None
    assert heading([body, colon, refs], index=2) is None


def test_a_section_label_ending_in_a_colon_stays_a_heading():
    # budget p5: "Detailed Budget Estimates by Agency:" and "Other Materials:" are labels of a
    # table of contents; their entries have no page numbers in the text, so nothing indexes them.
    label = Block([line(100, "Detailed Budget Estimates by Agency:", size=10.0, bold=True)])
    entry = Block([line(116, "Explanation of Estimates ........", size=10.0, x0=99.0)])
    assert heading([label, entry]) == (10.0, True)


def test_a_colon_label_over_indented_sub_entries_with_page_numbers_is_not_a_heading():
    # budget p1285: "Nuclear Regulatory Commission:" over "Office of Inspector General ....496"
    label = Block([line(100, "Nuclear Regulatory Commission:", size=8.0, x0=30.0)])
    sub = Block([line(110, "Office of Inspector General ................................496",
                      size=8.0, x0=42.0)])
    assert heading([label, sub]) is None
    assert "indented sub-entries" in heading_decision(label, 6.5, [label, sub])[1]


def test_a_colon_label_on_an_index_page_is_not_a_heading():
    # Many lines of the page end in page references, so the page is an index.
    label = Block([line(100, "Surface Transportation Board:", size=8.0, x0=30.0)])
    lines = [Block([line(100 + 10 * k, f"Entry number {k} ........................{k}",
                         size=8.0, x0=30.0)]) for k in range(1, 8)]
    assert heading([label, *lines]) is None


def test_a_bold_colon_line_running_on_in_lowercase_is_not_a_heading():
    # A run-in lead: the sentence goes on in the next block, in lowercase.
    lead = Block([line(100, "Note that the rules apply to:", bold=True)])
    rest = Block([line(112, "employers and payers of income for each year.")])
    assert heading([lead, rest]) is None


def test_single_characters_and_symbols_are_never_headings():
    # naca p2 and p6 ("w", "u", "V") and p17's index letters ("C", "D")
    for text in ("w", "u", "V", "C", "•", "–"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) is None, text


def test_an_index_section_letter_set_larger_than_the_entries_is_a_heading():
    # p17 p138: the section letters Q and R are 9 pt bold over 8 pt index entries
    for text in ("Q", "R", "I"):
        letter = Block([line(100, text, size=9.0, bold=True)])
        assert heading([letter, *index_page()], body=8.0) == (9.0, True), text


def test_an_index_letter_no_larger_than_the_entries_is_not_a_heading():
    letter = Block([line(100, "Q", size=8.0, bold=True)])
    assert heading([letter, *index_page()], body=8.0) is None


def test_a_capital_letter_larger_than_body_text_off_an_index_is_not_a_heading():
    # Outside an index a lone capital is a stray character, however large it is set.
    letter = Block([line(100, "R", size=9.0, bold=True)])
    body = [Block([line(120 + 14 * k, "A paragraph of ordinary body text, at the usual size.", size=8.0)])
            for k in range(3)]
    assert heading([letter, *body], body=8.0) is None


def test_a_lone_roman_numeral_is_never_a_heading():
    # budget-appendix p5: the folio "i" became an h4
    for text in ("i", "ii", "iv.", "IX"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) is None, text


def test_a_number_alone_is_never_a_heading():
    # nchs-bar-chart: the "15" of the "15 YEARS" logo; p17: form numbers ("501") and chapter
    # numbers set apart from their titles ("10."); budget-appendix: folios ("13")
    for text in ("15", "501", "10.", "2026", "1 1", "75 80", "--- 20"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) is None, text


def test_a_title_with_a_number_in_it_is_still_a_heading():
    for text in ("2025 Tax Changes", "10. Standard Deduction", "Form 1040"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) == (14.0, True), text


def test_roman_letters_inside_words_are_still_headings():
    # Words that happen to be spelled with roman letters are not numerals.
    for text in ("Civil Procedure", "Mild", "Dim"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) == (14.0, True), text


def test_a_style_gets_the_same_level_in_a_small_and_a_large_directory():
    # pima (12 plan-name headings) and maricopa (96) have the same styles in the same order.
    # Their provider names must get the same level, whatever the size of the directory.
    from collections import Counter

    from reflow.analyze.headings import heading_levels
    pima = Counter({(18.0, True): 12, (14.0, True): 250, (12.0, True): 313,
                    (11.0, False): 4827})
    maricopa = Counter({(18.0, True): 96, (14.0, True): 689, (12.0, True): 1451,
                        (11.0, False): 25531})
    small, large = heading_levels(pima), heading_levels(maricopa)
    assert small[(11.0, False)] == large[(11.0, False)]
    assert small[(14.0, True)] == large[(14.0, True)]
    assert small[(12.0, True)] == large[(12.0, True)]


def test_a_capitalised_section_title_slightly_below_body_size_is_a_heading():
    # naca p4: "TECHNICAL COMMITTEES" is set at 9.8 pt under 10.3 pt body text
    title = Block([line(100, "TECHNICAL COMMITTEES", size=9.8, bold=True)])
    body = Block([line(120, "A paragraph of ordinary body text, at the usual size.", size=10.3)])
    assert heading([title, body], body=10.3) == (9.8, True)


def test_a_mixed_case_bold_entry_below_body_size_is_not_a_heading():
    # p17 p138: an index entry set in bold at 7.5 pt under 8 pt body text
    entry = Block([line(100, "Privacy Act and Paperwork", size=7.5, bold=True)])
    body = Block([line(120, "A paragraph of ordinary body text, at the usual size.", size=8.0)])
    assert heading([entry, body], body=8.0) is None


def index_page(top=300.0):
    """Five entries that end in page numbers: the page around a wrapped entry is an index."""
    return [Block([line(top + 10 * k, f"Index entry number {k} {10 + k}", size=8.0, width=120)])
            for k in range(5)]


def joined(blocks, body=8.0, ruled=()):
    from reflow.analyze.headings import join_wrapped_headings
    return join_wrapped_headings(blocks, body, ruled)


def test_a_name_wrapped_onto_a_second_line_in_the_same_style_is_one_heading():
    # pima p445: "Felix Tacoronte, Carmen L, MD" over "(F)", both bold
    name = Block([line(100, "Felix Tacoronte, Carmen L, MD", size=10.0, bold=True, width=120)])
    rest = Block([line(112, "(F)", size=10.0, bold=True, width=20)])
    blocks = joined([name, rest], body=8.0)
    assert len(blocks) == 1
    assert blocks[0].text == "Felix Tacoronte, Carmen L, MD (F)"
    assert heading(blocks, body=8.0) == (10.0, True)


def test_a_run_in_lead_that_runs_on_in_another_style_is_not_a_heading():
    # p17 p15: a bold lead "Adoption taxpayer identification number" over regular "(ATIN). If you..."
    lead = Block([line(100, "Adoption taxpayer identification number", size=8.0, bold=True)])
    rest = Block([line(110, "(ATIN). If you are in the process of adopting a child.", size=8.0)])
    blocks = joined([lead, rest], body=8.0)
    assert len(blocks) == 2
    assert heading(blocks, body=8.0) is None
    assert "bracket" in heading_decision(blocks[0], 8.0, blocks)[1]


def test_a_title_that_reads_as_a_sentence_is_not_a_heading():
    # p17 p130: "Employers can register to use" (bold) over "Business Services Online. The ..."
    lead = Block([line(100, "Employers can register to use", size=8.0, bold=True)])
    rest = Block([line(110, "Business Services Online. The IRS offers an online service.", size=8.0)])
    blocks = joined([lead, rest], body=8.0)
    assert heading(blocks, body=8.0) is None


def test_a_wrapped_index_entry_and_its_page_number_are_one_entry_not_a_heading():
    # budget p1292: "Telecommunications Relay Services Fund, Federal Communications" / "Commission 1163"
    head = Block([line(100, "Telecommunications Relay Services Fund, Federal Communications",
                       size=8.0, bold=True, width=160)])
    rest = Block([line(110, "Commission 1163", size=8.0, width=60)])
    blocks = joined([head, rest, *index_page()], body=8.0)
    assert blocks[0].entry and blocks[0].text.endswith("Commission 1163")
    assert len(blocks) == 6
    assert heading(blocks, body=8.0) is None


def test_a_section_heading_over_a_short_line_with_a_number_is_not_an_index_entry():
    # Outside an index, "Line 1" under a bold heading is a second heading, not a page number.
    head = Block([line(100, "Specific Instructions", size=10.0, bold=True, width=120)])
    rest = Block([line(112, "Line 1", size=10.0, bold=True, width=30)])
    blocks = joined([head, rest], body=8.0)
    assert len(blocks) == 2


def test_a_wrapped_index_entry_is_one_block_even_when_its_title_is_a_heading_style():
    # budget p1285: "North Atlantic Treaty Organization Security Investment" / "Program 274"
    head = Block([line(100, "North Atlantic Treaty Organization Security Investment", size=8.0,
                       bold=True, width=190)])
    rest = Block([line(110, "Program 274", size=8.0, width=50)])
    blocks = joined([head, rest, *index_page()], body=8.0)
    assert len(blocks) == 6
    assert blocks[0].text == "North Atlantic Treaty Organization Security Investment Program 274"


def test_a_short_label_inside_a_ruled_form_area_is_not_a_heading():
    # fw9 p1: "Sign" and "Here" in the signature box, "or" between two boxes
    box = (35.0, 575.2, 577.0, 600.9)
    sign = Block([line(576.6, "Sign", size=10.0, bold=True, x0=36.0, width=21.0)])
    here = Block([line(588.0, "Here", size=10.0, bold=True, x0=36.0, width=23.0)])
    signature = Block([line(577.0, "Signature of", size=7.0, x0=70.0, width=50.0),
                       line(584.0, "U.S. person  Date", size=7.0, x0=70.0, width=80.0)])
    blocks = [sign, here, signature]
    assert heading([sign]) == (10.0, True)
    for label in (sign, here):  # they share their column, so neither is a banner
        assert heading_decision(label, 8.0, blocks, [box])[0] is None
    assert "ruled form area" in heading_decision(sign, 8.0, blocks, [box])[1]


def test_a_county_banner_alone_in_its_column_of_a_shared_box_is_a_heading():
    # directory p1: "Adams" (14 pt bold, body 11 pt) in the page-top drawing that also holds
    # the running title, the date and a provider of the right column
    box = (21.6, 21.6, 590.34, 90.54)
    title = Block([line(30, "CICP Provider Directory", size=11.0, bold=True, x0=380.0, width=208.0)])
    date = Block([line(44, "Revised: January 11, 2024", size=9.0, x0=411.0, width=152.0)])
    pavilion = Block([line(60, "Children's Colorado Health Pavilion", size=12.0, bold=True,
                           x0=314.0, width=212.0)])
    adams = Block([line(66, "Adams", size=14.0, bold=True, x0=140.0, width=48.0)])
    blocks = [title, date, pavilion, adams]
    assert heading_decision(adams, 11.0, blocks, [box])[0] == (14.0, True)


def test_a_county_banner_at_a_column_foot_is_a_heading():
    # directory p1: "Arapahoe" in a banner box that holds nothing else, with no block below it
    box = (30.0, 700.0, 300.0, 750.0)
    arapahoe = Block([line(715, "Arapahoe", size=14.0, bold=True, x0=100.0, width=70.0)])
    assert heading_decision(arapahoe, 11.0, [arapahoe], [box])[0] == (14.0, True)


def test_an_ocr_label_alone_in_its_column_stays_a_form_label():
    box = (30.0, 700.0, 300.0, 750.0)
    ocr = Line(page=1, bbox=(100.0, 715.0, 170.0, 725.0), text="Arapahoe", size=14.0, bold=True,
               italic=False, font="Helvetica", ocr=True)
    label = Block([ocr])
    assert heading_decision(label, 11.0, [label], [box])[0] is None


def test_a_plain_label_at_body_size_alone_in_its_column_stays_a_form_label():
    box = (30.0, 700.0, 300.0, 750.0)
    date = Block([line(715, "Date", size=10.0, x0=100.0, width=30.0)])
    assert heading_decision(date, 10.0, [date], [box])[0] is None


def test_a_lone_column_header_beside_a_larger_heading_is_not_a_heading():
    # budget p5: "Page" set at the right of "TABLE OF CONTENTS"
    title = Block([line(100, "TABLE OF CONTENTS", size=13.0, bold=True, x0=200.0, width=160.0)])
    page = Block([line(102, "Page", size=10.0, x0=500.0, width=30.0)])
    assert heading([title, page], body=8.0, index=1) is None
    assert heading([title], body=8.0) == (13.0, True)


def test_a_bold_kicker_in_the_corner_beside_a_much_larger_title_is_not_a_heading():
    # az-accident-insurance-flyer p1: "EMPLOYER" at the right edge, on the rows of the headline's
    # first line, which is 2.8 times its size; the headline's box reaches under it.
    def at(text, box, size):
        return Line(page=1, bbox=box, text=text, size=size, bold=True, italic=False,
                    font="Helvetica")

    kicker = Block([at("EMPLOYER", (503.2, 96.9, 581.9, 114.0), 14.0)])
    title = Block([at("How accident", (35.5, 103.9, 288.5, 151.4), 39.0),
                   at("insurance can help enhance", (35.5, 144.9, 548.2, 192.4), 39.0)])
    assert heading([title, kicker], body=10.0, index=1) is None
    assert heading([title, kicker], body=10.0) == (39.0, True)


def test_a_short_bold_heading_beside_a_title_under_twice_its_size_is_still_a_heading():
    side = Block([line(97, "Eligibility", size=14.0, bold=True, x0=503.0, width=79.0)])
    title = Block([line(97, "How accident insurance helps", size=20.0, bold=True, x0=35.0,
                        width=300.0)])
    assert heading([title, side], body=10.0, index=1) == (14.0, True)


def test_a_two_word_heading_away_from_any_form_or_title_is_still_a_heading():
    body = Block([line(100, "Who Must File", size=12.0, bold=True)])
    assert heading([body], body=8.0) == (12.0, True)


def test_a_name_followed_by_a_phone_number_is_still_a_heading():
    # pima p542: "Sound Medical Supply" over "(253) 274-5000": a number is not a continuation
    name = Block([line(100, "Sound Medical Supply", size=11.0, bold=True)])
    phone = Block([line(112, "(253) 274-5000", size=11.0)])
    blocks = joined([name, phone], body=11.0)
    assert len(blocks) == 2
    assert heading(blocks, body=11.0) == (11.0, True)


def test_a_section_label_above_a_heading_in_a_ruled_box_is_a_heading():
    # pima p44: "GENERAL PRACTICE" in a ruled box, with the city heading "Marana" under it
    box = (30.0, 90.0, 300.0, 130.0)
    label = Block([line(100, "GENERAL PRACTICE", size=11.0, bold=True, width=120)])
    city = Block([line(118, "Marana", size=11.0, bold=True, width=60)])
    assert heading_decision(label, 8.0, [label, city], [box])[0] == (11.0, True)


def test_a_section_title_with_a_sentence_under_it_in_a_ruled_box_stays_a_heading():
    # fw9 p43: "Start Here" over a question in a shaded box
    box = (30.0, 90.0, 300.0, 160.0)
    label = Block([line(100, "Start Here", size=8.1, bold=True, width=50)])
    question = Block([line(110, "Will you owe $1,000 or more for 2026 after withholding?",
                           size=8.0, width=200)])
    assert heading_decision(label, 8.0, [label, question], [box])[0] == (8.1, True)


def test_a_caption_line_is_never_a_heading_even_at_heading_size():
    # census p60: "Table A-1. Summary" set bold at 14 pt; epa and nasa: "Table ES-1:" and
    # "Figure D-1." at heading size became h4-h6.
    for text in ("Figure 3. Population by region", "Table A-1. Summary of results",
                 "Table ES-1: Emissions by sector", "Figure D-1. Site map",
                 "Fig. 2: Trends", "Table D-1 Requirements Verification Matrix",
                 "Figure 4-A. Do You Have To Pay Estimated Tax?"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) is None, text
        assert "caption" in heading_decision(block, 10.0, [block])[1], text


def test_a_title_that_starts_with_a_figure_or_table_word_is_still_a_heading():
    for text in ("Figures of Merit", "Table of Contents", "Figure out your taxes"):
        block = Block([line(100, text, size=14.0, bold=True)])
        assert heading([block]) == (14.0, True), text


def test_a_short_documents_lone_display_line_does_not_push_its_sections_down():
    # irs-w4-2026: "W-4" is one 24 pt line, and its sections are 14 pt. The sections are the
    # document's top level under the title (h2); the 12 pt and 10 pt sub-parts go below them.
    from collections import Counter

    from reflow.analyze.headings import heading_levels
    w4 = Counter({(24.0, True): 1, (14.0, True): 3, (12.0, True): 2, (10.0, True): 2})
    levels = heading_levels(w4)
    assert levels[(14.0, True)] == 2
    assert levels[(24.0, True)] == 2
    assert levels[(12.0, True)] == 3
    assert levels[(10.0, True)] == 4


def test_a_bold_step_label_wrapped_in_a_narrow_left_column_is_a_heading():
    # irs-w4-2026 p1: "Step 1: Enter Personal Information" is set in a 54 pt left column, so it
    # wraps to four lines, with the form's instructions beside it at x 122. A form section label.
    label = Block([line(100, "Step 1:", size=10.0, bold=True, x0=36.0, width=54.0),
                   line(111.5, "Enter", size=10.0, bold=True, x0=36.0, width=54.0),
                   line(123, "Personal", size=10.0, bold=True, x0=36.0, width=54.0),
                   line(134.5, "Information", size=10.0, bold=True, x0=36.0, width=54.0)])
    instructions = Block([line(100, "(a) First name and middle initial", size=7.0, x0=122.0,
                               width=200.0)])
    assert heading([label, instructions]) == (10.0, True)


def test_a_bold_step_run_in_that_runs_on_in_regular_type_is_not_a_heading():
    # A run-in sentence in prose: the label is bold, but its text goes on in regular type.
    lead = Block([line(100, "Step 1: Enter Personal Information", size=10.0, bold=True,
                       x0=36.0, width=54.0),
                  line(111.5, "and then the rest of the instruction goes on here.", size=10.0,
                       x0=36.0, width=54.0),
                  line(123, "more words", size=10.0, x0=36.0, width=54.0),
                  line(134.5, "and more words", size=10.0, x0=36.0, width=54.0)])
    beside = Block([line(100, "A paragraph beside the run-in, at the usual size.", x0=122.0)])
    assert heading([lead, beside]) is None


def test_a_centred_title_whose_second_line_starts_with_a_stray_dash_is_one_heading():
    # naca p1: the text layer puts "- " before the second line of a centred title; the page has none
    first = Block([line(100, "EFFECT OF CENTRIFUGAL FORCE ON THE ELASTIC", size=15.6, x0=124.45, width=399.4)])
    second = Block([line(108, "- CURVE OF A VIBRATING CANTILEVER BEAM", size=15.6, x0=148.14, width=352.1)])
    blocks = joined([first, second], body=10.3)
    assert len(blocks) == 1
    assert blocks[0].text == "EFFECT OF CENTRIFUGAL FORCE ON THE ELASTIC CURVE OF A VIBRATING CANTILEVER BEAM"
    assert heading(blocks, body=10.3) == (15.6, False)


def test_a_dash_line_under_a_heading_that_is_not_centred_is_not_joined():
    # a list item under a heading, set at the left margin: not the second line of the title
    title = Block([line(100, "EFFECT OF CENTRIFUGAL FORCE ON THE ELASTIC", size=15.6, x0=124.45, width=399.4)])
    item = Block([line(108, "- curve of a vibrating beam", size=15.6, x0=72.0, width=200.0)])
    assert len(joined([title, item], body=10.3)) == 2


def test_a_centred_line_of_capitals_at_body_size_is_a_section_title():
    # naca p2: "AERONAUTIC SYMBOLS" is set at 10.7 pt, centred over the page's text
    title = Block([line(100, "AERONAUTIC SYMBOLS", size=10.7, x0=236.0, width=128.0)])
    text = Block([line(120, "A line of ordinary text that spans the whole measure.", x0=72.0,
                       width=456.0)])
    assert heading([title, text]) == (10.7, False)


def test_a_capitals_line_at_the_margin_or_in_a_ruled_box_is_not_a_section_title():
    left = Block([line(100, "NOTE ON USE", size=10.7, x0=72.0, width=128.0)])
    text = Block([line(120, "A line of ordinary text that spans the whole measure.", x0=72.0,
                       width=456.0)])
    assert heading([left, text]) is None
    centred = Block([line(100, "AERONAUTIC SYMBOLS", size=10.7, x0=236.0, width=128.0)])
    box = (230.0, 95.0, 370.0, 115.0)
    assert heading_decision(centred, 10.3, [centred, text], [box])[0] is None
def test_a_callout_label_is_a_note_not_a_heading():
    # p17 p25 and p94: "TIP" set at 9.2 pt, 1.15x body text, over the paragraph it labels
    tip = Block([line(100, "TIP", size=9.2, bold=True)])
    text = Block([line(112, "Use the worksheet to check your figures before you file.", size=8.0)])
    assert heading([tip, text], body=8.0) is None
    assert roles_on_a_page(line(100, "TIP", size=9.2, bold=True),
                           line(112, "Use the worksheet to check your figures before you file.", size=8.0))[0] \
        == (Role.NOTE, "TIP")


def test_a_callout_label_with_a_colon_is_a_note():
    nodes = roles_on_a_page(line(100, "CAUTION:", size=9.2, bold=True),
                            line(112, "Keep a copy of every form you send to the IRS.", size=8.0))
    assert nodes[0] == (Role.NOTE, "CAUTION:")


def test_a_title_case_word_is_not_a_callout_label():
    # "Tip" as the first word of a heading stays a heading: only the all-capitals label is a callout
    tip = Block([line(100, "Tip Calculator Worksheets", size=9.2, bold=True)])
    text = Block([line(112, "Use the worksheet to check your figures before you file.", size=8.0)])
    assert heading([tip, text], body=8.0) == (9.2, True)


@pytest.mark.parametrize("text", [
    "LINE 11", "LINE 1a LINE 2", "LINE33", "J. LI N E 24a", "LINE 25 LINE 28 LINE 33",
    "Insured\u2019s name (LINE 4)", "ID number (LINE 1a)"])
def test_the_label_of_a_line_of_a_form_is_text_not_a_heading(text):
    # az-claim-form-instruction-booklet p3 and p4: the red "LINE 11" labels beside the
    # picture of a claim form and in the list under it, bold and set large, as OCR reads them
    label = Block([line(100, text, size=14.0, bold=True)])
    body = Block([line(130, "Your diagnosis, found on the statement from your provider.", size=10.0)])
    assert heading([label, body]) is None
    assert "label of a line of a form" in heading_decision(label, 10.0, [label, body])[1]


@pytest.mark.parametrize("text", [
    "Line 7 Standard Deduction", "LINEN CARE", "LINE OF CREDIT", "Your Provider and Diagnosis Information"])
def test_a_heading_that_only_starts_like_a_line_label_stays_a_heading(text):
    # the label is LINE and a number, in capitals: "Line 7" names a section of a tax guide
    head = Block([line(100, text, size=14.0, bold=True)])
    body = Block([line(130, "Your diagnosis, found on the statement from your provider.", size=10.0)])
    assert heading([head, body]) == (14.0, True)


def test_a_bold_label_over_a_column_of_numbers_is_not_a_heading():
    # p17 p94 Useful Items: "Publication" in bold at body size, over the publication number "501"
    label = Block([line(100, "Publication", size=8.0, bold=True, width=43)])
    number = Block([line(112, "501", size=8.0, width=14)])
    assert heading([label, number], body=8.0) is None


def test_a_bold_label_after_a_column_of_numbers_is_not_a_heading():
    # p17 p94 Useful Items: "Schedule A (Form 1040)" after the last number of its column ("970")
    number = Block([line(100, "970", size=8.0, width=14, x0=419.0)])
    label = Block([line(123, "Schedule A (Form 1040)", size=8.0, bold=True, x0=419.0, width=92)])
    assert heading([label, number], body=8.0) is None


def test_a_bold_label_with_no_number_beside_it_is_still_a_heading():
    label = Block([line(100, "Getting Tax Forms", size=8.0, bold=True)])
    body = Block([line(112, "The IRS publishes forms and publications every year.", size=8.0)])
    assert heading([label, body], body=8.0) == (8.0, True)


def test_a_chapter_numeral_set_above_its_title_is_one_heading():
    # p17 p94: "10." set alone in display type, over "Standard Deduction" at the same left edge
    numeral = Block([line(100, "10.", size=18.0, bold=True, x0=42.0, width=20)])
    title = Block([line(123, "Standard Deduction", size=18.0, bold=True, x0=42.0, width=150)])
    blocks = joined([numeral, title], body=8.0)
    assert len(blocks) == 1
    assert blocks[0].text == "10. Standard Deduction"
    assert heading(blocks, body=8.0) == (18.0, True)


def test_a_list_number_hanging_left_of_its_text_is_not_joined_to_it():
    number = Block([line(100, "1.", size=18.0, bold=True, x0=72.0, width=12)])
    item = Block([line(123, "Who should itemize", size=18.0, bold=True, x0=90.0, width=150)])
    assert len(joined([number, item], body=8.0)) == 2


def test_a_chapter_numeral_with_body_text_under_it_is_not_joined():
    numeral = Block([line(100, "10.", size=18.0, bold=True, x0=42.0, width=20)])
    text = Block([line(123, "This chapter discusses the standard deduction.", size=8.0, x0=42.0)])
    assert len(joined([numeral, text], body=8.0)) == 2


# --- stacked titles, bold sentences, index letters ---------------------------------------------------------------------

def test_a_part_title_stacked_in_a_narrow_column_is_a_heading():
    # p17 p94: "Standard / Deduction, / Itemized / Deductions, and / Other / Deductions" at 17.5 pt
    words = ["Standard", "Deduction,", "Itemized", "Deductions, and", "Other", "Deductions"]
    title = Block([line(100 + 24 * i, w, size=17.5, width=120.0) for i, w in enumerate(words)])
    assert len(title.lines) > 3
    assert heading([title], body=8.0) == (17.5, False)


def test_a_long_run_of_large_prose_lines_is_not_a_stacked_title():
    prose = Block([line(100 + 24 * i, "a sentence of twelve words set in large type for a pull",
                        size=17.5) for i in range(5)])
    assert heading([prose], body=8.0) is None


def test_a_bold_sentence_alone_over_a_regular_paragraph_is_a_heading():
    # p17 p130: "Ways to check on the status of your refund." over a regular-weight list
    sub = Block([line(100, "Ways to check on the status of", size=8.0, bold=True, width=124.0),
                 line(110, "your refund.", size=8.0, bold=True, width=60.0)])
    under = Block([line(124, "Go to IRS.gov/Refunds.", size=8.0, width=200.0)])
    assert heading([sub, under], body=8.0) == (8.0, True)


def test_a_bold_sentence_over_bold_text_or_filling_its_line_is_not_a_heading():
    sub = Block([line(100, "Ways to check on the status of your refund.", size=8.0, bold=True)])
    also_bold = Block([line(112, "Go to IRS.gov/Refunds.", size=8.0, bold=True)])
    assert heading([sub, also_bold], body=8.0) is None
    two = Block([line(100, "This is the first sentence. This is the second one.", size=8.0, bold=True)])
    under = Block([line(112, "Body text follows here.", size=8.0)])
    assert heading([two, under], body=8.0) is None
    last = Block([line(100, "A bold sentence that is the last block.", size=8.0, bold=True)])
    assert heading([last], body=8.0) is None


def test_a_numbered_title_in_capitals_a_size_under_the_body_is_a_heading():
    # naca p2: "1. FUNDAMENTAL AND DERIVED UNITS" at 8.8 pt in a document whose body is 10.3 pt
    title = Block([line(100, "1. FUNDAMENTAL AND DERIVED UNITS", size=8.8, x0=248.0, width=185.0)])
    wide = Block([line(60, "AERONAUTIC SYMBOLS", size=10.7, x0=276.0, width=128.0)])
    side = Block([line(300, "Weight=mg", size=8.8, x0=150.0, width=380.0)])
    assert heading([title, wide, side], body=10.3) == (8.8, False)
    plain = Block([line(100, "FUNDAMENTAL AND DERIVED UNITS", size=8.8, x0=248.0, width=185.0)])
    assert heading([plain, wide, side], body=10.3) is None


def test_index_letters_take_the_level_below_the_index_heading_capped_at_3():
    from reflow.analyze.headings import fix_heading_levels
    from reflow.model import Node, Role
    # the index letters share the entries' style (h5) in the document-wide ranking
    nodes = [Node(Role.H2, page=1, text="Rights"), Node(Role.H3, page=1, text="Help"),
             Node(Role.H5, page=1, text="Index"),
             Node(Role.H5, page=2, text="B", attrs={"index_letter": True}),
             Node(Role.H5, page=2, text="C", attrs={"index_letter": True})]
    fix_heading_levels(nodes)
    assert [n.role for n in nodes[3:]] == [Role.H3, Role.H3]


def test_a_single_letter_heading_without_an_index_marker_keeps_the_stack_level():
    from reflow.analyze.headings import fix_heading_levels
    from reflow.model import Node, Role
    nodes = [Node(Role.H2, page=1, text="Index"), Node(Role.H4, page=1, text="B")]
    fix_heading_levels(nodes)
    assert nodes[1].role == Role.H3


def _outline(items):
    """Run fix_heading_levels over (natural level, page, text, label_caps) and return the levels."""
    from reflow.model import HEADING_BY_LEVEL
    from reflow.analyze.headings import fix_heading_levels
    from reflow.model import Node
    nodes = [Node(HEADING_BY_LEVEL[level - 1], page=page, text=text, attrs={"label_caps": True} if caps else {})
             for level, page, text, caps in items]
    fix_heading_levels(nodes)
    return [HEADING_BY_LEVEL.index(n.role) + 1 for n in nodes]


def test_a_bold_caps_label_under_a_numbered_section_ranks_one_level_below_it():
    assert _outline([(2, 1, "3. PROPERTY IDENTIFICATION INFORMATION", False),
                     (2, 1, "ENTER APPLICABLE NUMBER FROM YOUR NOTICE", True),
                     (2, 1, "PROPERTY TYPE", True),
                     (2, 1, "4. VALUE", False)]) == [2, 3, 3, 2]


def test_a_numbered_heading_is_never_demoted():
    assert _outline([(2, 1, "3. PROPERTY", False), (2, 1, "4. VALUE ADDED", True)]) == [2, 2]


def test_a_label_on_another_page_or_after_a_plain_heading_keeps_its_level():
    assert _outline([(2, 1, "3. PROPERTY", False), (2, 2, "PROPERTY TYPE", True)]) == [2, 2]
    assert _outline([(2, 1, "3. PROPERTY", False), (2, 1, "Certification", False),
                     (2, 1, "SIGNATURE OF APPLICANT", True)]) == [2, 2, 2]


def test_a_single_word_label_is_a_section_of_its_own():
    assert _outline([(2, 2, "8. REFUND", False), (2, 2, "CERTIFICATION", True)]) == [2, 2]


def test_a_section_n_heading_does_not_open_a_numbered_section():
    assert _outline([(2, 3, "SECTION 2. CONTACT INFORMATION", False),
                     (2, 3, "AUTHORIZATION OF AGENT", True)]) == [2, 2]


def test_a_label_only_drops_when_it_shares_the_sections_level():
    assert _outline([(3, 1, "3. PROPERTY", False), (2, 1, "PROPERTY TYPE", True)]) == [2, 2]


def test_a_demoted_label_never_goes_below_h6():
    assert _outline([(2, 1, "Part", False), (3, 1, "Chapter", False), (4, 1, "Topic", False),
                     (5, 1, "Item", False), (6, 1, "3. DEEP", False),
                     (6, 1, "DEEP LABEL", True)]) == [2, 3, 4, 5, 6, 6]


# A scan's body line read at a larger size (da-legislation p15: a 21.6 pt box among 11 pt lines,
# too tall for rhythm.snap_ocr_sizes). Body text 10 pt, x 72 to 540; the candidate is 13.8 pt.
LONG_LINE = "The party filing the motion is entitled to recover costs and fees"


def _page(candidate, *, ocr=True):
    def body(top, text):
        return Block([Line(page=15, bbox=(72.0, top, 540.0, top + 10), text=text, size=10.0,
                           bold=False, italic=False, font="GlyphLessFont", ocr=ocr)])
    return [body(80, "Section 4. Section 30.020, Civil Practice and Remedies Code, is amended"),
            body(120, "Under the rules of the court the clerk keeps the record of each motion."),
            candidate,
            body(200, "Section 5. The changes in law made by this Act apply to an action filed.")]


def _candidate(text, *, ocr=True, size=13.8, bold=False, x1=538.0, top=150.0, lines=1):
    return Block([Line(page=15, bbox=(72.0, top + i * 1.5 * size, x1, top + i * 1.5 * size + size),
                       text=text, size=size, bold=bold, italic=False, font="GlyphLessFont",
                       ocr=ocr) for i in range(lines)])


def test_a_long_ocr_line_that_fills_its_column_is_not_a_heading():
    cand = _candidate(LONG_LINE)
    blocks = _page(cand)

    style, why = heading_decision(cand, 10.0, blocks)
    assert style is None
    assert why == "OCR body text: eight or more words that run to the column's edge"


def test_the_same_line_from_the_text_layer_is_still_a_heading():
    cand = _candidate(LONG_LINE, ocr=False)

    assert heading(_page(cand, ocr=False), 2) == (13.8, False)


def test_a_born_digital_bold_title_of_ten_words_at_the_edge_stays_a_heading():
    cand = _candidate("Annual Report of the Board of Directors to the Members", ocr=False,
                      bold=True)

    assert heading(_page(cand, ocr=False), 2) == (13.8, True)


def test_an_ocr_display_title_stays_a_heading():
    # Bold, two lines, twice the body size: the display-title exemption overrides the veto.
    cand = _candidate("Conference Committee Report on the House Bill and Senate", size=20.0,
                      bold=True, lines=2)

    assert heading(_page(cand), 2) == (20.0, True)


def test_a_short_ocr_heading_short_of_the_edge_stays_a_heading():
    cand = _candidate("Conference Committee Report", x1=300.0)

    assert heading(_page(cand), 2) == (13.8, False)


def test_a_long_ocr_title_in_capitals_stays_a_heading():
    # naca p1: "EFFECT OF CENTRIFUGAL FORCE ON THE ELASTIC" / "CURVE OF A VIBRATING CANTILEVER
    # BEAM", centred, reaches the column's edge within four of its sizes.
    cand = _candidate("EFFECT OF CENTRIFUGAL FORCE ON THE ELASTIC CURVE OF A VIBRATING BEAM")

    assert heading(_page(cand), 2) == (13.8, False)


def scanned(top, text, stroke, **kw):
    return Line(page=1, bbox=(72.0, top, 400.0, top + 10), text=text, size=10.0, bold=False,
                italic=False, font="GlyphLessFont", ocr=True, stroke=stroke, **kw)


PARAGRAPH = [scanned(130 + i * 12, text, 1.0) for i, text in enumerate([
    "Management is unaware of any conditions that could significantly",
    "impact the financial position of the District or any other factor"])]


def test_a_heavy_one_line_ocr_block_is_a_heading_from_the_page_image():
    from reflow.analyze.headings import ink_cue
    head = Block([scanned(100, "Conditions Affecting Current Financial Position", 2.12)])
    blocks = [head, Block(PARAGRAPH)]
    style, why = heading_decision(head, 10.0, blocks)
    assert style == (10.0, True)
    assert why == "strokes 2.12× as heavy as the page's text on the page image"
    assert ink_cue(head, 10.0).stroke == 2.12
    assert ink_cue(blocks[1], 10.0) is None   # two lines: a paragraph


@pytest.mark.parametrize("text,stroke", [
    ("conditions affecting current financial position", 2.1),   # fails ocr_gate: lowercase
    ("Management is unaware of any such conditions.", 2.1),     # a sentence
    ("By signing the filled-out form, you:", 2.1),               # a lead-in to a list
    ("Request Info", 2.1),                                        # under 12 letters
    ("Conditions Affecting Current Financial Position", 1.4),    # regular weight
])
def test_a_heavy_ocr_line_that_reads_as_text_or_is_light_is_not_a_heading(text, stroke):
    head = Block([scanned(100, text, stroke)])
    assert heading([head, Block(PARAGRAPH)]) is None


def test_a_heavy_ocr_line_with_a_doubtful_word_is_not_a_heading():
    from reflow.extract.ocrwords import GradedWord
    doubt = GradedWord((72, 100, 140, 110), "Condltions", "Condltions", 40, "doubt", None, 1.0)
    head = Block([scanned(100, "Condltions Affecting Current Financial Position", 2.1,
                          grades=(doubt,))])
    assert heading([head, Block(PARAGRAPH)]) is None


def test_a_heavy_line_its_text_refuses_keeps_its_ink_cue_for_review_and_its_rank():
    # naca's bold head "APPARATU A D PROCEDUR E", which a sure reading contradicts, may not
    # be navigation, but the reviewer must still be told it looks like a heading (refused_ink).
    # A size heading the gate refuses still ranks its style: the levels follow the type, not how
    # well OCR read it.
    from reflow.analyze.headings import ranked_style, refused_ink
    from reflow.extract.ocrwords import GradedWord
    doubt = GradedWord((72, 100, 140, 110), "APPARATU", "APPARATU", 79, "doubt", None, 1.0,
                       contradicted=True)
    head = Block([scanned(100, "APPARATU A D PROCEDUR E", 2.1, grades=(doubt,))])
    assert heading([head, Block(PARAGRAPH)]) is None
    assert refused_ink(head, 10.0).stroke == 2.1
    clean = Block([scanned(100, "Conditions Affecting Current Financial Position", 2.12)])
    assert refused_ink(clean, 10.0) is None          # its cue makes it a heading instead
    big = Block([scanned(60, "REPORT O. 914- N\"'-TIONAL", 1.0, grades=(doubt,))])
    big.lines[0].size = 16.0
    blocks = [big, Block(PARAGRAPH)]
    assert heading_decision(big, 10.0, blocks)[0] is None
    assert ranked_style(big, 10.0, blocks) == (16.0, False)
    # Tesseract's own doubt is still a reason a style may not rank (a fragment, a remnant).
    unsure = GradedWord((72, 60, 140, 76), "rker", "rker", 50, "doubt", None, 1.0)
    fragment = Block([scanned(60, "rker", 1.0, grades=(unsure,))])
    fragment.lines[0].size = 16.0
    assert ranked_style(fragment, 10.0, [fragment, Block(PARAGRAPH)]) is None


def test_a_born_digital_line_is_judged_as_before():
    head = Block([line(100, "Conditions Affecting Current Financial Position")])
    assert head.first.stroke is None
    assert heading([head, Block([line(112, "Management is unaware of any conditions.")])]) is None


def underlined_line(words, *, width=None, top=100.0, stroke=1.0):
    """An OCR line of graded words (text, underlined), 6 points a letter and one 4-point space
    apart, its box as wide as its words unless `width` says otherwise."""
    from reflow.extract.ocrwords import GradedWord
    graded, x = [], 72.0
    for text, under in words:
        graded.append(GradedWord((x, top, x + 6 * len(text), top + 10), text, text, 90, "read",
                                 None, 1.0, underlined=under))
        x += 6 * len(text) + 4
    text = " ".join(t for t, _ in words)
    right = 72.0 + width if width is not None else x - 4
    return Line(page=1, bbox=(72.0, top, right, top + 10), text=text, size=10.0, bold=False,
                italic=False, font="GlyphLessFont", ocr=True, stroke=stroke, grades=tuple(graded))


CHALLENGES = ["Challenges", "in", "front", "of", "us"]


@pytest.mark.parametrize("words,width,expected", [
    ([(w, True) for w in CHALLENGES], None, True),                       # every word underlined
    ([(w, w != "front") for w in CHALLENGES], None, False),              # one word is not
    ([(w, True) for w in CHALLENGES], 400.0, False),                     # under 90% of the line
    ([("us", False), ("and", True), ("us", True)], None, False),         # the first "us" is not
    ([(w, True) for w in CHALLENGES] + [("—", False)], None, True),      # a dash has no letter
    ([("Challenges", True)], None, False),                               # one word is no line
])
def test_a_line_is_underlined_when_every_lettered_word_is_and_they_span_it(words, width, expected):
    from reflow.analyze.headings import underlined
    assert underlined(underlined_line(words, width=width)) is expected


def test_an_underlined_ocr_line_is_a_heading_candidate_and_an_ungraded_one_is_not():
    from reflow.analyze.headings import ink_cue
    head = Block([underlined_line([(w, True) for w in CHALLENGES])])
    blocks = [head, Block(PARAGRAPH)]
    cue = ink_cue(head, 10.0)
    assert (cue.underlined, cue.why) == (True, "underlined on the page image")
    assert heading(blocks) == (10.0, True)
    bare = Block([replace(head.first, grades=None)])
    assert ink_cue(bare, 10.0) is None and heading([bare, Block(PARAGRAPH)]) is None


def test_an_unranked_style_takes_the_level_of_the_ranked_style_closest_in_size():
    from reflow.analyze.headings import style_level
    levels = {(18.0, True): 2, (12.0, True): 3}
    assert style_level(levels, (12.0, True)) == 3
    assert style_level(levels, (10.0, True)) == 3
    assert style_level(levels, (17.0, True)) == 2
    assert style_level({}, (10.0, True)) == 2


def test_a_section_number_line_that_holds_the_title_is_a_heading():
    # legislation p34: OCR reads the justified row as two lines, and the added section's
    # body, opened by its printed label, sits beside the title on the same row.
    title = Block([line(745, "Sec. 25A.00111.", size=11.0, x0=124, width=80),
                   line(745, "JUDGE'S EXPENSES; OFFICIAL DUTIES.", size=11.0, x0=215, width=270)])
    body = Block([line(745, "(a) A business court judge", size=11.0, x0=495, width=50)])
    assert heading([title, body], body=11.0) == (11.0, False)


def test_a_section_number_line_that_opens_an_amendment_sentence_is_not_a_heading():
    # legislation p5 and p24: the SECTION line holds no title.
    opener = Block([line(300, "SECTION 6. Section 30.015(a), Civil Practice and Remedies",
                         size=11.0, x0=124, width=420),
                    line(323, "Code, is amended to read as follows:", size=11.0, x0=78, width=250)])
    assert heading([opener], body=11.0) is None


def test_a_section_title_wrapped_onto_the_next_row_is_one_heading():
    # legislation p30: the title runs to the row's end, and its last word is the next row,
    # beside the body's printed label.
    top = Block([line(792, "Sec. 25A.0041.", x0=123, width=80),
                 line(792, "RULES RELATED TO JURISDICTIONAL", x0=215, width=330)])
    tail = Block([line(815, "DETERMINATION.", x0=79, width=98)])
    body = Block([line(815, "(a) The supreme court by rule shall", x0=190, width=355)])
    blocks = join_wrapped_headings([top, tail, body], 11.0)
    assert [b.text for b in blocks][0] == "Sec. 25A.0041. RULES RELATED TO JURISDICTIONAL DETERMINATION."
    assert heading(blocks, body=11.0) == (10.0, False)


# --- demoted_by_twin: a name the row-grid rule kept from a column of headings ---------------

def _entry(x, top, text, *, size=11.0, lines=1, bold=True, width=150):
    return Block([Line(page=1, bbox=(x, top + 12.3 * i, x + width, top + 12.3 * (i + 1)),
                       text=text if i == 0 else "only", size=size, bold=bold, italic=False,
                       font="Helvetica-Bold" if bold else "Helvetica") for i in range(lines)])


def _node(block, role):
    refusal = heading_why(HeadingWhy.ROW_TWIN)
    why = [Why(f"paragraph: not a heading ({refusal})", refusal.code)] if role == Role.P else []
    return Node(role, page=1, bbox=block.bbox, text=block.text, attrs={"why": why})


def test_the_twin_rule_is_read_back_by_its_code_not_by_its_words():
    reddy, reddymasu = _entry(220.8, 69.6, "Reddy, Sudhakar A, MD"), _entry(220.8, 148.0, "Reddymasu, Savio C, MD")
    sachdev = _entry(400.0, 145.4, "Sachdev, Mankanwal S, MD", lines=2)
    blocks = [reddy, reddymasu, sachdev]
    nodes = [_node(reddy, Role.H5), _node(reddymasu, Role.P), _node(sachdev, Role.H5)]
    nodes[1].attrs["why"] = [Why("reworded: a grid of names", HeadingWhy.ROW_TWIN)]
    assert demoted_by_twin(blocks, nodes, 11.0) == [nodes[1]]
    nodes[1].attrs["why"] = [f"paragraph: not a heading ({heading_why(HeadingWhy.ROW_TWIN)})"]
    assert demoted_by_twin(blocks, nodes, 11.0) == []


def test_the_heading_rule_names_the_reasons_that_later_steps_branch_on():
    title = Block([line(745, "Sec. 25A.00111.", size=11.0, x0=124, width=80),
                   line(745, "JUDGE'S EXPENSES; OFFICIAL DUTIES.", size=11.0, x0=215, width=270)])
    body = Block([line(745, "(a) A business court judge", size=11.0, x0=495, width=50)])
    style, why = heading_decision(title, 11.0, [title, body])
    assert style is not None and code_of(why) == HeadingWhy.SECTION_TITLE
    assert not ranks(title, 11.0, why)
    assert not ranks(title, 11.0, Why("worded another way", HeadingWhy.SECTION_TITLE))
    big = Block([line(100, "Overview", size=16.0)])
    style, why = heading_decision(big, 10.0, [big])
    assert style is not None and code_of(why) is None and ranks(big, 10.0, why)


def test_a_heading_made_by_ink_is_known_by_its_code_not_by_its_words():
    head = Block([underlined_line([(w, True) for w in CHALLENGES])])
    blocks = [head, Block(PARAGRAPH)]
    style, why = heading_decision(head, 10.0, blocks)
    assert style is not None and code_of(why) == HeadingWhy.INK
    assert from_ink(head, 10.0, why) is not None and not ranks(head, 10.0, why)
    assert from_ink(head, 10.0, "underlined on the page image") is None   # the words alone are not it


def test_a_name_level_with_a_heading_in_the_next_column_is_flagged():
    """A directory: Reddymasu sits level with Sachdev, a heading 29 pt away in the next column,
    and Reddy above it in its own column is a heading."""
    reddy, reddymasu = _entry(220.8, 69.6, "Reddy, Sudhakar A, MD"), _entry(220.8, 148.0, "Reddymasu, Savio C, MD")
    sachdev = _entry(400.0, 145.4, "Sachdev, Mankanwal S, MD", lines=2)
    blocks = [reddy, reddymasu, sachdev]
    nodes = [_node(reddy, Role.H5), _node(reddymasu, Role.P), _node(sachdev, Role.H5)]
    assert demoted_by_twin(blocks, nodes, 11.0) == [nodes[1]]
    # its twin a paragraph too, or no heading at its left edge: the grid rule stands
    assert demoted_by_twin(blocks, [nodes[0], nodes[1], _node(sachdev, Role.P)], 11.0) == []
    assert demoted_by_twin(blocks, [_node(reddy, Role.P), nodes[1], nodes[2]], 11.0) == []


def test_a_row_of_form_labels_is_not_flagged():
    """Bold labels at the margin under a bold section heading that starts there too. Beside
    them: another label (a twin, but no heading), a heading in another size, and a two-line
    heading whose second line only is on their row; none of those is a heading twin."""
    section = _entry(36, 60, "Applicant details")
    name, date = _entry(36, 100, "Name"), _entry(200, 100, "Date")
    office_bigger = _entry(380, 100, "Office use", size=12.0)
    office_two_lines = _entry(380, 87.7, "Office use", lines=2)
    for beside in (office_bigger, office_two_lines):
        blocks = [section, name, date, beside]
        nodes = [_node(section, Role.H3), _node(name, Role.P), _node(date, Role.P),
                 _node(beside, Role.H3)]
        assert demoted_by_twin(blocks, nodes, 11.0) == []


def test_body_text_beside_a_numbered_heading_is_not_flagged():
    """A two-column statute in one regular 11 pt face: "Sec. 4." is a heading by its number,
    and the sentence beside it would be body text even without its twin."""
    sec1 = _entry(36, 60, "Sec. 1. DEFINITIONS.", bold=False, width=250)
    sentence = _entry(36, 300, "This Act takes effect September 1, 2027.", bold=False, width=250)
    sec4 = _entry(311, 300, "Sec. 4. EFFECTIVE DATE.", bold=False, width=250)
    blocks = [sec1, sentence, sec4]
    nodes = [_node(sec1, Role.H3), _node(sentence, Role.P), _node(sec4, Role.H3)]
    assert demoted_by_twin(blocks, nodes, 11.0) == []


def test_a_continued_label_beside_a_heading_is_not_flagged():
    """A directory: bold "Cardiology (continued)" level with a heading in the next column, and
    "Dermatology" below at its left edge. A "(continued)" heading is kept as a context
    paragraph without its twin too."""
    cardiology = _entry(36, 100, "Cardiology (continued)")
    medicine = _entry(215, 100, "Internal Medicine and Pediatrics", lines=2)
    dermatology = _entry(36, 400, "Dermatology")
    blocks = [cardiology, medicine, dermatology]
    nodes = [_node(cardiology, Role.P), _node(medicine, Role.H4), _node(dermatology, Role.H4)]
    assert demoted_by_twin(blocks, nodes, 11.0) == []
