"""A printed form ruled into boxes is read as a form, not as one table: the grid finder's
form layout (analyze.form_grid), the check boxes a form draws (analyze.drawn_boxes, extract.drawings)
and what the planner (analyze.forms) and the table repair (analyze.tables.repair) do for them.
Synthetic pages: boxes, lines and widgets at set positions."""

from __future__ import annotations

import pickle
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
from pikepdf import Dictionary

from reflow.analyze import drawn_boxes, form_grid
from reflow.analyze.forms import plan_forms
from reflow.extract.drawings import page_drawings_and_fills
from reflow.extract.forms import Detection, DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.extract.tables.cells import TableData
from reflow.analyze.tables.repair import unscramble_cells
from reflow.model import Role

SIZE = 6.0


def _line(x0: float, top: float, text: str, bold: bool = False, size: float = SIZE) -> Line:
    return Line(1, (x0, top, x0 + 3 * len(text), top + size), text, size, bold, False, "Arial")


def _square(x: float, top: float, side: float = 10.0) -> tuple[float, float, float, float]:
    return (x, top, x + side, top + side)


def _grid(rows: list[list[tuple[float, float, float, float] | None]], texts: list[list[str | None]]
          ) -> TableData:
    boxes = [b for row in rows for b in row if b is not None]
    return TableData(1, (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                         max(b[3] for b in boxes)), texts, grid=rows)


def _form_page() -> tuple[TableData, list[Line]]:
    """Two columns of boxes of every size, each with a numbered caption at its top-left and room
    below, then a services band of four rows of one set of columns (a taller header and three
    lines)."""
    rows: list = []
    texts: list = []
    lines: list[Line] = []
    top = 10.0
    number = 1
    for split, height in ((100, 30), (70, 34), (120, 28), (90, 40), (80, 32)):
        rows.append([(10, top, split, top + height), (split, top, 190, top + height)])
        texts.append([f"{number}. A caption", f"{number + 1}. B caption"])
        lines += [_line(12, top + 2, f"{number}. A caption"), _line(split + 2, top + 2, f"{number + 1}. B caption")]
        number += 2
        top += height
    cols = [(10, 40), (40, 80), (80, 120), (120, 160), (160, 190)]
    for k in range(4):
        height = 20 if k == 0 else 12   # the header row is taller than the lines under it
        rows.append([(x0, top, x1, top + height) for x0, x1 in cols])
        heads = ["24. Date", "25. Area", "26. Tooth", "27. Number", "28. Surface"]
        texts.append(heads if k == 0 else [""] * 5)
        if k == 0:
            lines += [_line(x0 + 2, top + 2, head) for (x0, _), head in zip(cols, heads)]
        top += height
    return _grid(rows, texts), lines


def test_a_grid_of_numbered_boxes_is_a_form_and_its_repeated_rows_stay_a_table():
    table, lines = _form_page()
    kept, regions = form_grid.split([table], lines)
    assert [t.method for t in kept] == ["ruled"] and len(kept[0].rows) == 4
    assert kept[0].header_rows == 1 and max(len(r) for r in kept[0].rows) == 5
    assert len(regions) == 1 and regions[0].bbox[3] == 174
    assert len(regions[0].lines) == 10


def test_a_table_with_a_numbered_first_column_is_not_a_form():
    rows = [[(10, 10 + 12 * i, 40, 22 + 12 * i), (40, 10 + 12 * i, 100, 22 + 12 * i)] for i in range(20)]
    texts = [[f"{i + 1}.", "item"] for i in range(20)]
    lines = [l for i in range(20) for l in (_line(12, 12 + 12 * i, f"{i + 1}."), _line(42, 12 + 12 * i, "item"))]
    table = _grid(rows, texts)
    kept, regions = form_grid.split([table], lines)
    assert kept == [table] and regions == []


def test_a_grid_that_numbers_its_rows_in_each_column_is_not_a_form():
    # the I-9's lists of acceptable documents: 1. 2. 3. under List A, again under List B
    rows, texts, lines = [], [], []
    for i in range(9):
        top = 10 + 40 * i
        rows.append([(10, top, 100, top + 40), (100, top, 190, top + 40)])
        texts.append([f"{i % 3 + 1}. one document", f"{i % 3 + 1}. another"])
        lines += [_line(12, top + 2, texts[-1][0]), _line(102, top + 2, texts[-1][1])]
    table = _grid(rows, texts)
    kept, regions = form_grid.split([table], lines)
    assert kept == [table] and regions == []


def test_a_grid_with_no_numbered_captions_is_not_a_form():
    table, lines = _form_page()
    plain = [_line(l.x0, l.top, l.text.split(". ", 1)[1]) for l in lines]
    kept, regions = form_grid.split([table], plain)
    assert kept == [table] and regions == []


def test_the_items_come_in_the_printed_numbering_and_a_bar_goes_before_its_first_item():
    table, lines = _form_page()
    _, [region] = form_grid.split([table], lines + [_line(12, 160 - 4, "SECTION BAR", bold=True)])
    grid = form_grid.build(region, set(), [])
    texts = [n.text for n in grid.nodes]
    numbered = [t for t in texts if t[0].isdigit()]
    assert numbered == [f"{n}. {'A' if n % 2 else 'B'} caption" for n in range(1, 11)]
    # a bar under the last caption has no item below it: it stays last
    assert grid.nodes[-1].role == Role.H2 and grid.nodes[-1].text == "SECTION BAR"


def test_a_bar_that_is_a_sentence_is_no_heading():
    table, lines = _form_page()
    bars = form_grid.bars(form_grid.Region(1, table.bbox, [], lines + [
        _line(12, 20, "Si la respuesta es Si, complete el punto 9.", bold=True),
        _line(12, 30, "SECTION BAR", bold=True)]))
    assert [l.text for l in bars] == ["SECTION BAR"]


def test_a_caption_that_wraps_stays_one_item_and_the_next_caption_starts_another():
    box = (10.0, 10.0, 100.0, 60.0)
    region = form_grid.Region(1, box, [box], [
        _line(12, 12, "52. Phone"), _line(22, 19, "Number ( ) -"), _line(12, 40, "53. Name")])
    texts = [n.text for n in form_grid.build(region, set(), []).nodes]
    assert texts == ["52. Phone Number ( ) -", "53. Name"]


def test_a_line_that_crosses_a_wall_is_cut_before_the_caption_after_it():
    left, right = (10.0, 10.0, 33.0, 40.0), (33.0, 10.0, 110.0, 40.0)
    line = _line(12, 12, "D ____ 32. Total Fee")
    region = form_grid.Region(1, (10, 10, 110, 40), [left, right], [line])
    # 3 points a character from x=12: the wall at x=33 is at character 7, where "32." starts
    cuts = form_grid.wall_cuts([region], [line])
    assert [p.text for p in cuts[id(line)]] == ["D ____", "32. Total Fee"]
    plain = _line(12, 12, "D ____ and more text")
    assert form_grid.wall_cuts([region], [plain]) == {}   # no caption after the wall: left whole


def test_a_column_that_every_row_of_the_band_merges_is_dropped_from_the_kept_table():
    table, lines = _form_page()
    grid, texts = list(table.grid[:5]), list(table.rows[:5])
    top = grid[-1][0][3]
    cols = [(10, 40), (40, 120), None, (120, 160), (160, 190)]
    heads = ["24. Date", "25. Area", None, "27. Number", "28. Surface"]
    for k in range(4):
        height = 20 if k == 0 else 12
        grid.append([c and (c[0], top, c[1], top + height) for c in cols])
        texts.append(heads if k == 0 else ["", "", None, "", ""])
        if k == 0:
            lines += [_line(c[0] + 2, top + 2, head) for c, head in zip(cols, heads) if c]
        top += height
    kept, regions = form_grid.split([_grid(grid, texts)], lines)
    assert len(regions) == 1
    assert [len(r) for r in kept[0].rows] == [4] * 4 and kept[0].rows[0][2] == "27. Number"
    assert [len(r) for r in kept[0].grid] == [4] * 4


def _services_with_subrows(subrows: bool = True) -> tuple[TableData, list[Line]]:
    """The form of `_form_page`, its band a header row of five columns over six lines, each a
    row of three merged cells with a shorter row of two (the shaded NPI line) under it."""
    table, lines = _form_page()
    grid = [row for row in table.grid[:5]]
    texts = [row for row in table.rows[:5]]
    top = grid[-1][0][3]
    cols = [(10, 40), (40, 80), (80, 120), (120, 160), (160, 190)]
    grid.append([(x0, top, x1, top + 20) for x0, x1 in cols])
    heads = ["24. Date", "25. Area", "26. Tooth", "27. Number", "28. Surface"]
    texts.append(heads)
    lines += [_line(x0 + 2, top + 2, head) for (x0, _), head in zip(cols, heads)]
    top += 20
    for _ in range(6):
        grid.append([(10, top, 120, top + 24), None, None, (120, top, 160, top + 12), (160, top, 190, top + 12)])
        texts.append(["", None, None, "", ""])
        grid.append([None, None, None, (120, top + 12, 160, top + 24), (160, top + 12, 190, top + 24)])
        texts.append([None, None, None, "NPI", ""])
        top += 24
    return _grid(grid, texts), lines


def test_service_lines_with_merged_subrows_stay_a_table_under_their_header():
    table, lines = _services_with_subrows()
    kept, regions = form_grid.split([table], lines)
    assert len(kept) == 1 and len(kept[0].rows) == 13 and kept[0].header_rows == 1
    assert len(regions) == 1 and regions[0].bbox[3] == table.grid[4][0][3]


def test_rows_that_alternate_with_no_header_over_them_are_no_band_and_the_grid_is_left_alone():
    table, lines = _services_with_subrows()
    table = TableData(table.page, table.bbox, table.rows[:5] + table.rows[6:], grid=table.grid[:5] + table.grid[6:])
    kept, regions = form_grid.split([table], lines)
    assert kept == [table] and regions == []


# -- drawn check boxes -----------------------------------------------------------------------


def _page(lines: list[Line], squares: list) -> PageContent:
    return PageContent(1, 612, 792, lines, [], squares=squares)


def test_a_drawn_square_with_a_label_beside_it_is_a_check_box():
    page = _page([_line(25, 102, "M"), _line(65, 102, "F")], [_square(10, 100), _square(50, 100)])
    assert drawn_boxes.check_boxes(page) == [_square(10, 100), _square(50, 100)]


def test_a_lone_square_or_squares_without_labels_are_no_check_boxes():
    assert drawn_boxes.check_boxes(_page([_line(25, 102, "M")], [_square(10, 100)])) == []
    assert drawn_boxes.check_boxes(_page([_line(300, 500, "far")], [_square(10, 100), _square(50, 100)])) == []


def test_touching_squares_are_a_comb_and_a_square_with_text_in_it_is_no_check_box():
    comb = [_square(10 + 10 * i, 100) for i in range(4)]
    assert drawn_boxes.check_boxes(_page([_line(60, 102, "ID")], comb)) == []
    page = _page([_line(25, 102, "Yes"), _line(65, 102, "No"), _line(52, 102, "X"), _line(105, 102, "Maybe")],
                 [_square(10, 100), _square(50, 100), _square(90, 100)])
    assert drawn_boxes.check_boxes(page) == [_square(10, 100), _square(90, 100)]


def test_a_square_beside_a_figure_or_off_the_page_is_no_check_box():
    marks = _page([_line(25, 102, "-0.5"), _line(65, 102, "1.2")], [_square(10, 100), _square(50, 100)])
    assert drawn_boxes.check_boxes(marks) == []   # a chart's markers, with numbers beside them
    slug = _page([_line(25, -98, "Draft"), _line(65, -98, "Pt. size")], [_square(10, -100), _square(50, -100)])
    assert drawn_boxes.check_boxes(slug) == []    # a printer's slug above the page


def test_a_box_labelled_on_its_left_is_a_check_box():
    page = _page([_line(2, 102, "Dental?"), _line(40, 102, "Medical?")], [_square(26, 100), _square(76, 100)])
    assert len(drawn_boxes.check_boxes(page)) == 2


def test_a_document_with_a_form_of_its_own_gets_no_drawn_boxes():
    page = _page([_line(25, 102, "M"), _line(65, 102, "F")], [_square(10, 100), _square(50, 100)])
    page.widgets.append(Widget(1, 0, "text", (200, 100, 260, 112)))
    assert drawn_boxes.inject([page]) == 0 and len(page.widgets) == 1


def test_injected_boxes_are_detected_checkboxes_and_leave_a_blank_widget_alone():
    page = _page([_line(25, 102, "M"), _line(65, 102, "F")], [_square(10, 100), _square(50, 100)])
    page.widgets.append(Widget(1, 0, "text", (200, 100, 260, 112), detected=Detection(DetectionSource.TEXT, "underscores")))
    assert drawn_boxes.inject([page]) == 2
    made = [w for w in page.widgets if w.kind == "checkbox"]
    assert [w.detected for w in made] == [Detection(DetectionSource.DRAWN, "square")] * 2
    assert [w.index for w in page.widgets if w.kind == "checkbox"] == [1, 2]


def _drawn_pdf(path: Path) -> Path:
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    page.Resources = Dictionary()
    ops = ["0.5 w",
           "10 100 10 10 re S",        # a stroked square: a check box
           "40 100 10 10 re S",
           "70 100 10 5 re S",         # a flat box
           "100 100 10 10 re f",       # a dark filled square: a bullet
           "130 100 m 140 100 l 135 110 l h S"]   # a triangle
    page.Contents = pdf.make_stream("\n".join(ops).encode())
    pdf.save(path)
    return path


def test_the_page_keeps_its_stroked_squares_apart_from_its_other_paths(tmp_path):
    doc = pdfium.PdfDocument(_drawn_pdf(tmp_path / "squares.pdf"))
    page = doc[0]
    try:
        squares: list = []
        page_drawings_and_fills(page, squares=squares)
    finally:
        page.close()
        doc.close()
    assert [round(s[0]) for s in sorted(squares)] == [10, 40]
    assert all(abs((s[2] - s[0]) - 10) < 1.5 for s in squares)


# -- the planner and the repair --------------------------------------------------------------


def _drawn(index: int, x: float, top: float) -> Widget:
    return Widget(1, index, "checkbox", (x, top, x + 10, top + 10), detected=Detection(DetectionSource.DRAWN, "square"))


def test_choices_in_two_boxes_of_a_form_grid_are_two_groups_not_one_stack():
    # "7. Gender  M F" in one box, "10. Relationship  Self Spouse" in the box under it: the boxes'
    # check boxes line up in a column, so without the grid they would be one stack.
    upper, lower = (10.0, 10.0, 100.0, 40.0), (10.0, 40.0, 100.0, 70.0)
    lines = [_line(12, 12, "7. Gender"), _line(25, 25, "M"), _line(55, 25, "F"),
             _line(12, 42, "10. Relationship"), _line(25, 55, "Self"), _line(55, 55, "Spouse")]
    widgets = [_drawn(0, 12, 24), _drawn(1, 42, 24), _drawn(2, 12, 54), _drawn(3, 42, 54)]
    page = PageContent(1, 612, 792, lines, [], widgets=widgets)
    region = form_grid.Region(1, (10, 10, 100, 70), [upper, lower], lines)
    plan = plan_forms(page, lines, lines, [form_grid.as_table(region)])
    groups = [n for nodes in plan.cells.values() for n in nodes if n.role == Role.GROUP]
    assert sorted((g.text, [k.text for k in g.children]) for g in groups) == [
        ("10. Relationship", ["Self", "Spouse"]), ("7. Gender", ["M", "F"])]
    assert plan.flow == []


def test_a_row_that_prints_each_label_before_its_box_names_the_last_box_by_the_words_before_it():
    # "Dental? [] Medical? [] (note)": the note after the last box is not its label
    box = (10.0, 10.0, 300.0, 30.0)
    lines = [_line(12, 14, "4. Dental?"), _line(80, 14, "Medical?"), _line(140, 14, "(If both, complete 5-11.)")]
    widgets = [_drawn(0, 60, 12), _drawn(1, 120, 12)]
    page = PageContent(1, 612, 792, lines, [], widgets=widgets)
    region = form_grid.Region(1, box, [box], lines)
    plan = plan_forms(page, lines, lines, [form_grid.as_table(region)])
    [group] = [n for nodes in plan.cells.values() for n in nodes if n.role == Role.GROUP]
    assert [k.text for k in group.children] == ["4. Dental?", "Medical?"]
    assert id(lines[2]) not in plan.consumed


def test_a_drawn_box_takes_the_words_tight_to_its_right_over_the_label_a_little_farther_left():
    # "Type of Appeal [] Denied Claim [] Cost Share": each box's own label touches it
    box = (10.0, 10.0, 300.0, 30.0)
    lines = [_line(12, 14, "Type of Appeal"), _line(72, 14, "Denied Claim"), _line(124, 14, "Cost Share")]
    widgets = [_drawn(0, 60, 12), _drawn(1, 112, 12)]
    page = PageContent(1, 612, 792, lines, [], widgets=widgets)
    region = form_grid.Region(1, box, [box], lines)
    plan = plan_forms(page, lines, lines, [form_grid.as_table(region)])
    [group] = [n for nodes in plan.cells.values() for n in nodes if n.role == Role.GROUP]
    assert [k.text for k in group.children] == ["Denied Claim", "Cost Share"]
    assert id(lines[0]) in plan.consumed   # the question is the legend, not a box's label


def test_a_cell_the_cells_reader_mixed_is_read_again_from_the_lines():
    box = (10.0, 10.0, 100.0, 40.0)
    table = TableData(1, box, [["5 2 . P N h u o m n b e e r ( ) -"]], grid=[[box]])
    lines = [_line(12, 12, "52. Phone"), _line(22, 15, "Number ( ) -")]
    fixed = unscramble_cells(table, lines)
    assert fixed.rows == [["52. Phone Number ( ) -"]]


def test_a_cell_that_only_differs_in_spaces_and_hyphens_is_left_alone():
    box = (10.0, 10.0, 100.0, 40.0)
    table = TableData(1, box, [["Number (thou- sands)"]], grid=[[box]])
    lines = [_line(12, 12, "Number (thou\u00ad"), _line(12, 20, "sands)")]
    assert unscramble_cells(table, lines) is table


def test_a_cell_is_left_alone_when_the_lines_do_not_spell_its_text():
    box = (10.0, 10.0, 100.0, 40.0)
    table = TableData(1, box, [["Date of birth"]], grid=[[box]])
    assert unscramble_cells(table, [_line(12, 12, "Date of"), _line(12, 20, "Birth day")]) is table


def test_a_planned_form_grid_survives_a_pickle_to_the_parent_process():
    box = (10.0, 10.0, 100.0, 60.0)
    lines = [_line(12, 12, "52. Phone"), _line(12, 40, "53. Name")]
    grid = form_grid.build(form_grid.Region(1, box, [box], lines), set(), [])
    back = pickle.loads(pickle.dumps(grid))
    assert [(n.role, n.text) for n in back.nodes] == [(Role.P, "52. Phone"), (Role.P, "53. Name")]


def test_drawn_ellipses_stacked_in_a_list_are_radio_buttons():
    ovals = [(10, 100 + 7 * i, 22, 107 + 7 * i) for i in range(3)]   # a list: they touch
    page = PageContent(1, 612, 792, [_line(30, 100 + 7 * i, "ELECTOR") for i in range(3)], [], ovals=ovals)
    assert drawn_boxes.radio_buttons(page) == ovals
    assert drawn_boxes.inject([page]) == 3 and {w.kind for w in page.widgets} == {"radio"}


def test_a_flat_box_a_little_wider_than_tall_is_a_check_box():
    flat = [(10.0, 100.0, 26.0, 111.8), (60.0, 100.0, 76.0, 111.8)]
    page = _page([_line(28, 102, "Denied Claim"), _line(78, 102, "Cost Share")], flat)
    assert drawn_boxes.check_boxes(page) == flat


def test_a_box_inside_a_line_and_a_question_run_into_its_first_option_are_cut_out():
    from reflow.analyze.drawn_lines import split_drawn_lines
    # "Induction of Labor? Yes [ ] No [ ] Reason": the second box stands inside the line "No Reason"
    q, tail = _line(10, 100, "Induction of Labor? Yes"), _line(100, 100, "No Reason")
    boxes = [_drawn(0, 80, 100), _drawn(1, 109, 100)]
    page = PageContent(1, 612, 792, [q, tail], [], widgets=boxes)
    cuts = split_drawn_lines(page, [q, tail])
    assert [p.text for p in cuts[id(q)]] == ["Induction of Labor?", "Yes"]
    assert [p.text for p in cuts[id(tail)]] == ["No", "Reason"]


def test_a_row_of_drawn_boxes_with_labels_after_them_gives_each_box_its_own_label():
    # "[] not known) EDD: ___ (From [] LMP [] U/S) [] HIV Screening Complete": each label touches
    # the box before it. The words between two boxes are not the next box's label, and the open
    # "(From" runs into the label of the box it leads.
    def row(x0: float, x1: float, text: str) -> Line:
        return Line(1, (x0, 329.4, x1, 338.9), text, 10.1, False, False, "Arial")
    lines = [row(134.9, 210.7, "not known) EDD:"), row(276.6, 303.4, "(From"), row(324.6, 344.8, "LMP"),
             row(368.3, 387.7, "U/S)"), row(419.9, 528.8, "HIV Screening Complete")]
    widgets = [Widget(1, i, "checkbox", (x, 325.4, x + 14.0, 337.2),
                      detected=Detection(DetectionSource.DRAWN, "square"))
               for i, x in enumerate((118.3, 307.9, 351.8, 403.4))]
    page = PageContent(1, 612, 792, lines, [], widgets=widgets)
    plan = plan_forms(page, lines, lines, [])
    [group] = plan.flow
    assert [k.text for k in group.children] == [
        "not known) EDD:", "(From LMP", "U/S)", "HIV Screening Complete"]
    assert plan.consumed == {id(line) for line in lines}
