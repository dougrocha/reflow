"""Scan-field rules on drawn pages: what each shape proposes, which doubts it carries, and how
scrub repairs the OCR text around the fields it explains."""

from dataclasses import replace

import pytest
from PIL import Image, ImageDraw
from prosepage import PROSE_EDGES, font, list_page, prose_page

from reflow.analyze import scan_rules as sr
from reflow.analyze.forms import plan_forms
from reflow.analyze.scan_rules import Doubt, Shape
from reflow.extract.forms import FF_COMB, Detection, DetectionSource, Widget
from reflow.extract.lines import Line
from reflow.extract.pages import PageContent
from reflow.model import Role

S = sr.SCALE


class Page:
    """A white US Letter page at the rules' resolution, drawn in points."""

    def __init__(self, paper=255):
        self.image = Image.new("L", (round(612 * S), round(792 * S)), paper)
        self.draw = ImageDraw.Draw(self.image)
        self.lines: list[Line] = []

    def hline(self, x0, x1, y, gray=0):
        y = round(y * S)
        self.draw.rectangle([round(x0 * S), y, round(x1 * S), y + 1], fill=gray)

    def dashed(self, x0, x1, y, dash=3.8, gap=1.5, missing=()):
        """A rule drawn as a run of short dashes, as a scan of a printed dashed line is; the
        dashes at the indices in `missing` are left out (a scan drops some)."""
        x, i = x0, 0
        while x + dash <= x1 + 0.01:
            if i not in missing:
                self.hline(x, x + dash, y)
            x += dash + gap
            i += 1

    def vdashed(self, x, y0, y1, dash=3.8, gap=1.5):
        """A vertical rule drawn as a run of short dashes."""
        y = y0
        while y + dash <= y1 + 0.01:
            self.vline(x, y, y + dash)
            y += dash + gap

    def dotted(self, x0, x1, y, pitch=5.3, gray=185, size=1.4):
        """A dotted rule: a row of small specks, pale on grey paper (a scan of a faint print)."""
        x = x0
        while x + size <= x1:
            self.draw.rectangle([round(x * S), round(y * S), round((x + size) * S),
                                 round((y + size) * S)], fill=gray)
            x += pitch

    def circle(self, cx, cy, d=6.0, width=1.0, fill=None):
        """An answer circle: a ring `d` pt across, drawn with a line `width` pt thick."""
        r = d / 2
        self.draw.ellipse([round((cx - r) * S), round((cy - r) * S), round((cx + r) * S),
                           round((cy + r) * S)], outline=0, width=max(1, round(width * S)),
                          fill=fill)

    def vline(self, x, y0, y1, gray=0):
        x = round(x * S)
        self.draw.rectangle([x, round(y0 * S), x + 1, round(y1 * S)], fill=gray)

    def box(self, x0, y0, x1, y1):
        self.hline(x0, x1, y0)
        self.hline(x0, x1, y1)
        self.vline(x0, y0, y1)
        self.vline(x1, y0, y1)

    def text(self, text, x0, top, x1=None, size=7.0):
        """An OCR line, with glyph-like ink under it: a stem every 2.5 pt."""
        x1 = x1 if x1 is not None else x0 + 4.5 * len(text)
        bbox = (x0, top, x1, top + size)
        for i in range(int((x1 - x0) / 2.5)):
            x = x0 + 1 + 2.5 * i
            self.draw.rectangle([round(x * S), round((top + 1) * S), round(x * S) + 1,
                                 round((top + size - 1) * S)], fill=0)
        line = Line(1, bbox, text, size, False, False, "GlyphLessFont", ocr=True)
        self.lines.append(line)
        return line

    def propose(self):
        return sr.propose(self.image, self.lines, 1)


def near(a, b, tol=1.5):
    return all(abs(p - q) <= tol for p, q in zip(a, b, strict=True))


def only(found, shape):
    return [c for c in found if c.shape is shape]


def test_checkbox_squares_with_option_text_are_certain():
    page = Page()
    for x in (72, 180, 288):
        page.box(x, 180, x + 8, 188)
        page.text("option", x + 12, 180.5)
    squares = only(page.propose(), Shape.SQUARE)
    assert [c.bbox[0] for c in squares] == pytest.approx([72, 180, 288], abs=1)
    assert all(c.certain and c.kind == "checkbox" for c in squares)


def test_a_square_with_a_tick_inside_has_ink_inside():
    page = Page()
    page.box(72, 180, 80, 188)
    page.text("ticked", 84, 180.5)
    page.box(72, 200, 80, 208)
    page.text("blank", 84, 200.5)
    page.draw.line([(73.5 * S, 182 * S), (78.5 * S, 187 * S)], fill=0, width=3)
    ticked, blank = sorted(only(page.propose(), Shape.SQUARE), key=lambda c: c.bbox[1])
    assert Doubt.INK_INSIDE in ticked.doubts
    assert Doubt.INK_INSIDE not in blank.doubts


def test_a_square_with_no_text_beside_it_has_no_caption():
    page = Page()
    page.box(300, 400, 308, 408)
    page.box(72, 180, 80, 188)
    page.text("captioned", 84, 180.5)
    lone = next(c for c in page.propose() if c.bbox[0] > 290)
    assert Doubt.NO_CAPTION in lone.doubts


def _comb(page, x0, y0, cells, width=14.4, height=24):
    page.hline(x0, x0 + cells * width, y0)
    page.hline(x0, x0 + cells * width, y0 + height)
    for i in range(cells + 1):
        page.vline(x0 + i * width, y0, y0 + height)


def test_an_ssn_comb_is_three_runs_with_their_box_counts():
    page = Page()
    page.text("Social security number", 418, 362)
    _comb(page, 418, 372, 3)
    _comb(page, 475, 372, 2)
    _comb(page, 518, 372, 4)
    combs = only(page.propose(), Shape.COMB)
    assert [c.cells for c in combs] == [3, 2, 4]
    assert near(combs[0].bbox, (418, 372, 461.2, 396))
    assert all(c.certain for c in combs)


def test_a_lone_pair_of_boxes_is_a_short_comb():
    page = Page()
    page.text("Code", 100, 362)
    _comb(page, 100, 372, 2)
    _comb(page, 300, 372, 5)
    page.text("Number", 300, 362)
    pair = next(c for c in only(page.propose(), Shape.COMB) if c.cells == 2)
    assert Doubt.SHORT_COMB in pair.doubts


def test_a_comb_drawn_in_pale_gray_is_read_through_its_dividers():
    page = Page()
    page.text("U.S. Social Security Number", 150, 214)
    for i in range(1, 9):
        page.vline(150 + i * 105 / 9, 226, 238, gray=200)
    page.box(150, 226, 255, 238)
    page.text("Last name", 300, 214)
    page.hline(300, 500, 210)
    page.hline(300, 500, 240)
    comb = next(c for c in page.propose() if c.bbox[0] == pytest.approx(150, abs=1))
    assert (comb.shape, comb.cells, comb.certain) == (Shape.COMB, 9, True)


def test_a_ruled_cell_offers_the_blank_strip_under_its_caption():
    page = Page()
    page.hline(57, 576, 96)
    page.hline(57, 576, 132)
    page.vline(57, 96, 132)
    page.text("1 Name of entity/individual", 62, 98.5)
    page.hline(57, 576, 170)
    page.text("2 Requester's name and address", 62, 134.5)
    one, two = only(page.propose(), Shape.CELL)
    assert near(one.bbox, (58.5, 105.5, 575, 132), tol=2)
    assert one.certain and not one.multiline
    assert two.multiline


def test_the_slack_under_a_short_entry_of_a_table_row_is_doubtful():
    page = Page()
    for y in (300, 330, 360):
        page.hline(36, 296, y)
    page.vline(166, 300, 360)
    page.text("If the payment is for", 38, 302)
    page.text("Then it is exempt for", 168, 302)
    page.text("all payees except 7", 168, 311)
    page.text("and 8 through 13", 168, 320)
    page.text("Interest", 38, 332)
    page.text("All payees", 168, 332)
    page.text("except those listed", 168, 341)
    page.text("under section 4", 168, 350)
    assert not [c for c in page.propose() if c.certain]
    slack = only(page.propose(), Shape.CELL)
    assert slack and all(Doubt.GRID_CELL in c.doubts for c in slack)


def test_an_underline_after_its_caption_is_the_space_above_it():
    page = Page()
    page.text("Exempt payee code (if any)", 420, 194)
    page.hline(544, 576, 203.5)
    page.text("Other (see instructions)", 72, 230, x1=158)
    page.hline(162, 446, 239.5)
    first, second = only(page.propose(), Shape.UNDERLINE)
    assert near(first.bbox, (544, 193, 576, 204), tol=1)
    assert near(second.bbox, (162, 229, 446, 240), tol=1)
    assert first.certain and second.certain


def test_a_rule_drawn_as_a_run_of_dashes_is_one_underline():
    page = Page()
    page.text("Last name", 72, 190, x1=118)
    page.dashed(122, 330, 199)
    page.text("First name", 72, 220, x1=124)
    page.dashed(128, 330, 229, missing={7, 8})   # two dashes the scan lost
    last, first = only(page.propose(), Shape.UNDERLINE)
    assert near(last.bbox, (122, 188, 330, 199), tol=3) and last.certain
    assert near(first.bbox, (128, 218, 330, 229), tol=3) and first.certain


def _dashed_table(page, rows=3, x=(72, 236, 400), top=200, height=20):
    for r in range(rows + 1):
        page.dashed(x[0], x[-1], top + height * r)
    for xv in x:
        page.vdashed(xv, top, top + height * rows)


def test_a_dashed_table_has_no_underline_fields():
    page = Page()
    _dashed_table(page)
    page.text("Item", 76, 203, x1=120)
    page.text("Amount", 240, 203, x1=300)
    page.text("Rent", 76, 223, x1=110)
    page.text("Food", 76, 243, x1=110)
    page.text("120", 240, 243, x1=260)
    assert only(page.propose(), Shape.UNDERLINE) == []


def test_a_few_dashes_are_no_underline():
    page = Page()
    page.text("Date", 72, 190)
    page.dashed(110, 125, 197)        # three dashes: a hyphen row, not a rule
    page.text("Place", 72, 220)
    page.hline(110, 200, 227)
    assert [c.bbox[1] for c in only(page.propose(), Shape.UNDERLINE)] == pytest.approx([216], abs=2)


def test_a_dashed_rule_is_not_made_of_text():
    page = Page()
    page.text("a line of ordinary words, set in small type that has no rule under it", 72, 300,
              x1=380)
    assert page.propose() == []


def test_a_faint_dotted_rule_is_one_underline():
    page = Page(paper=205)   # grey paper, dots no darker than 185: under every ink threshold
    page.text("(Last name)", 100, 206, x1=150)
    page.dotted(100, 300, 202)
    page.text("(First name)", 100, 406, x1=156)
    page.dotted(100, 300, 402)
    last, first = only(page.propose(), Shape.UNDERLINE)
    assert near(last.bbox, (100, 191, 300, 203), tol=3) and last.certain
    assert near(first.bbox, (100, 391, 300, 403), tol=3) and first.certain


def test_paper_grain_and_a_few_dots_are_no_underline():
    page = Page(paper=205)
    for i in range(0, 400, 7):                      # grain: specks scattered over the paper
        page.draw.point((round((72 + (i * 37) % 300) * S), round((300 + (i * 53) % 90) * S)), fill=195)
    page.dotted(100, 125, 202)                      # five dots: a short ellipsis, not a rule
    assert page.propose() == []


def test_the_leader_dots_before_a_checkbox_are_no_underline():
    page = Page(paper=205)
    page.text("Never attended school", 72, 190)
    page.dotted(168, 232, 197)
    page.box(236, 188, 247, 199)
    page.text("Kindergarten", 72, 215)
    page.dotted(130, 232, 222)
    page.box(236, 213, 247, 224)
    assert only(page.propose(), Shape.UNDERLINE) == []


@pytest.mark.parametrize("gap", [4, 12, 20, 35, 50])
def test_the_leader_dots_of_a_table_of_contents_are_no_underline_whatever_the_page_number_gap(gap):
    page = Page(paper=205)
    for i, (title, number) in enumerate((("Introduction", "3"), ("Methods", "17"),
                                         ("Results", "42"), ("Discussion", "58"))):
        y = 200 + 20 * i
        end = 72 + 5.5 * len(title)
        page.text(title, 72, y, x1=end)
        page.dotted(end + 6, 480 - gap, y + 6)
        page.text(number, 480, y, x1=480 + 5 * len(number))
    assert [c for c in page.propose() if c.certain] == []


def _contents(entries, gap=20, read_numbers=True, title=True):
    """A scanned table of contents: a title, a dotted leader and a right-aligned page number
    per entry. `read_numbers` False leaves the numbers on the page but out of the OCR text."""
    page = Page(paper=205)
    if title:
        page.text("Table of contents", 72, 140, size=14)
    for i, (title, number) in enumerate(entries):
        y = 200 + 20 * i
        end = 72 + 5.5 * len(title)
        page.text(title, 72, y, x1=end)
        page.dotted(end + 6, 480 - gap, y + 6)
        page.text(number, 480, y, x1=480 + 5 * len(number))
        if not read_numbers:
            page.lines.pop()
    return page


@pytest.mark.parametrize("numbers", [("A-1", "B-1", "C-1", "D-1"), ("3-4", "5-9", "10-12", "13-20"),
                                     ("p. 3", "p. 5", "p. 10", "p. 13"), ("A-1", "A-12", "B-3", "B-14"),
                                     ("pp. 3-4", "pp. 5-9", "page 10", "page 13")])
def test_the_leader_dots_before_a_page_number_with_a_prefix_or_a_range_are_no_underline(numbers):
    page = _contents(zip(("Appendix A", "Chapter 1", "Chapter 2", "Chapter 3"), numbers, strict=True),
                     title=False)
    assert [c for c in page.propose() if c.certain] == []


@pytest.mark.parametrize("gap", [4, 20, 50])
def test_the_leader_dots_before_a_page_number_ocr_missed_are_no_underline(gap):
    page = _contents((("Introduction", "3"), ("Methods", "17"), ("Results", "42"),
                      ("Discussion", "58")), gap=gap, read_numbers=False)
    assert [c for c in page.propose() if c.certain] == []


def test_unread_trailing_ink_without_a_contents_heading_does_not_remove_entry_lines():
    page = _contents((("Introduction", "3"), ("Methods", "17"), ("Results", "42"),
                      ("Discussion", "58")), read_numbers=False, title=False)
    assert len(only(page.propose(), Shape.UNDERLINE)) == 4


@pytest.mark.parametrize("read_units", [True, False])
def test_dotted_entry_lines_keep_their_fields_when_ocr_misses_printed_units(read_units):
    face = font("sans", 10)
    if face is None:
        pytest.skip("no sans font on this machine")
    page = Page(paper=205)
    page.text("Medical history", 72, 140, size=14)
    for y, label, unit in ((200, "Weight", "kg"), (230, "Height", "inches"),
                           (260, "Temperature", "degrees")):
        raw = page.draw.textbbox((72 * S, y * S), label, font=face)
        bbox = tuple(v / S for v in raw)
        page.draw.text((72 * S, y * S), label, font=face, fill=0)
        page.lines.append(Line(1, bbox, label, 10, False, False, "OCR", ocr=True))
        page.dotted(bbox[2] + 6, 430, bbox[3] - 1)
        unit_y = (bbox[3] - 8) * S
        page.draw.text((455 * S, unit_y), unit, font=face, fill=0)
        if read_units:
            unit_box = tuple(v / S for v in page.draw.textbbox((455 * S, unit_y), unit, font=face))
            page.lines.append(Line(1, unit_box, unit, 10, False, False, "OCR", ocr=True))
    fields = only(page.propose(), Shape.UNDERLINE)
    assert len(fields) == 3 and all(c.certain for c in fields)


def test_leader_dots_inside_one_ocr_line_that_ends_in_a_page_number_are_no_underline():
    page = Page(paper=205)
    for i, title in enumerate(("Introduction", "Methods", "Results", "Discussion")):
        y = 200 + 20 * i
        page.text(f"{title} . . . . . . . . {3 + 14 * i}", 72, y, x1=490)
        page.dotted(72 + 5.5 * len(title) + 6, 440, y + 6)
    assert [c for c in page.propose() if c.certain] == []


def test_a_dotted_rule_followed_by_a_word_far_off_is_still_a_place_to_write():
    page = Page(paper=205)
    page.text("Name", 72, 200, x1=100)
    page.dotted(106, 300, 206)
    page.text("(print clearly)", 340, 200, x1=410)
    page.text("City", 72, 240, x1=95)
    page.dotted(101, 300, 246)
    page.text("(print clearly)", 340, 240, x1=410)
    assert len([c for c in only(page.propose(), Shape.UNDERLINE) if c.certain]) == 2


def _row_of_boxes(page, label, x0, xs, y, dots_from=None):
    """A form row that prints its label first: the label, leader dots, then the boxes."""
    page.text(label, x0, y + 1)
    page.dotted(dots_from if dots_from is not None else x0 + 4.5 * len(label) + 3, xs[0] - 4, y + 7)
    for x in xs:
        page.box(x, y, x + 11, y + 11)


def test_a_square_labelled_on_its_left_past_leader_dots_is_certain():
    page = Page(paper=205)
    _row_of_boxes(page, "Never attended school", 72, [236], 188)
    _row_of_boxes(page, "Kindergarten", 72, [236], 213)
    squares = only(page.propose(), Shape.SQUARE)
    assert len(squares) == 2 and all(c.certain for c in squares)


def test_a_row_of_squares_takes_the_label_left_of_its_first():
    page = Page(paper=205)
    _row_of_boxes(page, "Elementary school", 72, [236, 252, 268, 284], 188)
    _row_of_boxes(page, "High school", 72, [236, 252], 213)
    squares = only(page.propose(), Shape.SQUARE)
    assert len(squares) == 6 and all(c.certain for c in squares)


def test_an_ocr_line_that_runs_on_over_its_square_still_labels_it():
    page = Page(paper=205)
    for y, label in ((188, "Never attended school"), (213, "Kindergarten")):
        page.box(236, y, 247, y + 11)
        line = page.text(label + " O", 72, y + 1, x1=230)
        # OCR read the square as the glyph "O" and let its label's box run on over it
        page.lines[-1] = replace(line, bbox=(*line.bbox[:2], 246, line.bbox[3]))
    squares = only(page.propose(), Shape.SQUARE)
    assert len(squares) == 2 and all(c.certain for c in squares)


def test_a_square_with_text_only_far_to_its_left_has_no_caption():
    page = Page(paper=205)
    page.text("Never attended school", 72, 189)
    page.box(300, 188, 311, 199)
    page.text("Kindergarten", 72, 214)
    page.box(300, 213, 311, 224)
    squares = only(page.propose(), Shape.SQUARE)
    assert all(Doubt.NO_CAPTION in c.doubts for c in squares)


def test_answer_circles_with_option_text_are_certain_checkboxes():
    page = Page()
    for y, option in ((190, "Head of household"), (205, "Wife of head"), (220, "Roomer, boarder")):
        page.circle(76, y + 3.5)
        page.text(option, 84, y)
    circles = only(page.propose(), Shape.CIRCLE)
    assert [round(c.bbox[1]) for c in circles] == pytest.approx([190, 205, 220], abs=1)
    assert all(c.certain and c.kind == "checkbox" for c in circles)
    assert near(circles[0].bbox, (73, 190.5, 79, 196.5), tol=1.5)


def test_a_filled_dot_a_letter_o_and_a_big_ring_are_no_circles():
    page = Page()
    page.circle(76, 193.5, fill=0)               # a bullet
    page.text("Bullet", 84, 190)
    page.circle(76, 213.5, d=2.8, width=0.7)     # the counter of a lower-case "o"
    page.text("Small o", 84, 210)
    page.circle(76, 243.5, d=26.0)               # a ring far bigger than a checkbox
    page.text("Big ring", 100, 240)
    assert only(page.propose(), Shape.CIRCLE) == []

def _prose(*args, **kwargs):
    page = prose_page(*args, **kwargs)
    if page is None:
        pytest.skip(f"no {args[0]} font on this machine")
    return page


def _circles_and_scrub(image, lines):
    """The circles the rules propose on a page of prose, and what scrub makes of the lines when
    it is told about all of them (the certain ones and the doubtful, as the assist may accept)."""
    circles = [c for c in sr.propose(image, lines, 1) if c.shape is Shape.CIRCLE]
    return circles, [ln.text for ln in sr.scrub(lines, circles)]


@pytest.mark.parametrize("name", ["bundled", "sans", "bold", "serif", "dejavu"])
@pytest.mark.parametrize("size", [9, 11, 12, 14, 18])
def test_round_glyphs_in_scanned_prose_are_no_certain_circles(name, size):
    image, lines = _prose(name, size, marks=False)
    circles, scrubbed = _circles_and_scrub(image, lines)
    assert [c for c in circles if c.certain] == []
    assert scrubbed == [ln.text for ln in lines]


@pytest.mark.parametrize("name", ["sans", "serif", "dejavu"])
@pytest.mark.parametrize("size", [9, 11, 12, 14])
def test_hollow_bullets_and_degree_signs_in_a_line_of_words_are_no_certain_circles(name, size):
    image, lines = _prose(name, size)
    circles, scrubbed = _circles_and_scrub(image, lines)
    assert [c for c in circles if c.certain] == []
    assert scrubbed == [ln.text for ln in lines]


@pytest.mark.parametrize("name", ["sans", "bold", "serif", "dejavu"])
@pytest.mark.parametrize("size", [18, 20, 24, 28, 36])
def test_a_small_hollow_bullet_between_words_in_large_type_is_no_certain_circle(name, size):
    image, lines = _prose(name, size, texts=["Items ◦ first ◦ second ◦ third", "Options: ◦ one ◦ two"])
    circles = [c for c in sr.propose(image, lines, 1) if c.shape is Shape.CIRCLE]
    assert [c for c in circles if c.certain] == []


BULLET_ITEMS = [
    ["Option one", "Option two", "Option three", "Option four"],
    ["The committee shall meet every second Tuesday", "Members may attend by video link",
     "Minutes are kept by the secretary"],
]


def _list(*args, **kwargs):
    page = list_page(*args, **kwargs)
    if page is None:
        pytest.skip(f"no {args[0]} font on this machine")
    return page


@pytest.mark.parametrize("items", BULLET_ITEMS)
@pytest.mark.parametrize("name, marker, size", [
    ("sans", "○", 10), ("sans", "○", 12), ("sans", "○", 14),
    ("serif", "○", 12), ("serif", "○", 14),
    ("dejavu", "○", 8), ("dejavu", "○", 9), ("dejavu", "○", 11),
    ("mono", "o", 10), ("mono", "o", 12)])
@pytest.mark.parametrize("ocr_marker", ["o", "O", "0", "○"])
def test_a_hollow_bulleted_list_is_no_certain_circles_and_scrub_leaves_its_words(
        name, marker, size, ocr_marker, items):
    image, lines = _list(name, size, items, marker, ocr_marker)
    circles = [c for c in sr.propose(image, lines, 1) if c.shape is Shape.CIRCLE]
    certain = [c for c in circles if c.certain]
    assert certain == []
    assert [ln.text for ln in sr.scrub(lines, certain)] == [ln.text for ln in lines]


@pytest.mark.parametrize("name, size", [("sans", 10), ("sans", 12), ("sans", 14), ("serif", 10), ("serif", 12),
                                        ("serif", 14), ("dejavu", 10), ("dejavu", 12)])
def test_a_lone_round_letter_that_opens_or_closes_a_line_of_words_is_no_certain_circle(name, size):
    image, lines = _prose(name, size, texts=PROSE_EDGES)
    circles = [c for c in sr.propose(image, lines, 1) if c.shape is Shape.CIRCLE]
    assert circles and all(Doubt.BULLET in c.doubts for c in circles)
    assert [c for c in circles if c.certain] == []


def test_a_hollow_bullet_is_doubtful_not_dropped_so_the_assist_may_still_be_asked():
    image, lines = _list("sans", 12, BULLET_ITEMS[0])
    circles = [c for c in sr.propose(image, lines, 1) if c.shape is Shape.CIRCLE]
    assert len(circles) == 4 and all(Doubt.BULLET in c.doubts for c in circles)


def test_a_column_of_drawn_circles_with_labels_in_type_of_their_own_stays_certain():
    page = Page()
    for y, option in ((190, "Head of household"), (205, "Wife of head"), (220, "Roomer, boarder")):
        page.circle(76, y + 3.5, d=5.8)
        page.text(option, 84, y, size=6.0)
    circles = only(page.propose(), Shape.CIRCLE)
    assert len(circles) == 3 and all(c.certain for c in circles)


@pytest.mark.parametrize("name", ["sans", "serif"])
@pytest.mark.parametrize("size", [11, 12, 14])
def test_round_glyphs_on_grey_blurred_paper_are_no_certain_circles(name, size):
    image, lines = _prose(name, size, paper=215, blur=0.6)
    circles, scrubbed = _circles_and_scrub(image, lines)
    assert [c for c in circles if c.certain] == []
    assert scrubbed == [ln.text for ln in lines]


def test_scrub_leaves_the_words_around_a_lone_o_that_sits_in_a_sentence():
    page = Page()
    page.text("uno o dos o tres personas", 72, 190, x1=190)
    page.text("Type O negative blood", 72, 205, x1=170)
    circles = [sr.Candidate(1, Shape.CIRCLE, (cx - 3, 190.5, cx + 3, 196.5)) for cx in (92, 119)]
    circles.append(sr.Candidate(1, Shape.CIRCLE, (106, 205.5, 112, 211.5)))
    assert [ln.text for ln in sr.scrub(page.lines, circles)] == [ln.text for ln in page.lines]


def test_a_ticked_circle_has_ink_inside_and_a_circle_with_no_text_has_no_caption():
    page = Page()
    page.circle(76, 193.5)
    for a, b in (((74.3, 191.8), (77.7, 195.2)), ((74.3, 195.2), (77.7, 191.8))):   # an X
        page.draw.line([round(a[0] * S), round(a[1] * S), round(b[0] * S), round(b[1] * S)], fill=0,
                       width=2)
    page.text("ticked", 84, 190)
    page.circle(76, 223.5)
    ticked, bare = only(page.propose(), Shape.CIRCLE)
    assert Doubt.INK_INSIDE in ticked.doubts
    assert Doubt.NO_CAPTION in bare.doubts


def test_a_caption_printed_on_the_rule_starts_the_space_after_it():
    page = Page()
    page.text("Name", 100, 192, x1=125)
    page.hline(100, 300, 200)
    page.text("City", 100, 222, x1=118)
    page.hline(100, 300, 230)
    name, city = only(page.propose(), Shape.UNDERLINE)
    assert name.bbox[0] == pytest.approx(126, abs=1) and name.certain


def test_an_underline_with_nothing_near_it_has_no_caption():
    page = Page()
    page.hline(300, 400, 600)
    page.text("Date", 72, 190)
    page.hline(110, 200, 197)
    page.text("Place", 72, 220)
    page.hline(110, 200, 227)
    lone = next(c for c in page.propose() if c.bbox[1] > 500)
    assert Doubt.NO_CAPTION in lone.doubts


def test_a_page_with_one_field_is_not_taken_for_a_form():
    page = Page()
    page.text("Signature", 72, 190)
    page.hline(120, 300, 197)
    (field,) = page.propose()
    assert field.doubts == {Doubt.FEW_FIELDS}


def test_glyph_stems_and_bold_counters_make_no_fields():
    page = Page()
    for i in range(40):
        x = 72 + 7 * i
        stem = [round(x * S), round(100 * S), round((x + 1.5) * S), round(110 * S)]
        page.draw.rectangle(stem, fill=0)
        if i % 3 == 0:  # a "T": its bar on two stems is no box
            page.draw.rectangle([round((x - 2) * S), round(100 * S), round((x + 3.5) * S),
                                 round(101.5 * S)], fill=0)
    for i in range(10):  # bold "o"s: counters smaller than any checkbox
        x = 72 + 9 * i
        page.draw.rectangle([round(x * S), round(130 * S), round((x + 5) * S), round(135 * S)],
                            outline=0, width=3)
    assert page.propose() == []


def test_bold_counters_set_tight_as_in_a_word_make_no_fields():
    page = Page()
    for i in range(10):   # the same bold "o"s a point apart
        x = 72 + 6 * i
        page.draw.rectangle([round(x * S), round(130 * S), round((x + 5) * S), round(135 * S)],
                            outline=0, width=3)
    assert page.propose() == []


def test_l_shaped_specks_on_grey_paper_raise_no_error_and_make_no_certain_field():
    page = Page(paper=205)
    for i in range(10):   # each: a 3 px row over a 7 px row, same left edge, 5 pt apart
        x, y = round((100 + 5 * i) * S), round(300 * S)
        page.draw.rectangle([x, y, x + 2, y], fill=150)
        page.draw.rectangle([x, y + 1, x + 6, y + 1], fill=150)
    assert [c for c in page.propose() if c.certain] == []


def test_a_black_label_bar_under_a_rule_is_no_underline():
    page = Page()
    page.hline(36, 576, 348)
    page.draw.rectangle([round(36 * S), round(349 * S), round(72 * S), round(360 * S)], fill=0)
    page.text("Part I", 80, 350)
    assert only(page.propose(), Shape.UNDERLINE) == []


def _options_page():
    page = Page()
    row = page.text("O individual ] C corporation ] Partnership", 72, 180, x1=300)
    squares = [sr.Candidate(1, Shape.SQUARE, (x, 180, x + 8, 188)) for x in (73, 160, 240)]
    return page, row, squares


def test_scrub_marks_the_squares_ocr_read_as_glyphs_so_each_box_gets_its_option():
    page, _, squares = _options_page()
    lines = sr.scrub(page.lines, squares)
    widgets = [Widget(1, i, "checkbox", c.bbox) for i, c in enumerate(squares)]
    content = PageContent(1, 612, 792, lines, images=[], widgets=widgets)
    plan = plan_forms(content, lines, lines, [])
    labels = [n.text for g in plan.flow if g.role is Role.GROUP for n in g.children]
    assert labels == ["individual", "C corporation", "Partnership"]


def test_scrub_leaves_a_row_alone_when_its_glyphs_do_not_match_its_squares():
    page, row, squares = _options_page()
    assert sr.scrub(page.lines, squares[:2]) == [row]


def test_scrub_drops_leader_dots_before_an_underline_and_keeps_unrelated_lines():
    page = Page()
    caption = page.text("Enter the code", 72, 192, x1=140)
    dots = [page.text(".", x, 192, x1=x + 1) for x in (150, 160, 170)]
    elsewhere = page.text(".", 400, 500, x1=401)
    underline = sr.Candidate(1, Shape.UNDERLINE, (180, 192, 230, 203))
    kept = sr.scrub([caption, *dots, elsewhere], [underline])
    assert kept == [caption, elsewhere]
    assert kept[0] is caption


def test_scrub_drops_the_dash_between_the_runs_of_a_comb():
    dash = Line(1, (451, 429, 455.5, 437), "-", 8, False, False, "GlyphLessFont", ocr=True)
    runs = [sr.Candidate(1, Shape.COMB, (418, 420, 446, 444), cells=2),
            sr.Candidate(1, Shape.COMB, (461, 420, 562, 444), cells=7)]
    assert sr.scrub([dash], runs) == []


def test_detect_reads_the_page_of_a_scanned_pdf(tmp_path):
    page = Page()
    page.text("Social security number", 418, 362)
    _comb(page, 418, 372, 3)
    _comb(page, 475, 372, 2)
    pdf = tmp_path / "scan.pdf"
    page.image.save(pdf, resolution=150)
    found = sr.detect(pdf, 1, page.lines)
    assert [(c.shape, c.cells, c.certain) for c in found] == [(Shape.COMB, 3, True),
                                                             (Shape.COMB, 2, True)]
    assert all(near(a.bbox, b.bbox, tol=1) for a, b in zip(found, page.propose(), strict=True))


def test_a_candidate_comb_becomes_a_comb_widget_the_planner_names():
    page = Page()
    page.text("Social security number", 418, 362)
    _comb(page, 418, 372, 3)
    _comb(page, 475, 372, 2)
    found = page.propose()
    widgets = [Widget(1, i, c.kind, c.bbox, flags=FF_COMB, maxlen=c.cells)
               for i, c in enumerate(found)]
    content = PageContent(1, 612, 792, page.lines, images=[], widgets=widgets)
    plan = plan_forms(content, page.lines, page.lines, [])
    fields = [n for n in plan.flow if n.role is Role.FIELD]
    assert [f.attrs["maxlen"] for f in fields] == [3, 2]
    assert all("Social security number" in (f.text or f.attrs.get("aria", "")) for f in fields)


def _planned(page, found):
    widgets = [Widget(1, i, c.kind, c.bbox, detected=Detection(DetectionSource.RULES, c.shape.value))
               for i, c in enumerate(found)]
    lines = sr.scrub(page.lines, found)
    content = PageContent(1, 612, 792, lines, images=[], widgets=widgets)
    plan = plan_forms(content, lines, lines, [])
    return [f for n in plan.flow for f in (n.children if n.role is Role.GROUP else [n])]


def test_a_checkbox_is_named_by_the_label_printed_left_of_it_past_the_leader():
    page = Page(paper=205)
    _row_of_boxes(page, "Never attended school", 72, [236], 188)
    _row_of_boxes(page, "Kindergarten", 72, [236], 213)
    fields = _planned(page, only(page.propose(), Shape.SQUARE))
    assert [f.text or f.attrs.get("aria") for f in fields] == ["Never attended school", "Kindergarten"]


def _born_digital(lines, widgets):
    """The names the planner gives the AcroForm widgets (`detected` None) under `lines`."""
    content = PageContent(1, 612, 792, lines, images=[], widgets=widgets)
    plan = plan_forms(content, lines, lines, [])
    fields = [f for n in plan.flow for f in (n.children if n.role is Role.GROUP else [n])]
    return [f.text for f in fields]


def _helv(text, x0, top, x1):
    return Line(1, (x0, top, x1, top + 9), text, 9.0, False, False, "Helv", ocr=False)


def test_a_born_digital_form_is_labelled_as_before_the_scan_work():
    # What main gives, with no scan-detected widget in the row: a label printed before its marker,
    # or a caption far left of a row of boxes, is not taken for the boxes' label.
    yes_no = [_helv("Yes \u25a1 No \u25a1", 72, 190, 130), _helv("Name", 72, 300, 100)]
    boxes = [Widget(1, 0, "checkbox", (90, 188, 100, 198)), Widget(1, 1, "checkbox", (118, 188, 128, 198)),
             Widget(1, 2, "text", (110, 298, 400, 312))]
    assert _born_digital(yes_no, boxes) == ["Name", None, None]
    agree = [_helv("I agree to the terms \u25a1", 72, 190, 172), _helv("Name", 72, 300, 100)]
    one = [Widget(1, 0, "checkbox", (164, 188, 174, 198)), Widget(1, 1, "text", (110, 298, 400, 312))]
    assert _born_digital(agree, one) == [None, "Name"]
    grades = [_helv("Highest grade completed", 72, 190, 176), _helv("Something else entirely", 72, 300, 175.5)]
    row = [Widget(1, i, "checkbox", (280 + 16 * i, 188, 291 + 16 * i, 199)) for i in range(3)]
    row.append(Widget(1, 3, "text", (200, 298, 400, 312)))
    assert _born_digital(grades, row) == ["Something else entirely", None, None, None]


def test_the_same_lines_label_the_boxes_the_scan_rules_drew():
    yes_no = [_helv("Yes \u25a1 No \u25a1", 72, 190, 130)]
    drawn = Detection(DetectionSource.RULES, "square")
    boxes = [Widget(1, 0, "checkbox", (90, 188, 100, 198), detected=drawn),
             Widget(1, 1, "checkbox", (118, 188, 128, 198), detected=drawn)]
    assert _born_digital(yes_no, boxes) == ["Yes", "No"]
    agree = [_helv("I agree to the terms \u25a1", 72, 190, 172)]
    assert _born_digital(agree, [Widget(1, 0, "checkbox", (164, 188, 174, 198), detected=drawn)]) == [
        "I agree to the terms"]


def test_a_label_between_two_boxes_belongs_to_the_one_it_is_nearer():
    page = Page(paper=205)
    page.text("Finished this grade", 72, 190, x1=160)
    page.dotted(164, 176, 196)
    page.box(180, 188, 191, 199)
    page.text("Did not finish this grade", 205, 190, x1=315)
    page.dotted(318, 330, 196)
    page.box(334, 188, 345, 199)
    page.text("Yes", 72, 220)
    page.box(100, 218, 111, 229)
    page.text("No", 72, 245)
    page.box(100, 243, 111, 254)
    fields = _planned(page, only(page.propose(), Shape.SQUARE))
    assert [f.text or f.attrs.get("aria") for f in fields][:2] == [
        "Finished this grade", "Did not finish this grade"]


def test_scrub_marks_the_square_that_ends_its_own_label_and_the_planner_reads_the_row():
    page = Page(paper=205)
    labels = ("Never attended school", "Kindergarten")
    squares = []
    for y, label in zip((188, 213), labels, strict=True):
        page.box(236, y, 247, y + 11)
        page.text(label + " . . . \u2022\u2022 _ O", 72, y + 1, x1=246)
        squares.append(sr.Candidate(1, Shape.SQUARE, (236, y, 247, y + 11)))
    lines = sr.scrub(page.lines, squares)
    assert [ln.text for ln in lines] == [f"{label} \u25a1" for label in labels]
    fields = _planned(page, squares)
    assert [f.text for f in fields] == list(labels)


def test_scrub_and_the_planner_treat_a_circle_like_a_square():
    page = Page()
    page.text("O Head of household", 72, 190, x1=160)
    page.text("O Wife of head", 72, 205, x1=140)
    circles = [sr.Candidate(1, Shape.CIRCLE, (73, 190.5, 79, 196.5)),
               sr.Candidate(1, Shape.CIRCLE, (73, 205.5, 79, 211.5))]
    lines = sr.scrub(page.lines, circles)
    assert [ln.text for ln in lines] == ["\u25a1 Head of household", "\u25a1 Wife of head"]
    assert [f.text for f in _planned(page, circles)] == ["Head of household", "Wife of head"]


def test_every_doubt_has_text_for_the_model():
    assert set(sr.DOUBT_TEXT) == set(Doubt)
