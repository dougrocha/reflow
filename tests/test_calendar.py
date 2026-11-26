import calendar
import random

import pytest

from datetime import date

from reflow.analyze.calendar import (KeyEntry, annotate, day_status, find_calendars,
                                     read_dated_list, read_key)
from reflow.analyze.table_nodes import table_node
from reflow.extract.drawings import Fill
from calendarpage import PITCH, ROW, _line, month_lines

def test_the_title_loses_its_letter_spacing():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100))
    assert (grid.title, grid.year, grid.month) == ("JULY 2024", 2024, 7)


def test_three_across_two_down_each_month_keeps_its_own_days():
    months = [(2024, 7), (2024, 8), (2024, 9), (2024, 10), (2024, 11), (2024, 12)]
    lines = []
    for i, (y, m) in enumerate(months):
        lines += month_lines(y, m, 40 + 230 * (i % 3), 100 + 190 * (i // 3))
    random.Random(7).shuffle(lines)
    grids = find_calendars(lines)
    assert [g.title for g in grids] == ["JULY 2024", "AUGUST 2024", "SEPTEMBER 2024",
                                       "OCTOBER 2024", "NOVEMBER 2024", "DECEMBER 2024"]
    for grid, (y, m) in zip(grids, months):
        shown = [l.text for week in grid.weeks for l in week if l]
        assert shown == [f"{d:02d}" for d in range(1, calendar.monthrange(y, m)[1] + 1)]
        assert grid.check() is None
    assert [len(g.lines) for g in grids] == [1 + 7 + n for n in (31, 31, 30, 31, 30, 31)]


def test_a_header_with_no_title_is_not_a_calendar():
    lines = [l for l in month_lines(2024, 7, 50, 100) if "J U L Y" not in l.text]
    assert find_calendars(lines) == []


def test_july_2024_starts_on_a_monday_and_passes_the_arithmetic():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100))
    assert [l.text if l else None for l in grid.weeks[0]] == [None, "01", "02", "03", "04", "05", "06"]
    assert grid.check() is None


def test_a_shifted_grid_fails_naming_the_first_date():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100, shift=1))
    assert "date 1 is in the Tuesday column, not Monday" in grid.check()


def test_a_missing_date_fails_naming_it():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100, skip={15}))
    assert "date 15 is missing" in grid.check()


def test_a_missing_last_date_fails_naming_it():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100, skip={31}))
    assert "date 31 is missing" in grid.check()


def test_a_duplicated_date_fails_naming_it():
    # A second 15 printed in the empty cell before the first of August's row 5.
    lines = month_lines(2024, 7, 50, 100, extra=[(15, 5, 4)])
    (grid,) = find_calendars(lines)
    assert "date 15 is printed twice" in grid.check()


def test_a_date_printed_twice_in_one_cell_is_kept_and_fails():
    lines = month_lines(2024, 7, 50, 100, extra=[(15, 1, 2)])
    (grid,) = find_calendars(lines)
    assert grid.check() is not None
    table = grid.to_table(1)
    assert any("15 15" in (c or "") for row in table.rows for c in row)


def test_empty_cells_are_empty_text_and_every_cell_has_a_region():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100))
    table = grid.to_table(1)
    assert table.method == "calendar" and table.header_rows == 1
    assert table.rows[0] == list("SMTWTFS")
    assert table.rows[1] == ["", "01", "02", "03", "04", "05", "06"]
    assert table.boxes[1][0] is None and table.boxes[1][1] is not None
    assert len(grid.regions) == len(grid.weeks) == 5
    assert all(len(r) == 7 for r in grid.regions)
    assert table.caption == "JULY 2024" and table.caption_bbox == grid.title_line.bbox
    # the regions tile the grid: each numeral lies in its own cell's region
    for r, week in enumerate(grid.weeks):
        for c, line in enumerate(week):
            if line:
                x0, y0, x1, y1 = grid.regions[r][c]
                assert x0 < (line.x0 + line.x1) / 2 < x1 and y0 < (line.top + line.bottom) / 2 < y1


def test_a_note_under_a_grid_ends_the_month_and_is_not_consumed():
    lines = month_lines(2024, 7, 50, 100)
    note = _line(60, 262, 200, 269, "February 17 will be a Bad Weather Make-up Day.", 6.7)
    below = _line(100, 272, 110, 280, "17", 8.5)  # a numeral under the note is not July's
    (grid,) = find_calendars([*lines, note, below])
    assert id(note) not in grid.lines and id(below) not in grid.lines
    assert grid.bbox[3] <= note.top + 1


def test_to_table_holds_the_boxes_the_layout_needs():
    (grid,) = find_calendars(month_lines(2024, 7, 50, 100))
    table = grid.to_table(3)
    assert table.page == 3
    assert len(table.cells) == 7 + 31
    assert len(table.grid) == len(table.rows) == len(table.boxes) == 6


@pytest.mark.parametrize("year,month", [(2025, 2), (2026, 2), (2024, 2)])
def test_february_in_a_leap_and_a_common_year(year, month):
    (grid,) = find_calendars(month_lines(year, month, 50, 100))
    assert grid.check() is None
    assert sum(1 for w in grid.weeks for l in w if l) == calendar.monthrange(year, month)[1]


# --- the colour key and the days it marks ---

RED, GREY, GREEN, YELLOW, TEAL = (255, 22, 22), (162, 170, 173), (126, 217, 87), (255, 222, 89), (0, 194, 203)
PINK = (255, 102, 196)
X, Y = 50, 100  # where month_lines puts JULY 2024


def day_box(d, year=2024, month=7):
    first = (calendar.monthrange(year, month)[0] + 1) % 7
    r, c = divmod(first + d - 1, 7)
    cx = X + 14 + c * PITCH
    return (cx - 5, Y + 46 + r * ROW, cx + 5, Y + 55 + r * ROW)


def band(d, rgb, order, points=()):
    """A fill behind day d, wider and taller than its numeral, as a printed cell colour."""
    x0, y0, x1, y1 = day_box(d)
    return Fill((x0 - 6, y0 - 2, x1 + 6, y1 + 2), rgb, "box", order, points)


def star(d, order):
    """A star at the top left of day d, outside its numeral's box."""
    x0, y0, _, _ = day_box(d)
    return Fill((x0 - 8, y0 - 6, x0 - 1, y0 + 1), TEAL, "mark", order)


KEY_LINES = [
    _line(20, 300, 120, 320, "COLOR KEY:", 20),
    _line(144, 301, 220, 311, "Closed / Holiday", 9),
    _line(254, 301, 340, 311, "Teacher PD or Work Day/", 9),
    _line(254, 311.5, 320, 321.5, "Student Holiday", 9),
    _line(144, 325, 260, 335, "Reporting Period Begins / Ends", 9),
    _line(294, 325, 400, 335, "First / Last Day of School", 9),
    _line(144, 345, 240, 355, "Student Early Release*", 9),
    _line(130, 360, 300, 369, "*All students will be released early.", 9),
    _line(20, 400, 300, 430, "DATES TO REMEMBER", 30),
]
SWATCHES = [Fill((130, 301, 140, 311), RED, "box", 1), Fill((240, 301, 250, 311), GREY, "box", 2),
            Fill((130, 325, 140, 335), TEAL, "mark", 3), Fill((280, 325, 290, 335), GREEN, "box", 4),
            Fill((130, 345, 140, 355), YELLOW, "box", 5)]
# Each key colour on some day of the grid: a swatch must mark a day.
DAYS = [band(1, RED, 10), band(8, GREY, 11), star(3, 12), band(22, GREEN, 13), band(29, YELLOW, 14)]


def keyed(*fills, lines=()):
    lines = month_lines(2024, 7, X, Y) + KEY_LINES + list(lines)
    every = SWATCHES + DAYS + list(fills)
    grids = find_calendars(lines)
    return lines, every, grids, read_key(lines, every, grids)


def labels_of(statuses, day):
    (grid,) = find_calendars(month_lines(2024, 7, X, Y))
    return {grid.cell_text(r, c): [s.entry.label for s in v] for (r, c), v in statuses.items()}.get(f"{day:02d}")


def test_the_key_reads_whole_labels_in_reading_order_and_a_footnote_is_no_label():
    *_, key = keyed()
    assert [(e.label, e.shape) for e in key] == [
        ("Closed / Holiday", "box"), ("Teacher PD or Work Day/ Student Holiday", "box"),
        ("Reporting Period Begins / Ends", "mark"), ("First / Last Day of School", "box"),
        ("Student Early Release*", "box")]
    assert key[1].label_boxes == ((254, 301, 340, 311), (254, 311.5, 320, 321.5))
    assert key[1].swatch == (240, 301, 250, 311)


def test_a_marker_below_the_key_region_is_no_swatch():
    # A red dot beside a heading under "DATES TO REMEMBER" is the list's marker, not the key's.
    *_, key = keyed(Fill((20, 441, 30, 451), RED, "box", 6),
                    lines=[_line(34, 441, 100, 451, "HOLIDAYS", 9)])
    assert [e.label for e in key].count("Closed / Holiday") == 1
    assert "HOLIDAYS" not in [e.label for e in key]


def test_no_anchor_no_key():
    lines = month_lines(2024, 7, X, Y) + [l for l in KEY_LINES if l.text != "COLOR KEY:"]
    assert read_key(lines, SWATCHES + DAYS, find_calendars(lines)) == []


def test_a_colour_hidden_under_a_later_fill_is_not_seen():
    _, fills, (grid,), key = keyed(band(9, PINK, 20), band(9, GREY, 21))
    found, unmapped = day_status(grid, fills, key)
    assert labels_of(found, 9) == ["Teacher PD or Work Day/ Student Holiday"]
    assert unmapped == []


def test_a_near_colour_matches_and_a_far_one_is_unmapped():
    near, far = (225, 52, 52), (155, 122, 122)   # 30 and 100 per channel from the key's red
    lines, fills, grids, key = keyed(band(10, near, 20), band(11, far, 21))
    found, unmapped = day_status(grids[0], fills, key)
    assert labels_of(found, 10) == ["Closed / Holiday"]
    assert labels_of(found, 11) is None
    assert [(grids[0].cell_text(r, c), rgb) for r, c, rgb, _ in unmapped] == [("11", far)]
    tables = [g.to_table(1) for g in grids]
    annotate(grids, tables, lines, fills)
    (fact,) = [f for f in tables[0].facts if f[0] == "calendar-colour-unmapped"]
    assert fact[2]["days"] == ["JULY 2024 11"] and fact[2]["colours"] == ["#9b7a7a"]
    assert fact[2]["boxes"] == list(band(11, far, 0).bbox)
    assert not any(day.get("status") for (r, c), day in tables[0].days.items()
                   if tables[0].rows[r][c] == "11")


def test_a_colour_two_key_entries_could_name_is_unmapped():
    _, fills, (grid,), key = keyed(Fill((400, 301, 410, 311), (250, 30, 30), "box", 7),
                                   lines=[_line(414, 301, 480, 311, "Closed", 9)])
    found, unmapped = day_status(grid, fills, key)
    assert labels_of(found, 1) is None and [rgb for *_, rgb, _ in unmapped] == [RED]


def test_a_day_with_two_colours_and_a_star_keeps_three_statuses_in_key_order():
    # Like May 22: yellow, a green triangle over its right half, and a star.
    x0, y0, x1, y1 = band(17, YELLOW, 0).bbox
    triangle = band(17, GREEN, 31, points=((x0 + 0.3 * (x1 - x0), y1), (x1, y0), (x1, y1)))
    _, fills, (grid,), key = keyed(band(17, YELLOW, 30), triangle, star(17, 32))
    found, _ = day_status(grid, fills, key)
    assert labels_of(found, 17) == ["Reporting Period Begins / Ends", "First / Last Day of School",
                                    "Student Early Release*"]


def test_a_star_beside_the_numeral_marks_its_day():
    mark = star(24, 20)
    assert mark.bbox[2] < day_box(24)[0]   # left of the numeral, not on it
    _, fills, (grid,), key = keyed(mark)
    found, _ = day_status(grid, fills, key)
    assert labels_of(found, 24) == ["Reporting Period Begins / Ends"]
    assert found[next(k for k in found if grid.cell_text(*k) == "24")][0].evidence == mark.bbox


# --- the dated list ---

SCHOOL_YEAR = [(2024, m) for m in range(7, 13)] + [(2025, m) for m in range(1, 7)]


def year_grids():
    lines = []
    for i, (y, m) in enumerate(SCHOOL_YEAR):
        lines += month_lines(y, m, 40 + 230 * (i % 3), 100 + 190 * (i // 3))
    return lines, find_calendars(lines)


def entry(rgb, shape="box"):
    return KeyEntry(rgb, shape, f"label {rgb}", ((0, 0, 1, 1),), (0, 0, 1, 1))


def group(x, y, rgb, heading, rows, shape="box", order=100):
    """A heading with its marker just left of it, and its rows under it. A row starting with a
    space is indented: the wrapped text of the row above."""
    marker = Fill((x, y, x + 10, y + 10), rgb, shape, order)
    lines = [_line(x + 14, y, x + 140, y + 11, heading, 11)]
    for i, text in enumerate(rows):
        indent = 6 if text.startswith(" ") else 0
        lines.append(_line(x + 4 + indent, y + 14 + 11 * i, x + 160, y + 23 + 11 * i, text.strip(), 9))
    return lines, [marker]


def read_groups(*groups, key=None):
    lines, grids = year_grids()
    fills = []
    for i, (rgb, heading, rows, *shape) in enumerate(groups):
        more, marker = group(20 + 200 * i, 900, rgb, heading, rows, *shape)
        lines += more
        fills += marker
    key = key if key is not None else [entry(RED), entry(GREY), entry(YELLOW), entry(TEAL, "mark")]
    return read_dated_list(lines, fills, key, grids).groups


def test_an_entry_gives_every_date_of_its_range_and_its_note():
    (holidays,) = read_groups((RED, "HOLIDAYS", ["October 14-18: Fall Break"]))
    (e,) = holidays.entries
    assert e.dates == tuple(date(2024, 10, d) for d in range(14, 19))
    assert e.note == "Fall Break" and holidays.complete and holidays.key == entry(RED)


def test_a_range_across_the_new_year_takes_each_month_in_its_printed_year():
    (holidays,) = read_groups((RED, "HOLIDAYS", ["December 23-January 3: Winter Break"]))
    (e,) = holidays.entries
    assert len(e.dates) == 12 and (e.dates[0], e.dates[-1]) == (date(2024, 12, 23), date(2025, 1, 3))
    assert e.note == "Winter Break"


def test_a_date_only_range_gives_its_dates_and_no_note():
    (pd,) = read_groups((GREY, "TEACHER PROFESSIONAL", ["August 1-13"]))
    (e,) = pd.entries
    assert len(e.dates) == 13 and e.note is None and pd.key == entry(GREY)


def test_a_single_date_under_the_yellow_marker():
    (early,) = read_groups((YELLOW, "STUDENT EARLY RELEASE", ["May 22"]))
    assert early.key == entry(YELLOW) and early.entries[0].dates == (date(2025, 5, 22),)


def test_an_en_dash_range_and_a_wrapped_entry():
    (holidays,) = read_groups((RED, "HOLIDAYS", ["March 17\u201321: Spring Break",
                                                 "January 20: Martin Luther King", " Jr. Day"]))
    spring, king = holidays.entries
    assert spring.dates == tuple(date(2025, 3, d) for d in range(17, 22)) and spring.note == "Spring Break"
    assert king.note == "Martin Luther King Jr. Day" and len(king.boxes) == 2
    assert holidays.complete


def test_a_line_the_grammar_cannot_read_is_unparsed_and_never_guessed():
    (periods,) = read_groups((TEAL, "REPORTING PERIODS",
                              ["1st August 14 to October 10.............41 Days"], "mark"))
    assert periods.entries == () and not periods.complete
    assert [t for t, _ in periods.unparsed] == ["1st August 14 to October 10.............41 Days"]
    assert periods.key == entry(TEAL, "mark")


def test_a_subtitle_names_no_date_and_is_not_unparsed():
    (holidays,) = read_groups((RED, "HOLIDAYS", ["District Closed", "July 1-5: Staff Holiday"]))
    assert holidays.complete and len(holidays.entries) == 1


def test_a_marker_of_no_key_colour_is_a_group_that_pairs_with_nothing():
    (staar,) = read_groups(((255, 184, 28), "STAAR TESTING", ["April 8: Reading"]))
    assert staar.key is None


def test_a_wide_gap_ends_the_group():
    lines, grids = year_grids()
    more, marker = group(20, 900, RED, "HOLIDAYS", ["July 1-5: Staff Holiday"])
    footer = _line(24, 960, 120, 970, "KISDTV: Spectrum Channel 17", 9)
    (holidays,) = read_dated_list(lines + more + [footer], marker, [entry(RED)], grids).groups
    assert holidays.complete and len(holidays.entries) == 1


# The list checked against the colours, through annotate, on July 2024 under KEY_LINES.

def checked(day_fills, *groups):
    lines = month_lines(2024, 7, X, Y) + KEY_LINES
    fills = SWATCHES + list(day_fills)
    for i, (rgb, heading, rows, *shape) in enumerate(groups):
        more, marker = group(20 + 200 * i, 440, rgb, heading, rows, *shape)
        lines += more
        fills += marker
    grids = find_calendars(lines)
    tables = [g.to_table(1) for g in grids]
    annotate(grids, tables, lines, fills)
    return tables[0]


def facts_of(table):
    return {code: data for code, _, data in table.facts}


def cell(table, day):
    return next(table.days.get((r, c), {}) for r, row in enumerate(table.rows)
                for c, text in enumerate(row) if r and text == f"{day:02d}")


REPORTING = ["1st July 2 to July 9..........6 Days", "2nd July 10 to July 15.......4 Days",
             "3rd July 16 to July 23.......6 Days", "4th July 24 to July 30.......5 Days"]


def test_an_entry_gives_its_note_to_each_date_with_the_box_it_is_printed_in():
    table = checked([band(d, RED, 20 + d) for d in range(1, 6)],
                    (RED, "HOLIDAYS", ["July 1-5: Staff Holiday"]))
    for d in range(1, 6):
        assert cell(table, d)["notes"] == [{"text": "Staff Holiday", "box": [24, 454, 180, 463]}]
        assert [s["label"] for s in cell(table, d)["status"]] == ["Closed / Holiday"]
    assert cell(table, 6) == {}
    assert facts_of(table) == {}


def test_a_red_day_no_entry_names_and_an_entry_with_no_red_day_disagree():
    table = checked([band(d, RED, 20 + d) for d in (1, 2, 3, 4, 5, 10)],
                    (RED, "HOLIDAYS", ["July 1-5: Staff Holiday", "July 25: Founders Day"]))
    mismatch = facts_of(table)["calendar-list-mismatch"]
    assert mismatch["entries"] == ["July 25: Founders Day", ""]
    assert mismatch["days"] == ["", "JULY 2024 10 — Closed / Holiday"]
    assert mismatch["boxes"][4:] == list(day_box(10))


def test_reporting_periods_unparsed_leave_their_stars_unvalidated_not_in_conflict():
    stars = [star(d, 40 + d) for d in (2, 9, 10, 15, 16, 23, 24, 30)]
    reds = [band(d, RED, 20 + d) for d in (1, 2, 3, 4, 5, 19)]
    table = checked(reds + stars, (RED, "HOLIDAYS", ["July 1-5: Staff Holiday"]),
                    (TEAL, "REPORTING PERIODS", REPORTING, "mark"))
    for d in (2, 9, 10, 15, 16, 23, 24, 30):
        assert "Reporting Period Begins / Ends" in [s["label"] for s in cell(table, d)["status"]]
    facts = facts_of(table)
    assert facts["calendar-list-unparsed"]["lines"] == REPORTING
    assert len(facts["calendar-list-unparsed"]["boxes"]) == 4 * 4
    assert facts["calendar-list-unparsed"]["unvalidated"] == ["REPORTING PERIODS"]
    assert facts["calendar-list-mismatch"]["days"] == ["JULY 2024 19 — Closed / Holiday"]


def test_a_partly_read_group_checks_its_entries_but_not_its_uncovered_days():
    table = checked([band(8, GREY, 20), band(22, GREY, 21)],
                    (GREY, "TEACHER PD", ["July 11", "Week of July 15"]))
    facts = facts_of(table)
    assert facts["calendar-list-mismatch"]["entries"] == ["July 11"]
    assert facts["calendar-list-unparsed"]["unvalidated"] == ["TEACHER PD"]


def test_uncoloured_weekends_inside_a_range_are_no_conflict():
    table = checked([band(d, RED, 20 + d) for d in (*range(8, 13), *range(15, 20))],
                    (RED, "HOLIDAYS", ["July 8-19: Summer Break"]))
    assert facts_of(table) == {}
    assert cell(table, 13)["notes"][0]["text"] == "Summer Break"   # printed: the range names it


def test_the_table_node_carries_each_days_annotations_and_the_tables_facts():
    lines = month_lines(2024, 7, X, Y) + KEY_LINES
    more, marker = group(20, 440, RED, "HOLIDAYS", ["July 1-5: Staff Holiday", "July 9 Founders",
                                                       "July 25: Founders Day"])
    (grid,) = find_calendars(lines + more)
    t = grid.to_table(1)
    annotate([grid], [t], lines + more, SWATCHES + marker + [band(2, RED, 20)])
    node = table_node(t)
    tds = {td.text: td for tr in node.children[1:] for td in tr.children}
    assert [s["label"] for s in tds["02"].attrs["day"]["status"]] == ["Closed / Holiday"]
    assert tds["03"].attrs["day"] == {"notes": [{"text": "Staff Holiday", "box": [24, 454, 180, 463]}]}
    assert tds["02"].text == "02" and "day" not in tds["06"].attrs
    assert {f.code for f in node.facts} == {"calendar-list-unparsed", "calendar-list-mismatch"}
