from dataclasses import replace

from reflow.extract.drawings import Drawing
from reflow.extract.lines import Line
from reflow.analyze.tables.aligned import aligned_tables, layout_gutter
from reflow.analyze.tables.records import split_records
from reflow.analyze.tables.repair import (
    adopt_unheld,
    merge_tables,
    recase_cells,
    repair_ruled,
    unheld_lines,
)
from reflow.analyze.tables.rows import SANS_WIDTHS, join_signs
from reflow.analyze.tables.vetoes import chart_grid, page_frame
from reflow.extract.tables.cells import TableData
from reflow.extract.tables.ruled import find_tables, is_entry_box, ruled_header_rows
from reflow.model import Role

PAGE_WIDTH = 612.0


def test_open_sided_table_keeps_spanning_label_and_stacked_cells_in_one_block(tmp_path):
    import pikepdf

    from conftest import make_text_pdf

    path = make_text_pdf(tmp_path / "blocks.pdf", [[
        (45, 43, 8, "stage"), (105, 43, 8, "size"),
        (165, 43, 8, "small"), (225, 43, 8, "large"),
        (45, 75, 8, "block A"), (105, 75, 8, "32x32"),
        (180, 63, 8, "shared operation"),
        (165, 83, 8, "first"), (165, 93, 8, "second"),
        (225, 83, 8, "third"), (225, 93, 8, "fourth"),
        (45, 113, 8, "block B"), (105, 113, 8, "16x16"),
        (165, 113, 8, "fifth"), (225, 113, 8, "sixth"),
    ]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        ops = []
        for y in (40, 60, 110, 130):
            ops.append(f"40 {792-y} m 280 {792-y} l S")
        ops.append("160 712 m 280 712 l S")  # a partial rule below the shared operation
        for x in (100, 160):
            ops.append(f"{x} 662 m {x} 752 l S")
        for top, bottom in ((40, 60), (80, 130)):
            ops.append(f"220 {792-bottom} m 220 {792-top} l S")
        pdf.pages[0].Contents = pdf.make_stream(
            pdf.pages[0].Contents.read_bytes() + b"\n" + "\n".join(ops).encode())
        pdf.save(path)

    [table] = find_tables(path, [1])[1]
    assert table.rows == [
        ["stage", "size", "small", "large"],
        ["block A", "32x32", "shared operation first second", "third fourth"],
        ["block B", "16x16", "fifth", "sixth"],
    ]
    assert table.bbox == (40, 40, 280, 130)


def test_a_ruled_cells_words_split_at_a_word_space_written_as_kerning(tmp_path):
    # fw9 and the I-9 with their word spaces written as TJ kerning, as pdfTeX writes them (#324):
    # an 8 pt space is 2.2 pt, under pdfplumber's default 3 pt at any size, and the cells read
    # "2553electingtobetaxedasa". A digit run kerned tight stays one word.
    from conftest import make_kerned_table_pdf

    [table] = find_tables(make_kerned_table_pdf(tmp_path / "kerned.pdf"), [1])[1]
    assert table.rows == [["two words", "2553 electing"], ["1706", "W-9"]]


def test_ruled_records_beside_a_tall_label_are_not_joined_into_one_block(tmp_path):
    import pikepdf

    from conftest import make_text_pdf

    path = make_text_pdf(tmp_path / "ledger.pdf", [[
        (45, 43, 8, "group"), (105, 43, 8, "item"), (165, 43, 8, "q1"), (225, 43, 8, "q2"),
        (45, 82, 8, "fees"),
        (105, 68, 8, "wire"), (165, 68, 8, "1,200"),
        (105, 93, 8, "audit"), (165, 93, 8, "300"), (225, 93, 8, "400"),
    ]])
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        ops = []
        for y, x0 in ((40, 40), (60, 40), (85, 100), (110, 40)):
            ops.append(f"{x0} {792-y} m 280 {792-y} l S")
        for x in (40, 100, 160, 280):
            ops.append(f"{x} 682 m {x} 752 l S")
        for top, bottom in ((40, 60), (85, 110)):
            ops.append(f"220 {792-bottom} m 220 {792-top} l S")
        pdf.pages[0].Contents = pdf.make_stream(
            pdf.pages[0].Contents.read_bytes() + b"\n" + "\n".join(ops).encode())
        pdf.save(path)

    [table] = find_tables(path, [1])[1]
    assert [[c or "" for c in row] for row in table.rows[1:]] == [
        ["fees", "wire", "1,200", ""],
        ["", "audit", "300", "400"],
    ]


def _text(x0, top, text, size=6.5, right=None):
    """A text segment. With `right`, the segment is placed so its right edge lands there."""
    width = sum(0.28 if ch in ". " else 0.55 for ch in text) * size
    if right is not None:
        x0 = right - width
    return Line(1, (round(x0, 2), top, round(x0 + width, 2), top + size), text, size,
                False, False, "Helvetica")


def _schedule(top, *, codes=36.0, labels=58.0, numbers=(234.0, 267.0, 300.0)):
    """A budget-style schedule: a code column, labels with dot leaders, and three numeric
    columns right-aligned to common edges, under a header of year labels. Positions are
    absolute page coordinates."""
    lines = [_text(codes, top, "Identification code 017–1106")]
    lines += [_text(0, top, h, right=r) for h, r in zip(("2023 actual", "2024 est.", "2025 est."),
                                                        numbers)]
    rows = [("0001", "Operating forces", ("8,268", "8,569", "8,869")),
            ("0003", "Training and recruiting", ("1,082", "1,129", "1,090")),
            ("0004", "Administration and Service-wide Activities", ("612", "585", "603"))]
    for i, (code, label, values) in enumerate(rows):
        y = top + 10 + 8 * i
        lines.append(_text(codes, y, code))
        lines.append(_text(labels, y, label + " " + "." * 40))
        lines += [_text(0, y, v, right=r) for v, r in zip(values, numbers)]
    return lines


def test_aligned_schedule_has_code_label_and_right_aligned_numbers():
    [table] = aligned_tables(1, _schedule(100.0), PAGE_WIDTH)
    assert table.method == "aligned"
    assert table.header_rows == 1
    assert table.rows[0][2:] == ["2023 actual", "2024 est.", "2025 est."]
    # Leaders are not cell text; the code and the label are separate cells.
    assert table.rows[1] == ["0001", "Operating forces", "8,268", "8,569", "8,869"]
    assert table.rows[3] == ["0004", "Administration and Service-wide Activities",
                             "612", "585", "603"]
    assert len(table.rows[0]) == 5
    assert "numbers right-aligned at x" in table.why


def test_two_layout_columns_never_merge_into_one_table():
    lines = _schedule(100.0) + _schedule(100.0, codes=312.0, labels=334.0,
                                         numbers=(510.0, 543.0, 576.0))
    tables = aligned_tables(1, lines, PAGE_WIDTH)
    assert len(tables) == 2
    for t in tables:
        assert t.bbox[2] <= 301 or t.bbox[0] >= 311


def test_a_six_point_gutter_between_schedule_and_column_is_found_exactly():
    # Budget p933: the left schedule's last figures end at x=300 and the right column starts at
    # x=305.98. The gap is 6 pt; on a whole-point grid it measured 5, so it was missed and the left
    # schedule's "2025 est." column went to the right-hand tables.
    left = _schedule(100.0)
    right = _schedule(100.0, codes=305.98, labels=328.0, numbers=(510.0, 543.0, 576.0))
    lines = left + right
    gutter = layout_gutter(lines, PAGE_WIDTH)
    assert gutter == (300.0, 305.98)
    tables = aligned_tables(1, lines, PAGE_WIDTH)
    assert len(tables) == 2
    left_table = min(tables, key=lambda t: t.bbox[0])
    assert left_table.rows[0][2:] == ["2023 actual", "2024 est.", "2025 est."]
    assert left_table.rows[1][4] == "8,869"


def test_prose_with_numbers_is_not_a_table():
    lines = [_text(72.0, 100.0 + 12 * i, f"Revenue rose to {3000 + i:,} in 2024 and then fell",
                   size=10.0) for i in range(6)]
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_two_columns_of_words_without_rules_are_not_a_table():
    lines = [_text(72.0, 100.0 + 12 * i, f"Left column line {i} of running text here", size=10.0)
             for i in range(5)]
    lines += [_text(320.0, 100.0 + 12 * i, f"Right column line {i} of running text", size=10.0)
              for i in range(5)]
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_empty_form_boxes_are_dropped():
    ssn = [["Social security number"], ["", "", "", "", "", "", "", "", ""]]
    ein = [["Employer identification number", None, None], ["", "–", ""]]
    assert is_entry_box(ssn)
    assert is_entry_box(ein)


def test_a_table_with_data_is_kept():
    assert not is_entry_box([["Year", "Revenue"], ["2024", "$10"], ["Total", ""]])


def test_table_node_records_how_the_table_was_found():
    from reflow.analyze.table_nodes import table_node
    node = table_node(TableData(1, (0, 0, 10, 10), [["Year", "Revenue"], ["2024", "$10"]],
                                method="aligned", header_rows=1,
                                why="aligned columns: 2 rows × 2 columns, 1 header row"))
    assert node.role == Role.TABLE
    assert node.attrs["method"] == "aligned"
    assert node.attrs["rows"] == 2 and node.attrs["columns"] == 2
    assert node.attrs["header_rows"] == 1
    assert node.attrs["why"] == ["aligned columns: 2 rows × 2 columns, 1 header row"]
    assert node.children[0].children[0].role == Role.TH
    assert node.children[1].children[0].role == Role.TD
    assert node.children[0].children[0].attrs["why"]


def test_aligned_table_extends_a_partly_ruled_one():
    ruled = TableData(1, (147, 633, 230, 670), [["8x8", "16x16"], ["1", "2"]])
    full = TableData(1, (82, 633, 254, 670), [["h", "8x8", "16x16"], ["x", "1", "2"]],
                     method="aligned")
    assert merge_tables([ruled], [full]) == [full]


def test_ruled_table_wins_where_an_aligned_one_only_overlaps():
    ruled = TableData(1, (100, 100, 300, 200), [["a", "b"], ["1", "2"]])
    overlap = TableData(1, (250, 150, 400, 260), [["c", "d"], ["3", "4"]], method="aligned")
    assert merge_tables([ruled], [overlap]) == [ruled]


def test_aligned_table_outside_ruled_tables_is_added():
    ruled = TableData(1, (100, 100, 300, 200), [["a", "b"], ["1", "2"]])
    other = TableData(1, (100, 400, 300, 500), [["c", "d"], ["3", "4"]], method="aligned")
    assert merge_tables([ruled], [other]) == [ruled, other]


def test_an_aligned_table_over_two_ruled_tables_does_not_join_them():
    # The rate sheet: BlueValue rates over Standard rates, each ruled. Alignment reads both
    # (and the title above) as one table, which must not replace the two.
    blue = TableData(1, (27, 121, 583, 351), [["BLUEVALUE RATES", None], ["AGE", "Plan A"], ["65", "$222.67"]])
    standard = TableData(1, (27, 366, 583, 597), [["STANDARD RATES", None], ["AGE", "Plan A"], ["65", "$359.63"]])
    both = TableData(1, (27, 43, 583, 597), [["AGE", "Plan A"], ["65", "$222.67"]], method="aligned")
    assert merge_tables([blue, standard], [both]) == [blue, standard]


def test_a_banner_over_column_names_is_two_header_rows():
    banner = [["BLUEVALUE RATES", None, None], ["AGE", "Plan A", "Plan C*"],
              ["65", "$222.67", "$178.66"], ["66", "$237.41", "$190.48"]]
    assert ruled_header_rows(banner) == 2
    # Column names that hold figures, or a first row of several cells, are not a banner.
    assert ruled_header_rows([banner[0], banner[2], banner[3]]) == 1
    assert ruled_header_rows(banner[1:]) == 1


def _listing(x, top, name, address, phone, n=4):
    """A provider listing: a name, an address, and a phone number, one under another."""
    lines = []
    for i in range(n):
        y = top + 30 * i
        lines += [_text(x, y, f"{name} {i}, MD (M)", size=9.0),
                  _text(x, y + 10, address, size=9.0),
                  _text(x, y + 20, f"(520) 555-01{i}2", size=9.0)]
    return lines


def test_directory_listings_are_not_a_table():
    lines = _listing(40.0, 100.0, "Zhang", "2200 S Houghton Rd Tucson, AZ 85748", None)
    lines += _listing(300.0, 100.0, "Irwin", "1521 E Tangerine Rd Oro Valley, AZ 85755", None)
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_index_entries_with_dot_leaders_are_not_a_table():
    lines = []
    for i in range(6):
        y = 100.0 + 12 * i
        lines.append(_text(40.0, y, f"Abrams, Madison G, PA . . . . . . . . 24{i}", size=9.0))
        lines.append(_text(300.0, y, f"Amend, Rance , FNP . . . . . . . . 7{i}", size=9.0))
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_a_label_running_into_a_number_column_loses_no_text():
    # The first line's 2023 value sits inline after its leader dots, so the label runs out
    # into the number column. The label cells must not vanish from a table that keeps going.
    lines = _schedule(100.0)
    lines.append(_text(36.0, 134.0, "1231"))
    lines.append(_text(58.0, 134.0, "Disbursements: Direct loan disbursements "
                       + "." * 60 + " 13,360,421"))
    tables = aligned_tables(1, lines, PAGE_WIDTH)
    kept = " ".join(c or "" for t in tables for row in t.rows for c in row)
    assert "13,360,421" in kept or tables == []


def _schedule_with_wrapped_label(next_line_code=False):
    """A schedule whose row 0004 wraps: its label's first line holds no figures, and its second
    line ("Activities") carries them. Row spacing is 8 points, as in _schedule. With
    `next_line_code`, the second line is a row of its own ("0005") and 0004 keeps no figures."""
    # A heading line across the table keeps the gap between the 2023 and 2024 columns from
    # reading as a layout gutter (a schedule's dot leaders do that in print).
    lines = [_text(36.0, 90.0, "Operating schedule of the agency, with its figures stated in millions of dollars for each year")]
    lines += [_text(36.0, 100.0, "Identification code 017–1106")]
    lines += [_text(0, 100.0, h, right=r) for h, r in zip(("2023 actual", "2024 est.", "2025 est."),
                                                          (234.0, 267.0, 300.0))]
    lines += [_text(36.0, 110.0, "0001"), _text(58.0, 110.0, "Operating forces")]
    lines += [_text(0, 110.0, v, right=r) for v, r in zip(("8,268", "8,569", "8,869"),
                                                          (234.0, 267.0, 300.0))]
    lines += [_text(36.0, 118.0, "0004"), _text(58.0, 118.0, "Administration and Service-wide")]
    if next_line_code:
        lines += [_text(36.0, 126.0, "0005"), _text(58.0, 126.0, "Training and recruiting")]
        lines += [_text(0, 126.0, v, right=r) for v, r in zip(("1,082", "1,129", "1,090"),
                                                              (234.0, 267.0, 300.0))]
    else:
        lines.append(_text(62.0, 126.0, "Activities"))
        lines += [_text(0, 126.0, v, right=r) for v, r in zip(("612", "585", "603"),
                                                              (234.0, 267.0, 300.0))]
    return lines


def test_wrapped_label_takes_the_figures_of_its_second_line():
    [table] = aligned_tables(1, _schedule_with_wrapped_label(), PAGE_WIDTH)
    rows = table.rows
    assert ["0004", "Administration and Service-wide Activities", "612", "585", "603"] in rows
    # The first line of the label holds no figure, so nothing is left on a row of its own.
    assert all(any(c for c in row) for row in rows)


def test_a_row_code_on_the_next_line_starts_a_new_row():
    # "0005" starts its own row, so the label above keeps no figures and stays a row of its own.
    [table] = aligned_tables(1, _schedule_with_wrapped_label(next_line_code=True), PAGE_WIDTH)
    rows = table.rows
    assert ["0004", "Administration and Service-wide", "", "", ""] in rows
    assert any(row[0] == "0005" for row in rows)


def test_heading_with_a_colon_labels_two_lines_of_figures():
    # A "Total:" block: two lines of figures under a heading, with no year labels above them.
    lines = [_text(40.0, 100.0, "Total:")]
    lines.append(_text(50.0, 110.0, "Budget Authority " + "." * 30))
    lines.append(_text(50.0, 120.0, "Outlays " + "." * 30))
    for y, values in ((110.0, ("755", "1,018", "1,855")), (120.0, ("732", "1,024", "1,789"))):
        lines += [_text(0, y, v, right=r) for v, r in zip(values, (234.0, 267.0, 300.0))]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows == [["Budget Authority", "755", "1,018", "1,855"],
                          ["Outlays", "732", "1,024", "1,789"]]


def test_two_figure_lines_without_a_heading_are_not_a_table():
    lines = [_text(50.0, 110.0, "Budget Authority " + "." * 30)]
    lines.append(_text(50.0, 120.0, "Outlays " + "." * 30))
    for y, values in ((110.0, ("755", "1,018", "1,855")), (120.0, ("732", "1,024", "1,789"))):
        lines += [_text(0, y, v, right=r) for v, r in zip(values, (234.0, 267.0, 300.0))]
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_running_text_that_starts_with_a_number_is_not_a_row_code():
    # "1992 and beyond …" in running text: the line is prose, not a row with a code.
    lines = [_text(40.0, 100.0 + 10 * i, f"1992 and beyond line {i}", size=7.0)
             for i in range(3)]
    lines += [_text(40.0, 130.0, "Object Classification", size=7.0)]
    from reflow.analyze.tables.rows import split_codes
    split = split_codes(lines)
    assert [l.text for l in split] == [l.text for l in lines]


def test_resnet_architecture_table_keeps_all_four_columns():
    # Synthetic copy of the layout of resnet p.7 (positions from the page): a label column and
    # three figure columns, with no rules of its own. A partial ruled table (2 × 2, from its
    # vertical rules) covers the middle of it; the aligned table covers the whole grid and
    # replaces the ruled one. Lost columns here mean the aligned lines were withheld.
    from reflow.analyze.layout import analyze
    from reflow.extract.drawings import Drawing
    from reflow.extract.pages import PageContent
    from reflow.model import Role
    # (x0, x1, top, text), size 9
    cells = [(82.17, 138.95, 635.30, "output map size"), (155.29, 180.39, 634.70, "32×32"),
             (196.73, 221.83, 634.70, "16×16"), (238.17, 254.30, 634.70, "8×8"),
             (96.49, 124.63, 647.76, "# layers"), (158.05, 177.63, 647.66, "1+2n"),
             (204.26, 214.30, 647.66, "2n"), (241.22, 251.26, 647.66, "2n"),
             (96.99, 124.14, 659.81, "# filters"), (163.36, 172.33, 659.80, "16"),
             (204.80, 213.76, 659.86, "32"), (241.75, 250.72, 659.80, "64")]
    lines = [Line(1, (x0, top, x1, top + 8.07), text, 9.0, False, False, "Helvetica")
             for x0, x1, top, text in cells]
    ruled = TableData(1, (147.12, 632.79, 230.0, 669.75),
                      [["32×32", "16×16"], ["1+2n 16", "2n 32"]], method="ruled")
    # The page's rules frame the grid (its horizontal rules): a words-and-figures table with
    # no numbers in full columns counts only when rules frame it.
    rules = [Drawing((82.0, 633.0, 255.0, 669.0), paths=6, rules=6)]
    page = PageContent(1, PAGE_WIDTH, 792.0, lines, [], rules)
    [table] = [n for n in analyze([page], {1: [ruled]}) if n.role == Role.TABLE]
    rows = [[c.text for c in tr.children] for tr in table.children]
    assert rows == [["output map size", "32×32", "16×16", "8×8"],
                    ["# layers", "1+2n", "2n", "2n"],
                    ["# filters", "16", "32", "64"]]


def test_figures_under_a_colon_heading_need_no_year_labels():
    # A schedule that continues into the next column: no year labels above it, only the
    # heading ending in a colon ("Offsets against gross budget authority and outlays:").
    # A wide line across the page keeps the gap between the figure columns from reading as a
    # layout gutter (see test_schedule_with_wrapped_label).
    lines = [_text(36.0, 80.0, "Operating schedule of the agency, with its figures stated in millions of dollars for each year"),
             _text(36.0, 88.0, "Operating schedule of the agency, with its figures stated in millions of dollars for each year")]
    lines.append(_text(40.0, 100.0, "Offsets against gross budget authority and outlays:"))
    rows = [("4030", "Federal sources", ("–211", "–325", "–331")),
            ("4040", "Offsets, total", ("–301", "–325", "–331")),
            ("4180", "Budget authority, net (total)", ("9,943", "9,866", "10,563"))]
    for i, (code, label, values) in enumerate(rows):
        y = 110.0 + 8 * i
        lines += [_text(36.0, y, code), _text(58.0, y, label)]
        lines += [_text(0, y, v, right=r) for v, r in zip(values, (234.0, 267.0, 300.0))]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows[0] == ["4030", "Federal sources", "–211", "–325", "–331"]
    assert table.rows[2] == ["4180", "Budget authority, net (total)", "9,943", "9,866", "10,563"]
    assert table.header_rows == 0


def _coded_block(codes=True, leaders=True):
    """The right-hand reestimate block of a budget page, its year headings a page back: rows that
    open with a line code (or, without `codes`, with a label), dot leaders, figures right-aligned.
    The block sits right of the page's centre, as the real one does (within one half of the page)."""
    rows = [("234011", "Guaranteed 502 Single Family Housing", ("–57", "–80", "–154")),
            ("234999", "Total subsidy outlays", ("–64", "–88", "–161")),
            ("235001", "Guaranteed 502 Single Family Housing, Purchase", ("19", "–45", "")),
            ("235011", "Guaranteed 502 Single Family Housing", ("138", "–535", "")),
            ("3510", "Budget authority", ("412", "412", "412"))]
    lines = []
    for i, (code, label, values) in enumerate(rows):
        y = 100.0 + 9 * i
        if codes:
            lines.append(_text(336.0, y, code, size=6.5))
        lines.append(_text(360.0 if codes else 336.0, y,
                           label + (" " + "." * 30 if leaders else ""), size=6.5))
        lines += [_text(0, y, v, right=r) for v, r in zip(values, (530.0, 563.0, 596.0))]
    return rows, lines


def test_budget_rows_with_line_codes_form_a_table_without_headings():
    # The rows carry leaders and figures, but a row code makes them rows of a schedule: they reach
    # table detection instead of reading as running text.
    rows, lines = _coded_block()
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows == [[code, label, *values] for code, label, values in rows]
    assert table.header_rows == 0


def test_figures_with_leaders_and_no_codes_or_headings_are_not_a_table():
    _, lines = _coded_block(codes=False)
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_a_table_records_the_boxes_of_its_cells():
    # The analysis drops the lines that are cells, not every line inside the table's box: a prose
    # line beside a schedule keeps its text. Empty values are not cells.
    _, lines = _coded_block()
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert len(table.cells) == len([l for l in lines if l.text])


def test_a_header_less_schedule_across_the_page_centre_is_not_a_table():
    # Two schedules side by side are never one table: the same rows, moved so that they straddle
    # the page's centre line, are not a header-less table.
    from dataclasses import replace
    _, lines = _coded_block()
    shifted = [replace(l, bbox=(l.x0 - 240, l.top, l.x1 - 240, l.bottom)) for l in lines]
    assert aligned_tables(1, shifted, PAGE_WIDTH) == []


def test_a_header_less_schedule_missing_a_year_column_is_not_a_table():
    # Budget p379: Status of Direct Loans with "2023 actual" empty in every row. With no year
    # headings, two columns of figures can't say which years they are: the first would read as
    # 2023 when it is 2024, so the rows stay as text.
    rows = [("1231", "Disbursements: Direct loan disbursements", ("478", "3,473")),
            ("1261", "Adjustments: Capitalized interest", ("9", "89")),
            ("1290", "Outstanding, end of year", ("487", "4,049"))]
    lines = []
    for i, (code, label, values) in enumerate(rows):
        y = 100.0 + 9 * i
        lines += [_text(336.0, y, code, size=6.5), _text(360.0, y, label + " " + "." * 30, size=6.5)]
        lines += [_text(0, y, v, right=r) for v, r in zip(values, (563.0, 596.0))]
    assert aligned_tables(1, lines, PAGE_WIDTH) == []



def _one_figure_row_under_years(heads=("2023 actual", "2024 est.", "2025 est.")):
    """Budget p458 (Employment Summary): a header of year labels over one row of figures, with
    a code and a label. Each year column holds one figure."""
    lines = [_text(36.0, 100.0, "Identification code 075–0139–0–1–551", size=6.0)]
    for h, (x0, x1) in zip(heads, ((208.5, 233.8), (244.3, 263.9), (277.3, 296.9))):
        lines.append(Line(1, (x0, 100.0, x1, 106.0), h, 6.0, False, False, "Helvetica"))
    # The code and the label are separate segments, as the extractor reads them: the code is a
    # figure-like cell, so the label's column has to come from the schedule's stub heading.
    lines.append(_text(36.0, 110.0, "1001", size=6.5))
    lines.append(Line(1, (52.5, 110.0, 201.0, 116.5),
                      "Direct civilian full-time equivalent employment " + "." * 20,
                      6.5, False, False, "Helvetica"))
    for v, r in zip(("1,012", "982", "879"), (234.0, 267.0, 300.0)):
        lines.append(_text(0, 110.0, v, right=r))
    return lines


def test_a_year_heading_over_one_row_of_figures_is_a_four_column_schedule():
    [table] = aligned_tables(1, _one_figure_row_under_years(), PAGE_WIDTH)
    assert table.header_rows == 1
    assert table.rows == [
        ["Identification code 075–0139–0–1–551", "2023 actual", "2024 est.", "2025 est."],
        ["1001 Direct civilian full-time equivalent employment", "1,012", "982", "879"],
    ]


def test_one_row_of_figures_under_non_year_headings_is_not_a_table():
    lines = _one_figure_row_under_years(heads=("Current", "Enacted", "Request"))
    assert aligned_tables(1, lines, PAGE_WIDTH) == []

# --- rows read as one line, and ruled tables that lose lines ------------------------------

def _line_at(x0, top, text, size=9.0, x1=None):
    width = sum(0.28 if ch in ". " else 0.55 for ch in text) * size
    return Line(1, (x0, top, x1 if x1 is not None else x0 + width, top + size), text, size,
                False, False, "Helvetica")


def test_a_row_of_figures_read_as_one_line_stays_in_its_table():
    # Table ES-2 of the EPA inventory: the CO2 row came out as one line of seven figures, and
    # the table was refused for it, so its rows (CO2, PFCs, LULUCF ...) were lost.
    numbers = (247.8, 288.3, 328.9, 369.3, 409.9, 450.3, 490.9)
    lines = [_line_at(76, 518, "Gas/Source")]
    lines += [_text(0, 518, y, size=9.0, right=r)
              for y, r in zip(("1990", "2005", "2018", "2019", "2020", "2021", "2022"), numbers)]
    rows = [("CO2", ("5,131.6", "6,126.9", "5,362.2", "5,234.5", "4,689.0", "5,017.2", "5,053.0")),
            ("CH4", ("871.7", "795.4", "771.5", "754.3", "735.3", "720.5", "702.4")),
            ("HFCs", ("47.7", "121.7", "163.9", "168.2", "170.3", "177.0", "182.8")),
            ("PFCs", ("39.5", "10.2", "7.4", "7.3", "6.6", "6.3", "6.7"))]
    for i, (label, values) in enumerate(rows):
        top = 530 + 12 * i
        lines.append(_line_at(76, top, label))
        if label == "CO2":  # one line, as the extractor read it
            first = sum(0.5 if ch.isdigit() else 0.25 for ch in values[0]) * 9.0
            lines.append(_line_at(numbers[0] - first, top, " ".join(values), x1=numbers[-1]))
        else:
            lines += [_text(0, top, v, size=9.0, right=r) for v, r in zip(values, numbers)]
    # A footnote under the table runs on into the figure columns: it is not a wrapped label.
    lines.append(_line_at(76, 578, "a Gross emissions totals do not include CH4 and N2O emissions "
                                   "from LULUCF.", x1=480))
    [table] = aligned_tables(1, lines)  # one layout column: a few lines only
    assert [r[0] for r in table.rows] == ["Gas/Source", "CO2", "CH4", "HFCs", "PFCs"]
    assert table.rows[1][1:] == ["5,131.6", "6,126.9", "5,362.2", "5,234.5", "4,689.0",
                                 "5,017.2", "5,053.0"]


def _ruled(rows, grid, bbox=(100, 100, 400, 160)):
    return TableData(1, bbox, rows, method="ruled", grid=grid)


def test_unheld_lines_finds_a_line_no_cell_holds():
    grid = [[(100, 100, 250, 130), (250, 100, 400, 130)],
            [(100, 130, 250, 160), (250, 130, 400, 160)]]
    table = _ruled([["Item", "Cost"], ["Pens", ""]], grid)
    held = _line_at(110, 105, "Item")
    lost = _line_at(260, 135, "12.50")  # the grid's text extraction left this one out
    assert unheld_lines(table, [held, lost]) == [lost]
    assert unheld_lines(replace(table, grid=[]), [held, lost]) == []  # no cell boxes: all held


def test_a_spanning_heading_outside_the_cells_is_added_with_a_colspan():
    # Census table A-7: "Full-time, year-round workers" sits right of the only cell of its
    # row ("Total workers"), over columns the rules do not reach.
    grid = [[(100, 100, 250, 120), None, None, None],
            [(100, 120, 175, 140), (175, 120, 250, 140), (250, 120, 325, 140), (325, 120, 400, 140)]]
    table = _ruled([["Total workers", None, None, None], ["a", "b", "c", "d"]], grid,
                   bbox=(100, 100, 400, 140))
    heading = _line_at(270, 104, "Full-time, year-round workers", x1=390)
    assert unheld_lines(table, [heading]) == [heading]
    fixed = repair_ruled(table, [heading])
    assert fixed.rows[0] == ["Total workers", None, "Full-time, year-round workers", None]
    assert unheld_lines(fixed, [heading]) == []
    assert "Full-time, year-round workers" in fixed.why


def test_cell_words_take_the_case_of_the_text_lines():
    # ADA dental claim form p1: the cells' reader maps small-capital glyphs one by one and
    # gave "OTHER COVERAgE"; the text layer's /ActualText says "other coverage".
    grid = [[(100, 100, 250, 130), (250, 100, 400, 130)]]
    table = _ruled([["OTHER COVERAgE (Mark box) 4. Dental?", "Medical?"]], grid,
                   bbox=(100, 100, 400, 130))
    lines = [_line_at(105, 105, "other coverage (Mark box)"), _line_at(105, 116, "4. Dental?"),
             _line_at(255, 105, "Medical?")]
    fixed = recase_cells(table, lines)
    assert fixed.rows == [["other coverage (Mark box) 4. Dental?", "Medical?"]]
    assert "case" in fixed.why
    assert recase_cells(fixed, lines) is fixed  # nothing left to change


def test_a_cell_word_the_lines_spell_two_ways_keeps_its_own_case():
    grid = [[(100, 100, 400, 130)]]
    table = _ruled([["NAME"]], grid, bbox=(100, 100, 400, 130))
    lines = [_line_at(105, 105, "Name"), _line_at(205, 105, "name")]
    assert recase_cells(table, lines).rows == [["NAME"]]


def test_a_line_whose_cell_is_taken_is_not_forced_into_it():
    grid = [[(100, 100, 250, 130), (250, 100, 400, 130)]]
    table = _ruled([["Item", "Cost"]], grid, bbox=(100, 100, 400, 130))
    stray = _line_at(260, 110, "extra")
    assert adopt_unheld(table, [stray]).rows == [["Item", "Cost"]]  # stays in the text flow


def test_an_aligned_table_replaces_a_ruled_one_that_loses_its_rows():
    # pdfplumber's grid had cells only in some rows: its text came out in a few cells.
    lines = [_line_at(110, 105 + 12 * i, t) for i, t in enumerate(["CO2", "CH4", "HFCs", "PFCs"])]
    grid = [[(100, 100, 250, 112), (250, 100, 400, 112)]] + [[None, None]] * 3
    ruled = _ruled([["CO2", ""], [None, None], [None, None], [None, None]], grid,
                   bbox=(100, 100, 400, 150))
    aligned = TableData(1, (105, 100, 400, 150), [["CO2"], ["CH4"], ["HFCs"], ["PFCs"]],
                        method="aligned", cells=[l.bbox for l in lines])
    assert merge_tables([ruled], [aligned]) == [ruled]  # no lines to judge by: ruled wins
    assert merge_tables([ruled], [aligned], lines) == [aligned]


def test_a_ruled_table_that_loses_many_lines_is_filled_again_by_column():
    # EPA p130: the grid had cells only in some rows (shaded rows), so most figures were lost.
    # The widest row gives the columns; every line goes to its row and column.
    cols = [(100, 150), (150, 200), (200, 250)]
    grid = [[(lo, 100 + 12 * i, hi, 112 + 12 * i) for lo, hi in cols] for i in range(1)]
    grid += [[(100, 112 + 12 * i, 250, 124 + 12 * i), None, None] for i in range(3)]
    rows = [["Gas", "1990", "2005"], ["CO2", None, None], ["CH4", None, None], ["HFCs", None, None]]
    table = _ruled(rows, grid, bbox=(100, 100, 250, 148))
    lines = [_line_at(102, 102, "Gas"), _line_at(170, 102, "1990"), _line_at(220, 102, "2005")]
    for i, (label, a, b) in enumerate([("CO2", "5.1", "6.1"), ("CH4", "0.9", "0.8"), ("HFCs", "4.7", "12")]):
        top = 114 + 12 * i
        lines += [_line_at(102, top, label), _line_at(172, top, a), _line_at(222, top, b)]
    assert len(unheld_lines(table, lines)) == 6
    fixed = repair_ruled(table, lines)
    assert fixed.rows == [["Gas", "1990", "2005"], ["CO2", "5.1", "6.1"], ["CH4", "0.9", "0.8"],
                          ["HFCs", "4.7", "12"]]
    assert unheld_lines(fixed, lines) == []


def _grid_table(fill_cells, coloured=0, curves=0):
    """A ruled table of two columns and four rows (eight cells), `fill_cells` of them with text."""
    flat = ["x"] * fill_cells + [""] * (8 - fill_cells)
    rows = [flat[i:i + 2] for i in range(0, 8, 2)]
    return TableData(1, (100, 100, 400, 220), rows, method="ruled", coloured=coloured,
                     coloured_curves=curves)


def _chart(marks=(), paths=12):
    return Drawing((90.0, 90.0, 410.0, 230.0), paths=paths, rules=0, fills=6, strokes=6,
                   outlines=6, marks=marks)


def test_a_sparse_grid_with_coloured_rules_inside_is_a_chart_gridline():
    # A coloured chart: its labels sit outside a grid with most cells empty.
    table = _grid_table(fill_cells=1, coloured=40)
    assert chart_grid(table, []).startswith("gridlines of a chart")


def test_a_sparse_worksheet_in_black_and_white_is_a_table():
    # A worksheet's answer boxes: sparse, no colour, no chart marks (census p33, Pub 17).
    table = _grid_table(fill_cells=3, coloured=0)
    assert chart_grid(table, [_chart(marks=(("ticks", 24),))]) is None


def test_a_filled_table_is_a_table_whatever_its_colour():
    table = _grid_table(fill_cells=8, coloured=3)
    assert chart_grid(table, []) is None


def test_a_full_grid_crossed_by_coloured_data_curves_is_a_chart_gridline():
    # Full cells and no dots, but four coloured data series run through the grid (resnet p8).
    table = _grid_table(fill_cells=8, coloured=10, curves=4)
    assert chart_grid(table, [_chart(marks=(("ticks", 30),))]).startswith("gridlines of a chart")


def test_a_full_grid_with_a_stray_coloured_curve_is_a_table():
    # One or two coloured curves in a table (a rule drawn as a curve, a logo) are not a chart.
    table = _grid_table(fill_cells=8, coloured=2, curves=2)
    assert chart_grid(table, [_chart(marks=(("ticks", 30),))]) is None


def test_a_grid_inside_a_drawing_with_dots_is_a_chart_gridline():
    # A chart's own small table of values: full cells, but dots around them.
    table = _grid_table(fill_cells=8, coloured=0)
    assert chart_grid(table, [_chart(marks=(("dots", 33),))]).startswith("gridlines inside a chart")


def test_a_legend_table_with_a_few_icons_and_zebra_bars_is_a_table():
    # Seven icons (dots) in a legend table, and a zebra-shaded table's repeated bars: tables.
    table = _grid_table(fill_cells=8, coloured=0)
    assert chart_grid(table, [_chart(marks=(("dots", 7),))]) is None
    assert chart_grid(table, [_chart(marks=(("bars", 11), ("ticks", 120)))]) is None
    sparse = _grid_table(fill_cells=3, coloured=0)
    assert chart_grid(sparse, [_chart(marks=(("bars", 11),))]) is None


def test_a_grid_outside_any_drawing_is_never_a_chart_by_marks_alone():
    table = _grid_table(fill_cells=8, coloured=0)
    assert chart_grid(table, [_chart(marks=(("wedges", 2),))]) is None


def _figure_rows(top, rows, rights=(300.0, 340.0), step=10.0):
    """Labels at the left margin and figures right-aligned to `rights`, one row per entry."""
    lines = []
    for i, (label, values) in enumerate(rows):
        y = top + step * i
        lines.append(_text(50, y, label))
        lines += [_text(0, y, v, right=r) for v, r in zip(values, rights)]
    return lines


def test_a_lower_case_label_with_its_own_figures_is_a_row_not_a_wrapped_line():
    # Table 14 (ResNet): "ours (ILSVRC'15)" starts in lower case but has figures of its own.
    lines = [_text(50, 88.0, "method"), _text(0, 88.0, "val", right=300.0),
             _text(0, 88.0, "test", right=340.0)]
    lines += _figure_rows(100.0, [("OverFeat (ILSVRC'13)", ("30.0", "29.9")),
                                  ("VGG (ILSVRC'14)", ("25.3", "24.8")),
                                  ("ours (ILSVRC'15)", ("9.0", "8.9"))])
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert [row[0] for row in table.rows] == ["method", "OverFeat (ILSVRC'13)", "VGG (ILSVRC'14)",
                                              "ours (ILSVRC'15)"]
    assert table.rows[3][1:] == ["9.0", "8.9"]


def test_a_heading_over_two_columns_is_a_colspan_of_the_header():
    # Table 14 (ResNet): "top-5 localization err" spans the two figure columns below it.
    heading = _text(0, 88.0, "top-5 localization err", right=340.0)
    lines = [heading, _text(50, 100.0, "method"), _text(0, 100.0, "val", right=300.0),
             _text(0, 100.0, "test", right=340.0)]
    lines += _figure_rows(110.0, [("VGG (ILSVRC'14)", ("25.3", "24.8")),
                                  ("ours (ILSVRC'15)", ("9.0", "8.9")),
                                  ("GoogLeNet (ILSVRC'14)", ("26.7", "25.6"))])
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows[0] == ["", "top-5 localization err", None]
    assert table.rows[1] == ["method", "val", "test"]
    assert [row[0] for row in table.rows[2:]] == ["VGG (ILSVRC'14)", "ours (ILSVRC'15)",
                                                  "GoogLeNet (ILSVRC'14)"]


def test_two_header_lines_over_figures_are_two_rows_not_one():
    # Table 7 (ResNet): "training data" over "07+12", then "test data" (lower case, no figures
    # of its own) over "VOC 07 test": a line of words over the figure columns is a header line.
    lines = [_text(50, 88.0, "training data"), _text(0, 88.0, "07+12", right=300.0),
             _text(0, 88.0, "07++12", right=340.0),
             _text(50, 98.0, "test data"), _text(0, 98.0, "VOC 07 test", right=300.0),
             _text(0, 98.0, "VOC 12 test", right=340.0)]
    lines += _figure_rows(108.0, [("VGG-16", ("73.2", "70.4")), ("ResNet-101", ("76.4", "73.8"))])
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows == [["training data", "07+12", "07++12"],
                          ["test data", "VOC 07 test", "VOC 12 test"],
                          ["VGG-16", "73.2", "70.4"],
                          ["ResNet-101", "76.4", "73.8"]]


def test_running_text_beside_a_table_is_not_its_label_column():
    # Page 11 (ResNet): a paragraph's lines share the rows of Table 12 on the right, so the
    # label column reads as sentences of about nine words.
    paragraph = ["We select two adjacent scales from the pyramid following",
                 "the feature maps of these two scales are merged by maxout as in",
                 "by over two points in the table"]
    lines = [_text(50, 100.0 + 10 * i, t) for i, t in enumerate(paragraph)]
    lines += [_text(340, 100.0 + 10 * i, t, size=6.5) for i, t in enumerate(
        ["GoogLeNet (ILSVRC'14)", "our single model (ILSVRC'15)", "our ensemble (ILSVRC'15)"])]
    lines += [_text(0, 100.0 + 10 * i, v, right=r) for i, vs in enumerate(
        [("43.9", "58.8"), ("60.5", "58.8"), ("63.6", "62.1")]) for v, r in zip(vs, (480.0, 520.0))]
    for table in aligned_tables(1, lines, PAGE_WIDTH):
        assert not any(cell and "We select two adjacent scales" in cell
                       for row in table.rows for cell in row)


def test_a_full_width_caption_does_not_hide_the_column_gutter_below_it():
    # Page 11 (ResNet): a full-width table caption is one line crossing the page centre. Among
    # few lines it is more than the share of crossings a page-wide gutter allows, so no gutter is
    # found for the page. The gutter below the caption is clear, and the right column's table
    # must not take the left column's paragraph into its label cells.
    caption = _text(50, 100.0, "Table 12: Results of the single-model and ensemble comparison "
                               "across the full width of the page, with the sources noted below")
    assert caption.x0 <= PAGE_WIDTH * 0.38 and caption.x1 >= PAGE_WIDTH * 0.62
    paragraph = ["We select two adjacent scales from the pyramid following",
                 "the feature maps of these two scales are merged by maxout as in",
                 "the detector, and the outputs are combined by a simple sum for",
                 "each of the four levels before the final classification layer",
                 "is applied to the merged map at every stride of the network.",
                 "The model is trained with the same schedule as the baseline."]
    left = [_text(50, 120.0 + 10 * i, t) for i, t in enumerate(paragraph)]
    right = _schedule(120.0, codes=312.0, labels=334.0, numbers=(510.0, 543.0, 576.0))
    source = _text(50, 180.0, "Source: the sources listed in the text, with the results of the "
                              "comparison across the full width of the page for each model")
    lines = [caption] + left + right + [source]
    assert layout_gutter(lines, PAGE_WIDTH) is None
    tables = aligned_tables(1, lines, PAGE_WIDTH)
    assert tables
    for table in tables:
        assert table.bbox[0] >= 300
        assert not any(cell and "We select two adjacent scales" in cell
                       for row in table.rows for cell in row)
    assert any(table.rows[1] == ["0001", "Operating forces", "8,268", "8,569", "8,869"]
               for table in tables)


def test_numbered_list_items_in_a_label_column_are_entries_not_running_text():
    # Page 5 (fw9): a framed two-column table whose labels are numbered list items, several of
    # them wrapped to a second line. Their words average more than a label column of sentences
    # may, but a numbered item is a list entry, so the table is kept with its items whole.
    lines = [_text(50, 100.0, "For this type of account:"),
             _text(220, 100.0, "Give name and SSN of:")]
    items = [
        (112.0, "2. Two or more individuals (joint", "account) or other joint owners", "The individuals"),
        (126.0, "3. Custodian account of a minor", "(Uniform Gift to Minors Act)", "The minor"),
        (140.0, "6. Sole proprietorship or disregarded", "entity owned by an individual", "The owner"),
        (154.0, "7. Grantor trust filing under Optional", "Form 1099 Filing Method 1", "The grantor"),
        (168.0, "9. A valid trust, estate, or pension trust", None, "The trust"),
        (180.0, "10. Corporation or LLC electing corporate status", None, "The corporation"),
        (192.0, "12. Partnership or multi-member LLC", None, "The partnership"),
    ]
    for top, first, cont, value in items:
        lines.append(_text(50, top, first))
        if cont:
            lines.append(_text(50, top + 8.0, cont))
        lines.append(_text(220, top, value))
    [table] = aligned_tables(1, lines, PAGE_WIDTH, [(40.0, 90.0, 590.0, 230.0)])
    assert table.header_rows == 1
    assert table.rows[0] == ["For this type of account:", "Give name and SSN of:"]
    assert table.rows[3] == ["6. Sole proprietorship or disregarded entity owned by an individual",
                             "The owner"]
    assert len(table.rows) == 8
    assert all(len(row) == 2 for row in table.rows)


def _sans(x0, top, text, size=9.0, right=None):
    """A text segment as wide as the sans glyph table says, so an estimated word position
    inside it is exact (the extractor keeps no glyph positions)."""
    width = sum(SANS_WIDTHS.get(ch, 556) for ch in text) * size / 1000
    if right is not None:
        x0 = right - width
    return Line(1, (round(x0, 2), top, round(x0 + width, 2), top + size), text, size,
                False, False, "Helvetica")


def _glued_rows(top=100.0):
    """A four-column table (ResNet Table 13) where one row's first three cells came out as
    one line: "RPN+RCNN ResNet-101 dense". The columns start where the glued line's words do."""
    col2 = _sans(50, 0, "RPN+RCNN").x1 + 8  # column gaps wider than a space
    col3 = _sans(col2, 0, "ResNet-101").x1 + 8
    lines = [_sans(50, top, "method"), _sans(col2, top, "network"), _sans(col3, top, "testing"),
             _sans(0, top, "err", right=220)]
    for i, (a, b, c, v) in enumerate([("RPN", "ResNet-101", "1-crop", "13.3"),
                                      (None, "RPN+RCNN ResNet-101 dense", None, "11.7"),
                                      ("RPN", "ResNet-101", "dense", "10.6")]):
        y = top + 10 + 10 * i
        if a is None:  # one line from the first column's left edge to the third's right edge
            glued = _sans(50, y, b)
            lines.append(replace(glued, bbox=(50.0, y, _sans(col3, y, "dense").x1, y + 9.0)))
        else:
            lines += [_sans(50, y, a), _sans(col2, y, b), _sans(col3, y, c)]
        lines.append(_sans(0, y, v, right=220))
    return lines


def test_a_line_glued_across_columns_is_cut_at_the_columns_of_its_neighbours():
    [table] = aligned_tables(1, _glued_rows(), PAGE_WIDTH)
    assert len(table.rows) == 4 and all(len(r) == 4 for r in table.rows)
    assert table.rows[2] == ["RPN+RCNN", "ResNet-101", "dense", "11.7"]


def test_running_text_under_a_table_is_not_cut_into_its_columns():
    lines = _glued_rows() + [_sans(50, 140.0, "The dense setting wins over the others here.")]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert len(table.rows) == 4
    assert not any(cell and "dense setting" in cell for row in table.rows for cell in row)


def test_two_line_headings_stacked_on_one_centre_are_one_header_row():
    # ResNet Table 13: "LOC" over "method", "LOC" over "network", "LOC error" over "on GT CLS".
    lines = []
    for text, under, x0 in (("LOC", "method", 50.0), ("LOC", "network", 110.0),
                            ("LOC error", "on GT CLS", 170.0)):
        low = _sans(x0, 108.0, under)
        high = _sans(0, 100.0, text)
        shift = (low.x0 + low.x1) / 2 - (high.x0 + high.x1) / 2
        lines += [low, replace(high, bbox=(high.x0 + shift, 100.0, high.x1 + shift, 109.0))]
    for i, v in enumerate(("13.3", "11.7", "10.6")):
        y = 118.0 + 10 * i
        lines += [_sans(50, y, "RPN"), _sans(110, y, "ResNet-101"), _sans(0, y, v, right=215)]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows[0] == ["LOC method", "LOC network", "LOC error on GT CLS"]
    assert len(table.rows) == 4 and table.header_rows == 1


def test_centred_labels_with_two_left_edges_are_one_label_column():
    # ResNet Table 6: labels centred in their column, so their left edges differ by more than
    # an indent, and the long ones reach under the "# layers" figures of other rows.
    lines = [_text(95, 100.0, "method", 9.0), _text(0, 100.0, "error (%)", 9.0, right=220)]
    for i, (label, n, err) in enumerate([("NIN [25]", None, "8.81"), ("DSN [24]", None, "8.22"),
                                         ("ResNet", "20", "8.75"), ("ResNet", "32", "7.51"),
                                         ("ResNet", "44", "7.17")]):
        y = 110.0 + 10 * i
        lines.append(_text(95 if n is None else 75, y, label, 9.0))
        if n:
            lines.append(_text(0, y, n, 9.0, right=133))
        lines.append(_text(0, y, err, 9.0, right=220))
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert len(table.rows) == 6 and all(len(r) == 3 for r in table.rows)
    assert table.rows[1] == ["NIN [25]", "", "8.81"]
    assert table.rows[3] == ["ResNet", "20", "8.75"]


def test_a_heading_over_two_number_columns_in_a_header_row_spans_them():
    # ResNet Table 9: "COCO train" over "@.5" and "@[.5, .95]", in a row with "data" beside it.
    lines = [_text(50, 100.0, "data", 9.0), _text(192, 100.0, "COCO train", 9.0),
             _text(50, 110.0, "mAP", 9.0), _text(0, 110.0, "@.5", 9.0, right=200),
             _text(0, 110.0, "@[.5, .95]", 9.0, right=250)]
    for i, (label, a, b) in enumerate([("baseline", "41.5", "21.2"), ("+box", "48.4", "27.2"),
                                       ("+context", "49.9", "29.9")]):
        y = 120.0 + 10 * i
        lines += [_text(50, y, label, 9.0), _text(0, y, a, 9.0, right=200),
                  _text(0, y, b, 9.0, right=250)]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.rows[0] == ["data", "COCO train", None]
    assert table.rows[1] == ["mAP", "@.5", "@[.5, .95]"]
    assert table.header_rows == 2 and len(table.rows) == 5


# --- grids of scanned words ---------------------------------------------------------------

def _ocr(x0, top, text, x1=None):
    line = _line_at(x0, top, text, size=10.0, x1=x1)
    return replace(line, ocr=True)


def _scanned_units(skew=0.0, header=True):
    """A units table as a scan gives it: no rules, a header on three levels and a hyphenated
    word, labels in the first column, the cells of a row a few points off its baseline."""
    lines = [_ocr(100, 30, "1. FUNDAMENTAL UNITS")]
    if header:
        lines += [_ocr(250, 60, "Metric"), _ocr(390, 60, "English"), _ocr(160, 76, "Symbol"),
                  _ocr(230, 88, "Unit"), _ocr(300, 86, "Abbrevia-"), _ocr(305, 94, "tion"),
                  _ocr(380, 88, "Unit"), _ocr(460, 86, "Abbrevia-"), _ocr(465, 94, "tion")]
    rows = [("Length", "l", "meter", "m", "foot", "ft"), ("Time", "t", "second", "s", "hour", "hr"),
            ("Force", "F", "kilogram", "kg", "pound", "lb"), ("Power", "P", "watt", "W", "horse", "hp")]
    for i, cells in enumerate(rows):
        top = 116 + 12 * i
        for x, text, shift in zip((100, 160, 230, 300, 380, 460), cells, (0, 1, -1, 2, -2, 1)):
            lines.append(_ocr(x, top + skew * shift, text))
    return lines


def test_scanned_words_in_columns_are_a_table_without_rules():
    [table] = aligned_tables(1, _scanned_units(), PAGE_WIDTH)
    assert table.rows[2] == ["Length", "l", "meter", "m", "foot", "ft"]
    assert table.rows[5][0] == "Power"
    assert len(table.rows) == 6


def test_row_skew_does_not_split_a_row():
    [table] = aligned_tables(1, _scanned_units(skew=2.0), PAGE_WIDTH)
    assert [row[2] for row in table.rows[2:]] == ["meter", "second", "kilogram", "watt"]
    assert [row[5] for row in table.rows[2:]] == ["ft", "hr", "lb", "hp"]


def test_a_header_split_over_lines_is_one_header():
    [table] = aligned_tables(1, _scanned_units(), PAGE_WIDTH)
    assert table.header_rows == 2
    assert table.rows[0] == ["", "", "Metric", None, "English", None]  # spans its two columns
    assert table.rows[1] == ["", "Symbol", "Unit", "Abbreviation", "Unit", "Abbreviation"]


def test_the_section_title_above_a_scanned_header_is_not_part_of_the_table():
    [table] = aligned_tables(1, _scanned_units(), PAGE_WIDTH)
    assert table.bbox[1] >= 60
    assert all("FUNDAMENTAL" not in (cell or "") for row in table.rows for cell in row)


def test_scanned_words_without_a_header_are_not_a_table():
    assert aligned_tables(1, _scanned_units(header=False), PAGE_WIDTH) == []


def test_typeset_words_in_columns_are_still_not_a_table():
    lines = [replace(l, ocr=False) for l in _scanned_units()]
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_scanned_columns_of_sentences_are_not_a_table():
    lines = []
    for i in range(4):
        for x in (100, 230, 360, 490):
            lines.append(_ocr(x, 100 + 12 * i, "the quick brown fox jumps over the lazy dog"))
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_short_sentences_in_headed_columns_are_not_a_table():
    sentences = ("We went home.", "It was late.", "They left early.", "She read it.")
    lines = [_ocr(x, 60, head) for x, head in
             zip((100, 230, 360, 490), ("News", "Travel", "Science", "Culture"))]
    for i, text in enumerate(sentences):
        for x in (100, 230, 360, 490):
            lines.append(_ocr(x, 90 + 12 * i, text))
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_columns_that_do_not_follow_the_stub_rows_are_not_a_table():
    # Four columns of words side by side, each with its own line pitch: not one row per label.
    lines = [_ocr(x, 60, head) for x, head in
             zip((100, 230, 360, 490), ("Name", "City", "Trade", "Note"))]
    for k, x in enumerate((100, 230, 360, 490)):
        for i in range(4):
            lines.append(_ocr(x, 90 + (12 + 8 * k) * i, f"word{k}{i}"))
    assert aligned_tables(1, lines, PAGE_WIDTH) == []


def test_a_nearby_sentence_and_a_numbered_title_are_not_header_lines():
    lines = _scanned_units() + [_ocr(230, 44, "Read this first.")]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert table.header_rows == 2
    texts = {cell for row in table.rows for cell in row}
    assert "Read this first." not in texts and not any("FUNDAMENTAL" in (t or "") for t in texts)


def test_a_cell_between_two_rows_stays_in_the_row_its_column_order_gives():
    lines = [replace(l, bbox=(l.x0, l.top + 7, l.x1, l.bottom + 7)) if l.text == "second" else l
             for l in _scanned_units()]
    [table] = aligned_tables(1, lines, PAGE_WIDTH)
    assert [row[2] for row in table.rows[2:]] == ["meter", "second", "kilogram", "watt"]


def test_a_cell_far_from_every_row_is_never_filed_in_one():
    lines = [replace(l, bbox=(l.x0, l.top + 40, l.x1, l.bottom + 40)) if l.text == "second" else l
             for l in _scanned_units()]
    tables = aligned_tables(1, lines, PAGE_WIDTH)
    assert all("second" not in (cell or "") for t in tables for row in t.rows for cell in row)


def test_an_abbreviation_after_a_rule_is_its_own_cell_in_a_grid_only():
    lines = _scanned_units()
    fused = [replace(l, text="foot _____ ____ ft", bbox=(380, l.top, 475, l.bottom))
             if l.text == "foot" else l for l in lines]
    fused = [l for l in fused if l.text != "ft"]
    [table] = aligned_tables(1, fused, PAGE_WIDTH)
    assert table.rows[2][4] == "foot" and table.rows[2][5] == "ft"
    # Outside a grid the same line is left whole.
    [line] = [replace(l, text="Name: _____ on file", bbox=(40, 40, 140, 50)) for l in lines[:1]]
    assert aligned_tables(1, [line], PAGE_WIDTH) == []


def test_a_page_of_scattered_ocr_text_tries_only_a_few_stub_columns():
    from reflow.analyze.tables.cells import read_cell
    from reflow.analyze.tables.scanned import MAX_GRID_STUBS, stub_columns
    lines = [_ocr(10 + 20 * c, 20 + 12 * r, f"w{c}{r}") for c in range(40) for r in range(4)]
    cells = [read_cell(l) for l in lines]
    assert len(stub_columns(cells)) <= MAX_GRID_STUBS


def test_zapf_dingbats_letters_read_as_the_marks_they_draw():
    from reflow.extract.chars import symbol_box
    assert symbol_box("ZTYYXD+ZapfDingbatsITC", "l") == "●"
    assert symbol_box("ZapfDingbats", "4") == "✔"
    assert symbol_box("Helvetica", "l") is None


def _checklist(top=100.0, label_x1=None):
    """A checkup checklist: ages over the columns, a dot under each age a service applies to,
    and one label set on two lines with its dots centred between them."""
    ages = [370.0 + 30 * i for i in range(5)]
    lines = [_line_at(x, top, str(18 + 10 * i)) for i, x in enumerate(ages)]
    rows = [("Influenza (annually)", 5), ("Human papillomavirus (HPV)", 1),
            ("Colorectal Cancer: Screening", 3), ("Depression in Adults: Screening", 5),
            ("Diabetes Mellitus: Screening", 5), ("Lung cancer screening", 2),
            ("Skin Cancer: Counseling", 5), ("Syphilis: Screening", 5)]
    y = top + 18
    for label, first in rows[:2]:
        lines.append(_line_at(40.0, y, label, x1=label_x1))
        lines.append(_line_at(ages[5 - first], y, " ".join("●" * first), x1=ages[-1] + 6))
        y += 18
    # "Aspirin …" over "Preventive Medication", the dots on a line of their own between them.
    # Label lines 13 pt apart (11 pt type), the 10 pt dots straddling the line between them.
    lines.append(_line_at(40.0, y, "Aspirin for the Prevention of Cardiovascular Disease:",
                          size=13.0, x1=label_x1 or 280.0))
    lines.append(_line_at(ages[2], y + 8.5, "● ● ●", size=9.6, x1=ages[-1] + 6))
    lines.append(_line_at(40.0, y + 13, "Preventive Medication", size=13.0))
    y += 31
    for label, first in rows[2:]:
        lines.append(_line_at(40.0, y, label, x1=label_x1))
        lines.append(_line_at(ages[5 - first], y, " ".join("●" * first), x1=ages[-1] + 6))
        y += 18
    return lines


def test_a_grid_of_marks_is_a_table_with_one_mark_per_cell():
    [table] = aligned_tables(1, _checklist(), PAGE_WIDTH)
    assert table.header_rows == 1
    assert table.rows[0] == ["", "18", "28", "38", "48", "58"]
    assert table.rows[1] == ["Influenza (annually)", "●", "●", "●", "●", "●"]
    assert table.rows[2] == ["Human papillomavirus (HPV)", "", "", "", "", "●"]
    assert table.rows[3] == ["Aspirin for the Prevention of Cardiovascular Disease: Preventive Medication",
                             "", "", "●", "●", "●"]
    assert table.rows[7] == ["Lung cancer screening", "", "", "", "●", "●"]
    assert len(table.rows) == 10


def test_a_gap_beside_a_column_of_marks_is_no_layout_gutter():
    # Short labels leave a clear strip down the middle of the page: the marks beside them are
    # the table's columns, so the page is not split into two layout columns there.
    assert layout_gutter(_checklist(label_x1=200.0), PAGE_WIDTH) is None


def _boxed(x0, top, x1, bottom, text="word"):
    return Line(1, (x0, top, x1, bottom), text, 9.0, False, False, "Sans")


def _framed(bboxes):
    """A ruled table whose cells are `bboxes`, one per row."""
    return TableData(1, (0.0, 0.0, 600.0, 800.0), [["x"] for _ in bboxes], grid=[[b] for b in bboxes])


def test_a_ruled_frame_around_the_whole_page_with_one_cell_of_text_is_the_pages_border():
    cells = [(0.0, 0.0, 600.0, 600.0), (0.0, 600.0, 600.0, 800.0)]
    lines = [_boxed(50, 20 + 10 * i, 90, 28 + 10 * i) for i in range(40)] + [_boxed(50, 700, 90, 708)]
    assert page_frame(_framed(cells), 600.0, 800.0, lines).startswith("the page's frame")


def test_a_full_page_form_with_text_in_many_cells_is_a_table():
    cells = [(0.0, 100.0 * i, 600.0, 100.0 * (i + 1)) for i in range(8)]
    lines = [_boxed(50, 100 * i + 20, 90, 100 * i + 28) for i in range(8) for _ in range(3)]
    assert page_frame(_framed(cells), 600.0, 800.0, lines) is None


def test_a_table_smaller_than_the_page_is_not_its_frame():
    table = TableData(1, (0.0, 0.0, 600.0, 400.0), [["x"]], grid=[[(0.0, 0.0, 600.0, 400.0)]])
    assert page_frame(table, 600.0, 800.0, [_boxed(50, 20, 90, 28)]) is None


def _ruled_figures(top=100.0, rows=8):
    """A table of labels, two columns of figures and a column of words: its label column ends at
    x=250 and the other columns start at x=290, so a gap of 40 pt lies near the middle of the
    page."""
    lines = [_line_at(76.0, top, "Item", x1=100.0)]
    lines += [_text(0, top, h, size=9.0, right=r) for h, r in zip(("2023", "2022", "Generated"),
                                                                   (330.0, 400.0, 470.0))]
    for i in range(rows):
        y = top + 15 * (i + 1)
        lines.append(_line_at(76.0, y, f"Account number {i + 1}", x1=250.0))
        lines += [_text(0, y, f"{1000 * (i + 1) + k:,}", size=9.0, right=r)
                  for k, r in enumerate((330.0, 400.0))]
        lines.append(_text(0, y, "Auto", size=9.0, right=470.0))
    return lines


def test_the_gap_inside_a_ruled_table_is_no_layout_gutter():
    # Audit p24 and the RFP's report table: the label column and the figures lie either side of
    # the middle of the page. The rules frame the table, so its rows cross the gap.
    lines = _ruled_figures()
    frame = [(70.0, 95.0, 480.0, 235.0)]
    assert layout_gutter(lines, PAGE_WIDTH) is not None
    assert layout_gutter(lines, PAGE_WIDTH, frame) is None
    [table] = aligned_tables(1, lines, PAGE_WIDTH, frame)
    assert [len(r) for r in table.rows] == [4] * 9
    assert table.rows[1] == ["Account number 1", "1,000", "1,001", "Auto"]


def test_rules_that_frame_no_rows_leave_the_gap_a_layout_gutter():
    # A rule under a heading is not a frame: no rows of the page lie inside it.
    assert layout_gutter(_ruled_figures(), PAGE_WIDTH, [(70.0, 80.0, 480.0, 82.0)]) is not None


def test_a_lone_currency_sign_joins_the_figure_on_its_right():
    lines = [_line_at(273.2, 100.0, "$", x1=278.9), _line_at(299.4, 100.0, "124,544", x1=334.4),
             _line_at(345.2, 100.0, "$", x1=350.9), _line_at(371.5, 100.0, "106,865", x1=406.4)]
    out = join_signs(lines)
    assert [(l.text, l.x0, l.x1) for l in out] == [("$ 124,544", 273.2, 334.4),
                                                    ("$ 106,865", 345.2, 406.4)]


def test_a_currency_sign_with_a_cell_between_or_far_from_a_figure_stays_alone():
    between = [_line_at(100.0, 100.0, "$", x1=105.7), _line_at(120.0, 100.0, "total", x1=150.0),
               _line_at(170.0, 100.0, "1,000", x1=200.0)]
    far = [_line_at(100.0, 100.0, "$", x1=105.7), _line_at(300.0, 100.0, "1,000", x1=330.0)]
    assert [l.text for l in join_signs(between)] == ["$", "total", "1,000"]
    assert [l.text for l in join_signs(far)] == ["$", "1,000"]


def _year_columns(width=792.0, top=100.0, rows=6, years=10):
    """A table of labels (x 52 to 167) and ten columns of figures with a gap of 13 pt between
    each: one of the gaps lies at the middle of a landscape page."""
    rights = [261.0 + 53 * k for k in range(years)]
    lines = [_text(0, top, f"FY {2023 - k}", size=8.0, right=r) for k, r in enumerate(rights)]
    for i in range(rows):
        y = top + 12 * (i + 1)
        lines.append(_line_at(52.0, y, f"Account number {i + 1}", x1=167.0))
        lines += [_text(0, y, f"{1000 * (i + 1) + k:,}", size=8.0, right=r)
                  for k, r in enumerate(rights)]
    return lines


def test_a_side_of_figures_only_is_more_columns_of_the_table_not_a_layout_column():
    # Audit pp 75 and 76: ten years of figures across a landscape page. The gap between two of
    # the year columns lies at the middle of the page, and the figures on its right have no
    # words: no layout column holds only figures.
    lines = _year_columns()
    assert layout_gutter(lines, 792.0) is None
    [table] = aligned_tables(1, lines, 792.0)
    assert [len(r) for r in table.rows][1:] == [11] * 6
    assert table.rows[1][0] == "Account number 1" and table.rows[1][10].endswith("1,009")


def _demographics(top=100.0, rows=6):
    """Audit p82: a year column and six columns of figures, under headings of three lines each;
    the gap between the third and the fourth column of figures lies at the middle of the page."""
    rights = [200.0, 280.0, 370.0, 450.0, 550.0, 630.0]
    heads = [("Total", "Personal", "Income"), ("Per", "Capita", "Income"), ("Personal", "Income", "Share"),
             ("Median", "Age", "Years"), ("Public", "School", "Pupils"), ("Jobless", "Rate", "Share")]
    lines = [_text(0, top + 10 * k, word, size=8.0, right=r - 4)
             for r, words in zip(rights, heads) for k, word in enumerate(words)]
    lines.append(_line_at(60.0, top + 20, "Year", size=8.0))
    for i in range(rows):
        y = top + 40 + 12 * i
        lines.append(_line_at(60.0, y, str(2013 + i), size=8.0))
        lines += [_text(0, y, f"{1000 * (i + 1) + k:,}", size=8.0, right=r) for k, r in enumerate(rights)]
    lines.append(_text(0, top + 40 + 12 * rows, "n/a", size=8.0, right=rights[3]))
    lines += [_text(0, top + 40 + 12 * rows, f"{9000 + k:,}", size=8.0, right=r)
              for k, r in enumerate(rights) if k != 3]
    lines.append(_line_at(60.0, top + 40 + 12 * rows, str(2013 + rows), size=8.0))
    return lines


def test_a_tables_rows_of_figures_cross_the_middle_under_their_headings():
    # The headings over the columns right of the gap are words, so that side is not figures
    # only; its rows still carry figures across the gap, so it is no layout gutter.
    lines = _demographics()
    assert layout_gutter(lines, 792.0) is None
    [table] = aligned_tables(1, lines, 792.0)
    assert {len(r) for r in table.rows} == {7}
    assert table.rows[-1][4] == "n/a" and table.rows[-1][5] == "9,004"


def test_a_side_of_words_beside_a_table_is_still_a_layout_column():
    lines = _year_columns(years=4)
    lines += [_line_at(480.0, 100.0 + 12 * i, f"Notes on the account number {i + 1}", x1=700.0)
              for i in range(8)]
    assert layout_gutter(lines, 792.0) is not None


def _dependents(extra=()):
    """A ruled grid: a banner row, then a record of two bands (labels over a blank row each)
    printed three times, then a closing row."""
    first, second = ["LAST NAME", "FIRST NAME", "M.I."], ["SOCIAL SECURITY NUMBER", "RELATIONSHIP", None]
    rows = [["Complete the following for all dependents.", None, None]]
    for _ in range(3):
        rows += [first, ["", "", ""], second, ["", "", ""]]
    rows += [["Signature", None, None], *extra]
    grid = [[(10.0, 20.0 * i, 110.0 * (j + 1), 20.0 * i + 18.0) for j in range(3)]
            for i in range(len(rows))]
    return TableData(1, (10.0, 0.0, 330.0, 20.0 * len(rows)), rows, grid=grid)


def test_a_record_printed_over_and_over_is_one_table_with_a_column_for_each_label():
    banner, record, closing = split_records(_dependents())
    assert banner.rows == [["Complete the following for all dependents.", None, None]]
    assert record.rows == [["LAST NAME", "FIRST NAME", "M.I.", "SOCIAL SECURITY NUMBER",
                            "RELATIONSHIP"]] + [[""] * 5] * 3
    assert record.bbox == (10.0, 20.0, 330.0, 258.0) and record.grid == []
    assert closing.rows == [["Signature", None, None]]


def test_a_grid_whose_blocks_differ_is_not_a_record():
    table = _dependents()
    table.rows[5] = ["LAST NAME", "FIRST NAME", "SUFFIX"]
    assert split_records(table) == [table]


def test_rows_of_figures_that_repeat_are_not_a_record():
    rows = [["Total", "1"], ["", ""], ["Total", "1"], ["", ""]]
    grid = [[(0.0, 10.0 * i, 10.0, 10.0 * i + 8), (10.0, 10.0 * i, 20.0, 10.0 * i + 8)] for i in range(4)]
    table = TableData(1, (0.0, 0.0, 20.0, 40.0), rows, grid=grid)
    assert split_records(table) == [table]


def test_a_form_grid_of_labels_with_choice_circles_is_no_chart():
    # Cobra p1: 25 choice circles and a logo make "dots" and "wedges" around a grid of labels.
    rows = [[f"LABEL {i}", f"OTHER {i}"] for i in range(12)]
    table = TableData(1, (0.0, 0.0, 100.0, 100.0), rows, method="ruled")
    marked = Drawing((0.0, 0.0, 100.0, 100.0), 30, 20, marks=(("dots", 25), ("wedges", 3)))
    assert chart_grid(table, [marked]) is None
    figures = TableData(1, (0.0, 0.0, 100.0, 100.0), [[str(i), str(i * 2)] for i in range(12)],
                        method="ruled")
    assert chart_grid(figures, [marked]).startswith("gridlines inside a chart")
