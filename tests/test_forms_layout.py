"""Form fields through layout and render: label binding, groups, text conservation, markup."""

import json
import re
from pathlib import Path

import pikepdf
import pytest
from conftest import make_pdf
from formpdf import make_form_pdf
from reflow.analyze.forms import plan_forms
from reflow.convert import convert
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.forms import FF_COMB, Detection, DetectionSource, Widget
from reflow.extract.tables.cells import TableData
from reflow.model import Role, Source
from testkit import walk_json


@pytest.fixture
def form_pdf(tmp_path):
    return make_form_pdf(tmp_path / "form.pdf")


def _converted(pdf, tmp_path):
    out = tmp_path / "out"
    convert(pdf, out, page_images=False)
    html = (out / "index.html").read_text(encoding="utf-8")
    doc = json.loads((out / "document.json").read_text(encoding="utf-8"))
    return html, doc


def test_layout_binds_labels_and_groups_the_radios(form_pdf, tmp_path):
    _, doc = _converted(form_pdf, tmp_path)
    nodes = list(walk_json(doc["root"]))
    fields = {n["attrs"]["name"]: n for n in nodes if n["role"] == "field"}
    assert fields["first"]["text"] == "First name"
    assert fields["first"]["attrs"]["tooltip"] == "Applicant first name"
    assert fields["state"]["text"] == "State"
    assert fields["agree"]["text"] == "I agree to the terms"
    [group] = [n for n in nodes if n["role"] == "group"]
    assert group["text"] == "Preferred contact"
    assert [c["text"] for c in group["children"]] == ["Email", "Phone"]
    assert group["attrs"]["radio"]
    assert group["children"][0]["attrs"]["group"] == group["attrs"]["name"]


def test_a_password_field_publishes_neither_its_value_nor_a_value_attribute(tmp_path):
    pdf = make_form_pdf(tmp_path / "pw.pdf", password=True)
    html, doc = _converted(pdf, tmp_path)
    [pw] = [n for n in walk_json(doc["root"]) if n["role"] == "field" and n["attrs"].get("name") == "pw"]
    assert pw["attrs"]["password"] is True
    assert "value" not in pw["attrs"]
    assert pw["text"] == "Password"
    assert "hunter2" not in json.dumps(doc) and "hunter2" not in html
    assert html.count('type="password"') == 1


def test_layout_keeps_every_printed_word_once(form_pdf, tmp_path):
    _, doc = _converted(form_pdf, tmp_path)
    texts = [n["text"] for n in walk_json(doc["root"]) if n.get("text") and n["role"] != "doc"
             and n.get("page")]
    words = " ".join(texts).split()
    for printed in ("Applicant", "details", "First", "name", "Notes", "State", "Preferred",
                    "contact", "Email", "Phone", "agree", "terms"):
        assert words.count(printed) == 1, printed


def test_fields_keep_reading_order_across_a_row(form_pdf, tmp_path):
    _, doc = _converted(form_pdf, tmp_path)
    order = [n["attrs"]["name"] for n in doc["root"]["children"] if n["role"] == "field"]
    assert order.index("state") < order.index("notes")  # the State box sits on First name's row


def test_render_labels_fieldset_select_and_note(form_pdf, tmp_path):
    html, _ = _converted(form_pdf, tmp_path)
    assert '<label for="f1-0">First name</label><input type="text" id="f1-0"' in html
    assert 'maxlength="20"' in html and 'aria-label="Applicant first name"' in html
    assert "<textarea" in html and 'rows="4"' in html
    assert '<fieldset class="fields"' in html and "<legend>Preferred contact</legend>" in html
    assert html.count('type="radio" name="g1-3"') == 2 and 'value="email"' in html
    assert re.search(r'<option value="CA" selected>CA</option>', html)
    assert html.count('class="form-note"') == 1 and "<form" not in html
    ids = re.findall(r'\bid="(f\d+-\d+)"', html)
    assert len(ids) == len(set(ids)) == 6
    for target in re.findall(r'<label for="([^"]+)"', html):
        assert target in ids


def test_controls_carry_their_pdf_field_name_and_export_value(form_pdf, tmp_path):
    """The viewer fills the original PDF from these: field name on every control, the export
    value on checkboxes and radios, and the manifest says the document is fillable."""
    html, doc = _converted(form_pdf, tmp_path)
    names = {n["attrs"]["name"] for n in walk_json(doc["root"]) if n["role"] == "field"}
    for name in names:
        assert f'data-pdf-field="{name}"' in html
    radios = re.findall(r'<input type="radio"[^>]*>', html)
    assert len(radios) == 2
    assert all('data-pdf-field="' in r for r in radios)
    assert sorted(re.search(r'data-pdf-value="([^"]+)"', r).group(1) for r in radios) == [
        "email", "phone"]
    assert "data-pdf-value" not in re.search(r'<input type="text"[^>]*>', html).group(0)
    assert json.loads((tmp_path / "out" / "manifest.json").read_text())["fillable"] is True


def test_a_document_without_fields_is_not_fillable(tmp_path):
    _converted(make_pdf(tmp_path / "plain.pdf", ["text"]), tmp_path)
    assert "fillable" not in json.loads((tmp_path / "out" / "manifest.json").read_text())


def test_no_form_means_no_note_and_no_controls(tmp_path):
    html, _ = _converted(make_pdf(tmp_path / "plain.pdf", ["text"]), tmp_path)
    assert "form-note" not in html and "<input" not in html


def _line(text, bbox):
    return Line(1, bbox, text, 10, False, False, "Helvetica")


def _plan(widgets, lines=(), tables=()):
    page = PageContent(1, 612, 792, list(lines), images=[], widgets=widgets)
    return plan_forms(page, lines, lines, tables)


def test_tooltip_names_a_control_before_its_bound_printed_label():
    label = _line("First name", (72, 90, 130, 100))
    widget = Widget(1, 0, "text", (72, 104, 292, 122), name="first",
                    tooltip="Applicant first name")
    [node] = _plan([widget], [label]).flow
    assert node.text == "First name"
    assert node.attrs["aria"] == "Applicant first name"
    assert node.attrs["label"] == "tooltip"


def test_stacked_field_label_does_not_absorb_the_bold_paragraph_above_it():
    continuation = Line(1, (36, 126, 210, 138),
                        "attach hearing evidence to this application.", 8.5, True, False,
                        "Arial-Bold")
    section = Line(1, (36, 140, 225, 153),
                   "1. APPLICANT INFORMATION - PLEASE PRINT", 8.5, True, False,
                   "Arial-Bold")
    label = Line(1, (36, 154, 273, 162),
                 "NAME OF APPLICANT (LAST, FIRST, MIDDLE INITIAL)", 6, False, False,
                 "Arial-Italic")
    widget = Widget(1, 0, "text", (36, 160, 386, 172), name="applicant")

    [node] = _plan([widget], [continuation, section, label]).flow

    assert node.text == label.text


@pytest.mark.parametrize("tooltip", ["f1_03", "form[0].f1_03[0]", "123", "12 34",
                                     "Text1", "TU3", "SSN"])
def test_internal_tooltips_fall_back_to_printed_labels(tooltip):
    label = _line("City", (72, 90, 130, 100))
    widget = Widget(1, 0, "text", (72, 104, 292, 122), name="f1_03", tooltip=tooltip)
    [node] = _plan([widget], [label]).flow
    assert node.text == "City"
    assert "aria" not in node.attrs
    assert "tooltip" not in node.attrs
    assert node.attrs["label"] == "printed"


@pytest.mark.parametrize("tooltip", ["City", "E-mail", "Prénom", "Applicant city"])
def test_word_tooltips_are_accessible_names(tooltip):
    widget = Widget(1, 0, "text", (72, 104, 292, 122), name="f1_03", tooltip=tooltip)
    [node] = _plan([widget]).flow
    assert node.attrs["aria"] == tooltip


def test_a_checkbox_and_its_followup_field_share_a_printed_label_across_a_gap():
    lines = [_line("Other (see instructions)", (86, 90, 160, 100))]
    widgets = [Widget(1, 0, "checkbox", (72, 90, 80, 100)),
               Widget(1, 1, "text", (210, 90, 300, 102))]
    plan = _plan(widgets, lines)
    assert plan.flow[0].text == "Other (see instructions)"
    assert plan.flow[1].attrs["aria"] == "Other (see instructions)"
    assert plan.consumed == {id(lines[0])}


def test_comb_segments_share_the_heading_above_the_whole_number():
    heading = _line("Employer identification number", (74, 70, 250, 80))
    widgets = [Widget(1, 0, "text", (72, 84, 100, 108), flags=FF_COMB),
               Widget(1, 1, "text", (114, 84, 215, 108), flags=FF_COMB)]
    plan = _plan(widgets, [heading])
    assert [node.attrs["aria"] for node in plan.flow] == [
        "Employer identification number", "Employer identification number"]
    assert plan.consumed == set()


def test_a_checkbox_can_take_its_long_printed_instruction_as_a_name_without_consuming_it():
    text = "Check this box if you have any foreign partners, owners, or beneficiaries. See instructions."
    line = _line(text, (72, 90, 430, 100))
    widget = Widget(1, 0, "checkbox", (440, 90, 448, 100))
    plan = _plan([widget], [line])
    assert plan.flow[0].attrs["aria"] == text
    assert plan.consumed == set()


def test_checkbox_label_is_split_from_its_printed_marker():
    line = _line("\uf06f Prescription drug", (72, 90, 180, 104))
    widget = Widget(1, 0, "checkbox", (72, 92, 82, 102), name="option")

    [node] = _plan([widget], [line]).flow

    assert node.text == "Prescription drug"


def test_inline_checkbox_options_bind_to_each_control_in_order():
    line = _line("\uf06f Spouse \uf06f Relative \uf06f Attorney", (10, 90, 220, 104))
    widgets = [Widget(1, i, "checkbox", (x, 92, x + 10, 102), name="relationship")
               for i, x in enumerate((10, 90, 160))]

    [group] = _plan(widgets, [line]).flow

    assert [node.text for node in group.children] == ["Spouse", "Relative", "Attorney"]


def test_same_row_group_question_wins_over_unrelated_prose_above():
    prose = _line("Definitions and helpful information are at the end of this form.",
                  (36, 134, 354, 150))
    question = _line("What is your request or concern about?", (36, 159, 226, 174))
    option = _line("\uf06f Prescription drug \uf06f Medical care", (244, 159, 444, 174))
    widgets = [Widget(1, i, "checkbox", (x, 162, x + 10, 172), name="request")
               for i, x in enumerate((244, 370))]

    [group] = _plan(widgets, [prose, question, option]).flow

    assert group.text == question.text


def test_a_sentence_fragment_left_of_a_group_does_not_replace_the_question_above():
    question = _line("Check one of the following boxes to attest to your status:", (300, 140, 560, 150))
    fragment = _line("provides for imprisonment and/or", (120, 159, 280, 169))
    widgets = [Widget(1, i, "checkbox", (300, y, 310, y + 10), name="status")
               for i, y in enumerate((159, 175))]
    options = [_line("1. A citizen", (314, 159, 400, 169)),
               _line("2. A noncitizen national", (314, 175, 440, 185))]

    [group] = _plan(widgets, [question, fragment, *options]).flow

    assert group.text == question.text


@pytest.mark.parametrize("header_rows, rows, expected", [
    (1, [["Name", "City"], ["Applicant", ""]], "City"),
    (0, [["", ""], ["Applicant", ""]], "Applicant"),
    (1, [["City", None], ["Applicant", ""]], "City"),
])
def test_a_table_control_uses_its_column_header_before_its_row_label(header_rows, rows, expected):
    table = TableData(1, (0, 0, 300, 80), rows, header_rows=header_rows,
                      grid=[[(0, 0, 150, 30), (150, 0, 300, 30)],
                            [(0, 30, 150, 80), (150, 30, 300, 80)]])
    widget = Widget(1, 0, "text", (160, 55, 290, 75), name="f1_03")
    plan = _plan([widget], tables=[table])
    [node] = plan.cells[(0, 1, 1)]
    assert node.attrs["aria"] == expected
    assert node.attrs["label"] == "cell-header"
    assert plan.tables[0].rows == rows


def test_a_control_without_a_name_source_has_a_missing_label_fact():
    widget = Widget(1, 0, "text", (72, 104, 292, 122), name="f1_03", tooltip="f1_03")
    [node] = _plan([widget]).flow
    assert node.text is None
    assert "aria" not in node.attrs
    assert node.attrs["label"] == "none"
    [fact] = node.facts
    assert (fact.code, fact.source, fact.detail) == (
        "label-missing", Source.HEURISTIC, "no tooltip, printed label or cell header")


def test_a_missing_control_name_reaches_review_json(tmp_path):
    pdf = make_form_pdf(tmp_path / "missing-label.pdf")
    with pikepdf.open(pdf, allow_overwriting_input=True) as form:
        first = form.Root.AcroForm.Fields[0]
        del first.TU
        first.Rect = [500, 50, 560, 68]
        form.save(pdf)
    html, doc = _converted(pdf, tmp_path)
    [node] = [n for n in walk_json(doc["root"]) if n.get("attrs", {}).get("name") == "first"]
    assert node["attrs"]["label"] == "none"
    assert re.search(r'<input[^>]*id="f1-0"[^>]*aria-label="first"', html)  # falls back to the field name
    review = json.loads((tmp_path / "out" / "review.json").read_text())
    [item] = [i for i in review if i["id"] == node["id"]]
    assert (item["needs"], item["label"]) == ("label", "Form control has no accessible name")


@pytest.mark.parametrize("names, legend_box", [
    (["tax[0]", "tax[1]"], (72, 70, 240, 80)),
    (["individual", "corporation"], (72, 70, 240, 80)),
    (["individual", "corporation"], (0, 90, 65, 100)),
])
def test_checkboxes_across_a_row_share_the_question_above_or_to_the_left(names, legend_box):
    lines = [_line("Tax classification", legend_box),
             _line("Individual", (86, 90, 140, 100)),
             _line("Corporation", (206, 90, 270, 100))]
    widgets = [Widget(1, i, "checkbox", (x, 90, x + 8, 100), name=names[i])
               for i, x in enumerate((72, 192))]
    plan = _plan(widgets, lines)
    [group] = plan.flow
    assert group.role == Role.GROUP
    assert group.text == "Tax classification"
    assert [c.text for c in group.children] == ["Individual", "Corporation"]
    assert plan.consumed == {id(line) for line in lines}


@pytest.mark.parametrize("kind", ["radio", "checkbox"])
def test_a_group_without_a_question_has_no_internal_name_fallback(kind):
    widgets = [Widget(1, i, kind, (72, top, 80, top + 10),
                      name="c1_1" if kind == "radio" else f"c1_1[{i}]")
               for i, top in enumerate((90, 110))]
    [group] = _plan(widgets).flow
    assert group.role == Role.GROUP
    assert "aria" not in group.attrs
    assert group.attrs["label"] == "none"
    assert [f.code for f in group.facts] == ["label-missing"]


def test_fw9_classification_boxes_share_the_printed_question(tmp_path):
    html, doc = _converted(Path(__file__).parents[1] / "samples" / "fw9.pdf", tmp_path)
    groups = [n for n in walk_json(doc["root"]) if n["role"] == "group"]
    [group] = [n for n in groups if "federal tax classification" in (n.get("text") or "")]
    assert group["text"] == (
        "3a Check the appropriate box for federal tax classification of the entity/individual "
        "whose name is entered on line 1. Check only one of the following seven boxes.")
    assert len(group["children"]) == 7
    assert [c["text"] for c in group["children"]][:5] == [
        "Individual/sole proprietor", "C corporation", "S corporation", "Partnership", "Trust/estate"]
    assert not any(re.fullmatch(r"[a-z]\d? \d+", label)
                   for label in re.findall(r'aria-label="([^"]*)"', html))
    review = json.loads((tmp_path / "out" / "review.json").read_text())
    reviewed = {i["id"] for i in review if i["needs"] == "label"}
    missing = [n for n in walk_json(doc["root"])
               if n["role"] == "field" and n["attrs"]["label"] == "none"]
    assert missing == []
    assert reviewed == set()
    fields = {n["attrs"]["fid"]: n for n in walk_json(doc["root"]) if n["role"] == "field"}
    for fid, expected in {
        "f1-8": "LLC. Enter the tax classification (C = C corporation, S = S corporation, P = Partnership)",
        "f1-10": "Other (see instructions)",
        "f1-19": "Social security number",
        "f1-20": "Social security number",
        "f1-21": "Employer identification number",
        "f1-22": "Employer identification number",
    }.items():
        assert fields[fid]["attrs"]["aria"] == expected
    assert "foreign partners, owners, or beneficiaries" in fields["f1-11"]["attrs"]["aria"]


def test_same_row_checkboxes_in_different_table_cells_are_not_grouped():
    table = TableData(1, (0, 0, 300, 60), [["Consent", "Opt-out"], ["", ""]], header_rows=1,
                      grid=[[(0, 0, 150, 30), (150, 0, 300, 30)],
                            [(0, 30, 150, 60), (150, 30, 300, 60)]])
    widgets = [Widget(1, 0, "checkbox", (70, 40, 78, 48), name="consent"),
               Widget(1, 1, "checkbox", (220, 40, 228, 48), name="optout")]
    plan = _plan(widgets, tables=[table])
    assert plan.flow == []
    assert len(plan.cells[(0, 1, 0)]) == 1 and len(plan.cells[(0, 1, 1)]) == 1


def test_same_row_checkboxes_far_apart_are_not_grouped():
    widgets = [Widget(1, 0, "checkbox", (72, 90, 80, 98), name="a"),
               Widget(1, 1, "checkbox", (500, 90, 508, 98), name="b")]
    plan = _plan(widgets)
    assert all(n.role != Role.GROUP for n in plan.flow)


def test_a_caption_that_belongs_to_the_next_row_is_not_taken_by_the_field_above():
    lines = [_line("Phone", (72, 118, 110, 128))]
    widgets = [Widget(1, 0, "text", (72, 100, 292, 114), name="a"),
               Widget(1, 1, "text", (72, 130, 292, 144), name="b")]
    first = _plan(widgets, lines).flow[0]
    assert first.attrs.get("aria") != "Phone"


def test_a_caption_wider_than_its_field_is_not_taken_from_below():
    lines = [_line("Print first and last name of the signer", (72, 118, 400, 128))]
    [node] = _plan([Widget(1, 0, "text", (72, 100, 292, 114), name="a")], lines).flow
    assert node.attrs.get("aria") != lines[0].text


def test_a_caption_under_a_signature_line_labels_it():
    lines = [_line("Signature of applicant", (72, 118, 180, 128))]
    [node] = _plan([Widget(1, 0, "text", (72, 100, 292, 114), name="a")], lines).flow
    assert node.attrs["aria"] == "Signature of applicant"
    assert node.attrs["label"] == "printed-nearby"


def test_a_bold_caps_title_whose_box_glyph_overlaps_the_field_above_still_names_its_group():
    title = Line(1, (36, 535, 128, 555.25), "PROPERTY TYPE ☑", 10, True, False, "Helvetica")
    lines = [title, _line("Single family", (50, 556, 120, 566)), _line("Condominium", (50, 570, 120, 580))]
    widgets = [Widget(1, 0, "text", (35, 522, 382, 536), name="address"),
               Widget(1, 1, "checkbox", (36.7, 555.1, 44, 563.4), name="a"),
               Widget(1, 2, "checkbox", (36.7, 569.2, 44, 577.4), name="b")]
    [group] = [n for n in _plan(widgets, lines).flow if n.role == Role.GROUP]
    assert group.attrs["aria"] == "PROPERTY TYPE"  # the heading stays in the page; the legend names the group


# -- worksheet rows: entry text, leader dots, line number, box ---------------------------------


def _worksheet_row(entry_lines, number, top=100.0, box=(504, 0, 576, 0)):
    """The lines of one worksheet entry (wrapping, top to bottom), the line number printed
    beside the last of them, and the answer box on that row."""
    lines = [_line(text, (65 if i == 0 else 80, top + 11 * i, 400, top + 11 * i + 10))
             for i, text in enumerate(entry_lines)]
    last = lines[-1].bbox
    lines.append(_line(number, (489, last[1], 498, last[3])))
    return lines, Widget(1, 0, "text", (box[0], last[1] - 1, box[2], last[3] + 1))


def test_a_line_number_beside_the_box_is_not_the_caption_of_a_worksheet_row():
    lines, widget = _worksheet_row(["Add lines 3a and 3b. Enter the result here"], "4")
    [node] = _plan([widget], lines).flow
    assert node.text == "Add lines 3a and 3b. Enter the result here"
    assert node.attrs["label"] == "printed"


def test_a_worksheet_row_is_named_by_the_whole_wrapped_entry():
    lines, widget = _worksheet_row(
        ["a Qualified tips. If your total income is less than $150,000, enter",
         "an estimate of your qualified tips up to $25,000"], "1a")
    [node] = _plan([widget], lines).flow
    assert node.text == ("a Qualified tips. If your total income is less than $150,000, enter "
                         "an estimate of your qualified tips up to $25,000")


def test_a_wrapped_entry_stops_at_the_sentence_that_ends_before_it():
    before = _line("Seniors age 65 or older. If your income is under $75,000:", (65, 89, 400, 99))
    lines, widget = _worksheet_row(["a Enter $6,000 if you are age 65 or older"], "3a")
    [node] = _plan([widget], [before, *lines]).flow
    assert node.text == "a Enter $6,000 if you are age 65 or older"
    assert node.text.count("Seniors") == 0


def test_a_lone_line_number_stays_the_caption_when_no_text_precedes_it():
    widget = Widget(1, 0, "text", (504, 100, 576, 112))
    [node] = _plan([widget], [_line("1a", (489, 101, 498, 111))]).flow
    assert node.text == "1a"


def test_a_number_that_is_part_of_the_text_is_not_a_line_number():
    label = _line("Line 4", (440, 101, 480, 111))
    widget = Widget(1, 0, "text", (484, 100, 576, 112))
    [node] = _plan([widget], [label]).flow
    assert node.text == "Line 4"


# -- printed check boxes: the text layer holds a "■" for each box, the widget is the box ----------


def _analysed(widgets, lines, tables=()):
    from reflow.analyze.layout import analyze
    nodes = analyze([PageContent(1, 612, 792, list(lines), images=[], widgets=widgets)])
    return [n for top in nodes for n in top.walk()]


def _yes_no_boxes(top, first=0):
    return [Widget(1, first + i, "checkbox", (x, top + 1, x + 9, top + 11), name=f"q{first}")
            for i, x in enumerate((495, 524))]


def test_a_question_printed_before_its_boxes_stays_in_the_text_and_names_them():
    # "Have you ...? . . . . ■ Yes ■ No": one text run holds the question, its leader dots and
    # both options. The options label the boxes; the question must not leave with them.
    question = "1. Have you been diagnosed with ESRD, or any other kidney disease that may require dialysis?"
    line = _line(f"{question} . . . . . . . . . . . ■ Yes ■ No", (29, 150, 560, 165))
    nodes = _analysed(_yes_no_boxes(151), [line])
    [group] = [n for n in nodes if n.role == Role.GROUP]
    assert group.text == question  # the group's legend, so the question is still on the page
    assert [c.text for c in group.children] == ["Yes", "No"]
    assert "label-missing" not in {f.code for n in nodes for f in n.facts}


def test_an_option_row_in_a_table_cell_leaves_no_box_glyph_in_the_cell():
    table = TableData(1, (30, 130, 570, 170), [["Are you an Arizona resident? ■ Yes ■ No"]],
                      grid=[[(30, 130, 570, 170)]])
    line = _line("Are you an Arizona resident? ■ Yes ■ No", (40, 140, 254, 155))
    boxes = [Widget(1, i, "checkbox", (x, 141, x + 9, 151), name="resident")
             for i, x in enumerate((196, 225))]
    plan = _plan(boxes, [line], [table])
    assert "■" not in plan.tables[0].rows[0][0]
    assert plan.tables[0].rows[0][0].startswith("Are you an Arizona resident?")


def test_the_box_of_the_next_option_is_not_part_of_a_checkbox_label():
    # The text layer reads "A ■" (A, then the box of D) and "M ■ F": each box takes its own word.
    a = _line("A ■", (77, 164, 102, 179))
    gender = _line("M ■ F", (209, 539, 241, 554))
    boxes = [Widget(1, 0, "checkbox", (65, 166, 74, 178), name="plan"),
             Widget(1, 1, "checkbox", (93, 166, 102, 178), name="plan"),
             Widget(1, 2, "checkbox", (196, 541, 206, 553), name="gender"),
             Widget(1, 3, "checkbox", (222, 541, 232, 553), name="gender")]
    labels = [n.text for n in _plan(boxes, [a, gender]).flow
              for n in (n.children or [n])]
    assert all("■" not in t for t in labels if t)
    assert "M" in labels and "F" in labels


@pytest.mark.parametrize("text", [
    "I agree • I understand that consent is optional",
    "Authorise □ account access",
])
def test_a_marker_inside_the_label_of_a_single_checkbox_keeps_the_whole_label(text):
    # No other box stands on the line, so the marker is printed text and not a box's place.
    box = Widget(1, 0, "checkbox", (72, 100, 82, 110), name="consent")
    nodes = _analysed([box], [_line(text, (86, 99, 340, 112))])
    [field] = [n for n in nodes if n.role == Role.FIELD]
    assert field.text == text
    assert field.attrs["label"] == "printed"
    # Every printed word is still somewhere in the output.
    printed = " ".join(n.text for n in nodes if n.text)
    assert all(word in printed for word in text.split())


def _printed_words(text):
    return re.findall(r"[^\s•◦▪▫●○■□\uf000-\uf8ff]+", text)


def _output_words(nodes):
    """Every word the output holds, as many times as it holds it: texts and accessible names."""
    return sorted(w for n in nodes for t in (n.text, n.attrs.get("aria")) if t
                  for w in _printed_words(t))


def test_only_the_markers_of_boxes_that_follow_end_a_checkbox_label():
    # "Agree • more" keeps its bullet; the trailing marker is the next box's.
    run = _line("I agree • more ■", (86, 99, 200, 112))
    boxes = [Widget(1, 0, "checkbox", (72, 100, 82, 110), name="consent"),
             Widget(1, 1, "checkbox", (190, 101, 200, 111), name="consent")]
    nodes = _analysed(boxes, [run])
    fields = [n for n in nodes if n.role == Role.FIELD]
    assert fields[0].text == "I agree • more"
    assert _output_words(nodes) == sorted(_printed_words(run.text))


PER_CHAR = 5.0  # the runs below are 5 pt a character from x=86, so a box can stand on a glyph


def _run_with_boxes(text, at, detected=False, first=True):
    """A run printed right of a box at x=72 (unless not `first`), with a box on each of the
    characters `at`."""
    def box(i, x):
        return Widget(1, i, "checkbox", (x, 100, x + 9, 110), name="" if detected else "q",
                      detected=Detection(DetectionSource.RULES, "square") if detected else None)
    xs = ([72.0] if first else []) + [86 + PER_CHAR * i - 2 for i in at]
    run = Line(1, (86, 99, 86 + PER_CHAR * len(text), 112), text, 10, False, False,
               "OCR" if detected else "Helvetica", ocr=detected)
    return [box(i, x) for i, x in enumerate(xs)], run


@pytest.mark.parametrize("text, at, detected, labels", [
    # The first box's label stops where the next boxes' own labels start.
    ("Male ■ Female", [5], False, ["Male", "Female"]),
    ("Yes ■ No ■ N/A", [4, 9], False, ["Yes", "No", "N/A"]),
    # A marker or box count that does not match the run's parts gives the later boxes no
    # label of their own, so the first keeps the whole run.
    ("Yes • see note ■ No", [15], False, None),
    ("A ■ B • C", [2], False, None),
    ("□ Yes □ No", [6], False, None),
    ("A ■ B ■", [2, 6], False, None),
    ("Yes ■ No       Unsure", [4, 13], False, None),
    ("Yes □ No       Unsure", [4, 13], True, None),
    # A scan's labels-first run with a box left of it: each word labels the box after it, and
    # the box on the left takes none of them a second time.
    ("A □ B □", [2, 6], True, [None, "A", "B"]),
])
def test_every_word_of_a_checkbox_run_is_in_the_output_once(text, at, detected, labels):
    boxes, run = _run_with_boxes(text, at, detected)
    nodes = _analysed(boxes, [run])
    assert _output_words(nodes) == sorted(_printed_words(text))
    if labels is not None:
        assert [n.text for n in nodes if n.role == Role.FIELD] == labels


def test_a_box_glyph_read_out_of_order_does_not_give_its_label_to_the_next_box():
    # "Q? [box] Yes [box] No", whose text layer holds both box glyphs before "Yes": "Q? ■",
    # "■ Yes", "No" (the Medicare supplement application, page 3). The second box ends where
    # "■ Yes" ends, with no room for "Yes" after it, so "Yes" is still the first box's.
    question = "Have you used tobacco products in the past 12 months?"
    lines = [_line(f"{question} ■", (39.5, 628.8, 327.2, 643.2)),
             _line("■ Yes", (329.9, 628.8, 364.2, 643.2)),
             _line("No", (367.6, 628.8, 381.6, 643.2))]
    boxes = [Widget(1, i, "checkbox", (x, 631.5, x + 9.5, 641.2), name="Check Box9", tooltip=question)
             for i, x in enumerate((317.8, 354.7))]
    nodes = _analysed(boxes, lines)
    [group] = [n for n in nodes if n.role == Role.GROUP]
    assert group.text == question
    assert [c.text for c in group.children] == ["Yes", "No"]
    assert _output_words(nodes) == sorted(_printed_words(" ".join(line.text for line in lines)))


def test_a_scan_row_that_prints_each_label_before_its_box_is_not_cut_as_a_question():
    # "Yes □ No □" on a scan: "Yes" is the first box's option, not a question before the boxes.
    boxes, run = _run_with_boxes("Yes □ No □", [4, 9], detected=True, first=False)
    nodes = _analysed(boxes, [run])
    assert [n.text for n in nodes if n.role == Role.FIELD] == ["Yes", "No"]
    assert _output_words(nodes) == ["No", "Yes"]


@pytest.mark.parametrize("text, at, options", [
    ("Yes ■ No ■", [4, 9], ["Yes", "No"]),
    ("Male ■ Female ■", [5, 14], ["Male", "Female"]),
    ("Yes \uf06e No \uf06e", [4, 9], ["Yes", "No"]),  # a symbol font's box character
    ("Smoker? Yes ■ No ■", [12, 17], ["Yes", "No"]),
])
def test_a_native_row_that_prints_each_label_before_its_box_gives_no_box_the_next_ones_label(
        text, at, options):
    # An AcroForm row with nothing after its last box prints each label before its box: "Yes"
    # is the first box's, not a question, so cutting the run there would name the first box "No".
    boxes, run = _run_with_boxes(text, at, first=False)
    nodes = _analysed(boxes, [run])
    fields = {n.attrs["fid"]: n for n in nodes if n.role == Role.FIELD}
    assert len(fields) == len(options)
    for box, option in zip(boxes, options):
        field = fields[f"f1-{box.index}"]
        # Its own option, or no name and a review flag, but never another box's option.
        assert field.text in (None, option)
        assert {field.text, field.attrs.get("aria")} & (set(options) - {option}) == set()
        assert field.text or "label-missing" in {f.code for f in field.facts}
    assert _output_words(nodes) == sorted(_printed_words(text))


def test_a_run_is_not_cut_when_the_words_after_its_last_box_print_their_own_box_after_them():
    # "Smoker? Yes ■" then "No ■": "No" is printed before a box of its own, so it is not the
    # label of the first run's box, and "Yes" is not the option of a question "Smoker?".
    run = _line("Smoker? Yes ■", (86, 99, 151, 112))
    no = _line("No ■", (154, 99, 174, 112))
    boxes = [Widget(1, i, "checkbox", (x, 100, x + 9, 110), name="q") for i, x in enumerate((142, 165))]
    nodes = _analysed(boxes, [run, no])
    assert not any(n.role == Role.GROUP and n.text for n in nodes)
    assert _output_words(nodes) == sorted(_printed_words(f"{run.text} {no.text}"))


@pytest.mark.parametrize("text, at, after", [
    ("Yes ■ No ■", [4, 9], "Date"),
    ("Male ■ Female ■", [5, 14], "Date of birth"),
    ("Sex: Male ■ Female ■", [10, 19], "Date of Birth:"),
    ("Smoker? Yes ■ No ■", [12, 17], "If yes, packs per day:"),
    ("Married . . . ■ Single . . . ■", [14, 29], "Date"),
])
def test_unrelated_words_right_of_a_labels_first_row_do_not_cut_it(text, at, after):
    # Words printed just right of the last box are not its label when they belong to another
    # field, and a head that does not read as a question ("Yes", "Sex: Male") is an option.
    boxes, run = _run_with_boxes(text, at, first=False)
    x0 = boxes[-1].bbox[2] + 8
    other = _line(after, (x0, 99, x0 + PER_CHAR * len(after), 112))
    nodes = _analysed(boxes, [run, other])
    first = next(n for n in nodes if n.role == Role.FIELD and n.attrs["fid"] == "f1-0")
    assert first.text is None and "label-missing" in {f.code for f in first.facts}
    assert not any(n.role == Role.GROUP and n.text for n in nodes)
    assert _output_words(nodes) == sorted(_printed_words(f"{text} {after}"))


@pytest.mark.parametrize("box", ["■", "\uf06e"])
def test_a_box_that_ends_the_run_belongs_to_the_next_option_whose_label_is_printed_apart(box):
    # "... last 63 days? . . . ■ Yes ■" then "No" on its own: the run's last box is No's.
    question = "(d) Have you lost coverage from another policy within the last 63 days?"
    run = _line(f"{question} . . . . . . {box} Yes {box}", (29, 150, 530, 165))
    no = _line("No", (545, 150, 560, 165))
    nodes = _analysed(_yes_no_boxes(151), [run, no])
    [group] = [n for n in nodes if n.role == Role.GROUP]
    assert group.text == question
    assert [c.text for c in group.children] == ["Yes", "No"]
    assert not any(re.search(r"[■\uf000-\uf8ff]", n.text or "") for n in nodes)


@pytest.mark.parametrize("name", ["same", ""])
def test_options_on_one_row_read_left_to_right_despite_slightly_different_tops(name):
    boxes = [Widget(1, 0, "checkbox", (495, 152, 504, 162), name=name),
             Widget(1, 1, "checkbox", (524, 150, 533, 160), name=name)]
    nodes = _analysed(boxes, [_line("Are you a resident? ■ Yes ■ No", (29, 150, 560, 165))])
    [group] = [n for n in nodes if n.role == Role.GROUP]
    assert group.text == "Are you a resident?"
    assert [c.text for c in group.children] == ["Yes", "No"]


def test_an_option_letter_is_not_cut_out_of_a_word_in_its_cell():
    table = TableData(1, (30, 130, 570, 170), [["Available throughout Arizona Plan: ■ A ■ D \uf0a8"]],
                      grid=[[(30, 130, 570, 170)]])
    lines = [_line("A ■", (77, 140, 102, 155)), _line("D", (106, 140, 114, 155))]
    boxes = [Widget(1, i, "checkbox", (x, 141, x + 9, 151), name="plan")
             for i, x in enumerate((65, 93))]
    plan = plan_forms(PageContent(1, 612, 792, lines, images=[], widgets=boxes),
                      lines, [], [table])
    assert plan.tables[0].rows[0][0] == "Available throughout Arizona Plan: \uf0a8"


def _contract_rows(tooltip):
    """A row label beside two rows: a shaded caption cell, and the text box's own cell under it
    (the group contract form's PERSON AUTHORIZED TO SIGN CONTRACT *)."""
    table = TableData(1, (0, 0, 400, 40), [["PERSON AUTHORIZED *", "Name and title"], [None, ""]],
                      method="ruled",
                      grid=[[(0, 0, 100, 40), (100, 0, 400, 15)], [None, (100, 15, 400, 40)]])
    widget = Widget(1, 0, "text", (102, 18, 398, 38), name="rep", tooltip=tooltip)
    [node] = _plan([widget], tables=[table]).cells[(0, 1, 1)]
    return node


def test_a_field_name_tooltip_gives_way_to_the_caption_over_its_box():
    # Group contract form p1: the tooltip "Name and title ..._Row" joins the author's field-name
    # parts; the shaded cell over the box prints the caption.
    node = _contract_rows("Name and title_Row")
    assert node.attrs["aria"] == "Name and title"
    assert node.attrs["label"] == "printed-nearby"
    assert node.attrs["tooltip"] == "Name and title_Row"  # kept, as the control's title


def test_a_plain_tooltip_still_names_a_box_under_a_caption():
    # "Primary Address Street Address" says more than the caption "Street Address" over it.
    node = _contract_rows("Representative name and title")
    assert node.attrs["aria"] == "Representative name and title"
    assert node.attrs["label"] == "tooltip"


def test_a_row_label_spans_the_rows_of_its_fields():
    from reflow.analyze.table_nodes import table_node

    table = TableData(1, (0, 0, 400, 40), [["PERSON AUTHORIZED *", "Name and title"], [None, ""]],
                      method="ruled",
                      grid=[[(0, 0, 100, 40), (100, 0, 400, 15)], [None, (100, 15, 400, 40)]])
    first, second = table_node(table).children
    assert first.children[0].attrs["rowspan"] == 2
    assert [c.text for c in second.children] == [""]  # the box's own cell only, no blank label cell


def test_a_row_label_reaching_down_from_the_header_rows_ends_them():
    # Group contract form p1: the table's first row was read as its header row, but its row
    # label spans the field rows under it; HTML lets no rowspan cross from the head to the body.
    from reflow.analyze.table_nodes import table_node

    table = TableData(1, (0, 0, 400, 40), [["PERSON AUTHORIZED *", "Name and title"], [None, ""]],
                      method="ruled", header_rows=1,
                      grid=[[(0, 0, 100, 40), (100, 0, 400, 15)], [None, (100, 15, 400, 40)]])
    node = table_node(table)
    assert node.attrs["header_rows"] == 0
    assert node.children[0].children[0].role == Role.TD
    assert node.children[0].children[0].attrs["rowspan"] == 2


def test_header_rows_end_above_every_span_that_crosses_them():
    # Staggered spans: lowering the boundary to row 1 for C leaves A crossing it.
    from reflow.analyze.table_nodes import table_node

    table = TableData(1, (0, 0, 200, 30), [["A", "B"], [None, "C"], ["D", None]],
                      method="ruled", header_rows=2,
                      grid=[[(0, 0, 100, 20), (100, 0, 200, 10)],
                            [None, (100, 10, 200, 30)], [(0, 20, 100, 30), None]])
    assert table_node(table).attrs["header_rows"] == 0


def _names(plan):
    """The printed label (or aria name) of every check box the plan placed, in order."""
    boxes = [k for n in plan.flow for k in ([n] if n.role == Role.FIELD else n.children)]
    return [k.text or k.attrs.get("aria") for k in boxes]


def test_a_row_of_numbers_names_the_boxes_over_it_one_number_each():
    """The missing-teeth chart: a number printed over each of 16 boxes."""
    line = _line(" ".join(str(n) for n in range(1, 5)), (28, 100, 108, 108))
    boxes = [Widget(1, k, "checkbox", (23 + 25 * k, 98, 36 + 25 * k, 110), name=str(31 + k)) for k in range(4)]
    assert _names(_plan(boxes, [line])) == ["1", "2", "3", "4"]


def test_a_numbered_row_with_a_different_number_of_boxes_is_left_alone():
    line = _line("1 2 3 4", (28, 100, 108, 108))
    boxes = [Widget(1, k, "checkbox", (23 + 25 * k, 98, 36 + 25 * k, 110), name=str(31 + k)) for k in range(3)]
    assert _names(_plan(boxes, [line])) != ["1", "2", "3"]


def test_labels_printed_before_their_boxes_name_the_box_that_follows_them():
    """"M ■ F ■": the last box has nothing right of it, so each label is the words left of its box."""
    lines = [_line("M", (310, 100, 318, 110)), _line("F", (340, 100, 347, 110))]
    boxes = [Widget(1, 0, "checkbox", (320, 100, 330, 110), name="a"),
             Widget(1, 1, "checkbox", (350, 100, 360, 110), name="b")]
    assert _names(_plan(boxes, lines)) == ["M", "F"]


def test_a_label_printed_after_each_box_is_not_read_as_printed_before():
    lines = [_line("Yes", (333, 100, 350, 110)), _line("No", (373, 100, 385, 110))]
    boxes = [Widget(1, 0, "checkbox", (320, 100, 330, 110), name="a"),
             Widget(1, 1, "checkbox", (360, 100, 370, 110), name="b")]
    assert _names(_plan(boxes, lines)) == ["Yes", "No"]


def test_a_hint_under_a_caption_names_the_box_with_both():
    lines = [_line("MEDICARE", (33, 96, 70, 104)), _line("(N.º de Medicare)", (33, 111, 70, 118)),
             _line("MEDICAID", (85, 96, 115, 104)), _line("(N.º de Medicaid)", (85, 111, 121, 118))]
    boxes = [Widget(1, 0, "checkbox", (20, 107, 30, 117), name="a"),
             Widget(1, 1, "checkbox", (72, 107, 82, 117), name="b")]
    assert _names(_plan(boxes, lines)) == ["MEDICARE (N.º de Medicare)", "MEDICAID (N.º de Medicaid)"]


def test_a_caption_under_a_box_that_reads_as_its_tooltip_is_not_the_next_boxes():
    """Captions printed under their rules sit right over the next row's box."""
    lines = [_line("Member ID", (36, 551, 89, 565)), _line("First Name", (36, 591, 89, 606))]
    widgets = [Widget(1, 0, "text", (36, 525, 297, 548), name="id", tooltip="Member ID"),
               Widget(1, 1, "text", (36, 566, 297, 589), name="first", tooltip="First Name")]
    first = _plan(widgets, lines).flow[1]
    assert first.text != "Member ID" and first.attrs["aria"] == "First Name"


def test_a_tooltip_another_box_repeats_gives_way_to_the_printed_caption():
    lines = [_line("Member ID", (36, 551, 89, 565)), _line("Date of Birth", (306, 551, 366, 565))]
    widgets = [Widget(1, 0, "text", (36, 525, 297, 548), name="id", tooltip="Member ID"),
               Widget(1, 1, "text", (308, 524, 350, 548), name="mm", tooltip="Member ID")]
    mm = next(n for n in _plan(widgets, lines).flow if n.attrs["name"] == "mm")
    assert mm.attrs["aria"] == "Date of Birth"


def test_a_text_box_drawn_over_its_options_label_follows_that_option():
    """"■ Other:" with the box drawn over the word: the box is the option's blank."""
    lines = [_line("Other:", (464, 443, 486, 453)), _line("< 34 Weeks", (375, 455, 417, 466))]
    boxes = [Widget(1, 0, "checkbox", (448, 443, 460, 454), name="a1"),
             Widget(1, 1, "checkbox", (448, 460, 460, 471), name="a2"),
             Widget(1, 2, "text", (461, 452, 559, 474), name="other")]
    plan = _plan(boxes, lines)
    [group] = [n for n in plan.flow if n.role == Role.GROUP]
    assert [k.attrs["kind"] for k in group.children] == ["checkbox", "text", "checkbox"]
    assert group.children[1].attrs["aria"] == "Other:"
