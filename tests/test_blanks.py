"""Paper blanks (analyze.blanks): a run of underscores in a text layer becomes a text field."""

from reflow.analyze import blanks
from reflow.analyze.forms import plan_forms
from reflow.extract.forms import Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData


def line(text, x0=54.0, x1=400.0, top=100.0, **kw):
    return Line(page=1, bbox=(x0, top, x1, top + 11.0), text=text, size=10.0, bold=False, italic=False,
                font="ArialMT", **kw)


def page(*lines, widgets=()):
    return PageContent(number=1, width=612, height=792, lines=list(lines), images=[],
                       widgets=list(widgets))


def texts(p):
    return [l.text for l in p.lines]


def test_a_run_becomes_a_field_and_its_caption_stays():
    p = page(line("Mother’s Name:______________ DOB:________"))
    assert blanks.detect(p, []) == 2
    assert texts(p) == ["Mother’s Name:", "DOB:"]
    assert [w.kind for w in p.widgets] == ["text", "text"]
    first, second = p.widgets
    assert p.lines[0].bbox[2] <= first.bbox[0] + 0.01 and first.bbox[2] <= second.bbox[0]
    assert all(w.detected and w.detected.source == "text" and not w.name for w in p.widgets)


def test_the_planner_labels_each_field_by_the_caption_on_its_left():
    p = page(line("Birth Weight: _______ grams Gestational Age: ______ weeks", x1=420.0))
    blanks.detect(p, [])
    plan = plan_forms(p, p.lines, p.lines, [])
    fields = [n for n in plan.flow if n.role.value == "field"]
    assert [n.text for n in fields] == ["Birth Weight:", "grams Gestational Age:"]
    assert all(n.attrs["detected"] == "text" for n in fields)
    assert plan.consumed == {id(p.lines[0]), id(p.lines[1])}


def test_short_runs_and_underscores_inside_words_are_not_blanks():
    p = page(line("Fill in __ the gap", x1=200.0), line("call my_var___name now", top=120.0, x1=250.0))
    assert blanks.detect(p, []) == 0
    assert texts(p) == ["Fill in __ the gap", "call my_var___name now"]


def test_spaced_groups_are_one_field_the_comb_of_an_id():
    p = page(line("AHCCCS ID: A __ __ __ __ __ __ __ __", x1=300.0))
    assert blanks.detect(p, []) == 1
    assert texts(p) == ["AHCCCS ID: A"]


def test_a_paragraph_with_underscores_is_not_a_formline():
    prose = " ".join(["word"] * 40) + " ______"
    p = page(line(prose, x1=580.0))
    assert blanks.detect(p, []) == 0 and texts(p) == [prose]


def test_ocr_text_rotated_text_and_table_lines_are_left_alone():
    table = TableData(page=1, bbox=(40.0, 90.0, 500.0, 130.0), rows=[["Name ______"]])
    p = page(line("Name ______", ocr=True), line("Name ______", top=140.0, rotated=True),
             line("Name ______", top=100.0, x1=120.0))
    assert blanks.detect(p, [table]) == 0
    assert all("______" in t for t in texts(p))


def test_a_box_the_pdf_made_for_the_blank_is_kept_and_its_caption_cut_free():
    box = Widget(page=1, index=0, kind="text", bbox=(150.0, 99.0, 250.0, 112.0), name="Member Name")
    p = page(line("Member Name: ____________ Phone:", x1=300.0), widgets=[box])
    assert blanks.detect(p, []) == 0
    assert texts(p) == ["Member Name:", "Phone:"]
    assert box.on_blank and len(p.widgets) == 1


def test_a_check_box_on_the_run_leaves_the_line_alone():
    check = Widget(page=1, index=0, kind="checkbox", bbox=(200.0, 99.0, 210.0, 110.0))
    p = page(line("Other ___________", x1=300.0), widgets=[check])
    assert blanks.detect(p, []) == 0 and texts(p) == ["Other ___________"]


def test_lines_of_blank_under_a_question_are_one_multiline_field():
    p = page(line("What happened? ______________________", x1=400.0),
             line("_" * 40, top=124.0, x0=54.0, x1=400.0),
             line("_" * 40, top=148.0, x0=54.0, x1=400.0))
    assert blanks.detect(p, []) == 1
    assert texts(p) == ["What happened?"]
    [area] = p.widgets
    assert area.multiline and area.bbox[1] == 100.0 and area.bbox[3] == 159.0


def test_a_lone_rule_across_the_page_is_not_a_blank():
    p = page(line("_" * 100, x0=40.0, x1=570.0))
    assert blanks.detect(p, []) == 0 and texts(p) == ["_" * 100]


def test_a_signature_line_is_a_text_input_named_by_its_caption():
    p = page(line("Provider’s Signature: ____________________ Date ________", x1=500.0))
    blanks.detect(p, [])
    plan = plan_forms(p, p.lines, p.lines, [])
    assert [n.text for n in plan.flow if n.role.value == "field"] == ["Provider’s Signature:", "Date"]


def test_a_converted_form_has_inputs_named_by_captions_and_no_pdf_field_to_fill(tmp_path):
    from conftest import make_text_pdf
    from reflow.convert import convert
    from reflow.render.chunks import BLANK_FORM_NOTE, FORM_NOTE, SCAN_FORM_NOTE

    pdf = make_text_pdf(tmp_path / "form.pdf", [[
        (54, 72, 12, "Patient Information", "B"),
        (54, 110, 10, "Name: ______________________ Phone: ______________"),
        (54, 140, 10, "Signature: ________________________________"),
        (54, 200, 10, "Nothing here is a blank."),
    ]])
    convert(pdf, tmp_path / "out")
    html = (tmp_path / "out/index.html").read_text()
    assert "_____" not in html
    assert html.count('data-field-source="blank"') == 3
    for caption in ("Name:", "Phone:", "Signature:"):
        assert f">{caption}</label>" in html
    assert BLANK_FORM_NOTE in html and FORM_NOTE not in html and SCAN_FORM_NOTE not in html
    assert "data-pdf-field" not in html


def test_a_blank_a_sentence_runs_on_through_stays_text():
    for text in ("I ____ agree.", "I ____ agree to the terms", "We ____ hereby swear."):
        p = page(line(text))
        assert blanks.detect(p, []) == 0 and texts(p) == [text], text
    assert blanks.detect(page(line("Birth Weight: ______ grams Gestational Age: ______")), []) == 2


def test_a_blank_in_quotation_marks_is_a_placeholder_not_a_field():
    for text in ('Enter "____" when unknown.', "Use “____” for missing values."):
        p = page(line(text))
        assert blanks.detect(p, []) == 0 and texts(p) == [text], text


def test_a_right_column_field_stays_in_its_column():
    """Two columns read one after the other: a field at the top of the right column goes after
    the whole left column, not after the left column's line on its row."""
    from reflow.analyze.forms import place_flow
    from reflow.model import Node, Role
    first, last = (Node(Role.P, page=1, bbox=b, text=t) for b, t in
                   (((40, 100, 300, 111), "left first"), ((40, 200, 300, 211), "left last")))
    below = Node(Role.P, page=1, bbox=(320, 150, 580, 161), text="right below")
    right = Node(Role.FIELD, page=1, bbox=(320, 100, 580, 111), attrs={"kind": "text"})
    assert place_flow([first, last, below], [right]) == [first, last, right, below]
