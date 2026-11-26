from dataclasses import replace

import pytest

from conftest import make_scan_pdf, make_text_pdf
from reflow.analyze.blocks import Block, join_lines, join_wrapped, marker_of, visual_lines
from reflow.analyze.constants import PUA_BULLETS
from reflow.analyze.layout import analyze
from reflow.analyze.worksheets import worksheet_rows
from reflow.extract.lines import SOFT_HYPHEN
from reflow.extract.pages import PageContent, extract_pages
from reflow.model import Node, Role


def layout(tmp_path, pages):
    return analyze(list(extract_pages(make_text_pdf(tmp_path / "t.pdf", pages))))


def publish_title(nodes, meta=None):
    """The title as the pipeline publishes it: chosen by analyze.title, its heading made the H1."""
    from reflow.analyze.evidence import title_apply
    from reflow.analyze.title import choose_title
    choice = choose_title(nodes, meta, None, "doc")
    title_apply(nodes, choice)
    return choice.text


def content(nodes):
    return [(n.role, n.text) for n in nodes if n.role != Role.ARTIFACT]


def body(top, text):
    return (72, top, 10, text)


def test_join_lines_rejoins_hyphenated_words():
    assert join_lines(["residual learn" + SOFT_HYPHEN, "ing works"]) == "residual learning works"
    assert join_lines(["one", " two "]) == "one two"


def test_wrapped_rtl_lines_read_top_to_bottom():
    for first, second in [("هل أنت معرض", "للخطر؟"), ("האם אתה", "בסיכון?")]:
        block = Block([_span(80, 180, 112, second), _span(40, 180, 100, first)])
        assert block.text == f"{first} {second}"
        assert join_lines([first, second]) == f"{first} {second}"


def test_arabic_fragments_on_one_row_read_right_to_left():
    block = Block([
        _span(421.03, 500.36, 93.96, "ّض للخطر؟", size=24.2),
        _span(493.92, 577.74, 93.96, "هل أنت معر", size=24.2),
    ])
    assert block.text == "هل أنت معرض للخطر؟"


def test_ltr_fragments_on_one_row_keep_extraction_order_when_tops_differ():
    bold = _span(40, 75, 100.2, "Note:")
    regular = _span(78, 190, 100.0, "the regular text follows")
    bold = replace(bold, bold=True)
    assert Block([bold, regular]).text == "Note: the regular text follows"


def test_rtl_row_keeps_spaces_and_attached_diacritics():
    block = Block([
        _span(40, 100, 100, "عربي"),
        _span(99, 180, 100, "نَص"),
        _span(80, 180, 112, "في السطر التالي"),
    ])
    assert block.text == "نَص عربي في السطر التالي"


def test_rtl_line_end_hyphens_do_not_join_words():
    assert join_lines(["هل أنت-", "معرض للخطر؟"]) == "هل أنت- معرض للخطر؟"
    assert join_lines(["هل أنت" + SOFT_HYPHEN, "معرض للخطر؟"]) == "هل أنت معرض للخطر؟"
    assert join_lines(["label-", "نص عربي"]) == "label- نص عربي"
    assert join_wrapped("user@example.", "نص عربي") == "user@example. نص عربي"
    assert join_wrapped("هذا مثال على رابط في السطر www.example-", "test.com") == (
        "هذا مثال على رابط في السطر www.example- test.com")
    assert join_lines(["هذا مثال على رابط في السطر www.example.", "com"]) == (
        "هذا مثال على رابط في السطر www.example.com")
    assert visual_lines(["هل أنت" + SOFT_HYPHEN, "معرض للخطر؟"]) == [
        "هل أنت", "معرض للخطر؟"]


def test_english_block_rows_read_top_to_bottom():
    block = Block([_span(40, 180, 112, "second line"), _span(40, 180, 100, "First line")])
    assert block.text == "First line second line"


def test_line_end_hyphen_joins_a_split_word_only_before_lowercase():
    assert join_lines(["your tax both ways on sep" + SOFT_HYPHEN, "arate returns"]) == (
        "your tax both ways on separate returns")
    assert join_lines(["the de-", "pendent care"]) == "the dependent care"
    assert join_lines(["the bottleneck for ResNet" + SOFT_HYPHEN, "34. Right"]) == (
        "the bottleneck for ResNet-34. Right")
    assert join_lines(["a well" + SOFT_HYPHEN, "Known rule"]) == "a well-Known rule"
    assert join_lines(["a well" + SOFT_HYPHEN, "(see below)"]) == "a well- (see below)"
    assert join_lines(["Visit www.arizona" + SOFT_HYPHEN, "urology.com today"]) == (
        "Visit www.arizona-urology.com today")


def test_headings_paragraphs_and_levels(tmp_path):
    pages = [[
        (72, 60, 20, "Annual Report", "B"),
        (72, 100, 14, "Overview", "B"),
        body(125, "The council met twelve times this year and"),
        body(137, "approved the budget."),
        body(160, "Turnout at public hearings doubled."),
        (72, 190, 10, "Next steps", "B"),
        body(210, "Staff will publish the plan in March."),
    ]]
    nodes = layout(tmp_path, pages)
    assert publish_title(nodes) == "Annual Report"
    assert content(nodes) == [
        (Role.H1, "Annual Report"),
        (Role.H2, "Overview"),
        (Role.P, "The council met twelve times this year and approved the budget."),
        (Role.P, "Turnout at public hearings doubled."),
        (Role.H3, "Next steps"),
        (Role.P, "Staff will publish the plan in March."),
    ]


def test_heading_levels_never_skip(tmp_path):
    nodes = layout(tmp_path, [[
        (72, 60, 24, "Title", "B"),
        (72, 100, 12, "Small heading straight after the title", "B"),
        body(125, "Text."),
    ]])
    publish_title(nodes)
    assert [n.role for n in nodes if n.role.startswith("h")] == [Role.H1, Role.H2]


def test_centered_capital_heading_lines_with_open_leading_stay_together(tmp_path):
    nodes = layout(tmp_path, [[
        (154, 100, 9, "TITLE II. COMMENCING AN ACTION; SERVICE OF PROCESS,", "B"),
        (211, 111, 9, "PLEADINGS, MOTIONS, AND ORDERS", "B"),
    ]])
    headings = [n for n in nodes if n.role.startswith("h")]
    assert len(headings) == 1
    assert headings[0].text == (
        "TITLE II. COMMENCING AN ACTION; SERVICE OF PROCESS, PLEADINGS, MOTIONS, AND ORDERS")


def test_centered_capital_lines_set_with_looser_leading_stay_separate_headings(tmp_path):
    nodes = layout(tmp_path, [[
        (120, 100, 20, "NATIONAL ADVISORY COMMITTEE"),
        (180, 134, 20, "FOR AERONAUTICS"),
        body(220, "Body text of the report."), body(232, "More body text of the report."),
    ]])
    assert [n.text for n in nodes if n.role.startswith("h")] == [
        "NATIONAL ADVISORY COMMITTEE", "FOR AERONAUTICS"]


def test_a_capital_bullet_line_is_not_joined_to_the_capital_line_above(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "Reviews"),
        (90, 120, 9, "ORR"),
        (90, 131, 9, "\u2022 FRR"),
        (90, 142, 9, "\u2022 SAR"),
    ]])
    assert [i.text for i in _li_nodes(nodes)] == ["FRR", "SAR"]


def test_stacked_distinct_centered_headings_set_solid_stay_separate(tmp_path):
    nodes = layout(tmp_path, [[
        (200, 100, 14, "DEPARTMENT OF THE TREASURY", "B"),
        (250, 114, 14, "INTERNAL REVENUE", "B"),
        body(160, "Body text of the form."), body(172, "More body text of the form."),
    ]])
    assert [n.text for n in nodes if n.role.startswith("h")] == [
        "DEPARTMENT OF THE TREASURY", "INTERNAL REVENUE"]


def test_metadata_title_leaves_content_headings_below_h1(tmp_path):
    nodes = layout(tmp_path, [[(72, 60, 24, "Cover text", "B"), body(100, "Text.")]])
    assert publish_title(nodes, "Annual Report 2025") == "Annual Report 2025"
    assert [n.role for n in nodes if n.role.startswith("h")] == [Role.H2]


def test_one_off_huge_text_does_not_push_headings_down(tmp_path):
    pages = [[(72, 200, 90, "Welcome", "B")]] + [
        [(72, 100 + 120 * j, 14, f"Section {i}.{j}", "B"), body(130 + 120 * j, "Text."),
         (72, 160 + 120 * j, 11, f"Item {i}.{j}", "B"), body(180 + 120 * j, "More text.")]
        for i in range(6) for j in range(4)]
    nodes = layout(tmp_path, pages)
    roles = {n.text: n.role for n in nodes if n.role.startswith("h")}
    assert roles["Welcome"] == Role.H2  # rare style: nearest frequent level
    assert roles["Section 0.0"] == Role.H2 and roles["Item 0.0"] == Role.H3


def test_decorative_text_is_an_artifact(tmp_path):
    nodes = layout(tmp_path, [[body(100, "Address line"), body(115, "____")]])
    assert content(nodes) == [(Role.P, "Address line")]


def test_two_columns_read_left_then_right(tmp_path):
    left = [(72, 100 + 14 * i, 10, f"left line {i} of the first column") for i in range(8)]
    right = [(320, 100 + 14 * i, 10, f"right line {i} of the second column") for i in range(8)]
    nodes = layout(tmp_path, [left + right])
    text = " ".join(n.text for n in nodes if n.role == Role.P)
    assert text.index("left line 7") < text.index("right line 0")


def test_page_numbers_and_running_headers_are_artifacts(tmp_path):
    pages = [[(72, 30, 9, "City of Example - Budget 2025"),
              body(100, f"Content of page {n}."),
              (300, 760, 9, str(n))] for n in range(1, 6)]
    nodes = layout(tmp_path, pages)
    artifacts = [n.text for n in nodes if n.role == Role.ARTIFACT]
    assert artifacts.count("City of Example - Budget 2025") == 5
    assert {"1", "2", "3", "4", "5"} <= set(artifacts)
    assert [n.text for n in nodes if n.role == Role.P] == [f"Content of page {n}." for n in range(1, 6)]


def test_lists(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "Bring the following:"),
        body(120, "• Photo ID"),
        body(134, "• Proof of address"),
        body(160, "1. Fill in the form"),
        body(174, "2. Sign it"),
    ]])
    lists = [n for n in nodes if n.role == Role.LIST]
    assert [l.attrs["ordered"] for l in lists] == [False, True]
    assert [c.text for c in lists[0].children] == ["Photo ID", "Proof of address"]
    assert [c.attrs["marker"] for c in lists[1].children] == ["1.", "2."]


def test_symbol_and_wingdings_bullets_start_list_items():
    nodes = analyze([PageContent(1, 612, 792, images=[], lines=[
        _line(72, 100, " Perform the check"),
        _line(72, 114, " Resolve the issues"),
        _line(72, 128, " Update the plan"),
    ])])
    lists = [n for n in nodes if n.role == Role.LIST]
    assert [c.text for c in lists[0].children] == [
        "Perform the check", "Resolve the issues", "Update the plan"]
    assert [c.attrs["marker"] for c in lists[0].children] == ["➢", "➢", "•"]
    assert not lists[0].attrs["ordered"]


def test_no_mapped_pua_bullet_survives_in_the_text():
    nodes = analyze([PageContent(1, 612, 792, images=[], lines=[
        _line(72, 100, "Tasks include  planning  review"),
        _line(72, 114, "Unmapped  glyph stays put"),
    ])])
    texts = _texts(nodes)
    assert not any("" in t for t in texts)
    assert any("planning • review" in t for t in texts)
    assert any("Unmapped  glyph stays put" in t for t in texts)


def test_every_mapped_pua_glyph_is_a_bullet_marker():
    for glyph in PUA_BULLETS.values():
        marker = marker_of(f"{glyph} an item")
        assert marker is not None and marker.kind == "bullet", glyph


def _texts(nodes):
    out = []
    for n in nodes:
        out += [n.text] if n.text else []
        out += _texts(n.children)
    return out


def test_phone_area_codes_are_not_list_markers(tmp_path):
    nodes = layout(tmp_path, [[body(100, "(520) 887-0095 Website: www.example.com")]])
    assert content(nodes) == [(Role.P, "(520) 887-0095 Website: www.example.com")]


def test_initials_are_not_list_markers(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "R. M. HAZEN, Director of Engineering"),
        body(112, "E. H. CHAMBERLIN, Executive Officer"),
        body(124, "Ordinary paragraph text follows the names."),
    ]])
    assert not [n for n in nodes if n.role == Role.LI]
    assert "E. H. CHAMBERLIN" in nodes[1].text


def test_capital_initials_in_a_run_are_still_initials(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "Y. Bengio. Fitnets: Hints for thin deep nets. In ICLR, 2015."),
        body(112, "Z. Huang, A. Karpathy, A. Khosla, M. Bernstein, et al."),
    ]])
    assert not [n for n in nodes if n.role == Role.LI]


def test_a_lone_numbered_line_is_not_a_list(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "Intro paragraph text here."),
        body(112, "3. Some sentence that stands alone"),
        body(150, "More text after a gap."),
    ]])
    assert [n.role for n in nodes] == [Role.P, Role.P, Role.P]


def test_letters_in_sequence_make_a_list(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "a. You can claim it"),
        body(112, "b. You must include it"),
    ]])
    lists = [n for n in nodes if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in lists[0].children] == ["a.", "b."]


def test_section_numbers_in_separate_blocks_are_not_a_list(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "1. UNITS"),
        body(120, "Some prose describing the units at some length here."),
        body(140, "Further prose about the units, a paragraph of its own."),
        body(200, "2. SYMBOLS"),
        body(220, "More prose describing the symbols at some length."),
    ]])
    assert not [n for n in nodes if n.role == Role.LI]
    assert [n.text for n in nodes if n.role == Role.P][0] == "1. UNITS"


def test_wrapped_lines_of_a_list_item_stay_in_the_item(tmp_path):
    nodes = layout(tmp_path, [[
        (60, 100, 10, "1. Your tax rate is generally higher than on a"),
        (72, 112, 10, "joint return."),
        (60, 124, 10, "2. Your exemption amount is half that allowed"),
        (72, 136, 10, "on a joint return."),
    ]])
    lists = [n for n in nodes if n.role == Role.LIST]
    assert [n.role for n in nodes] == [Role.LIST]
    assert [c.text for c in lists[0].children] == [
        "Your tax rate is generally higher than on a joint return.",
        "Your exemption amount is half that allowed on a joint return."]


def test_a_line_at_the_marker_ends_the_list(tmp_path):
    nodes = layout(tmp_path, [[
        (60, 100, 10, "1. Certification instructions. You must cross out"),
        (72, 112, 10, "item 2 above."),
        (60, 124, 10, "Braille-Ready File (BRF). Go to the IRS site for more."),
    ]])
    assert [n.role for n in nodes] == [Role.LIST, Role.P]
    assert [c.text for c in nodes[0].children] == [
        "Certification instructions. You must cross out item 2 above."]
    assert nodes[1].text.startswith("Braille-Ready File (BRF).")


def test_a_word_split_across_an_item_end_stays_in_the_item(tmp_path):
    nodes = layout(tmp_path, [[
        (60, 100, 10, "• You are ultimately responsi-"),
        (50, 112, 10, "ble for providing the information."),
        (60, 124, 10, "Next paragraph text."),
    ]])
    assert [n.role for n in nodes] == [Role.LIST, Role.P]
    assert [c.text for c in nodes[0].children] == [
        "You are ultimately responsible for providing the information."]


def test_an_outdented_sentence_continuation_stays_in_the_item(tmp_path):
    nodes = layout(tmp_path, [[
        (44, 100, 10, "3. Real estate transactions. You must sign the certification. You may"),
        (36, 112, 10, "cross out item 2 of the certification."),
        (44, 124, 10, "4. Other payments. You must give your correct TIN."),
        (36, 136, 10, "Next paragraph text."),
    ]])
    lists = [n for n in nodes if n.role == Role.LIST]
    assert [c.text for c in lists[0].children][0] == (
        "Real estate transactions. You must sign the certification. You may "
        "cross out item 2 of the certification.")
    assert nodes[-1].role == Role.P and nodes[-1].text == "Next paragraph text."


def test_an_indented_paragraph_after_a_bullet_item_is_not_part_of_it(tmp_path):
    nodes = layout(tmp_path, [[
        (60, 100, 10, "• First bullet that wraps"),
        (72, 112, 10, "onto a second line."),
        (60, 124, 10, "• Second bullet."),
        (66, 140, 10, "An indented paragraph after the list."),
        (60, 152, 10, "Its wrapped line returns to the margin."),
    ]])
    lists = [n for n in nodes if n.role == Role.LIST]
    assert [c.text for c in lists[0].children] == [
        "First bullet that wraps onto a second line.", "Second bullet."]
    assert [n.text for n in nodes if n.role == Role.P] == [
        "An indented paragraph after the list. Its wrapped line returns to the margin."]


def test_number_finishing_a_hyphenated_word_is_not_a_marker(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "The bottleneck block is shown for ResNet-"),
        body(112, "34. Right: a bottleneck block for ResNet-50."),
    ]])
    assert [n.role for n in nodes] == [Role.P]
    assert "34. Right" in nodes[0].text


def test_worksheet_line_numbers_are_list_items_not_headings(tmp_path):
    # A worksheet: bold line numbers down the margin, text beside each, and an amount on the right.
    nodes = layout(tmp_path, [[
        (45, 100, 9, "1", "B"), (64, 100, 9, "Deductions for qualified tips and loan interest."),
        (64, 112, 9, "a Qualified tips. If your income is low, enter"),
        (480, 112, 9, "1a $"),
        (45, 130, 9, "2", "B"), (64, 130, 9, "Add lines 1a and 1b. Enter the result here"),
        (480, 130, 9, "2 $"),
        (45, 150, 9, "3", "B"), (64, 150, 9, "Seniors age 65 or older. Enter the amount here"),
        (45, 170, 9, "4", "B"), (64, 170, 9, "Add lines 2 and 3. Enter the result here"),
        (480, 170, 9, "4 $"),
    ]])
    assert [n.role for n in nodes] == [Role.LIST]
    items = nodes[0].children
    assert [c.attrs["marker"] for c in items] == ["1", "2", "3", "4"]
    assert nodes[0].attrs["ordered"] is True
    assert items[0].text.startswith("Deductions for qualified tips") and "1a $" in items[0].text
    assert items[1].text == "Add lines 1a and 1b. Enter the result here 2 $"


def test_lettered_sub_lines_of_a_worksheet_line_nest_under_it(tmp_path):
    # Lines 6 and 8 have lettered sub-lines (a, b) at their text's left edge, amounts on the right.
    nodes = layout(tmp_path, [[
        (45, 100, 9, "6", "B"), (64, 100, 9, "Itemized deductions. Enter your deductions"),
        (64, 112, 9, "a Medical and dental expenses. Enter expenses"), (480, 112, 9, "6a $"),
        (64, 124, 9, "b State and local taxes. Enter taxes paid"), (480, 124, 9, "6b $"),
        (45, 150, 9, "7", "B"), (64, 150, 9, "Add lines 6a and 6b. Enter the result here"),
        (480, 150, 9, "7 $"),
        (45, 170, 9, "8", "B"), (64, 170, 9, "Limitation on itemized deductions."),
        (64, 182, 9, "a Enter your total income"), (480, 182, 9, "8a $"),
        (64, 194, 9, "b Subtract line 4 from line 8a"), (480, 194, 9, "8b $"),
    ]])
    assert [n.role for n in nodes] == [Role.LIST]
    items = nodes[0].children
    assert [c.attrs["marker"] for c in items] == ["6", "7", "8"]
    assert items[2].text == "Limitation on itemized deductions."
    for item, texts in ((items[0], ["Medical and dental", "State and local"]),
                        (items[2], ["Enter your total income", "Subtract line 4"])):
        (inner,) = [c for c in item.children if c.role == Role.LIST]
        assert inner.attrs["ordered"] is True
        assert [c.attrs["marker"] for c in inner.children] == ["a", "b"]
        assert [c.text.startswith(t) for c, t in zip(inner.children, texts)] == [True, True]
        assert inner.children[0].text.endswith("6a $" if item is items[0] else "8a $")


def test_bullets_under_a_worksheet_line_follow_it_and_the_numbering_goes_on(tmp_path):
    nodes = layout(tmp_path, [[
        (45, 100, 9, "1", "B"), (64, 100, 9, "Deductions for qualified tips."),
        (45, 120, 9, "2", "B"), (64, 120, 9, "Enter:"),
        (100, 134, 9, "• first choice"), (100, 146, 9, "• second choice"),
        (45, 170, 9, "3", "B"), (64, 170, 9, "Add lines 1 and 2. Enter the result here"),
    ]])
    assert [n.role for n in nodes] == [Role.LIST, Role.LIST, Role.LIST]
    numbered, bullets, last = nodes
    assert [c.attrs["marker"] for c in numbered.children] == ["1", "2"]
    assert numbered.attrs["ordered"] is True and bullets.attrs["ordered"] is False
    assert [c.attrs["marker"] for c in last.children] == ["3"]
    assert [c.text for c in bullets.children] == ["first choice", "second choice"]


def test_a_bracket_belongs_to_the_number_its_middle_sits_beside(tmp_path):
    # The bracket's top is above number 2's row, but its middle is beside number 2.
    nodes = layout(tmp_path, [[
        (45, 100, 9, "1", "B"), (64, 100, 9, "Deductions for qualified tips."),
        (45, 120, 9, "2", "B"), (64, 120, 9, "Enter:"),
        (100, 112, 9, "{ • first choice"),
        (45, 170, 9, "3", "B"), (64, 170, 9, "Add lines 1 and 2. Enter the result here"),
    ]])
    assert [n.role for n in nodes] == [Role.LIST, Role.P, Role.LIST]
    assert [c.attrs["marker"] for c in nodes[0].children] == ["1", "2"]
    assert nodes[1].text.startswith("{ • first choice")
    assert [c.attrs["marker"] for c in nodes[2].children] == ["3"]


def test_a_table_column_of_numbers_is_not_a_worksheet(tmp_path):
    # Rows of a table: a label, then a middle column of 1, 2, 3 with another cell well to its right.
    nodes = layout(tmp_path, [[
        (64, 100, 9, "Collected obligations for the year"), (200, 100, 9, "1"), (260, 100, 9, "7"),
        (64, 120, 9, "Uncollected balance brought forward"), (200, 120, 9, "2"), (260, 120, 9, "8"),
        (64, 140, 9, "Net budget authority for the year"), (200, 140, 9, "3"), (260, 140, 9, "9"),
    ]])
    assert not any(n.role == Role.LIST for n in nodes)


def test_bold_numbers_without_a_run_beside_text_stay_paragraphs(tmp_path):
    nodes = layout(tmp_path, [[
        (45, 100, 9, "5", "B"), (64, 100, 9, "Enter the amount of your savings here"),
        (45, 130, 9, "9", "B"), (64, 130, 9, "Enter the amount of your other income"),
        (45, 160, 9, "12", "B"), (64, 160, 9, "Enter the total of the lines above"),
    ]])
    assert not any(n.role == Role.LIST for n in nodes)
    assert "5" in [n.text for n in nodes]


def test_sibling_headings_share_a_level():
    from reflow.analyze.headings import fix_heading_levels
    from reflow.model import Node
    nodes = [Node(Role(r), text=t) for r, t in
             [("h2", "Specialty"), ("h3", "City"), ("h5", "Provider A"), ("h5", "Provider B"),
              ("h3", "Next city"), ("h5", "Provider C")]]
    fix_heading_levels(nodes)
    assert [n.role.value for n in nodes] == ["h2", "h3", "h4", "h4", "h3", "h4"]


def test_a_continued_heading_becomes_a_context_paragraph(tmp_path):
    nodes = layout(tmp_path, [[(72, 100, 14, "CARDIOLOGY (continued)", "B"),
                               body(130, "Dr. Example, 1 Main St")]])
    assert content(nodes) == [
        (Role.P, "Continued: Cardiology (continued from the previous page)"),
        (Role.P, "Dr. Example, 1 Main St")]
    assert nodes[0].attrs.get("context") is True


def test_continued_context_at_page_start_becomes_one_breadcrumb(tmp_path):
    nodes = layout(tmp_path, [[
        (72, 100, 14, "NON-PHYSICIANS (continued)", "B"),
        (72, 130, 14, "FAMILY NURSE", "B"),
        (72, 146, 14, "PRACTITIONER", "B"),
        (90, 162, 14, "(continued)", "B"),
        (72, 190, 12, "Gilbert (continued)", "B"),
        (72, 220, 10, "Hobbs, Michael T, FNP (M)", "B"),
        body(232, "6343 S Higley Rd"),
        body(244, "Gilbert, AZ 85298"),
        body(256, "Website: www.nextcare.com"),
        (72, 400, 12, "Mesa (continued)", "B"),
        (72, 430, 10, "Smith, Ann, FNP (F)", "B"),
    ]])
    nodes = [n for n in nodes if n.role != Role.ARTIFACT]
    assert nodes[0].text == ("Continued: Non-Physicians › Family Nurse Practitioner › "
                             "Gilbert")
    assert nodes[0].attrs["context"] is True
    assert nodes[0].attrs["trail"] == ["Non-Physicians", "Family Nurse Practitioner",
                                       "Gilbert"]
    assert nodes[1].role.startswith("h")
    later = [n for n in nodes[1:] if n.attrs.get("context")]
    assert [n.text for n in later] == ["Continued: Mesa"]


def test_merge_context_flags_orphan_content():
    from reflow.analyze.breaks import merge_context
    from reflow.model import Node
    nodes = merge_context([Node(Role.P, page=3, text="CARDIOLOGY (continued)",
                                attrs={"context": True}),
                           Node(Role.P, page=3, text="Phoenix (continued)",
                                attrs={"context": True}),
                           Node(Role.P, page=3, text="1 Main St Phoenix, AZ 85004")])
    assert [n.text for n in nodes] == [
        "Continued: Cardiology › Phoenix (continued from the previous page)",
        "1 Main St Phoenix, AZ 85004"]
    assert merge_context([]) == []


def test_bare_continued_joins_the_running_header_above(tmp_path):
    from reflow.analyze.blocks import Block
    from reflow.analyze.breaks import attach_continued
    from reflow.extract.lines import Line
    header = Line(page=1, bbox=(65, 68, 191, 84), text="NON-PHYSICIANS", size=14.0,
                  bold=True, italic=False, font="F")
    bare = Line(page=1, bbox=(91, 84, 166, 101), text="(continued)", size=14.0, bold=True,
                italic=False, font="F")
    [block] = attach_continued([Block([bare])], [header])
    assert block.text == "NON-PHYSICIANS (continued)"


def test_rule_only_drawings_are_never_figures():
    from reflow.analyze.figures import figure_boxes
    from reflow.extract.drawings import Drawing
    from reflow.extract.pages import PageContent
    rules = Drawing((72, 100, 540, 300), paths=12, rules=12)    # booktabs-style table
    chart = Drawing((72, 400, 540, 600), paths=40, rules=10)    # curves and boxes
    page = PageContent(1, 612, 792, lines=[], images=[], drawings=[rules, chart])
    assert [f.bbox for f in figure_boxes(page, [])] == [(72, 400, 540, 600)]


def test_tables_replace_their_text_and_take_their_caption(tmp_path):
    from reflow.extract.tables.cells import TableData
    pages = list(extract_pages(make_text_pdf(tmp_path / "t.pdf", [[
        body(80, "Table 1. Revenue by year."),
        body(100, "Year"), (200, 100, 10, "Revenue"),
        body(114, "2024"), (200, 114, 10, "$10"),
        body(150, "After the table."),
    ]])))
    table = TableData(1, (60, 95, 300, 130), [["Year", "Revenue"], ["2024", "$10"], ["Total", None]])
    nodes = analyze(pages, {1: [table]})
    assert [n.role for n in nodes if n.role != Role.ARTIFACT] == [Role.TABLE, Role.P]
    t = nodes[0]
    assert t.children[0].role == Role.CAPTION and t.children[0].text == "Table 1. Revenue by year."
    header, row, total = t.children[1:]
    assert [c.role for c in header.children] == [Role.TH, Role.TH]
    assert [c.text for c in row.children] == ["2024", "$10"]
    assert total.children[0].attrs["colspan"] == 2


def _line(x0, top, text, size=10.0):
    from reflow.extract.lines import Line
    return Line(1, (x0, top, x0 + 6 * len(text), top + size * 1.2), text, size, False, False, "Helvetica")


# --- icon legends ----------------------------------------------------------------------

OK, NO = "ok-key", "no-key"


def _icon(key, x0, top, side=11.0):
    from reflow.extract.images import Icon
    return Icon((x0, top, x0 + side, top + side), key)


def _directory_page(number, providers, legend=True):
    """A directory page: bold provider names, each followed by an icon on its right and
    an address below, and a footer legend "[ok] Accepting New Patients [no] Not ..."."""
    from reflow.extract.lines import Line
    from reflow.extract.pages import PageContent
    lines, icons = [], []
    for i, (name, key) in enumerate(providers):
        top = 100 + 60 * i
        lines.append(Line(number, (40, top, 40 + 6 * len(name), top + 12), name, 11, True,
                          False, "Helvetica"))
        lines.append(Line(number, (40, top + 13, 160, top + 25), "6343 S Higley Rd", 11,
                          False, False, "Helvetica"))
        icons.append(_icon(key, 40 + 6 * len(name) + 1, top))
    if legend:
        lines.append(Line(number, (166, 744, 283, 756), "Accepting New Patients", 11, False,
                          False, "Helvetica"))
        lines.append(Line(number, (311, 744, 447, 756), "Not Accepting New Patients", 11,
                          False, False, "Helvetica"))
        icons += [_icon(OK, 152, 744), _icon(NO, 297, 744)]
    return PageContent(number, 612, 792, lines, images=[i.bbox for i in icons], icons=icons)


def _directory():
    return [_directory_page(1, [("Hasley, Patricia, ANP (F)", OK), ("Dyer, Kim, FNP (F)", NO)]),
            _directory_page(2, [("Hobbs, Michael T, FNP (M)", OK)]),
            _directory_page(3, [("Ibe, Geoff R, FNP (M)", NO), ("Huston, Amy, FNP (F)", OK)])]


def test_legend_from_footer_repeated_on_pages():
    from reflow.analyze.figures import find_legend
    assert find_legend(_directory()) == {OK: "Accepting New Patients",
                                         NO: "Not Accepting New Patients"}


def test_legend_icons_mark_the_text_beside_them():
    nodes = analyze(_directory())
    status = {n.text: n.attrs.get("status") for n in nodes
              if n.role != Role.ARTIFACT and "Higley" not in n.text}
    assert status == {
        "Hasley, Patricia, ANP (F)": "Accepting New Patients",
        "Dyer, Kim, FNP (F)": "Not Accepting New Patients",
        "Hobbs, Michael T, FNP (M)": "Accepting New Patients",
        "Ibe, Geoff R, FNP (M)": "Not Accepting New Patients",
        "Huston, Amy, FNP (F)": "Accepting New Patients",
    }
    # Each provider carries the meaning, so the legend needn't repeat on every page.
    legend = [n for n in nodes if "Accepting New Patients" in (n.text or "")]
    assert len(legend) == 6 and all(n.role == Role.ARTIFACT for n in legend)
    assert not any(n.role == Role.FIGURE for n in nodes)  # icons are never figures


def test_one_legend_counts_when_its_icon_marks_text_elsewhere():
    from reflow.analyze.figures import find_legend
    used = [_directory_page(1, [("Hasley, Patricia, ANP (F)", OK)]),
            _directory_page(2, [("Hobbs, Michael T, FNP (M)", OK)], legend=False)]
    assert find_legend(used) == {OK: "Accepting New Patients"}
    assert find_legend([_directory_page(1, [])]) == {}  # once, and nothing else uses it


def test_icons_before_names_are_not_a_legend():
    from reflow.analyze.figures import find_legend
    from reflow.extract.pages import PageContent
    pages = []
    for n in range(1, 6):
        names = [_line(60, 100 + 40 * i, f"Provider {n}-{i}") for i in range(4)]
        icons = [_icon(OK, 45, 100 + 40 * i) for i in range(4)]
        pages.append(PageContent(n, 612, 792, names, images=[], icons=icons))
    assert find_legend(pages) == {}


def test_paragraph_beside_a_legend_icon_gets_the_status():
    from reflow.extract.lines import Line
    pages = _directory()
    pages[0].lines.append(Line(1, (40, 300, 160, 312), "Clinic open Saturdays", 11, False,
                               False, "Helvetica"))
    pages[0].icons.append(_icon(OK, 162, 300))
    [node] = [n for n in analyze(pages) if n.text == "Clinic open Saturdays"]
    assert node.role == Role.P
    assert node.attrs["status"] == "Accepting New Patients"


def test_shapes_behind_prose_are_backdrops_not_figures():
    from reflow.analyze.figures import figure_boxes
    from reflow.extract.drawings import Drawing
    from reflow.extract.pages import PageContent
    backdrop = Drawing((30, 60, 330, 400), paths=8, rules=0)   # header bars and side tab
    lines = [_line(40, 190 + 13 * i, text) for i, text in enumerate([
        "Tamburro, Larry S, MD (M)", "4600 E Shea Blvd, Ste 100", "Phoenix, AZ 85028",
        "(602) 955-8700", "Website: www.theintracare.com", "Language: Spanish"])]
    page = PageContent(1, 612, 792, lines=lines, images=[], drawings=[backdrop])
    assert figure_boxes(page, lines, body=10.0) == []


def test_label_absorption_does_not_chain_through_short_lines():
    from reflow.analyze.figures import figure_boxes
    from reflow.extract.drawings import Drawing
    from reflow.extract.pages import PageContent
    chart = Drawing((100, 100, 300, 250), paths=30, rules=4)
    label = _line(120, 255, "iter. (1e4)")                       # 5pt below the chart
    column = [_line(120, 268 + 13 * i, t) for i, t in enumerate(  # each 1pt below the last
        ["Phoenix, AZ 85029", "(602) 569-5437", "com", "Language: English"])]
    page = PageContent(1, 612, 792, lines=[label, *column], images=[], drawings=[chart])
    [found] = figure_boxes(page, page.lines, body=10.0)
    assert found.bbox[3] == label.bottom  # took the axis label, not the column below it


def test_section_title_repeated_on_some_of_its_pages_is_content(tmp_path):
    # An 18pt section title on every third page of a 60-page document: 10 copies, under a
    # quarter of the pages and under half of its 28-page span. A count alone does not make
    # running text, so each copy stays and the first is the section's heading.
    pages = [[body(100, f"Listing {n}.")] for n in range(60)]
    for n in range(10, 40, 3):
        pages[n].insert(0, (72, 25, 18, "Hospitals and Laboratories", "B"))
    nodes = layout(tmp_path, pages)
    titles = [n for n in nodes if n.text == "Hospitals and Laboratories"]
    assert [n.role == Role.ARTIFACT for n in titles] == [False] * 10
    assert titles[0].page == 11 and titles[0].role.startswith("h")


def test_names_in_separate_columns_are_still_headings(tmp_path):
    # Bold provider names that line up across the columns of a directory page.
    cols = (40, 220, 400)
    items = []
    for i, name in enumerate(["Teekamdas, Roma, MD", "Tontsch, Chandra, MD", "Vanood, Sophia, MD"]):
        x = cols[i]
        items += [(x, 100, 11, name, "B"), (x, 114, 11, "4530 E Muirwood Dr"),
                  (x, 127, 11, "Phoenix, AZ 85048")]
    for row in range(4):  # enough lines per column for a clean column split
        items += [(x, 160 + 13 * row, 11, f"Language: English {row}") for x in cols]
    nodes = layout(tmp_path, [items])
    headings = [n.text for n in nodes if n.role.startswith("h")]
    assert headings == ["Teekamdas, Roma, MD", "Tontsch, Chandra, MD", "Vanood, Sophia, MD"]


def test_addresses_at_the_top_of_columns_are_not_running_headers(tmp_path):
    # Different addresses with the same shape at the top margin on many pages.
    pages = [[(72, 40, 10, f"{4100 + n} N {100 + n}th Ave, Ste {n}"), body(120, "Listing.")]
             for n in range(15)]
    nodes = layout(tmp_path, pages)
    assert not any(n.role == Role.ARTIFACT and "Ave" in (n.text or "") for n in nodes)


def test_a_sentence_that_mentions_a_table_or_figure_is_not_a_caption(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "Table 3 shows that all three options are better than the plain one."),
        body(130, "Fig. 6 (middle) shows the behaviors of the deeper networks."),
        body(160, "Table 4. Error rates of the single-model results."),
        body(190, "Table 6-1. Rules for the sale of property."),
    ]])
    assert [n.role for n in nodes] == [Role.P, Role.P, Role.CAPTION, Role.CAPTION]


def test_join_wrapped_rejoins_url_with_dot():
    """URLs wrapped at dots should be joined without spaces."""
    assert join_wrapped("www.example.", "com") == "www.example.com"
    assert join_wrapped("www.banner.", "health.com") == "www.banner.health.com"


def test_join_wrapped_rejoins_url_with_slash():
    """URLs wrapped at slashes should be joined without spaces."""
    assert join_wrapped("http://example.com/", "path") == "http://example.com/path"
    assert join_wrapped("https://example.com/", "path/to/resource") == "https://example.com/path/to/resource"


def test_join_wrapped_rejoins_url_with_hyphen():
    """Domain names with hyphens should be joined without spaces."""
    assert join_wrapped("www.", "arizona-urology.com") == "www.arizona-urology.com"
    assert join_wrapped("https://example.", "api-key") == "https://example.api-key"


def test_join_wrapped_rejoins_email():
    """Emails wrapped should be joined without spaces."""
    assert join_wrapped("user@example.", "com") == "user@example.com"
    assert join_wrapped("contact@domain.", "org") == "contact@domain.org"


def test_join_wrapped_preserves_normal_spacing():
    """Normal text should still have spaces."""
    assert join_wrapped("Dr.", "Smith") == "Dr. Smith"
    assert join_wrapped("Ste.", "100") == "Ste. 100"
    assert join_wrapped("e.g.", "this") == "e.g. this"
    assert join_wrapped("U.S.", "government") == "U.S. government"


def test_join_wrapped_respects_uppercase_starts():
    """Don't join if next fragment starts with uppercase (new sentence)."""
    assert join_wrapped("www.example.", "Com") == "www.example. Com"
    assert join_wrapped("http://example.", "Org") == "http://example. Org"


def test_join_wrapped_empty_inputs():
    """Empty inputs should be handled gracefully."""
    assert join_wrapped("", "test") == "test"
    assert join_wrapped("test", "") == "test"
    assert join_wrapped("", "") == ""


def test_join_lines_with_url_wrapping():
    """join_lines should use join_wrapped to handle URLs across lines."""
    # Simple URL wrapping
    result = join_lines(["Visit www.example.", "com for more"])
    assert result == "Visit www.example.com for more"

    # URL with path
    result = join_lines(["See https://example.com/", "path"])
    assert result == "See https://example.com/path"

    # Normal text should still space correctly
    result = join_lines(["Dr.", "Smith"])
    assert result == "Dr. Smith"

    # Mixed: normal text and URL
    result = join_lines(["Visit www.banner.", "health.com or Dr.", "Smith"])
    assert result == "Visit www.banner.health.com or Dr. Smith"


def test_address_blocks_keep_their_lines(tmp_path):
    nodes = layout(tmp_path, [[
        (72, 100, 10, "Ibe, Geoff R, FNP (M)", "B"),
        body(112, "3126 S Higley Rd, Ste 109"),
        body(124, "Gilbert, AZ 85295"),
        body(136, "(480) 827-5780"),
        body(148, "Website: www.bannerhealth."),
        body(160, "com"),
        body(172, "Languages: English, Spanish"),
    ]])
    address = [n for n in nodes if n.role == Role.P][0]
    assert address.attrs["lines"] == ["3126 S Higley Rd, Ste 109", "Gilbert, AZ 85295",
                                      "(480) 827-5780", "Website: www.bannerhealth.com",
                                      "Languages: English, Spanish"]
    assert address.text == " ".join(address.attrs["lines"])


def test_wrapped_web_addresses_and_suites_stay_on_one_line():
    from reflow.analyze.blocks import visual_lines
    assert visual_lines(["Website: www.", "optimalreliefandwellness.", "com",
                         "Language: English"]) == [
        "Website: www.optimalreliefandwellness.com", "Language: English"]
    assert visual_lines(["Website:", "example.com"]) == ["Website: example.com"]
    assert visual_lines(["3303 E Baseline Rd Bldg 2, Ste", "203", "Gilbert, AZ 85234"]) == [
        "3303 E Baseline Rd Bldg 2, Ste 203", "Gilbert, AZ 85234"]
    assert visual_lines(["learn" + SOFT_HYPHEN, "ing"]) == ["learning"]


def test_prose_and_single_lines_are_not_addresses(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "Send the completed form to our office in Phoenix, AZ 85004"),
        body(112, "and allow two weeks for processing before you call us about"),
        body(124, "the status of your application or any missing documents."),
        body(150, "Phone: (480) 555-0100"),
        body(176, "Gilbert is a town in Maricopa County with a population"),
        body(188, "of about 270,000 residents."),
    ]])
    assert [n for n in nodes if "lines" in n.attrs] == []



# --- reading order: gutters ---------------------------------------------------------------

def _span(x0, x1, top, text, size=10.0):
    from reflow.extract.lines import Line
    return Line(1, (x0, top, x1, top + size * 1.2), text, size, False, False, "Helvetica")


def _order(lines):
    from reflow.analyze.columns import xy_cut
    ordered, _ = xy_cut(lines, 10.0 * 1.2)
    return [l.text for l in ordered]


def test_narrow_gutter_separates_two_columns():
    # A 6 pt gutter at 10 pt type (0.6 em): narrower than 8 pt, so the text on both sides must
    # be prose for it to count as a gutter.
    left = [_span(40, 190, 100 + 12 * i, f"left column line {i} of prose") for i in range(6)]
    right = [_span(196, 340, 100 + 12 * i, f"right column line {i} of prose") for i in range(6)]
    assert _order(left + right) == ([f"left column line {i} of prose" for i in range(6)]
                                    + [f"right column line {i} of prose" for i in range(6)])


def test_narrow_gutter_separates_three_columns():
    cols = [
        [_span(40, 190, 100 + 12 * i, f"one column line {i} of prose") for i in range(5)],
        [_span(197, 350, 100 + 12 * i, f"two column line {i} of prose") for i in range(5)],
        [_span(357, 520, 100 + 12 * i, f"three column line {i} of prose") for i in range(5)],
    ]
    text = _order([l for col in cols for l in col])
    assert text == [f"{c} column line {i} of prose" for c in ("one", "two", "three") for i in range(5)]


def test_heading_across_the_gutter_splits_the_columns():
    # Two columns; a heading across both sits between their third and fourth lines.
    tops = [100 + 12 * i for i in range(3)] + [136 + 12 * i for i in range(4)]
    left = [_span(40, 290, t, f"left column line {i} of prose") for i, t in enumerate(tops)]
    right = [_span(306, 540, t, f"right column line {i} of prose") for i, t in enumerate(tops)]
    heading = _span(40, 540, 128, "Full width heading")
    assert _order(left + right + [heading]) == (
        [f"left column line {i} of prose" for i in range(3)]
        + [f"right column line {i} of prose" for i in range(3)] + ["Full width heading"]
        + [f"left column line {i} of prose" for i in range(3, 7)]
        + [f"right column line {i} of prose" for i in range(3, 7)])


def test_form_rows_across_the_gutter_do_not_hide_the_columns():
    # Rows that run across the gutter sit above a two-column text: the text is still read
    # column by column, and the rows keep their order.
    rows = [_span(40, 420, 100 + 12 * i, f"form row {i} with a field label") for i in range(4)]
    left = [_span(40, 290, 160 + 12 * i, f"left column line {i} of prose") for i in range(6)]
    right = [_span(306, 560, 160 + 12 * i, f"right column line {i} of prose") for i in range(6)]
    assert _order(rows + left + right) == (
        [f"form row {i} with a field label" for i in range(4)]
        + [f"left column line {i} of prose" for i in range(6)]
        + [f"right column line {i} of prose" for i in range(6)])


def test_single_column_with_ragged_lines_keeps_its_order():
    lines = [_span(72, 72 + 6 * (30 + 25 * (i % 4)), 100 + 12 * i, f"line {i}") for i in range(12)]
    lines.append(_span(300, 420, 100 + 12 * 12, "a note on the right"))
    assert _order(lines) == [f"line {i}" for i in range(12)] + ["a note on the right"]


def test_numeric_cells_beside_labels_read_row_by_row():
    # A table without rules: labels on the left, one number per row on the right. Short cells
    # are not two columns of text, so the rows keep their order.
    heading = _span(40, 540, 100, "Table heading across the page")
    rows = []
    for i in range(5):
        rows.append(_span(40, 200, 120 + 12 * i, f"row label {i} here"))
        rows.append(_span(380, 400, 120 + 12 * i, f"{i}.0"))
    rows.append(_span(40, 540, 120 + 12 * 5, "Totals are shown after the rows"))
    text = _order([heading] + rows)
    assert text[:3] == ["Table heading across the page", "row label 0 here", "0.0"]
    assert text.index("row label 1 here") < text.index("1.0") < text.index("row label 2 here")


def test_a_ruled_box_does_not_reorder_its_cells():
    from reflow.analyze.columns import xy_cut
    # Two columns of prose cells, with one cell across both (so no clean gutter), in a ruled box.
    # The box does not make every line a separate break: a form's boxes are read cell by cell,
    # as the same lines read without the box.
    lines = []
    for i in range(4):
        lines += [_span(40, 290, 100 + 12 * i, f"cell alpha {i} has several words"),
                  _span(306, 540, 100 + 12 * i, f"cell beta {i} has several words")]
    lines.append(_span(40, 540, 100 + 12 * 4, "a cell across both columns of the table"))
    ruled = [(30, 90, 550, 160)]
    boxed, _ = xy_cut(lines, 12.0, ruled)
    plain, _ = xy_cut(lines, 12.0)
    assert [l.text for l in boxed] == [l.text for l in plain]


def test_short_contact_lines_form_columns_across_a_heading():
    # A directory page: two-word contact lines (a city and state, a website) in two columns,
    # with a heading across both. Two words a line is still a column, not table cells.
    tops = [100 + 12 * i for i in range(3)] + [136 + 12 * i for i in range(4)]
    left = [_span(40, 190, t, f"addr {i}") for i, t in enumerate(tops)]
    right = [_span(306, 460, t, f"phone {i}") for i, t in enumerate(tops)]
    heading = _span(40, 460, 128, "Full width heading")
    assert _order(left + right + [heading]) == (
        [f"addr {i}" for i in range(3)] + [f"phone {i}" for i in range(3)] + ["Full width heading"]
        + [f"addr {i}" for i in range(3, 7)] + [f"phone {i}" for i in range(3, 7)])

def test_a_banner_wrapped_over_two_lines_is_one_breadcrumb(tmp_path):
    # maricopa p2258: the banner "PSYCHIATRIC NURSE / PRACTITIONER (continued)" wraps onto two
    # lines, and "Phoenix (continued)" is a second banner under it.
    nodes = layout(tmp_path, [[
        (72, 100, 14, "PSYCHIATRIC NURSE", "B"),
        (72, 116, 14, "PRACTITIONER (continued)", "B"),
        (72, 140, 12, "Phoenix (continued)", "B"),
        (72, 170, 10, "Smith, Ann, FNP (F)", "B"),
        body(182, "1 Main St, Phoenix, AZ 85004. Office hours are Monday through Friday."),
        body(194, "Accepting new patients under most plans, including the Blue plans."),
    ]])
    nodes = [n for n in nodes if n.role != Role.ARTIFACT]
    assert nodes[0].text == "Continued: Psychiatric Nurse Practitioner › Phoenix"
    assert nodes[0].attrs["trail"] == ["Psychiatric Nurse Practitioner", "Phoenix"]
    assert nodes[1].role.startswith("h") and nodes[1].text == "Smith, Ann, FNP (F)"


def test_a_centred_banner_with_bare_continued_lines_is_one_breadcrumb(tmp_path):
    # maricopa p2258: each line of the black banner sits at its own indent, and "(continued)"
    # is set on a line of its own under "PRACTITIONER" and under "Phoenix".
    nodes = layout(tmp_path, [[
        (72, 100, 14, "PSYCHIATRIC NURSE", "B"),
        (84, 116, 14, "PRACTITIONER", "B"),
        (96, 132, 14, "(continued)", "B"),
        (88, 160, 12, "Phoenix", "B"),
        (96, 176, 12, "(continued)", "B"),
        (72, 210, 10, "Smith, Ann, FNP (F)", "B"),
        body(222, "1 Main St, Phoenix, AZ 85004. Office hours are Monday through Friday."),
        body(234, "Accepting new patients under most plans, including the Blue plans."),
    ]])
    nodes = [n for n in nodes if n.role != Role.ARTIFACT]
    assert nodes[0].text == "Continued: Psychiatric Nurse Practitioner › Phoenix"
    assert nodes[0].attrs["trail"] == ["Psychiatric Nurse Practitioner", "Phoenix"]
    assert all("Continued: Practitioner" != n.text and "Continued: Phoenix" != n.text
               for n in nodes)


def test_a_title_across_a_form_header_stays_one_unit_beside_the_columns():
    # A form header: a column of small labels on the left, a box on the right, and a two-line
    # title in the middle, set larger. The title reads as one unit, never woven into the labels.
    labels = [_span(40, 150, 34 + 12 * i, f"label {i}", 8) for i in range(5)]
    box = [_span(470, 560, 40 + 12 * i, f"box line {i}", 8) for i in range(4)]
    title = [_span(200, 440, 36, "Request for Taxpayer", 14),
             _span(180, 460, 54, "Identification Number", 14)]
    left = [_span(40, 290, 110 + 12 * i, f"left column line {i} of prose", 8) for i in range(6)]
    right = [_span(306, 560, 110 + 12 * i, f"right column line {i} of prose", 8) for i in range(6)]
    text = _order(labels + box + title + left + right)
    at = text.index("Request for Taxpayer")
    assert text[at + 1] == "Identification Number"
    assert not any(t.startswith(("label", "box line")) for t in text[at:at + 2])
    assert text.index("left column line 0 of prose") > at + 1
    assert sorted(text) == sorted(t.text for t in labels + box + title + left + right)


def test_a_hyphen_at_a_line_end_does_not_join_the_line_of_another_column():
    # "afford-" ends a line of the left column; the next line in reading order is in the right
    # column. The word is not rejoined across the columns.
    from reflow.analyze.breaks import make_blocks
    left = _span(40, 290, 100, "help make afford-", 10)
    right = _span(306, 540, 100, "rural homes for people", 10)
    blocks = make_blocks([left, right])
    assert [[l.text for l in b.lines] for b in blocks] == [["help make afford-"],
                                                          ["rural homes for people"]]


# --- form rows and rotated margin text -----------------------------------------------------

def test_form_row_label_on_the_right_reads_with_its_left_piece():
    # A field line crosses the gutter (a soft gutter), and a signature line sits below the columns:
    # "U.S. person" on the left and "Date" on the right, on one baseline. The date reads right after
    # its label, not after the whole right column.
    crossing = _span(40, 330, 100, "a field line that partly crosses the gutter")
    left = [_span(40, 290, 124 + 12 * i, f"left column line {i} of prose") for i in range(8)]
    right = [_span(316, 560, 124 + 12 * i, f"right column line {i} of prose") for i in range(6)]
    sign = [_span(40, 110, 216, "U.S. person"), _span(400, 430, 216, "Date")]
    assert _order([crossing] + left + right + sign) == (
        ["a field line that partly crosses the gutter"]
        + [f"left column line {i} of prose" for i in range(8)]
        + ["U.S. person", "Date"]
        + [f"right column line {i} of prose" for i in range(6)])


def test_form_row_across_a_clean_gutter_reads_together():
    # Two columns with no line crossing the gutter, and a signature line at the foot of the right one:
    # "U.S. person" and "Date" read as one row, not after the whole left column.
    left = [_span(40, 290, 124 + 12 * i, f"left column line {i} of prose") for i in range(8)]
    right = [_span(316, 560, 124 + 12 * i, f"right column line {i} of prose") for i in range(6)]
    sign = [_span(40, 110, 216, "U.S. person"), _span(400, 430, 216, "Date")]
    assert _order(left + right + sign) == (
        [f"left column line {i} of prose" for i in range(8)] + ["U.S. person", "Date"]
        + [f"right column line {i} of prose" for i in range(6)])


def test_a_label_beside_a_wrapped_line_is_not_read_inside_the_sentence():
    # p17 p43: a flowchart's "No" sits on the baseline of a box's wrapped line "withholding and
    # refundable", but the box's sentence goes on below it: "No" is not read in its middle.
    left = [_span(40, 290, 124 + 12 * i, f"left column line {i} of prose") for i in range(8)]
    right = [_span(316, 560, 124 + 12 * i, f"right column line {i} of prose") for i in range(6)]
    box = [_span(40, 160, 216, "withholding and refundable"), _span(40, 150, 228, "credits be at least")]
    order = _order(left + right + box + [_span(400, 412, 216, "No")])
    assert order.index("credits be at least") == order.index("withholding and refundable") + 1


def test_directory_rows_across_the_gutter_keep_their_columns():
    # Short lines of two directory columns share baselines, but each right line has a right-hand
    # neighbour above and below it: no row is joined, the columns read one after the other.
    crossing = _span(40, 330, 100, "a heading line that partly crosses the gutter")
    left = [_span(40, 120, 124 + 12 * i, f"city {i}") for i in range(8)]
    right = [_span(316, 380, 124 + 12 * i, f"phone {i}") for i in range(8)]
    assert _order([crossing] + left + right) == (
        ["a heading line that partly crosses the gutter"]
        + [f"city {i}" for i in range(8)] + [f"phone {i}" for i in range(8)])


def _page(number, lines):
    from reflow.extract.pages import PageContent
    return PageContent(number, 612.0, 792.0, lines, images=[], drawings=[])


def _rotated(number, x0, top, text):
    from reflow.extract.lines import Line
    return Line(number, (x0, top, x0 + 9.0, top + 9.0 * len(text) * 0.5), text, 8.0, False,
                False, "Helvetica", rotated=True, turn=1)


def test_rotated_side_tab_repeated_on_every_page_is_furniture():
    from reflow.analyze.artifacts import artifact_reasons
    pages = [_page(n, [_rotated(n, 20, 300, "Pima County"), _span(72, 100, 100, f"body {n}")])
             for n in range(1, 5)]
    tabs = [l for p in pages for l in p.lines if l.rotated]
    reasons = artifact_reasons(pages)
    assert [reasons[id(l)] for l in tabs] == ["rotated text (margin stamp)"] * 4


def test_rotated_instruction_on_one_page_is_content_read_in_its_place():
    from reflow.analyze.artifacts import artifact_reasons
    from reflow.analyze.layout import analyze
    stamp = _rotated(1, 34, 154, "Print or type. See Specific Instructions on page 3.")
    page = _page(1, [_span(72, 300, 100, "1 Name of entity"), stamp,
                     _span(72, 300, 200, "3a Check the appropriate box")])
    assert id(stamp) not in artifact_reasons([page])
    text = [n.text for n in analyze([page]) if n.role != Role.ARTIFACT]
    instruction = text.index("Print or type. See Specific Instructions on page 3.")
    assert text.index("1 Name of entity") < instruction < text.index("3a Check the appropriate box")


def test_a_block_of_rotated_rows_repeating_on_every_page_is_content():
    # A sideways table printed the same way on several pages: its rows are a block, not a stamp,
    # so none is dropped, and the rows read in their turned order (here, left to right).
    from reflow.analyze.artifacts import artifact_reasons
    from reflow.analyze.layout import analyze
    pages = []
    for n in range(1, 5):
        rows = [_rotated(n, 100 + 12 * i, 150, f"row {i} of the table") for i in range(8)]
        pages.append(_page(n, rows))
    assert artifact_reasons(pages) == {}
    text = " ".join(x.text for x in analyze(pages[:1]) if x.role != Role.ARTIFACT)
    assert text == " ".join(f"row {i} of the table" for i in range(8))


def test_a_running_footer_turned_on_landscape_pages_is_still_furniture():
    # census: 10 portrait pages print the footer at the same height; 3 landscape pages, read as
    # displayed, carry it sideways mid-way down the edge. Both kinds are page furniture.
    from reflow.analyze.artifacts import artifact_reasons
    footer = "Income and Poverty in the United States: 2020"
    pages = [_page(n, [_span(72, 300, 100, f"body {n}"), _span(400, 560, 746, f"{footer} {n}")])
             for n in range(1, 11)]
    for n in range(11, 14):
        page = _page(n, [_span(72, 300, 100, f"body {n}"), _rotated(n, 760, 336, f"{footer} {n}")])
        page.width, page.height = 792.0, 612.0
        pages.append(page)
    footers = [l for p in pages for l in p.lines if l.text.startswith(footer)]
    reasons = artifact_reasons(pages)
    assert all(id(l) in reasons for l in footers)
    assert all(reasons[id(l)].startswith("running header or footer") for l in footers
               if not l.rotated)


def test_a_sideways_axis_label_is_not_a_heading():
    # resnet p8: rotated axis labels, "error (%)" at 5 pt and "std" at 6 pt. On their own
    # pseudo-page "std" is the larger type, yet it is a chart label, not a heading.
    from reflow.analyze.layout import analyze
    from reflow.extract.lines import Line

    def label(x0, top, bottom, text, size):
        return Line(1, (x0, top, x0 + 4.6, bottom), text, size, False, False, "Times-Roman",
                    rotated=True, turn=1)

    page = _page(1, [_span(72, 300, 400 + 12 * i, f"body line {i} of the page text")
                     for i in range(6)]
                 + [label(100.6, 109, 127.7, "error (%)", 5.0),
                    label(264.2, 109, 127.8, "error (%)", 5.0),
                    label(417.8, 109, 127.8, "error (%)", 5.0),
                    label(62.2, 290.8, 297.8, "std", 6.0), label(62.2, 232.1, 239.1, "std", 6.0)])
    nodes = [n for n in analyze([page]) if n.role != Role.ARTIFACT]
    assert any("std" in n.text for n in nodes)
    assert not any(n.role.startswith("h") for n in nodes)


def _index(entries):
    """An index page: (x, top, text) entries at 8 pt, enough of them end in page numbers."""
    return [(x, top, 8, text) for x, top, text in entries]


def test_index_sub_entries_nest_under_their_main_entry(tmp_path):
    # Main entries at x=72, sub-entries indented under them (p17 p138).
    entries = [(72, 100, "Passive activity:"), (84, 110, "Losses 23"),
               (72, 120, "Patents:"), (84, 130, "Royalties 72"), (84, 140, "Infringement 74"),
               (72, 150, "Pass-through entities 102")]
    entries += [(72, 160 + 10 * k, f"Entry number {k} {40 + k}") for k in range(6)]
    nodes = layout(tmp_path, [_index(entries)])
    (index,) = [n for n in nodes if n.role == Role.LIST]
    assert [c.text for c in index.children][:3] == ["Passive activity:", "Patents:",
                                                    "Pass-through entities 102"]
    passive, patents = index.children[0], index.children[1]
    assert [c.text for c in passive.children[0].children] == ["Losses 23"]
    assert [c.text for c in patents.children[0].children] == ["Royalties 72", "Infringement 74"]


def test_a_wrapped_index_entry_is_one_item(tmp_path):
    # p17 p138: "Decedent's unrecovered" / "investment in 14" is one entry at the hanging indent.
    entries = [(72, 100, "Decedent's unrecovered"), (84, 110, "investment in 14"),
               (72, 120, "Benefits from previous"), (84, 130, "employer's plan 80")]
    entries += [(72, 140 + 10 * k, f"Entry number {k} {40 + k}") for k in range(8)]
    nodes = layout(tmp_path, [_index(entries)])
    (index,) = [n for n in nodes if n.role == Role.LIST]
    assert index.children[0].text == "Decedent's unrecovered investment in 14"
    assert index.children[1].text == "Benefits from previous employer's plan 80"


def test_a_wrapped_index_entry_whose_last_word_has_a_leader_is_one_item(tmp_path):
    # budget p1285: the title wraps, and its last word carries the dot leader and page label on
    # the line below, at the hanging indent. The leader line is not an entry of its own.
    entries = [(30, 100, "Nonproliferation, Antiterrorism, Demining, and Related"),
               (36, 110, "Programs" + "." * 40 + " 777")]
    entries += [(30, 130 + 10 * k, f"Entry number {k} {40 + k}") for k in range(9)]
    nodes = layout(tmp_path, [_index(entries)])
    (index,) = [n for n in nodes if n.role == Role.LIST]
    assert len(index.children) == 10
    assert index.children[0].text.startswith(
        "Nonproliferation, Antiterrorism, Demining, and Related Programs")
    assert index.children[1].text == "Entry number 0 40"


def test_a_leader_line_under_an_entry_with_a_page_label_is_its_own_entry(tmp_path):
    # The entry above already ends in its page label, so the indented leader line starts a new one.
    entries = [(30, 100, "Losses 23"), (36, 110, "Programs" + "." * 40 + " 777")]
    entries += [(30, 130 + 10 * k, f"Entry number {k} {40 + k}") for k in range(9)]
    nodes = layout(tmp_path, [_index(entries)])
    items = [c.text for n in nodes if n.role == Role.LIST for c in n.children]
    assert items[:2] == ["Losses 23", "Programs"]


def test_lettered_sub_items_nest_under_their_number(tmp_path):
    # p17 p25: 8. with lettered a./b. indented under it, then 9. with its own a./b.
    nodes = layout(tmp_path, [[
        (60, 100, 10, "8. If you lived with your spouse at any time"),
        (72, 112, 10, "a. You can't claim the credit for the elderly, and"),
        (72, 124, 10, "b. You must include in income a greater percentage."),
        (60, 136, 10, "9. The following credits are reduced at income levels."),
        (72, 148, 10, "a. The child tax credit and the credit for other dependents."),
        (72, 160, 10, "b. The retirement savings contributions credit."),
    ]])
    (lst,) = [n for n in nodes if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in lst.children] == ["8.", "9."]
    assert [s.attrs["marker"] for s in lst.children[0].children[0].children] == ["a.", "b."]
    assert [s.attrs["marker"] for s in lst.children[1].children[0].children] == ["a.", "b."]


def _long(text: str) -> str:
    """Pads a legal item's first line to the column edge, so the layout sees it fill the line."""
    return text + " " + "word " * max(0, 44 - len(text.split()))


def test_legal_enumerations_nest_by_level(tmp_path):
    # Rule 26(a)(1)(A) and (B): (a) > (1) > (A) > (i), with (B) a sibling of (A) and (i)-(ii)
    # under (A). Each level indents further, and the wrapped lines of (i) return to the margin.
    nodes = layout(tmp_path, [[
        (156, 100, 9, "(a) Required disclosures."),
        (174, 112, 9, "(1) Initial disclosure."),
        (192, 124, 9, _long("(A) In general. Except as exempted a party must provide")),
        (183, 136, 9, "to the other parties the following information:"),
        (210, 148, 9, _long("(i) the name and address of each individual likely to")),
        (201, 160, 9, "have discoverable information, and the subjects of it;"),
        (210, 172, 9, _long("(ii) a copy of all documents and tangible things")),
        (192, 184, 9, _long("(B) Proceedings exempt from initial disclosure. The following")),
        (210, 196, 9, _long("(i) an action for review on an administrative record;")),
    ]])
    (top,) = [n for n in nodes if n.role == Role.LIST]
    (a,) = top.children
    assert a.attrs["marker"] == "(a)" and a.text == "Required disclosures."
    (one,) = [c for c in a.children if c.role == Role.LIST]
    (lvl1,) = one.children
    assert lvl1.attrs["marker"] == "(1)"
    (lst1,) = [c for c in lvl1.children if c.role == Role.LIST]
    assert [c.attrs["marker"] for c in lst1.children] == ["(A)", "(B)"]
    a_item, b_item = lst1.children
    assert a_item.text.endswith("to the other parties the following information:")
    (sub_a,) = [c for c in a_item.children if c.role == Role.LIST]
    assert [c.attrs["marker"] for c in sub_a.children] == ["(i)", "(ii)"]
    assert sub_a.children[0].text.endswith("have discoverable information, and the subjects of it;")
    (sub_b,) = [c for c in b_item.children if c.role == Role.LIST]
    assert [c.attrs["marker"] for c in sub_b.children] == ["(i)"]


def test_uppercase_legal_label_can_lead_lowercase_roman_subitems(tmp_path):
    nodes = layout(tmp_path, [[
        (165, 100, 9, "(1) Appointing Class Counsel. A court must consider:"),
        (183, 112, 9, "(A) must consider:"),
        (201, 124, 9, "(i) the work counsel has done in identifying potential claims;"),
    ]])
    items = _li_nodes(nodes)
    assert [item.attrs["marker"] for item in items] == ["(1)", "(A)", "(i)"]
    assert items[1].children[0].children[0].attrs["marker"] == "(i)"


def _li_nodes(nodes):
    found = []
    for n in nodes:
        if n.role == Role.LI:
            found.append(n)
        found += _li_nodes(n.children)
    return found


def test_a_legal_run_continues_across_a_page_break(tmp_path):
    # "(a)" ends one page and "(b)" starts the next: one run, so both are list items with
    # their labels kept as markers, not two paragraphs.
    nodes = layout(tmp_path, [
        [(156, 600, 9, _long("(a) Real party in interest. A party must bring the action in"))],
        [(156, 100, 9, _long("(b) Capacity. A party may sue as the representative of others"))],
    ])
    items = _li_nodes(nodes)
    assert [i.attrs["marker"] for i in items] == ["(a)", "(b)"]
    assert items[1].text.startswith("Capacity.")


def test_a_roman_run_is_numerals_and_a_letter_run_is_letters(tmp_path):
    # "(i)" after "(h)" is a letter; "(i)" before "(ii)", and "(v)" after "(iv)", are numerals.
    letters = layout(tmp_path, [[
        body(100, "(g) first letter item here"),
        body(112, "(h) second letter item here"),
        body(124, "(i) third letter item here"),
        body(136, "(j) fourth letter item here"),
    ]])
    (lst,) = [n for n in letters if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in lst.children] == ["(g)", "(h)", "(i)", "(j)"]
    (tmp_path / "r").mkdir()
    romans = layout(tmp_path / "r", [[
        body(100, "(iii) first numeral item here"),
        body(112, "(iv) second numeral item here"),
        body(124, "(v) third numeral item here"),
        body(136, "(vi) fourth numeral item here"),
    ]])
    (lst,) = [n for n in romans if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in lst.children] == ["(iii)", "(iv)", "(v)", "(vi)"]


def test_a_roman_run_starting_at_i_is_numerals(tmp_path):
    # Rule 26(a)(1)(B): "(i)" before "(ii)" is the numeral one, under a lettered item.
    nodes = layout(tmp_path, [[
        body(100, "(B) Proceedings exempt from initial disclosure, which are these:"),
        (90, 112, 10, "(i) an action for review on an administrative record;"),
        (90, 124, 10, "(ii) a forfeiture action in rem arising from a federal statute;"),
        (90, 136, 10, "(iii) a petition for habeas corpus or any other proceeding;"),
    ]])
    (top,) = [n for n in nodes if n.role == Role.LIST]
    (b,) = top.children
    (sub,) = [c for c in b.children if c.role == Role.LIST]
    assert [c.attrs["marker"] for c in sub.children] == ["(i)", "(ii)", "(iii)"]


def test_capital_letter_before_a_roman_run_is_still_its_sibling(tmp_path):
    # "(H)" then the indented numerals (i)-(iii), then "(I)" back at the level of "(H)": the
    # letter guard must not read "(H)" as the letter before "(i)".
    from reflow.analyze.breaks import make_blocks
    from reflow.extract.pages import extract_pages
    page = [[
        (192, 100, 10, _long("(H) Proceedings exempt from initial disclosure, which are these:")),
        (210, 112, 10, _long("(i) an action for review on an administrative record;")),
        (210, 124, 10, _long("(ii) a forfeiture action in rem arising from a federal statute;")),
        (210, 136, 10, _long("(iii) a petition for habeas corpus or any other proceeding;")),
        (192, 148, 10, _long("(I) a different matter that stands at the same level;")),
    ]]
    # The numerals are roman, not letters: (i) follows an upper-case (H), not a lower-case (h).
    blocks = make_blocks(next(extract_pages(make_text_pdf(tmp_path / "b.pdf", page))).lines)
    kinds = {b.text[:4]: b.marker.kind for b in blocks if b.marker and b.text[:1] == "("}
    assert kinds["(i) "] == kinds["(ii)"] == kinds["(iii"] == "roman"
    assert kinds["(I) "] == "alpha"
    (tmp_path / "l").mkdir()
    nodes = layout(tmp_path / "l", page)
    (top,) = [n for n in nodes if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in top.children] == ["(H)", "(I)"]
    (sub,) = [c for c in top.children[0].children if c.role == Role.LIST]
    assert [c.attrs["marker"] for c in sub.children] == ["(i)", "(ii)", "(iii)"]


def test_a_legal_label_is_never_an_initial(tmp_path):
    assert marker_of("(A) In General. Except as exempted").initial is False
    assert marker_of("R. M. Hazen wrote").initial is True


def test_a_bare_number_continues_the_sentence_above_it(tmp_path):
    # p17 p25: "See chapter 1 of Pub." / "519." is one sentence, not an orphan number.
    nodes = layout(tmp_path, [[
        body(100, "for the entire tax year. See chapter 1 of Pub."),
        body(112, "519."),
    ]])
    assert [n.text for n in nodes] == ["for the entire tax year. See chapter 1 of Pub. 519."]


def test_a_short_last_line_ends_the_paragraph(tmp_path):
    # p17 p25: "...the return." is set short; "Nonresident alien..." starts a new paragraph.
    nodes = layout(tmp_path, [[
        body(100, "This is a long line of body text that runs across the full width of the column here."),
        body(112, "Short last line ends here."),
        body(124, "Nonresident alien starts the next paragraph and runs on."),
    ]])
    assert [n.text for n in nodes] == [
        "This is a long line of body text that runs across the full width of the column here. "
        "Short last line ends here.",
        "Nonresident alien starts the next paragraph and runs on."]


def test_a_title_on_an_index_like_page_stays_a_heading(tmp_path):
    # pima / maricopa: a contents page whose entries end in page numbers still has its title
    # set larger than the entries, and a bold line without a page number is not an entry.
    entries = [(72, 100, 16, "Table of Contents"), (72, 120, 8, "Passive activity: Losses 23")]
    entries += [(72, 130 + 10 * k, 8, f"Entry number {k} {40 + k}") for k in range(12)]
    nodes = layout(tmp_path, [entries])
    assert [n.text for n in nodes if n.role.startswith("h")] == ["Table of Contents"]




def test_a_short_side_tab_is_furniture_even_on_one_page():
    # "Pima County" in the outer margin: three words or fewer at the page edge is a tab, however
    # few pages it appears on. A longer sideways instruction is content (see the test above).
    from reflow.analyze.artifacts import artifact_reasons
    tab = _rotated(1, 8, 300, "Pima County")
    assert artifact_reasons([_page(1, [tab, _span(72, 100, 100, "body")])])[id(tab)] == (
        "rotated text (margin stamp)")


def test_a_table_atop_the_right_column_follows_the_left_column():
    # Budget p473: a page heading across both columns, then the left column's text; the right
    # column opens with a schedule continued from the page before. The table belongs after the
    # left column, not between the page heading and the left column's first paragraph.
    from reflow.analyze.placement import attach_tables
    from reflow.extract.tables.cells import TableData
    from reflow.model import Node
    heading = Node(Role.H2, page=1, bbox=(200, 40, 410, 56), text="DEPARTMENT OF HOMELAND SECURITY")
    left = [Node(Role.P, page=1, bbox=(36, 70 + 40 * i, 300, 100 + 40 * i), text=f"Left {i}")
            for i in range(3)]
    right = Node(Role.P, page=1, bbox=(312, 300, 576, 330), text="Right column text")
    table = TableData(1, (312, 70, 576, 280), [["3200", "Obligated balance", "185", "98", "75"]] * 3,
                      method="aligned", header_rows=0)
    nodes = attach_tables([heading, *left, right], [table])
    assert [n.text if n.role != Role.TABLE else "table" for n in nodes] == [
        heading.text, "Left 0", "Left 1", "Left 2", "table", "Right column text"]


def test_a_table_under_a_heading_in_one_column_stays_under_it():
    from reflow.analyze.placement import attach_tables
    from reflow.extract.tables.cells import TableData
    from reflow.model import Node
    heading = Node(Role.H2, page=1, bbox=(36, 40, 300, 56), text="Schedule")
    after = Node(Role.P, page=1, bbox=(36, 300, 576, 330), text="After")
    table = TableData(1, (150, 70, 576, 280), [["3200", "Obligated balance", "185", "98", "75"]] * 3,
                      method="aligned", header_rows=0)
    nodes = attach_tables([heading, after], [table])
    assert [n.text if n.role != Role.TABLE else "table" for n in nodes] == ["Schedule", "table", "After"]


def test_a_table_under_a_wide_title_stays_under_it_when_text_sits_beside_it():
    # Census p34: a title wider than its table, and a label to the right of the table (not an
    # earlier column): the table stays under its title.
    from reflow.analyze.placement import attach_tables
    from reflow.extract.tables.cells import TableData
    from reflow.model import Node
    title = Node(Role.H4, page=1, bbox=(53, 55, 320, 78), text="Title")
    beside = Node(Role.P, page=1, bbox=(331, 82, 370, 92), text="Beside")
    after = Node(Role.P, page=1, bbox=(57, 91, 381, 475), text="After")
    table = TableData(1, (153, 80, 319, 475), [["1984", "160.2"]] * 3, method="aligned", header_rows=0)
    nodes = attach_tables([title, beside, after], [table])
    assert [n.text if n.role != Role.TABLE else "table" for n in nodes] == [
        "Title", "table", "Beside", "After"]


def test_a_line_no_table_cell_holds_stays_in_the_text(tmp_path):
    # The text-loss guard: the grid's cells hold "Year" and "Revenue" only; the line
    # "Note: unaudited" lies inside the table's box and in no cell, so it must not vanish.
    from reflow.extract.tables.cells import TableData
    pages = list(extract_pages(make_text_pdf(tmp_path / "t.pdf", [[
        body(100, "Year"), (200, 100, 10, "Revenue"),
        body(114, "2024"), (200, 114, 10, "$10"),
        body(128, "Note: unaudited"),
    ]])))
    grid = [[(60, 95, 190, 110), (190, 95, 300, 110)],
            [(60, 110, 190, 124), (190, 110, 300, 124)]]
    table = TableData(1, (60, 95, 300, 140), [["Year", "Revenue"], ["2024", "$10"]],
                      method="ruled", grid=grid)
    nodes = analyze(pages, {1: [table]})
    kept = [n for n in nodes if n.role == Role.P]
    assert [n.text for n in kept] == ["Note: unaudited"]
    assert "kept in the text" in kept[0].attrs["why"][-1]
    assert nodes[0].role == Role.TABLE
    assert [c.text for tr in nodes[0].children for c in tr.children] == ["Year", "Revenue", "2024", "$10"]


def test_a_wrapped_page_reference_does_not_lead_a_list(tmp_path):
    # SCOTUS p6: "582 U. S. 449," / "462. Moreover, ..." is a citation's page wrapped to the start
    # of a line. The indented "(d)" and "(1)" items below don't make "462." the head of a list.
    nodes = layout(tmp_path, [[
        (72, 100, 10, "See Trinity Lutheran Church v. Comer, 582 U. S. 449,"),
        (72, 112, 10, "462. Moreover, the burden here is of the same character."),
        (90, 128, 10, "(d) Having concluded that the parents are likely to succeed,"),
        (108, 140, 10, "the Court turns to the remaining factors here."),
        (108, 156, 10, "(1) In most circumstances, the government is free to act,"),
        (126, 168, 10, "though not where it burdens the parents' exercise."),
    ]])
    heads = [n.children[0].attrs.get("marker") for n in nodes if n.role == Role.LIST]
    assert "462." not in heads
    assert any(n.role == Role.P and "462. Moreover" in (n.text or "") for n in nodes)


def test_a_chained_label_is_a_chain_of_markers():
    m = marker_of("(a)(1) The Judicial Conference")
    assert m.text == "(a)(1)" and m.kind == "alpha" and m.value == 1
    assert (m.inner.kind, m.inner.value) == ("num", 1)
    m = marker_of("(A)(i) the work counsel has done")
    assert m.text == "(A)(i)" and (m.inner.kind, m.inner.value) == ("roman", 1)
    assert marker_of("(d)(i) If the property").inner.kind == "roman"


def test_chained_labels_open_the_outer_item_with_the_inner_nested(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "(a)(1) The first rule applies."),
        body(112, "(2) The second rule applies."),
        body(124, "(b) The third rule applies."),
        body(136, "(c)(1) The fourth rule applies."),
    ]])
    (lst,) = [n for n in nodes if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in lst.children] == ["(a)", "(b)", "(c)"]
    assert lst.children[0].text == ""
    inner = [c for c in lst.children[0].children if c.role == Role.LIST][0]
    assert [(c.attrs["marker"], c.text) for c in inner.children] == [
        ("(1)", "The first rule applies."), ("(2)", "The second rule applies.")]
    assert [c.attrs["marker"] for c in lst.children[2].children[0].children] == ["(1)"]


def test_a_lone_legal_label_is_an_item_in_a_legal_text(tmp_path):
    run = [body(100 + 12 * i, f"({i + 1}) Rule number {i + 1} applies.") for i in range(31)]
    lone = [body(560, "(c) INITIAL ORDER. After the conference, the court enters it.")]
    nodes = layout(tmp_path, [run + lone])
    assert [n.role for n in nodes] == [Role.LIST]
    assert nodes[0].children[-1].attrs["marker"] == "(c)"
    # the same label in a text with no legal items stays a paragraph
    nodes = layout(tmp_path, [[body(100, "A paragraph that stands alone."), *lone]])
    assert [n.role for n in nodes] == [Role.P, Role.P]


def test_a_roman_legal_item_at_page_start_continues_the_list_from_the_page_before(tmp_path):
    nodes = layout(tmp_path, [
        [(165, 100, 9, "(1) Appointing Class Counsel. A court must consider:"),
         (183, 112, 9, "(A) must consider:"),
         (201, 124, 9, "(i) the work counsel has done in identifying potential claims;")],
        [body(100, "(ii) are satisfied if, during the stated period, process was delivered or mailed"),
         body(112, "to the United States attorney, the Attorney General, or the agency.")],
    ])
    items = _li_nodes(nodes)
    assert [item.attrs["marker"] for item in items] == ["(1)", "(A)", "(i)", "(ii)"]


def test_a_roman_label_at_page_start_without_a_list_before_it_stays_a_paragraph(tmp_path):
    nodes = layout(tmp_path, [
        [body(100, "An ordinary paragraph that ends the first page and is not a list item at all.")],
        [body(100, "(ii) are satisfied if, during the stated period, process was delivered or mailed"),
         body(112, "to the United States attorney, the Attorney General, or the agency.")],
    ])
    assert _li_nodes(nodes) == []


def test_uppercase_legal_siblings_stay_flat(tmp_path):
    nodes = layout(tmp_path, [[
        (165, 100, 9, "(1) Appointing Class Counsel. A court must consider:"),
        (183, 112, 9, "(A) the first thing the court must consider;"),
        (183, 124, 9, "(B) the second thing the court must consider;"),
        (183, 136, 9, "(C) the third thing the court must consider; and"),
        (183, 148, 9, "(D) the fourth thing the court must consider."),
    ]])
    items = _li_nodes(nodes)
    assert [i.attrs["marker"] for i in items] == ["(1)", "(A)", "(B)", "(C)", "(D)"]
    top = next(i for i in items if i.attrs["marker"] == "(1)")
    assert [c.attrs["marker"] for c in top.children[0].children] == ["(A)", "(B)", "(C)", "(D)"]
    assert all(not any(n.role == Role.LI for n in c.children[0].children)
               for c in top.children[0].children if c.children and c.children[0].children)


def test_a_worksheet_answer_stays_with_its_lettered_row(tmp_path):
    nodes = layout(tmp_path, [[
        (72, 100, 10, "A. Enter the total amount from box 5"),
        (84, 112, 10, "of all your forms. ......"),
        (300, 112, 10, "A. $1,500"),
        (72, 130, 10, "Note: A note stops the row."),
        (72, 148, 10, "B. Multiply line A by 50%"),
        (300, 148, 10, "B."),
        (340, 148, 10, "750"),
        (72, 166, 10, "C. Add lines A and B ......"),
        (300, 166, 10, "C. $2,250"),
    ]])
    texts = [n.text for n in nodes if n.role == Role.P]
    assert any("of all your forms. ...... $1,500" in t for t in texts)
    assert any(t.startswith("B. Multiply line A by 50%") and t.endswith("750") for t in texts)
    assert any(t.startswith("C. Add") and t.endswith("$2,250") for t in texts)
    assert "Note: A note stops the row." in texts


def test_lettered_lines_without_answer_boxes_are_not_a_worksheet(tmp_path):
    pages = list(extract_pages(make_text_pdf(tmp_path / "t.pdf", [[
        (72, 100, 10, "A. Introduction to the plan"),
        (72, 118, 10, "B. Scope of the plan"),
        (72, 136, 10, "C. Cost of the plan"),
    ]])))
    _, labels, members = worksheet_rows(pages[0].lines)
    assert labels == {} and members == set()


def test_side_by_side_lettered_columns_are_not_a_worksheet(tmp_path):
    pages = list(extract_pages(make_text_pdf(tmp_path / "t.pdf", [[
        (72, 100, 10, "A. Introduction to the topic"),
        (72, 118, 10, "B. Background and context"),
        (72, 136, 10, "C. Methods that were used"),
        (330, 100, 10, "A. Results in summary"),
        (330, 118, 10, "B. Discussion of results"),
        (330, 136, 10, "C. Conclusions drawn"),
    ]])))
    _, labels, members = worksheet_rows(pages[0].lines)
    assert labels == {} and members == set()


def test_a_chained_outer_label_stays_open_past_a_long_inner_run(tmp_path):
    nodes = layout(tmp_path, [[
        body(100, "(a)(1) The first rule applies."),
        body(112, "(2) The second rule applies."),
        body(124, "(3) The third rule applies."),
        body(136, "(4) The fourth rule applies."),
        body(148, "(b) The fifth rule applies."),
    ]])
    (lst,) = [n for n in nodes if n.role == Role.LIST]
    assert [c.attrs["marker"] for c in lst.children] == ["(a)", "(b)"]
    inner = [c for c in lst.children[0].children if c.role == Role.LIST][0]
    assert [c.attrs["marker"] for c in inner.children] == ["(1)", "(2)", "(3)", "(4)"]


def test_a_citation_fragment_is_not_promoted_to_a_legal_item(tmp_path):
    run = [body(100 + 12 * i, f"({i + 1}) Rule number {i + 1} applies.") for i in range(31)]
    frag = [body(560, "(a) of this section and to any motion filed in the court.")]
    nodes = layout(tmp_path, [run + frag])
    items = [c for n in nodes if n.role == Role.LIST for c in n.children]
    assert all(c.attrs["marker"] != "(a)" for c in items)
    assert any(n.role == Role.P and n.text.startswith("(a) of this section") for n in nodes)


# --- month grids ---------------------------------------------------------------------------

def _calendar_page(**kw):
    from calendarpage import _line as cal_line, month_lines
    lines = (month_lines(2024, 7, 40, 100, **kw) + month_lines(2024, 8, 270, 100)
             + [cal_line(40, 300, 220, 310, "COLOR KEY: green is a holiday", 9.0)])
    return PageContent(1, 612, 792, images=[], lines=lines)


def _months(nodes):
    return [n for n in nodes if n.role == Role.TABLE]


def test_each_printed_month_is_a_table_with_a_caption_and_every_line_has_one_owner():
    page = _calendar_page()
    nodes = analyze([page])
    months = _months(nodes)
    assert [m.children[0].text for m in months] == ["JULY 2024", "AUGUST 2024"]
    assert all(m.children[0].role == Role.CAPTION for m in months)
    owners = {}
    for m in months:
        for cell in (c for tr in m.children[1:] for c in tr.children if c.text):
            owners.setdefault(cell.text, []).append(m.children[0].text)
    assert owners["01"] == ["JULY 2024", "AUGUST 2024"] and owners["31"] == ["JULY 2024", "AUGUST 2024"]
    flow = [n.text for n in nodes if n.role not in (Role.TABLE,)]
    assert flow == ["COLOR KEY: green is a holiday"]
    # 7 letters + 31 + 7 + 31 days leave the flow once, as cells; the key stays once, as text
    texts = [c.text for m in months for tr in m.children[1:] for c in tr.children if c.text]
    assert len(texts) == 2 * 7 + 31 + 31


def test_month_cells_and_captions_have_boxes_and_distinct_ids():
    page = _calendar_page()
    months = _months(analyze([page]))
    for m in months:
        cap = m.children[0]
        assert cap.bbox is not None and cap.text.replace(" ", "") in {
            "".join(l.text.split()) for l in page.lines if l.bbox == cap.bbox}
        for tr in m.children[1:]:
            for cell in tr.children:
                assert (cell.bbox is not None) == bool(cell.text)
                assert len(cell.attrs["cell"]) == 4
    cells = [c for m in months for tr in m.children[1:] for c in tr.children if c.bbox]
    assert len({c.id for c in cells}) == len(cells)
    first = months[0].children[2].children[1]
    assert first.text == "01" and first.bbox in [l.bbox for l in page.lines if l.text == "01"]


def test_month_cells_survive_the_document_round_trip():
    from reflow.model import Document, PageInfo
    root = Node(Role.DOC, children=_months(analyze([_calendar_page()])))
    again = Document.from_dict(Document(root=root, pages=[PageInfo(1, 612, 792)]).to_dict())
    cells = [c for t in again.root.children for tr in t.children[1:] for c in tr.children if c.text]
    assert len(cells) == 2 * 7 + 31 + 31 and all(c.bbox and len(c.attrs["cell"]) == 4 for c in cells)
    assert [t.children[0].bbox for t in again.root.children] == [t.children[0].bbox for t in root.children]
    assert again.root.children[0].children[0].role == Role.CAPTION


def test_a_month_that_fails_the_weekday_arithmetic_is_still_published_as_printed():
    months = _months(analyze([_calendar_page(shift=1)]))
    july = months[0]
    assert [f.code for f in july.facts if f.code != "prior"] == ["calendar-grid-mismatch"]
    assert "date 1 is in the Tuesday column" in july.facts[-1].detail
    assert not [f for f in months[1].facts if f.code == "calendar-grid-mismatch"]
    assert any(c.text == "31" for tr in july.children[1:] for c in tr.children)


def test_a_block_carries_its_lines_unicode_errors_to_the_review_queue():
    from reflow.extract.lines import Line
    from reflow.extract.pages import PageContent
    bad = Line(1, (40, 100, 240, 112), "ध्यान द यद आप", 11, False, False, "Mangal",
               unicode_errors=2)
    good = Line(1, (40, 200, 240, 212), "Plain paragraph here.", 11, False, False, "Arial")
    nodes = [n for n in analyze([PageContent(1, 612, 792, lines=[bad, good], images=[])])
             if n.role != Role.ARTIFACT]
    assert {n.text: n.attrs.get("unicode_errors") for n in nodes} == {
        "ध्यान द यद आप": 2, "Plain paragraph here.": None}


# OCR words read inside a picture of a scanned page move into the figure as its label nodes.
SCAN_BOX = (0.0, 0.0, 612.0, 792.0)
PHOTO_BOX = (100.0, 300.0, 400.0, 500.0)


def _scan_line(bbox, text, ocr=True):
    from reflow.extract.lines import Line
    return Line(1, bbox, text, 10.0, False, False, "GlyphLessFont", ocr=ocr)


def _photo_page(lines, image=SCAN_BOX):
    from reflow.extract.scan import InkRegion, PageScan, ScannedObject
    scan = PageScan("scanned", (ScannedObject(image, "text", (), 0.05),),
                    (InkRegion(PHOTO_BOX, "picture"),))
    return PageContent(1, 612, 792, lines, [image], scan=scan)


def _flow_text(nodes):
    return [n.text for top in nodes if top.role != Role.FIGURE for n in top.walk() if n.text]


def test_ocr_lines_inside_a_picture_become_its_label_nodes():
    letter = _scan_line((72, 100, 500, 112), "Honorable President and Members of the Board")
    noise = [_scan_line((150, 350, 220, 362), "T2 — -"),
             _scan_line((360, 420, 430, 432), "d L >"),   # crosses the right edge, centre inside
             _scan_line((392, 460, 470, 472), "M E")]     # centre outside: stays in the flow
    nodes = analyze([_photo_page([letter, *noise])])

    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    labels = [c for c in figure.children if c.attrs.get("figure_label")]
    assert [(c.text, c.bbox) for c in labels] == [("T2 — -", noise[0].bbox), ("d L >", noise[1].bbox)]
    assert all(c.role == Role.P and c.source.value == "ocr" for c in labels)
    assert "ocr-in-picture" in {f.code for f in figure.facts}
    flow = " ".join(_flow_text(nodes))
    assert "T2" not in flow and "d L >" not in flow
    assert "Honorable President" in flow and "M E" in flow


def test_live_lines_inside_a_scanned_picture_stay_in_the_flow():
    # A born-digital page: a large image under live text. (A page-size raster would make every
    # line on the page an OCR line, see layout._prepare.)
    title = _scan_line((150, 380, 350, 400), "Annual Report 2024", ocr=False)
    nodes = analyze([_photo_page([title], (0.0, 0.0, 612.0, 600.0))])
    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert not [c for c in figure.children if c.attrs.get("figure_label")]
    assert "ocr-in-picture" not in {f.code for f in figure.facts}
    assert "Annual Report 2024" in _flow_text(nodes)


def test_repeated_label_words_each_keep_their_own_box():
    twins = [_scan_line((120, 320, 200, 332), "PUMP"), _scan_line((300, 470, 380, 482), "PUMP")]
    nodes = analyze([_photo_page(twins)])
    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert [(c.text, c.bbox) for c in figure.children] == [("PUMP", t.bbox) for t in twins]


def test_scan_pictures_grouped_under_one_caption_keep_only_their_own_labels():
    # Two photos under one caption on a born-digital page: the group keeps the OCR read inside
    # each photo, not every line in the combined box. Live text over a photo and OCR text in
    # the gutter between them stay in the flow.
    from reflow.extract.scan import InkRegion, PageScan, ScannedObject
    image = (0.0, 0.0, 612.0, 600.0)
    left, right = (100.0, 100.0, 280.0, 300.0), (290.0, 100.0, 470.0, 300.0)
    lines = [_scan_line((120, 150, 260, 162), "Section Two Overview", ocr=False),
             _scan_line((276, 200, 294, 212), "Rd xa"),          # centred in the gutter
             _scan_line((300, 150, 380, 162), "T2 — -"),          # read inside the right photo
             _scan_line((100, 310, 470, 322), "Figure 1. Two views of the plant", ocr=False)]
    scan = PageScan("scanned", (ScannedObject(image, "text", (), 0.05),),
                    (InkRegion(left, "picture"), InkRegion(right, "picture")))
    nodes = analyze([PageContent(1, 612, 792, lines, [image], scan=scan)])

    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.bbox == (100.0, 100.0, 470.0, 300.0)
    assert [c.text for c in figure.children if c.attrs.get("figure_label")] == ["T2 — -"]
    flow = _flow_text(nodes)
    assert "Section Two Overview" in flow and "Rd xa" in flow


# Unread figures: a pen region of the page scan is a crop with reflow's note, no alt to write.
def _unread_page(lines, regions, objects=None, words=None):
    from reflow.extract.scan import PageScan, ScannedObject
    objects = objects or (ScannedObject(SCAN_BOX, "text", (), 0.05),)
    return PageContent(1, 612, 792, lines, [o.bbox for o in objects],
                       scan=PageScan("scanned", tuple(objects), tuple(regions)), words=words)


PEN_BOX = (300.0, 200.0, 420.0, 240.0)
LETTER = [_scan_line((72, 100, 500, 112), "Honorable President and Members of the Board"),
          _scan_line((72, 300, 500, 312), "Respectfully submitted to the Board this day.")]


@pytest.mark.parametrize("share, kind, code, handwritten", [
    (0.95, "handwriting", "unread-handwriting", True), (None, "unread-text", "unread-text", False)])
def test_an_unread_figure_says_what_reflow_knows(share, kind, code, handwritten):
    from reflow.analyze.review import queue
    from reflow.extract.scan import InkRegion
    from reflow.model import Document, PageInfo
    nodes = analyze([_unread_page(LETTER, [InkRegion(PEN_BOX, "pen", share)])])
    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["unread"] is True and figure.attrs["kind"] == kind
    assert figure.attrs["handwritten"] is handwritten
    assert figure.attrs["alt"] == ("Handwriting, not transcribed." if handwritten
                                   else "Writing that OCR could not read, not transcribed.")
    assert "needs" not in figure.attrs
    doc = Document(Node(Role.DOC, children=nodes), [PageInfo(1, 612, 792)], {"lang": "en"})
    [item] = [i for i in queue(doc) if i["id"] == figure.id]
    codes = {f.code for f in figure.facts}
    assert code in codes and "alt-missing" not in codes
    assert item["targets"] == [{"code": code, "box": list(figure.bbox)}]


def test_a_ledger_strip_that_turns_unread_needs_no_alt_and_keeps_its_caption():
    from reflow.extract.scan import InkRegion, ScannedObject
    strip = (72.0, 150.0, 540.0, 400.0)
    caption = _scan_line((72, 410, 400, 422), "Figure 1. The permit ledger for 1988")
    nodes = analyze([_unread_page([LETTER[0], caption],
                                  [InkRegion((80.0, 160.0, 530.0, 390.0), "pen")],
                                  (ScannedObject(strip, "plain", (), 0.3),))])
    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.bbox == strip and figure.attrs["unread"] is True
    assert "needs" not in figure.attrs and not any(f.code == "alt-missing" for f in figure.facts)
    assert [c.role for c in figure.children] == [Role.CAPTION]


def test_a_read_line_inside_a_pen_region_stays_in_the_flow_by_position():
    from reflow.extract.scan import InkRegion
    printed = _scan_line((305, 205, 400, 217), "President")
    nodes = analyze([_unread_page([LETTER[0], printed, LETTER[1]],
                                  [InkRegion((300.0, 190.0, 420.0, 250.0), "pen", 0.95)])])
    tops = [n for n in nodes if n.role != Role.ARTIFACT]
    assert "President" in _flow_text(nodes)
    order = [n.role if n.role == Role.FIGURE else n.text for n in tops]
    assert order.index(Role.FIGURE) < order.index("Respectfully submitted to the Board this day.")
    assert order.index("Honorable President and Members of the Board") < order.index(Role.FIGURE)


# Signatures: a pen region on a short line or a ruling is a signature crop placed before it.
def _role(bbox, text, conf=90):
    from reflow.extract.ocrwords import GradedWord
    from reflow.ingest.ocr_trust import Grade
    grade = Grade.READ if conf >= 80 else Grade.DOUBT
    return replace(_scan_line(bbox, text), grades=tuple(
        GradedWord(bbox, w, w, conf, grade, None, 1.0) for w in text.split()))


PRESIDENT = (320.0, 252.0, 380.0, 264.0)   # 12 pt under PEN_BOX


@pytest.mark.parametrize("conf, alt, signs", [(90, "Signature above “President”.", "President"),
                                              (78, "Signature.", None)])
def test_a_signature_is_a_crop_with_a_warning_and_no_alt_to_write(conf, alt, signs):
    from reflow.analyze.review import queue
    from reflow.extract.scan import InkRegion
    from reflow.model import Document, PageInfo
    page = _unread_page([LETTER[0], _role(PRESIDENT, "President", conf), LETTER[1]],
                        [InkRegion(PEN_BOX, "pen", None)])
    nodes = analyze([page])
    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.attrs["kind"] == "signature" and figure.attrs["alt"] == alt
    assert figure.attrs.get("signs") == signs   # the printed line a name read by a model may match
    assert figure.attrs["unread"] is True and figure.attrs["handwritten"] is True
    assert "needs" not in figure.attrs
    doc = Document(Node(Role.DOC, children=nodes), [PageInfo(1, 612, 792)], {"lang": "en"})
    [item] = [i for i in queue(doc) if i["id"] == figure.id]
    assert [f.code for f in figure.facts if f.code != "prior"] == ["unread-signature"]
    assert (item["label"], item["severity"]) == ("Signature not transcribed", "warning")
    assert "President" in _flow_text(nodes)


def test_short_typed_text_on_a_ruling_is_no_signature_without_pen():
    from reflow.analyze.figures import figure_boxes
    from reflow.extract.drawings import Drawing
    page = _unread_page([LETTER[0], _role(PRESIDENT, "Signature of applicant")], [])
    page.drawings = [Drawing((280.0, 248.0, 460.0, 249.0), 1, 1)]
    assert figure_boxes(page, page.lines) == []


def _siblings_of(nodes, text):
    """The sibling list holding the node whose text is `text`, anywhere in the tree."""
    stack = [nodes]
    while stack:
        level = stack.pop()
        if any(n.text == text for n in level):
            return level
        stack += [n.children for n in level]
    raise AssertionError(f"no node {text!r}")


def test_a_signature_sits_directly_before_its_role_after_lists_and_sections():
    from reflow.analyze.sections import nest_sections
    from reflow.extract.scan import InkRegion
    title = replace(_scan_line((72, 60, 400, 78), "Resolution of the Board"), size=18.0, bold=True)
    # A printed line across the signature's foot, read before the role: it keeps its place.
    note = _scan_line((300, 228, 420, 236), "signed for the district by its officer")
    page = _unread_page([title, LETTER[0], note, _role(PRESIDENT, "President"), LETTER[1]],
                        [InkRegion(PEN_BOX, "pen", None)])
    nodes = [n for n in analyze([page]) if n.role != Role.ARTIFACT]
    for tree in (nodes, nest_sections(nodes)):
        level = _siblings_of(tree, "President")
        at = next(i for i, n in enumerate(level) if n.text == "President")
        assert level[at - 1].role == Role.FIGURE and level[at - 1].attrs["kind"] == "signature"
        assert level[at - 2].text == "signed for the district by its officer"
    [figure] = [n for n in nodes if n.role == Role.FIGURE]
    assert figure.children == []


def test_a_field_placed_between_a_signature_and_its_role_goes_before_the_signature():
    from reflow.analyze.forms import place_flow
    figure = Node(Role.FIGURE, page=1, bbox=PEN_BOX)
    role = Node(Role.P, page=1, bbox=PRESIDENT, text="President")
    field = Node(Role.FIELD, page=1, bbox=(300.0, 244.0, 420.0, 250.0))
    assert place_flow([figure, role], [field], [figure]) == [field, figure, role]
    assert place_flow([figure, role], [field]) == [figure, field, role]


def test_a_bold_line_on_a_scan_is_a_heading_flagged_for_review(tmp_path):
    from test_extract import HEAVY_PAGE
    pdf = make_scan_pdf(tmp_path / "s.pdf", [HEAVY_PAGE])
    info: dict = {}
    nodes = [n for n in analyze(list(extract_pages(pdf)), info=info) if n.role != Role.ARTIFACT]
    assert info["levels"] == {}   # not ranked: an ink heading moves no other heading's level
    head, *rest = nodes
    assert head.role.value.startswith("h") and head.text == "Conditions Affecting the District"
    [fact] = [f for f in head.facts if f.code == "heading-from-ink"]
    assert fact.data["stroke"] >= 1.5 and fact.data["underlined"] is False
    assert head.confidence <= 0.5
    assert [n.role for n in rest] == [Role.P]
    assert not [f for f in rest[0].facts if f.code == "heading-from-ink"]


def test_a_bold_line_on_a_scan_with_a_visible_ocr_layer_is_a_heading_too(tmp_path):
    from test_extract import HEAVY_PAGE
    pdf = make_scan_pdf(tmp_path / "s.pdf", [HEAVY_PAGE], visible=True)
    pages = list(extract_pages(pdf))
    assert not any(line.ocr for line in pages[0].lines)   # visible text: OCR only by the scan
    head = next(n for n in analyze(pages) if n.role != Role.ARTIFACT)
    assert head.role.value.startswith("h") and head.text == "Conditions Affecting the District"
    assert any(f.code == "heading-from-ink" for f in head.facts)


# --- form section bars ------------------------------------------------------------

def _form_table():
    """A two-column form table: a bold bar over each half, regular field labels under them, and
    a bar the grid cut into three cells."""
    from reflow.extract.lines import Line
    from reflow.extract.tables.cells import TableData

    def line(x0, top, text, bold=False, width=200):
        return Line(1, (x0, top, x0 + width, top + 9), text, 7.0, bold, False,
                    "Arial-Black" if bold else "ArialMT")
    rows = [["HEADER INFOR", "mAT", "ION", None],
            ["PATIENT INFORMATION", None, "1. Name", None],
            ["2. Date of Birth", None, "3. Gender", None]]
    grid = [[(10, 10, 50, 22), (50, 10, 70, 22), (70, 10, 90, 22), (90, 10, 300, 22)],
            [(10, 22, 300, 34), None, (300, 22, 600, 34), None],
            [(10, 34, 300, 46), None, (300, 34, 600, 46), None]]
    lines = [line(12, 11, "HEADER INFORMATION", bold=True, width=70),
             line(12, 23, "Patient Information", bold=True), line(302, 23, "1. Name"),
             line(12, 35, "2. Date of Birth"), line(302, 35, "3. Gender")]
    return TableData(1, (10, 10, 600, 46), rows, grid=grid), lines


def test_a_bold_bar_of_a_form_table_is_a_heading_in_its_cell():
    from reflow.analyze.table_nodes import mark_section_bars, table_node
    table, lines = _form_table()
    mark_section_bars([table], {}, lines)
    node = table_node(table)
    first, second = node.children[0].children, node.children[1].children
    assert [c.text for c in first] == ["", "", ""] and first[0].children[0].text == "HEADER INFORMATION"
    bar = second[0].children[0]
    # The heading takes the text of its line: the cells' reader may lose the case.
    assert bar.role == Role.H2 and bar.text == "Patient Information" and second[0].text == ""
    assert bar.bbox == lines[1].bbox
    assert second[1].text == "1. Name" and not second[1].children


def test_a_table_set_in_bold_has_no_bars():
    from reflow.analyze.table_nodes import mark_section_bars
    table, lines = _form_table()
    lines = [replace(l, bold=True) for l in lines]
    mark_section_bars([table], {}, lines)
    assert not table.bars and not table.bar_parts


def test_a_row_of_column_headers_has_no_bars():
    """Bold headers each over one column of their table are not a form's section bars."""
    from reflow.analyze.table_nodes import mark_section_bars
    from reflow.extract.lines import Line
    from reflow.extract.tables.cells import TableData

    def line(x0, top, text, bold=False):
        return Line(1, (x0, top, x0 + 60, top + 9), text, 7.0, bold, False,
                    "Arial-Black" if bold else "ArialMT")
    rows = [["Service", "Cost"], ["Exam", "$0"], ["Lenses", "$10"]]
    grid = [[(10, y, 300, y + 12), (300, y, 600, y + 12)] for y in (10, 22, 34)]
    lines = [line(12, 11, "Service", bold=True), line(302, 11, "Cost", bold=True),
             line(12, 23, "Exam"), line(302, 23, "$0"), line(12, 35, "Lenses"), line(302, 35, "$10")]
    table = TableData(1, (10, 10, 600, 46), rows, grid=grid)
    mark_section_bars([table], {}, lines)
    assert not table.bars and not table.bar_parts


def test_a_column_of_lines_that_start_with_one_lone_symbol_is_a_list_of_bullets():
    """OCR reads a printed bullet as "¢" or "¢+": three or more lines in a column that start
    with the same symbol are bullets; the same symbol twice, or a price, is not."""
    from dataclasses import replace

    from reflow.analyze.lists import ocr_bullets

    def ocr(x0, top, text):
        return replace(_line(x0, top, text), ocr=True)
    column = [ocr(46.0, 100, "¢ Mary Armes 1860-1947"), ocr(47.5, 120, "¢+ Joe Francis 1854-1919"),
              ocr(48.7, 140, "¢ Mabel Spear 1832-1911")]
    assert [line.text for line in ocr_bullets(column)] == [
        "• Mary Armes 1860-1947", "• Joe Francis 1854-1919", "• Mabel Spear 1832-1911"]
    pair = column[:2]
    assert ocr_bullets(pair) == pair
    beside = column[:2] + [ocr(300.0, 140, "¢ Elsewhere")]
    assert ocr_bullets(beside) == beside
    prices = [ocr(46.0, 100 + 20 * i, f"$ {i}5.00") for i in range(3)]
    assert ocr_bullets(prices) == prices
    borders = [ocr(46.0, 100 + 20 * i, f"| Name of provider {i}") for i in range(3)]
    assert ocr_bullets(borders) == borders
    typed = [replace(line, ocr=False) for line in column]
    assert ocr_bullets(typed) == typed


def test_a_control_goes_after_the_text_left_of_it_on_its_row_even_when_its_group_reaches_left():
    from reflow.analyze.forms import place_flow
    heading = Node(Role.P, page=1, bbox=(29, 310, 148, 320), text="CLINICAL INFORMATION")
    label = Node(Role.P, page=1, bbox=(29, 329, 53, 339), text="LMP:")
    below = Node(Role.P, page=1, bbox=(29, 350, 200, 360), text="Date of entry")
    wic = Node(Role.FIELD, page=1, bbox=(403, 306, 417, 318), attrs={"kind": "checkbox"})
    edd = Node(Role.FIELD, page=1, bbox=(118, 325, 132, 337), attrs={"kind": "checkbox"})
    group = Node(Role.GROUP, page=1, bbox=(118, 306, 417, 337), children=[wic, edd])
    assert place_flow([heading, label, below], [group]) == [heading, label, group, below]
