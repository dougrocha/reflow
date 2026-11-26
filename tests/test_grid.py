"""extract.grid: the cell search returns the cells pdfplumber's returns, in the same order."""

from pathlib import Path
from types import SimpleNamespace

import pdfplumber
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pdfplumber.table import TableFinder, edges_to_intersections, intersections_to_cells
from pdfplumber.utils import rect_to_edges

from reflow.extract.grid import RuledFinder, cells_of, covered_fills
from reflow.extract.pagecontent import WORD_GAP
from reflow.extract.tables.ruled import (
    _between_bands,
    _complete_open_sides,
    _extract,
    _header_block,
    _join_block_rows,
)
from testkit import needs, sample

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _edge(orientation: str, a: int, b: int, c: int) -> dict:
    """A rule on a grid of 10 pt: `a` is its fixed coordinate, b..c its extent."""
    lo, hi = sorted((b, c))
    if orientation == "h":
        return {"orientation": "h", "x0": lo * 10.0, "x1": hi * 10.0, "top": a * 10.0, "bottom": a * 10.0}
    return {"orientation": "v", "x0": a * 10.0, "x1": a * 10.0, "top": lo * 10.0, "bottom": hi * 10.0}


rules = st.builds(_edge, st.sampled_from("hv"), st.integers(0, 8), st.integers(0, 8), st.integers(0, 8))


@settings(max_examples=400, deadline=None)
@given(st.lists(rules, max_size=40))
def test_the_cells_are_pdfplumbers_cells_in_its_order(edges):
    intersections = edges_to_intersections(edges, 3, 3)
    assert cells_of(intersections) == intersections_to_cells(intersections)



def _near(orientation: str, a: float, b: float, c: float) -> dict:
    """A rule off the 10 pt grid by a fraction of a point, so edges almost meet or almost repeat."""
    lo, hi = sorted((b, c))
    if orientation == "h":
        return {"orientation": "h", "x0": lo, "x1": hi, "top": a, "bottom": a}
    return {"orientation": "v", "x0": a, "x1": a, "top": lo, "bottom": hi}


# A rule's position takes one of two offsets, so rules still line up into cells; its ends are off
# the grid by any fraction, so edges almost meet.
position = st.builds(lambda n, offset: n * 10.0 + offset, st.integers(0, 8),
                     st.sampled_from([0.0, 0.3]))
end = st.builds(lambda n, offset: n * 10.0 + offset, st.integers(0, 8),
                st.floats(-2.5, 2.5, allow_nan=False, allow_infinity=False))
near_rules = st.builds(_near, st.sampled_from("hv"), position, end, end)


@settings(max_examples=400, deadline=None)
@given(st.lists(near_rules, max_size=40), st.floats(0, 5), st.floats(0, 5))
def test_fractional_and_near_duplicate_rules_give_pdfplumbers_cells(edges, x_tol, y_tol):
    edges = edges + edges[: len(edges) // 3]   # some rules drawn twice
    intersections = edges_to_intersections(edges, x_tol, y_tol)
    assert cells_of(intersections) == intersections_to_cells(intersections)

@settings(max_examples=100, deadline=None)
@given(st.integers(2, 7), st.integers(2, 7), st.lists(st.tuples(st.integers(0, 6), st.integers(0, 6)), max_size=6))
def test_a_ruled_grid_with_gaps_gives_the_same_cells(columns, rows, gaps):
    edges = [_edge("h", y, 0, columns) for y in range(rows + 1)] \
        + [_edge("v", x, 0, rows) for x in range(columns + 1)]
    for x, y in gaps:   # a short rule between two grid lines is missing
        edges.append(_edge("h", y, x, x + 1))
    intersections = edges_to_intersections(edges, 3, 3)
    assert cells_of(intersections) == intersections_to_cells(intersections)


def test_a_page_of_a_real_document_has_the_same_tables():
    with pdfplumber.open(SAMPLES / "fw9.pdf") as pdf:
        for page in pdf.pages[:3]:
            ours, theirs = RuledFinder(page), TableFinder(page)
            assert ours.cells == theirs.cells
            assert [t.cells for t in ours.tables] == [t.cells for t in theirs.tables]
            assert [t.bbox for t in ours.tables] == [t.bbox for t in theirs.tables]


@pytest.mark.parametrize("name", ["fw9", pytest.param("resnet", marks=needs(sample("resnet.pdf")))])
def test_the_text_of_a_table_is_what_table_extract_gives(name):
    # With words split at WORD_GAP font sizes: resnet's pdfTeX cells space their words by
    # kerning, which pdfplumber's default 3 pt misses ("outputsize").
    seen = 0
    with pdfplumber.open(sample(f"{name}.pdf")) as pdf:
        for page in pdf.pages[:6]:
            for table in RuledFinder(page).tables:
                assert _extract(page, table) == table.extract(x_tolerance_ratio=WORD_GAP)
                seen += 1
    assert seen


def _fill(x0: float, top: float, x1: float, bottom: float, colour: object = 0.9, **more) -> dict:
    return {"x0": x0, "top": top, "x1": x1, "bottom": bottom, "fill": True, "stroke": False,
            "non_stroking_color": colour, **more}


def test_a_fill_on_a_larger_fill_of_its_colour_is_covered():
    # A shaded cell as Word draws it: the cell, then the text area inside its padding.
    assert covered_fills([_fill(0, 0, 100, 20), _fill(5, 0, 95, 20)]) == {1}
    assert covered_fills([_fill(5, 2, 95, 18), _fill(0, 0, 100, 20)]) == {0}
    assert covered_fills([_fill(0, 0, 100, 20, (1, 0.8, 0)), _fill(5, 0, 95, 20, [1, 0.8, 0])]) == {1}


def test_a_fill_that_shows_is_not_covered():
    assert covered_fills([_fill(0, 0, 100, 20), _fill(5, 0, 95, 20, 0.5)]) == set()
    assert covered_fills([_fill(0, 0, 100, 20), _fill(5, 0, 105, 20)]) == set()
    assert covered_fills([_fill(0, 0, 100, 20), _fill(5, 0, 95, 20, stroke=True)]) == set()
    # Painted on a fill of another colour that lies on the larger one: it shows again.
    assert covered_fills([_fill(0, 0, 100, 20), _fill(0, 0, 100, 20, 1.0), _fill(5, 0, 95, 20)]) \
        == set()
    # The other colour was painted first, or elsewhere: nothing shows.
    assert covered_fills([_fill(0, 0, 100, 20, 1.0), _fill(0, 0, 100, 20), _fill(5, 0, 95, 20)]) \
        == {2}
    assert covered_fills([_fill(0, 0, 100, 20), _fill(200, 0, 300, 20, 1.0), _fill(5, 0, 95, 20)]) \
        == {2}


def test_of_two_fills_with_one_box_the_later_is_covered():
    assert covered_fills([_fill(0, 0, 100, 20), _fill(0, 0, 100, 20), _fill(0, 0, 100, 20)]) \
        == {1, 2}


def test_a_page_sized_fill_covers_no_fill_on_it():
    area = 612.0 * 792.0
    background = _fill(0, 0, 612, 792, 1.0)
    cells = [_fill(100, 100, 200, 120, 1.0), _fill(200, 100, 300, 120, 1.0)]
    assert covered_fills([background, *cells]) == {1, 2}   # the page is unknown
    assert covered_fills([background, *cells], area) == set()
    # A panel of half the page is a background too; a smaller one still covers.
    assert covered_fills([_fill(0, 0, 612, 396, 1.0), *cells], area) == set()
    assert covered_fills([_fill(0, 0, 612, 395, 1.0), *cells], area) == {1, 2}


def test_a_page_background_leaves_the_cells_of_a_table_alone():
    # Each cell is a fill, the rows alternating grey and white, on a white page: no rules.
    white, grey = (1, 1, 1), (0.9, 0.9, 0.9)
    cells = [_fill(100 + 100 * c, 100 + 20 * r, 200 + 100 * c, 120 + 20 * r,
                   grey if r % 2 == 0 else white) for r in range(6) for c in range(3)]
    page = _Rects([_fill(0, 0, 612, 792, white), *cells])
    [table] = RuledFinder(page).tables
    assert [len(row.cells) for row in table.rows] == [3] * 6


def _cell(x0: float, top: float, x1: float, bottom: float, colour: object = 0.0) -> list[dict]:
    """A cell's four rules, each a thin black fill, as Word draws a border."""
    return [_fill(x0, top, x1, top + 0.5, colour), _fill(x0, bottom, x1, bottom + 0.5, colour),
            _fill(x0, top, x0 + 0.5, bottom, colour), _fill(x1, top, x1 + 0.5, bottom + 0.5, colour)]


class _Rects:
    """The part of a pdfplumber page that RuledFinder reads: its rectangles as edges."""
    lines: list = []
    curve_edges: list = []
    bbox = (0.0, 0.0, 612.0, 792.0)

    def __init__(self, rects: list[dict]):
        self.rects = [{**r, "width": r["x1"] - r["x0"], "height": r["bottom"] - r["top"],
                       "y0": -r["bottom"], "y1": -r["top"], "doctop": r["top"],
                       "object_type": "rect"} for r in rects]
        self.edges = [e for r in self.rects for e in rect_to_edges(r)]


def test_the_padding_of_a_shaded_cell_is_no_column():
    grid = [r for x in (100, 200) for y in (100, 120) for r in _cell(x, y, x + 100, y + 20)]
    shade = [_fill(100.5, 100.5, 200, 120), _fill(105, 102, 195, 118),
             _fill(200.5, 100.5, 300, 120), _fill(205, 102, 295, 118)]
    page = _Rects(grid + shade)
    assert len(TableFinder(page).cells) > 4   # pdfplumber: the text areas' sides are rules
    [table] = RuledFinder(page).tables
    assert [len(row.cells) for row in table.rows] == [2, 2]
    assert len(table.cells) == 4


def _word(text: str, x: float, top: float, size: float = 10.0) -> list[dict]:
    return [{"text": ch, "x0": x + k * size / 2, "x1": x + (k + 1) * size / 2, "top": top,
             "bottom": top + size, "doctop": top, "size": size, "height": size, "width": size / 2,
             "upright": True, "fontname": "F", "object_type": "char"} for k, ch in enumerate(text)]


def _banded(names: tuple[str, str]) -> _Rects:
    """Two grids of two rows by two columns, each under a bar 5 pt high, drawn as one grid;
    the lower one's column names are `names`."""
    rects = [_fill(100, 95, 300.5, 100, (1, 0.8, 0)), _fill(100, 140.5, 300.5, 145, (1, 0.8, 0)),
             _fill(100, 145, 300.5, 145.5, 0.0), _fill(100, 140, 100.5, 145.5, 0.0),
             _fill(300, 140, 300.5, 145.5, 0.0)]
    rects += [r for top in (100, 120, 145, 165) for x in (100, 200)
              for r in _cell(x, top, x + 100, top + 20)]
    page = _Rects(rects)
    page.chars = [c for text, x, top in (
        ("Item", 110, 105), ("Needs", 210, 105), ("crown", 110, 125), ("chart", 210, 125),
        (names[0], 110, 150), (names[1], 210, 150), ("graft", 110, 170), ("film", 210, 170))
        for c in _word(text, x, top)]
    return page


def test_a_bar_over_a_table_is_no_row_and_the_column_names_again_start_a_table():
    page = _banded(("Item", "Needs"))
    [whole] = RuledFinder(page).tables
    assert len(whole.rows) == 6   # pdfplumber: each bar is a row of one cell
    upper, lower = _between_bands(page, [whole])
    assert _extract(page, upper) == [["Item", "Needs"], ["crown", "chart"]]
    assert _extract(page, lower) == [["Item", "Needs"], ["graft", "film"]]


def test_a_column_name_again_in_other_words_starts_a_table():
    page = _banded(("Item list", "Needs"))
    assert len(_between_bands(page, RuledFinder(page).tables)) == 2


def test_one_value_again_under_a_bar_is_no_second_table():
    # No header: a value of the first row is in its column again, under a bar; that is a value
    # again, not the column names.
    page = _banded(("Crowns", "Needs"))
    [table] = _between_bands(page, RuledFinder(page).tables)
    assert _extract(page, table) == [["Item", "Needs"], ["crown", "chart"],
                                     ["Crowns", "Needs"], ["graft", "film"]]


def test_a_bar_between_the_groups_of_one_table_leaves_one_table():
    page = _banded(("inlay", "both"))
    [table] = _between_bands(page, RuledFinder(page).tables)
    assert _extract(page, table) == [["Item", "Needs"], ["crown", "chart"],
                                     ["inlay", "both"], ["graft", "film"]]


def test_an_empty_row_that_holds_a_line_of_text_is_kept():
    page = _Rects([r for top in (100, 120, 140) for r in (
        _cell(100, top, 300, top + 20) if top == 120 else
        [*_cell(100, top, 200, top + 20), *_cell(200, top, 300, top + 20)])])
    page.chars = [c for text, x, top in (("Item", 110, 105), ("Needs", 210, 105),
                                         ("crown", 110, 145), ("chart", 210, 145))
                  for c in _word(text, x, top)]
    [table] = _between_bands(page, RuledFinder(page).tables)
    assert len(table.rows) == 3


def _cut_bar(tall_example: bool) -> _Rects:
    """A bar 5 pt high over two rows of three columns, a rule through it cutting it into three
    cells; with `tall_example`, the third column's header runs up through the bar."""
    rects = []
    for x in (100, 200):
        rects += _cell(x, 100, x + 100, 105)
    rects += _cell(300, 100 if tall_example else 105, 400, 125)
    if not tall_example:
        rects += _cell(300, 100, 400, 105)
    for top in (105, 125):
        for x in (100, 200):
            rects += _cell(x, top, x + 100, top + 20)
    rects += _cell(300, 125, 400, 145)
    page = _Rects(rects)
    page.chars = [c for text, x, top in (
        ("Item", 110, 110), ("Needs", 210, 110), ("Example", 310, 110 if tall_example else 110),
        ("crown", 110, 130), ("chart", 210, 130), ("graft", 310, 130)) for c in _word(text, x, top)]
    return page


def test_a_bar_the_grid_cuts_into_cells_is_no_row():
    page = _cut_bar(False)
    [whole] = RuledFinder(page).tables
    assert len(whole.rows) == 3 and len(whole.rows[0].cells) == 3
    [table] = _between_bands(page, [whole])
    assert _extract(page, table) == [["Item", "Needs", "Example"], ["crown", "chart", "graft"]]


def test_a_header_that_runs_up_through_a_cut_bar_starts_where_the_bar_ends():
    page = _cut_bar(True)
    [table] = _between_bands(page, RuledFinder(page).tables)
    assert _extract(page, table) == [["Item", "Needs", "Example"], ["crown", "chart", "graft"]]
    assert len(table.rows) == 2 and len(table.rows[0].cells) == 3
    assert all(abs(c[1] - 105.25) < 0.01 for c in table.rows[0].cells)


def test_open_sides_are_read_from_the_edges_the_finder_reads():
    # Three wide rules, each the border of a fill lying on a larger fill of its own colour (Word
    # shades a cell twice): the finder ignores them, so they are no group of rules running out
    # past the table. Read from page.edges, they would be (and the stand-in page cannot crop).
    page = _Rects([_fill(50, 95, 350, 145), _fill(50, 100, 350, 102), _fill(50, 120, 350, 122),
                   _fill(50, 138, 350, 140)])
    table = SimpleNamespace(bbox=(100.0, 100.0, 300.0, 140.0))
    assert _complete_open_sides(page, table) is table


def _box(x0: float, top: float, x1: float, bottom: float) -> tuple[float, float, float, float]:
    return (x0, top, x1, bottom)


def test_a_heading_over_the_names_of_its_columns_is_two_header_rows():
    # "User Roles" stands beside both rows; "Areas" spans three columns over their names.
    rows = [["User Roles", None, "Areas", None, None], [None, None, "Eligibility", "Training", "Claims"],
            ["1", "General", "x", "x", ""], ["2", "Other", "x", "", ""]]
    grid = [[_box(0, 0, 100, 30), None, _box(100, 0, 250, 15), None, None],
            [None, None, _box(100, 15, 150, 30), _box(150, 15, 200, 30), _box(200, 15, 250, 30)],
            [_box(0, 30, 20, 45), _box(20, 30, 100, 45), _box(100, 30, 150, 45),
             _box(150, 30, 200, 45), _box(200, 30, 250, 45)],
            [_box(0, 45, 20, 60), _box(20, 45, 100, 60), _box(100, 45, 150, 60),
             _box(150, 45, 200, 60), _box(200, 45, 250, 60)]]
    assert _header_block(rows, grid) == 2
    joined, _ = _join_block_rows(rows[2:], grid[2:])
    assert joined == rows[2:]
    # With a figure among the names it is a record, and stays one row.
    rows[1][3] = "12"
    assert _header_block(rows, grid) == 0
    # With an empty cell it is no header either.
    rows[1][3] = ""
    assert _header_block(rows, grid) == 0
